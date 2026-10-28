"""Постоянное офлайн-состояние: сбои, повторы и изоляция на искусственных данных."""
from contextlib import closing, redirect_stdout, redirect_stderr
from dataclasses import asdict, replace
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from mail_organizer.cli import main
from mail_organizer.incremental import (Checkpoint, OfflineState, Page, Scope,
                                        StateDrift, StateError, load_pages)
from mail_organizer.offline import load_rules, parse_messages

ROOT = Path(__file__).resolve().parents[1]


class CommitFault:
    def __init__(self, db, commit_first):
        self.db, self.commit_first = db, commit_first

    def __getattr__(self, name):
        return getattr(self.db, name)

    def execute(self, sql, parameters=()):
        if sql == "COMMIT":
            if self.commit_first:
                self.db.execute(sql)
            raise sqlite3.OperationalError("искусственная потеря ответа COMMIT")
        return self.db.execute(sql, parameters)


class IncrementalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "synthetic.sqlite3"
        self.scope = Scope("gmail", "demo-gmail", "all")
        messages = parse_messages(ROOT / "tests/fixtures/offline_messages.json")
        self.msg = replace(messages[0], account_alias=self.scope.account_alias)
        self.rules = load_rules(ROOT / "policy")
        self.page = Page("page-1", Checkpoint(), "synthetic-c1", (self.msg,))

    def state(self):
        state = OfflineState.create(self.path)
        self.addCleanup(state.close)
        return state

    def counts(self, state):
        return tuple(state._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                     for table in ("message_metadata", "decisions", "actions", "provider_cursors", "sync_batches"))

    def test_restart_and_exact_replay_have_no_duplicate_proposals(self):
        with closing(OfflineState.create(self.path)) as state:
            self.assertEqual(state.commit_page(self.scope, self.page, self.rules), "committed")
        with closing(OfflineState.open(self.path)) as state:
            self.assertEqual(state.checkpoint(self.scope), Checkpoint(1, "synthetic-c1"))
            self.assertEqual(state.commit_page(self.scope, self.page, self.rules), "already_committed")
            self.assertEqual(self.counts(state), (1, 1, 1, 1, 1))

    def test_second_page_and_old_page_replay(self):
        state = self.state()
        state.commit_page(self.scope, self.page, self.rules)
        second = Page("page-2", Checkpoint(1, "synthetic-c1"), "synthetic-c2", ())
        state.commit_page(self.scope, second, self.rules)
        self.assertEqual(state.commit_page(self.scope, self.page, self.rules), "already_committed")
        self.assertEqual(state.checkpoint(self.scope), Checkpoint(2, "synthetic-c2"))
        self.assertEqual(self.counts(state), (1, 1, 1, 1, 2))

    def test_changed_replay_and_stale_checkpoint_are_rejected(self):
        state = self.state()
        state.commit_page(self.scope, self.page, self.rules)
        for bad in (replace(self.page, next_cursor="different"), replace(self.page, batch_id="page-other")):
            with self.assertRaises(StateDrift):
                state.commit_page(self.scope, bad, self.rules)
        self.assertEqual(self.counts(state), (1, 1, 1, 1, 1))

    def test_aba_cursor_value_still_checks_revision(self):
        state = self.state()
        for page in (self.page, Page("p2", Checkpoint(1, "synthetic-c1"), "c2", ()),
                     Page("p3", Checkpoint(2, "c2"), "synthetic-c1", ())):
            state.commit_page(self.scope, page, self.rules)
        with self.assertRaises(StateDrift):
            state.commit_page(self.scope, Page("p4", Checkpoint(1, "synthetic-c1"), "c4", ()), self.rules)

    def test_policy_or_scope_change_stops_without_reclassification(self):
        state = self.state()
        state.commit_page(self.scope, self.page, self.rules)
        page = Page("p2", Checkpoint(1, "synthetic-c1"), "c2", ())
        with self.assertRaises(StateDrift):
            state.commit_page(self.scope, page, [],)
        with self.assertRaises(StateDrift):
            state.commit_page(replace(self.scope, scope_key="folder"), replace(self.page, messages=()), self.rules)
        self.assertEqual(self.counts(state), (1, 1, 1, 1, 1))

    def test_accounts_and_providers_do_not_share_cursor_or_message_id(self):
        state = self.state()
        for scope in (self.scope, replace(self.scope, account_alias="second"), replace(self.scope, provider="outlook")):
            msg = replace(self.msg, provider=scope.provider, account_alias=scope.account_alias)
            state.commit_page(scope, replace(self.page, messages=(msg,)), self.rules)
        self.assertEqual(self.counts(state), (3, 3, 3, 3, 3))

    def test_wrong_account_duplicate_and_no_progress_are_rejected(self):
        state = self.state()
        for page in (replace(self.page, messages=(replace(self.msg, account_alias="foreign"),)),
                     replace(self.page, messages=(self.msg, self.msg)),
                     Page("p", Checkpoint(1, "c1"), "c1", ())):
            with self.assertRaises(StateError if page.messages == () else ValueError):
                state.commit_page(self.scope, page, self.rules)
        self.assertEqual(self.counts(state), (0, 0, 0, 0, 0))

    def test_protected_update_removes_previous_proposal(self):
        state = self.state()
        state.commit_page(self.scope, self.page, self.rules)
        protected = replace(self.msg, labels=("ChatGPT-Control/Commands",))
        state.commit_page(self.scope, Page("p2", Checkpoint(1, "synthetic-c1"), "c2", (protected,)), self.rules)
        self.assertEqual(self.counts(state), (1, 1, 0, 1, 2))
        self.assertEqual(state._conn.execute("SELECT status FROM decisions").fetchone(), ("PROTECTED",))

    def test_failure_mid_page_rolls_back_metadata_decisions_cursor_and_receipt(self):
        state = self.state()
        state._conn.execute("CREATE TRIGGER fail_receipt BEFORE INSERT ON sync_batches BEGIN SELECT RAISE(ABORT, 'test'); END")
        with self.assertRaises(StateError):
            state.commit_page(self.scope, self.page, self.rules)
        self.assertEqual(self.counts(state), (0, 0, 0, 0, 0))
        with self.assertRaises(StateError):
            state.commit_page(self.scope, self.page, self.rules)

    def test_commit_errors_before_or_after_actual_commit_never_report_success(self):
        for commit_first in (False, True):
            path = Path(self.tmp.name) / f"fault-{commit_first}.sqlite3"
            with closing(OfflineState.create(path)) as state:
                state._conn = CommitFault(state._conn, commit_first)
                with self.assertRaises(StateError):
                    state.commit_page(self.scope, self.page, self.rules)
                with self.assertRaises(StateError):
                    state.checkpoint(self.scope)
            with closing(OfflineState.open(path)) as state:
                self.assertEqual(state.checkpoint(self.scope).revision, int(commit_first))
                self.assertEqual(state.commit_page(self.scope, self.page, self.rules),
                                 "already_committed" if commit_first else "committed")

    def test_separate_connections_reject_stale_writer(self):
        state = self.state()
        with closing(OfflineState.open(self.path)) as second:
            before = second.checkpoint(self.scope)
            state.commit_page(self.scope, self.page, self.rules)
            with self.assertRaises(StateDrift):
                second.commit_page(self.scope, replace(self.page, batch_id="stale", expected=before), self.rules)
            self.assertEqual(self.counts(second), (1, 1, 1, 1, 1))

    def test_busy_writer_stops_without_retry(self):
        state = self.state()
        with closing(OfflineState.open(self.path)) as second:
            state._conn.execute("BEGIN IMMEDIATE")
            try:
                with self.assertRaises(StateError):
                    second.commit_page(self.scope, self.page, self.rules)
            finally:
                state._conn.execute("ROLLBACK")
            self.assertEqual(self.counts(state), (0, 0, 0, 0, 0))

    def test_process_exit_before_or_after_commit(self):
        script = '''
import os, sys
from pathlib import Path
from mail_organizer.incremental import OfflineState, load_pages
from mail_organizer.offline import load_rules
scope, pages = load_pages(Path(sys.argv[2]))
state = OfflineState.open(Path(sys.argv[1]))
class ExitBeforeCommit:
    def __getattr__(self, name): return getattr(db, name)
    def execute(self, sql, parameters=()):
        if sql == "COMMIT": os._exit(23)
        return db.execute(sql, parameters)
if sys.argv[4] == "before":
    db = state._conn
    state._conn = ExitBeforeCommit()
state.commit_page(scope, pages[0], load_rules(Path(sys.argv[3])))
os._exit(24)
'''
        fixture = self.write_fixture()
        for phase in ("before", "after"):
            path = Path(self.tmp.name) / f"crash-{phase}.sqlite3"
            OfflineState.create(path).close()
            env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
            run = subprocess.run([sys.executable, "-c", script, str(path), str(fixture), str(ROOT / "policy"), phase],
                                 cwd=ROOT, env=env, capture_output=True, timeout=15)
            self.assertEqual(run.returncode, 23 if phase == "before" else 24, run.stderr)
            with closing(OfflineState.open(path)) as state:
                self.assertEqual(state.checkpoint(self.scope).revision, 0 if phase == "before" else 1)

    def test_missing_unknown_corrupt_and_old_schema_are_not_recreated(self):
        with self.assertRaises(OSError):
            OfflineState.open(self.path)
        self.assertFalse(self.path.exists())
        for data in (b"", b"not a database"):
            self.path.write_bytes(data)
            with self.assertRaises(StateError):
                OfflineState.open(self.path)
            self.assertEqual(self.path.read_bytes(), data)
        with closing(sqlite3.connect(Path(self.tmp.name) / "old.sqlite3")) as db:
            db.execute("CREATE TABLE schema_version(version)")
            db.execute("INSERT INTO schema_version VALUES (1)")
            db.commit()
        with self.assertRaises(StateError):
            OfflineState.open(Path(self.tmp.name) / "old.sqlite3")

    def test_create_never_overwrites_and_open_rejects_symlink(self):
        self.path.write_bytes(b"checkpoint")
        with self.assertRaises(FileExistsError):
            OfflineState.create(self.path)
        self.assertEqual(self.path.read_bytes(), b"checkpoint")
        link = Path(self.tmp.name) / "link.sqlite3"
        link.symlink_to(self.path)
        with self.assertRaises(StateError):
            OfflineState.open(link)

    @unittest.skipUnless(os.name == "posix", "Проверка POSIX-прав")
    def test_private_file_permissions(self):
        self.state()
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.path.chmod(0o644)
        with self.assertRaises(StateError):
            OfflineState.open(self.path)

    def test_no_subject_sender_labels_or_body_in_persistent_database(self):
        state = self.state()
        state.commit_page(self.scope, self.page, self.rules)
        dump = "\n".join(state._conn.iterdump())
        for value in (self.msg.subject, self.msg.sender_domain, "ChatGPT-Control/Commands", "body", "subject"):
            self.assertNotIn(value, dump)

    def write_fixture(self):
        path = Path(self.tmp.name) / "pages.json"
        msg = {**asdict(self.msg), "labels": list(self.msg.labels)}
        path.write_text(json.dumps({"schema_version": 1, **asdict(self.scope), "pages": [{
            "batch_id": self.page.batch_id, "expected_revision": 0, "expected_cursor": None,
            "next_cursor": self.page.next_cursor, "messages": [msg]}]}), encoding="utf-8")
        return path

    def test_fixture_rejects_unknown_fields_wrong_account_and_malformed_cursor(self):
        path = self.write_fixture()
        raw = json.loads(path.read_text(encoding="utf-8"))
        variants = [{**raw, "access_token": "synthetic"}, {**raw, "provider": []}]
        for mutation in ("body", "wrong-account", "cursor", "revision"):
            value = json.loads(json.dumps(raw))
            p = value["pages"][0]
            if mutation == "body": p["messages"][0]["body"] = "synthetic"
            if mutation == "wrong-account": p["messages"][0]["account_alias"] = "other"
            if mutation == "cursor": p["next_cursor"] = "secret\nline"
            if mutation == "revision": p["expected_revision"] = True
            variants.append(value)
        for variant in variants:
            path.write_text(json.dumps(variant), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_pages(path)

    def test_cli_replay_prints_only_counts_and_never_uses_network(self):
        fixture = self.write_fixture()
        output = io.StringIO()
        with patch("socket.socket", side_effect=AssertionError("Сеть запрещена")), redirect_stdout(output):
            self.assertEqual(main(["state-init", "--database", str(self.path)]), 0)
            for _ in range(2):
                self.assertEqual(main(["sync-pages", "--dry-run", "--database", str(self.path),
                                       "--fixture", str(fixture), "--policy-dir", str(ROOT / "policy")]), 0)
        self.assertIn("уже сохранено: 1", output.getvalue())
        for value in (self.msg.subject, self.msg.message_id, self.msg.sender_domain, self.page.next_cursor):
            self.assertNotIn(value, output.getvalue())

    def test_cli_failure_does_not_leak_path_or_cursor(self):
        output = io.StringIO()
        with redirect_stderr(output), self.assertRaises(SystemExit) as error:
            main(["sync-pages", "--dry-run", "--database", str(self.path),
                  "--fixture", str(self.write_fixture()), "--policy-dir", str(ROOT / "policy")])
        self.assertEqual(error.exception.code, 2)
        self.assertNotIn(str(self.path), output.getvalue())
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
