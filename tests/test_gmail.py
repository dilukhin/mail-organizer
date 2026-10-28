"""Серверный Gmail-адаптер на искусственных ответах, без OAuth и сети."""
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from mail_organizer.gmail import (GmailError, GmailHistoryAdapter, HISTORY_SCOPE_KEY,
                                  ReadFailure, parse_labels, parse_metadata, sync_history)
from mail_organizer.incremental import Checkpoint, OfflineState, Page, Scope, StateDrift
from mail_organizer.offline import classify, load_rules

ROOT = Path(__file__).resolve().parents[1]


class SyntheticReader:
    def __init__(self, fixture):
        self.fixture = deepcopy(fixture)
        self.calls = []

    def read(self, operation, parameters):
        self.calls.append((operation, dict(parameters)))
        if operation == "history.list":
            value = self.fixture["history"][parameters.get("pageToken", "first")]
        elif operation == "labels.list":
            value = self.fixture["labels"]
        elif operation == "messages.get":
            value = self.fixture["messages"][parameters["id"]]
        else:
            raise AssertionError("Не разрешённый тестом метод")
        if isinstance(value, Exception):
            raise value
        return deepcopy(value)


class GmailTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads((ROOT / "tests/fixtures/gmail_history.json").read_text(encoding="utf-8"))
        self.scope = Scope("gmail", "demo-gmail", HISTORY_SCOPE_KEY)
        self.expected = Checkpoint(1, "100")
        self.rules = load_rules(ROOT / "policy")
        self.reader = SyntheticReader(self.fixture)
        self.adapter = GmailHistoryAdapter(self.reader)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "synthetic.sqlite3"

    def state(self):
        state = OfflineState.create(self.path)
        self.addCleanup(state.close)
        # Только искусственная отправная точка; начальная инвентаризация ещё не реализована.
        state.commit_page(self.scope, Page("synthetic-bootstrap", Checkpoint(), "100", ()), self.rules)
        return state

    def assert_unchanged(self, state):
        self.assertEqual(state.checkpoint(self.scope), self.expected)
        self.assertEqual(state._conn.execute("SELECT COUNT(*) FROM message_metadata").fetchone()[0], 0)
        self.assertEqual(state._conn.execute("SELECT COUNT(*) FROM sync_batches").fetchone()[0], 1)

    def test_pagination_deduplicates_ids_and_uses_only_final_history_id(self):
        page = self.adapter.prepare(self.scope, self.expected)
        self.assertEqual(page.next_cursor, "160")
        self.assertEqual([m.message_id for m in page.messages], ["msg-a", "msg-b"])
        calls = self.reader.calls
        self.assertEqual([op for op, _ in calls], ["history.list", "history.list", "labels.list", "messages.get", "messages.get"])
        self.assertEqual(calls[0][1]["startHistoryId"], "100")
        self.assertEqual(calls[1][1]["startHistoryId"], "100")
        self.assertEqual(calls[1][1]["pageToken"], "synthetic-page-2")
        for op, params in calls:
            self.assertEqual(params["userId"], "me")
            if op == "messages.get":
                self.assertEqual(params["format"], "metadata")
                self.assertEqual(params["metadataHeaders"], ("From", "Subject"))
                self.assertNotIn("body", params["fields"])
                self.assertNotIn("raw", params["fields"])

    def test_sqlite_end_to_end_and_restart_without_chatgpt_or_network(self):
        state = self.state()
        with patch("socket.socket", side_effect=AssertionError("Сеть запрещена")):
            self.assertEqual(sync_history(state, self.scope, self.adapter, self.rules), "committed")
        with closing(OfflineState.open(self.path)) as reopened:
            self.assertEqual(reopened.checkpoint(self.scope), Checkpoint(2, "160"))
            rows = reopened._conn.execute("SELECT status FROM decisions ORDER BY message_id").fetchall()
            self.assertEqual(rows, [("PROPOSED",), ("PROTECTED",)])
            self.assertEqual(reopened._conn.execute("SELECT has_attachments FROM message_metadata").fetchall(), [(None,), (None,)])
            self.assertEqual(reopened._conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0], 1)
            dump = "\n".join(reopened._conn.iterdump())
            for value in ("receipts.invalid", "Control/Commands", "Receipt", "sender@", "Bearer", "snippet"):
                self.assertNotIn(value, dump)

    def test_prepared_page_exact_retry_is_idempotent(self):
        state = self.state()
        page = self.adapter.prepare(self.scope, self.expected)
        self.assertEqual(state.commit_page(self.scope, page, self.rules), "committed")
        self.assertEqual(state.commit_page(self.scope, page, self.rules), "already_committed")
        self.assertEqual(state._conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0], 1)

    def test_stale_prepared_window_cannot_overwrite_other_writer(self):
        state = self.state()
        page = self.adapter.prepare(self.scope, self.expected)
        state.commit_page(self.scope, Page("other-window", self.expected, "170", ()), self.rules)
        with self.assertRaises(StateDrift):
            state.commit_page(self.scope, page, self.rules)
        self.assertEqual(state.checkpoint(self.scope), Checkpoint(2, "170"))

    def test_no_changes_does_not_write_or_read_metadata(self):
        state = self.state()
        reader = SyntheticReader({"history": {"first": {"historyId": "100"}}})
        self.assertEqual(sync_history(state, self.scope, GmailHistoryAdapter(reader), self.rules), "no_changes")
        self.assertEqual(len(reader.calls), 1)
        self.assert_unchanged(state)

    def test_empty_but_advanced_window_can_commit(self):
        state = self.state()
        reader = SyntheticReader({"history": {"first": {"historyId": "130"}}, "labels": {"labels": []}})
        self.assertEqual(sync_history(state, self.scope, GmailHistoryAdapter(reader), self.rules), "committed")
        self.assertEqual(state.checkpoint(self.scope), Checkpoint(2, "130"))

    def test_expired_second_page_or_missing_message_never_advances_cursor(self):
        state = self.state()
        for target in ("history", "message"):
            reader = SyntheticReader(self.fixture)
            if target == "history": reader.fixture["history"]["synthetic-page-2"] = ReadFailure(404)
            else: reader.fixture["messages"]["msg-b"] = ReadFailure(404)
            with self.assertRaises(GmailError) as error:
                sync_history(state, self.scope, GmailHistoryAdapter(reader), self.rules)
            self.assertEqual(error.exception.reason, "cursor_expired" if target == "history" else "message_disappeared")
            self.assert_unchanged(state)

    def test_access_errors_rate_limits_and_transport_errors_do_not_retry_or_leak(self):
        state = self.state()
        for failure in (ReadFailure(401), ReadFailure(403), ReadFailure(429), ReadFailure(503),
                        RuntimeError("Bearer synthetic-private-value")):
            reader = SyntheticReader(self.fixture)
            reader.fixture["history"]["first"] = failure
            with self.assertRaises(GmailError) as error:
                sync_history(state, self.scope, GmailHistoryAdapter(reader), self.rules)
            self.assertNotIn("synthetic-private", str(error.exception))
            self.assertEqual(len(reader.calls), 1)
            self.assert_unchanged(state)

    def test_pagination_cycle_and_budget_overrun_stop(self):
        state = self.state()
        for variant in ("cycle", "budget"):
            reader = SyntheticReader(self.fixture)
            if variant == "cycle":
                reader.fixture["history"]["synthetic-page-2"] = {"historyId": "160", "nextPageToken": "synthetic-page-2"}
            else:
                reader.fixture["history"] = {"first" if i == 0 else f"p{i}":
                    {"historyId": "160", "nextPageToken": f"p{i+1}"} for i in range(10)}
            with self.assertRaises(GmailError):
                sync_history(state, self.scope, GmailHistoryAdapter(reader), self.rules)
            self.assertLessEqual(len(reader.calls), 10)
            self.assert_unchanged(state)

    def test_deletion_requires_explicit_future_reconciliation(self):
        state = self.state()
        self.reader.fixture["history"]["first"]["history"][0]["messagesDeleted"] = [{"message": {"id": "msg-a"}}]
        with self.assertRaises(GmailError) as error:
            sync_history(state, self.scope, self.adapter, self.rules)
        self.assertEqual(error.exception.reason, "deletion_requires_inventory")
        self.assert_unchanged(state)

    def test_record_and_unique_message_limits_do_not_commit_partial_window(self):
        state = self.state()
        for limit in ("records", "messages"):
            reader = SyntheticReader(self.fixture)
            if limit == "records":
                records = [{"id": str(101+i), "messagesAdded": [{"message": {"id": "msg-a"}}]} for i in range(100)]
                reader.fixture["history"]["first"] = {"historyId": "300", "history": records, "nextPageToken": "synthetic-page-2"}
                reader.fixture["history"]["synthetic-page-2"] = {"historyId": "300", "history": [
                    {"id": "250", "messagesAdded": [{"message": {"id": "msg-a"}}]}]}
            else:
                reader.fixture["history"] = {"first": {"historyId": "300", "history": [
                    {"id": "150", "messagesAdded": [{"message": {"id": f"msg-{i}"}} for i in range(60)]},
                    {"id": "200", "messagesAdded": [{"message": {"id": f"msg-{i}"}} for i in range(60,101)]}]}}
            with self.assertRaises(GmailError):
                sync_history(state, self.scope, GmailHistoryAdapter(reader), self.rules)
            self.assertTrue(all(op == "history.list" for op, _ in reader.calls))
            self.assert_unchanged(state)

    def test_history_order_summary_only_and_malformed_history_id_stop(self):
        state = self.state()
        bad_pages = [
            {"historyId": "99"}, {"historyId": "100", "history": [{"id": "101"}]},
            {"historyId": "110", "history": [{"id": "100"}]},
            {"historyId": "110", "history": [{"id": "105", "messages": [{"id": "msg-a"}]}]},
            {"historyId": "110", "history": [{"id": "105", "messagesDeleted": ""}]},
            {"historyId": "110", "history": [{"id": "105", "messages": [{"id": "msg-b"}],
                "messagesAdded": [{"message": {"id": "msg-a"}}]}]},
            {"historyId": "not-an-id"}, {"historyId": "110", "history": "invalid"}]
        for bad in bad_pages:
            reader = SyntheticReader({"history": {"first": bad}})
            with self.assertRaises(GmailError):
                sync_history(state, self.scope, GmailHistoryAdapter(reader), self.rules)
            self.assert_unchanged(state)

    def test_initial_inventory_scope_and_invalid_cursor_require_no_source_call(self):
        for scope, expected in ((self.scope, Checkpoint()), (replace(self.scope, provider="outlook"), self.expected),
                                (replace(self.scope, scope_key="all"), self.expected),
                                (self.scope, Checkpoint(1, "not-history"))):
            with self.assertRaises(GmailError):
                self.adapter.prepare(scope, expected)
        self.assertEqual(self.reader.calls, [])

    def test_unknown_label_duplicate_headers_wrong_id_and_raw_body_rejected(self):
        state = self.state()
        for key in ("label", "headers", "id", "body"):
            reader = SyntheticReader(self.fixture)
            msg = reader.fixture["messages"]["msg-a"]
            if key == "label": msg["labelIds"] = ["unmapped"]
            if key == "headers": msg["payload"]["headers"] = [{"name": "From", "value": "a@receipts.invalid"}] * 2
            if key == "id": msg["id"] = "wrong"
            if key == "body": msg["payload"]["body"] = {"data": "synthetic"}
            with self.assertRaises(GmailError):
                sync_history(state, self.scope, GmailHistoryAdapter(reader), self.rules)
            self.assert_unchanged(state)

    def test_label_names_protect_opaque_ids_and_missing_sender_is_review(self):
        labels = parse_labels(self.fixture["labels"])
        protected = parse_metadata(self.fixture["messages"]["msg-b"], "msg-b", self.scope, labels)
        self.assertIsNone(protected.sender_domain)
        self.assertEqual(classify(protected, self.rules).status, "PROTECTED")
        ordinary = replace(protected, labels=())
        self.assertEqual(classify(ordinary, self.rules).reason_code, "incomplete_metadata")

    def test_encoded_subject_empty_subject_and_ambiguous_sender_are_conservative(self):
        labels = parse_labels(self.fixture["labels"])
        msg = deepcopy(self.fixture["messages"]["msg-a"])
        msg["payload"]["headers"][1]["value"] = "=?utf-8?b?UmVjZWlwdA==?="
        self.assertEqual(parse_metadata(msg, "msg-a", self.scope, labels).subject, "Receipt")
        msg["payload"]["headers"] = [{"name": "From", "value": "a@receipts.invalid, b@other.invalid"}]
        parsed = parse_metadata(msg, "msg-a", self.scope, labels)
        self.assertEqual(parsed.subject, "")
        self.assertEqual(classify(parsed, self.rules).status, "REVIEW_REQUIRED")

    def test_schema_two_is_refused_without_automatic_replacement(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("CREATE TABLE schema_version(version INTEGER CHECK(version=2))")
            db.execute("INSERT INTO schema_version VALUES (2)")
            db.commit()
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            OfflineState.open(self.path)
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
