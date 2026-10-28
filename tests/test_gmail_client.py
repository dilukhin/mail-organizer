"""HTTPS/OAuth и привязка ящика: искусственные ответы, без внешней сети."""
from contextlib import closing
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import ssl
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from mail_organizer.gmail import HISTORY_SCOPE_KEY, METADATA_SCOPE, ReadFailure
from mail_organizer.gmail_client import GmailHttpsReader, HISTORY_FIELDS, LABEL_FIELDS, MESSAGE_FIELDS, sync_verified_history
from mail_organizer.gmail_http import HttpFailure, exchange, parse_json
from mail_organizer.gmail_oauth import AccessSession, OAuthFailure, refresh_session
from mail_organizer.incremental import Checkpoint, OfflineState, Page, Scope, StateDrift, StateError
from mail_organizer.offline import load_rules


class Response:
    status = 200

    def __init__(self, data=b'{"ok":true}', headers=None):
        self.data = data
        self.headers = headers if headers is not None else [("Content-Type", "application/json"), ("Content-Length", str(len(data)))]
        self.reads = []

    def getheaders(self):
        return self.headers

    def read1(self, size):
        self.reads.append(size)
        result, self.data = self.data[:size], self.data[size:]
        return result


class HttpsTests(unittest.TestCase):
    def setUp(self):
        self.network = patch("socket.socket", side_effect=AssertionError("Внешняя сеть запрещена"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.response = Response()
        self.conn = Mock()
        self.conn.getresponse.return_value = self.response
        self.factory = patch("mail_organizer.gmail_http.http.client.HTTPSConnection", return_value=self.conn)
        self.construct = self.factory.start()
        self.addCleanup(self.factory.stop)

    def get(self):
        return exchange("gmail", "/gmail/v1/users/me/labels?fields=labels%28id%2Cname%29", bearer="synthetic-access")

    def test_fixed_hosts_tls_timeouts_header_and_close(self):
        self.assertEqual(self.get(), {"ok": True})
        args, kw = self.construct.call_args
        self.assertEqual(args, ("gmail.googleapis.com", 443))
        self.assertEqual(kw["timeout"], 10)
        self.assertTrue(kw["context"].check_hostname)
        self.assertEqual(kw["context"].verify_mode, ssl.CERT_REQUIRED)
        self.assertGreaterEqual(kw["context"].minimum_version, ssl.TLSVersion.TLSv1_2)
        args, kw = self.conn.request.call_args
        self.assertEqual(args[0], "GET")
        self.assertNotIn("synthetic-access", args[1])
        self.assertEqual(kw["headers"]["Authorization"], "Bearer synthetic-access")
        self.assertEqual(kw["headers"]["Accept-Encoding"], "identity")
        self.conn.sock.settimeout.assert_called()
        self.conn.close.assert_called_once()

    def test_oauth_only_fixed_post_form_no_authorization_header(self):
        form = {"client_id": "synthetic-client", "client_secret": "synthetic-secret", "refresh_token": "synthetic-refresh", "grant_type": "refresh_token"}
        exchange("oauth", "/token", form=form)
        self.assertEqual(self.construct.call_args.args, ("oauth2.googleapis.com", 443))
        args, kw = self.conn.request.call_args
        self.assertEqual(args, ("POST", "/token"))
        self.assertNotIn("Authorization", kw["headers"])
        self.assertEqual(parse_qs(kw["body"].decode())["refresh_token"], ["synthetic-refresh"])

    def test_redirect_errors_do_not_read_body_or_retry(self):
        for status in (301, 302, 307, 308, 400, 401, 403, 429, 503):
            self.response.status = status
            self.construct.reset_mock()
            with self.assertRaises(HttpFailure) as error:
                self.get()
            self.assertEqual(error.exception.status, status)
            self.assertEqual(self.response.reads, [])
            self.construct.assert_called_once()

    def test_arbitrary_url_endpoint_path_and_header_injection_rejected(self):
        for endpoint, path, bearer in (("other", "/token", "synthetic"), ("gmail", "https://other.invalid/", "synthetic"),
                                      ("gmail", "//other.invalid/labels", "synthetic"),
                                      ("gmail", "/gmail/v1/users/me/messages/../profile", "synthetic"),
                                      ("gmail", "/gmail/v1/users/me/labels", "bad\r\nheader")):
            with self.assertRaises(HttpFailure):
                exchange(endpoint, path, bearer=bearer)
        self.construct.assert_not_called()

    def test_compression_wrong_type_duplicate_length_and_oversized_length_rejected(self):
        variants = [
            [("Content-Type", "text/html")],
            [("Content-Type", "application/json"), ("Content-Encoding", "gzip")],
            [("Content-Type", "application/json"), ("Content-Length", "1"), ("Content-Length", "2")],
            [("Content-Type", "application/json"), ("Content-Length", "1048577")],
            [("Content-Type", "application/json"), ("Content-Length", "10"), ("Transfer-Encoding", "chunked")]]
        for headers in variants:
            self.response.headers = headers
            with self.assertRaises(HttpFailure):
                self.get()
            self.assertEqual(self.response.reads, [])

    def test_missing_length_bounded_and_truncation_not_accepted(self):
        for data, headers in ((b"x" * 1048577, [("Content-Type", "application/json")]),
                              (b"{}", [("Content-Type", "application/json"), ("Content-Length", "3")])):
            response = Response(data, headers)
            self.conn.getresponse.return_value = response
            with self.assertRaises(HttpFailure):
                self.get()
            self.assertLessEqual(sum(response.reads), 1048577 + 65536)

    def test_duplicate_json_fields_nonfinite_deep_invalid_utf8_rejected(self):
        for raw in (b'{"emailAddress":"a","emailAddress":"b"}', b'{"x":NaN}', b"[]", b'"text"', b"\xff", b'{"x":' + b"[" * 1100 + b"]" * 1100 + b"}"):
            with self.assertRaises(HttpFailure):
                parse_json(raw)

    def test_timeout_tls_failure_and_body_deadline_hide_details(self):
        for failure in (TimeoutError("synthetic-private-url"), ssl.SSLCertVerificationError("synthetic-private-url")):
            self.conn.request.side_effect = failure
            with self.assertRaises(HttpFailure) as error:
                self.get()
            self.assertNotIn("private", str(error.exception))
        self.conn.request.side_effect = None
        with patch("mail_organizer.gmail_http.time.monotonic", side_effect=[0, 21]):
            with self.assertRaises(HttpFailure):
                self.get()


@unittest.skipUnless(os.name == "posix", "OAuth-файл поддерживается только в POSIX")
class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "credentials.gmail.json"
        self.grant = {"schema_version": 1, "client_id": "synthetic-client", "client_secret": "synthetic-secret",
                      "refresh_token": "synthetic-refresh", "expected_email": "demo@example.invalid", "scopes": [METADATA_SCOPE]}
        self.write()
        self.response = {"access_token": "synthetic-access", "token_type": "Bearer", "expires_in": 3600, "scope": METADATA_SCOPE}
        self.wire = patch("mail_organizer.gmail_oauth.exchange", return_value=self.response)
        self.send = self.wire.start()
        self.addCleanup(self.wire.stop)

    def write(self):
        self.path.write_text(json.dumps(self.grant), encoding="utf-8")
        self.path.chmod(0o600)

    def test_single_refresh_access_only_in_memory_and_safe_repr(self):
        before = self.path.read_bytes()
        session = refresh_session(self.path)
        self.assertEqual(session.bearer(), "synthetic-access")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertNotIn("synthetic", repr(session))
        self.send.assert_called_once()
        self.assertEqual(self.send.call_args.args, ("oauth", "/token"))
        self.assertEqual(self.send.call_args.kwargs["form"]["grant_type"], "refresh_token")

    def test_scope_must_be_confirmed_exactly_and_no_retry(self):
        for i, scope in enumerate((None, "https://www.googleapis.com/auth/gmail.modify", METADATA_SCOPE + " openid")):
            self.path = self.path.parent / f"scope-{i}.json"
            self.write()
            self.send.return_value = {**self.response, "scope": scope}
            self.send.reset_mock()
            with self.assertRaises(OAuthFailure):
                refresh_session(self.path)
            self.send.assert_called_once()

    def test_rejection_invalid_token_expiry_and_sensitive_exceptions_stop(self):
        before = self.path.read_bytes()
        variants = [{**self.response, "access_token": "bad\nvalue"}, {**self.response, "expires_in": True},
                    {**self.response, "token_type": "Other"}]
        for i, response in enumerate(variants):
            self.path = self.path.parent / f"bad-{i}.json"
            self.write()
            self.send.return_value = response
            with self.assertRaises(OAuthFailure):
                refresh_session(self.path)
        self.path = self.path.parent / "failure.json"
        self.write()
        self.send.side_effect = RuntimeError("synthetic-secret must not appear")
        with self.assertRaises(OAuthFailure) as error:
            refresh_session(self.path)
        self.assertNotIn("synthetic", str(error.exception))
        self.assertEqual(self.path.read_bytes(), before)

    def test_uncertain_refresh_blocks_next_process_attempt_before_network(self):
        self.send.side_effect = HttpFailure(503)
        with self.assertRaises(OAuthFailure): refresh_session(self.path)
        self.send.assert_called_once()
        self.send.reset_mock()
        self.send.side_effect = None
        self.send.return_value = self.response
        with self.assertRaises(OAuthFailure): refresh_session(self.path)
        self.send.assert_not_called()
        record = next(self.path.parent.glob(".mail-organizer-refresh-*.json"))
        self.assertEqual(json.loads(record.read_text())["phase"], "refresh_pending")

    def test_confirmed_refresh_receipt_allows_next_explicit_run(self):
        refresh_session(self.path)
        refresh_session(self.path)
        self.assertEqual(self.send.call_count, 2)
        record = next(self.path.parent.glob(".mail-organizer-refresh-*.json"))
        self.assertEqual(json.loads(record.read_text())["phase"], "refresh_completed")

    def test_rotation_with_bad_scope_keeps_candidate_and_stops(self):
        self.response.update(refresh_token="synthetic-rotated", scope="unexpected-scope")
        before = self.path.read_bytes()
        with self.assertRaises(OAuthFailure): refresh_session(self.path)
        self.assertEqual(self.path.read_bytes(), before)
        candidate = next(self.path.parent.glob(".mail-organizer-grant-*"))
        self.assertEqual(json.loads(candidate.read_text())["refresh_token"], "synthetic-rotated")

    def test_rotated_refresh_saved_and_read_back_before_return(self):
        self.response["refresh_token"] = "synthetic-rotated"
        session = refresh_session(self.path)
        self.assertEqual(session.bearer(), "synthetic-access")
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["refresh_token"], "synthetic-rotated")
        self.assertNotIn("access_token", saved)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_changed_file_after_network_does_not_get_overwritten(self):
        changed = {**self.grant, "refresh_token": "different-local-value"}
        def source(*a, **kw):
            self.path.write_text(json.dumps(changed))
            return {**self.response, "refresh_token": "synthetic-rotated"}
        self.send.side_effect = source
        with self.assertRaises(OAuthFailure):
            refresh_session(self.path)
        self.assertEqual(json.loads(self.path.read_text()), changed)
        candidates = list(self.path.parent.glob(".mail-organizer-grant-*"))
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].stat().st_mode & 0o777, 0o600)

    def test_unrotated_grant_drift_also_stops_before_positive_result(self):
        def source(*a, **kw):
            self.path.write_text(json.dumps({**self.grant, "expected_email": "other@example.invalid"}))
            return self.response
        self.send.side_effect = source
        with self.assertRaises(OAuthFailure): refresh_session(self.path)
        self.send.reset_mock()
        with self.assertRaises(OAuthFailure): refresh_session(self.path)
        self.send.assert_not_called()

    def test_failed_atomic_replace_keeps_old_and_private_candidate(self):
        self.response["refresh_token"] = "synthetic-rotated"
        before = self.path.read_bytes()
        with patch("mail_organizer.gmail_oauth.os.replace", side_effect=OSError("synthetic-secret")):
            with self.assertRaises(OAuthFailure):
                refresh_session(self.path)
        self.assertEqual(self.path.read_bytes(), before)
        candidate = next(self.path.parent.glob(".mail-organizer-grant-*"))
        self.assertEqual(json.loads(candidate.read_text())["refresh_token"], "synthetic-rotated")

    def test_insecure_missing_symlink_hardlink_or_foreign_config_rejected_before_post(self):
        self.path.chmod(0o644)
        with self.assertRaises(OAuthFailure): refresh_session(self.path)
        self.path.chmod(0o600)
        link = self.path.parent / "symlink.json"
        link.symlink_to(self.path)
        with self.assertRaises(OAuthFailure): refresh_session(link)
        hardlink = self.path.parent / "hardlink.json"
        os.link(self.path, hardlink)
        with self.assertRaises(OAuthFailure): refresh_session(hardlink)
        hardlink.unlink()
        self.grant["token_uri"] = "https://other.invalid/"
        self.write()
        with self.assertRaises(OAuthFailure): refresh_session(self.path)
        self.send.assert_not_called()

    def test_directory_permissions_and_busy_file_lock_rejected_before_post(self):
        import fcntl
        self.path.parent.chmod(0o755)
        with self.assertRaises(OAuthFailure): refresh_session(self.path)
        self.path.parent.chmod(0o700)
        with self.path.open("rb") as locked:
            fcntl.flock(locked.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(OAuthFailure): refresh_session(self.path)
        self.send.assert_not_called()


class ClientBindingTests(unittest.TestCase):
    def setUp(self):
        self.scope = Scope("gmail", "demo", HISTORY_SCOPE_KEY)
        self.session = AccessSession("synthetic-access", time.monotonic() + 1000, "demo@example.invalid", "synthetic-client")
        self.source = patch("mail_organizer.gmail_client.exchange", return_value={"emailAddress": "demo@example.invalid"})
        self.send = self.source.start()
        self.addCleanup(self.source.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_profile_verified_before_reader_exposes_only_closed_requests(self):
        reader = GmailHttpsReader(self.session, self.scope)
        self.assertEqual(self.send.call_args.args, ("gmail", "/gmail/v1/users/me/profile?fields=emailAddress"))
        self.send.return_value = {"labels": []}
        reader.read("labels.list", {"userId": "me", "fields": LABEL_FIELDS})
        path = self.send.call_args.args[1]
        self.assertEqual(urlsplit(path).path, "/gmail/v1/users/me/labels")
        self.assertNotIn("synthetic-access", path)
        self.assertEqual(reader.identity_sha256, self.session.identity_sha256())

    def test_wrong_account_has_no_followup_requests(self):
        self.send.return_value = {"emailAddress": "other@example.invalid"}
        with self.assertRaises(ReadFailure):
            GmailHttpsReader(self.session, self.scope)
        self.send.assert_called_once()

    def test_widened_fields_other_user_path_and_write_methods_rejected_before_http(self):
        reader = GmailHttpsReader(self.session, self.scope)
        self.send.reset_mock()
        for op, params in (("messages.send", {"userId": "me"}), ("labels.list", {"userId": "other", "fields": LABEL_FIELDS}),
                           ("messages.get", {"userId": "me", "id": "x", "format": "full", "metadataHeaders": ("From", "Subject"), "fields": MESSAGE_FIELDS}),
                           ("messages.get", {"userId": "me", "id": "../profile", "format": "metadata", "metadataHeaders": ("From", "Subject"), "fields": MESSAGE_FIELDS}),
                           ("history.list", {"userId": "me", "startHistoryId": "100", "maxResults": 100, "fields": "*"})):
            with self.assertRaises(ReadFailure): reader.read(op, params)
        self.send.assert_not_called()

    def test_opaque_page_token_encoded_as_query_not_url(self):
        reader = GmailHttpsReader(self.session, self.scope)
        reader.read("history.list", {"userId": "me", "startHistoryId": "100", "maxResults": 100, "fields": HISTORY_FIELDS, "pageToken": "https://other.invalid/?secret=value"})
        parts = urlsplit(self.send.call_args.args[1])
        self.assertEqual(parts.netloc, "")
        self.assertEqual(parse_qs(parts.query)["pageToken"], ["https://other.invalid/?secret=value"])

    def test_expired_session_does_not_refresh_or_read(self):
        reader = GmailHttpsReader(self.session, self.scope)
        self.send.reset_mock()
        with patch("mail_organizer.gmail_oauth.time.monotonic", return_value=self.session._expires_at + 1):
            with self.assertRaises(ReadFailure) as error:
                reader.read("labels.list", {"userId": "me", "fields": LABEL_FIELDS})
        self.assertEqual(error.exception.status, 401)
        self.send.assert_not_called()

    def test_durable_binding_rejects_other_identity_and_old_unbound_state(self):
        identity = self.session.identity_sha256()
        path = Path(self.tmp.name) / "state.sqlite3"
        with closing(OfflineState.create(path)) as state:
            state.bind_identity(self.scope, identity)
            state.bind_identity(self.scope, identity)
            page = Page("bootstrap-test", Checkpoint(), "100", ())
            with self.assertRaises(StateDrift): state.commit_page(self.scope, page, [])
            state.commit_page(self.scope, page, [], identity_sha256=identity)
        with closing(OfflineState.open(path)) as state:
            for wrong in ("a"*64, replace(self.session, _client_id="other-client").identity_sha256()):
                with self.assertRaises(StateDrift): state.bind_identity(self.scope, wrong)
            self.assertEqual(state.checkpoint(self.scope), Checkpoint(1, "100"))
            dump = "\n".join(state._conn.iterdump())
            self.assertNotIn("example.invalid", dump)
        other = Path(self.tmp.name) / "unbound.sqlite3"
        with closing(OfflineState.create(other)) as state:
            state.commit_page(self.scope, Page("old", Checkpoint(), "100", ()), [])
            with self.assertRaises(StateDrift): state.bind_identity(self.scope, identity)

    def test_wrong_scope_and_binding_write_error_fail_closed(self):
        path = Path(self.tmp.name) / "fault.sqlite3"
        with closing(OfflineState.create(path)) as state:
            state._conn.execute("CREATE TRIGGER fail_binding BEFORE INSERT ON account_bindings BEGIN SELECT RAISE(ABORT, 'test'); END")
            with self.assertRaises(StateError): state.bind_identity(self.scope, self.session.identity_sha256())
            self.assertEqual(state._conn.execute("SELECT COUNT(*) FROM account_bindings").fetchone()[0], 0)
            with self.assertRaises(StateError): state.checkpoint(self.scope)

    def test_binding_commit_reply_loss_blocks_current_handle_and_preserves_confirmed_row(self):
        class LostReply:
            def __init__(self, db): self.db = db
            def __getattr__(self, name): return getattr(self.db, name)
            def execute(self, sql, parameters=()):
                result = self.db.execute(sql, parameters)
                if sql == "COMMIT": raise sqlite3.OperationalError("искусственная потеря ответа")
                return result
        path = Path(self.tmp.name) / "lost-reply.sqlite3"
        with closing(OfflineState.create(path)) as state:
            state._conn = LostReply(state._conn)
            with self.assertRaises(StateError): state.bind_identity(self.scope, self.session.identity_sha256())
            with self.assertRaises(StateError): state.checkpoint(self.scope)
        with closing(OfflineState.open(path)) as state:
            state.bind_identity(self.scope, self.session.identity_sha256())
            self.assertEqual(state._conn.execute("SELECT COUNT(*) FROM account_bindings").fetchone()[0], 1)

    @unittest.skipUnless(os.name == "posix", "OAuth-файл поддерживается только в POSIX")
    def test_full_refresh_profile_history_sqlite_with_fake_https_and_blocked_socket(self):
        root = Path(__file__).resolve().parents[1]
        fixture = json.loads((root / "tests/fixtures/gmail_history.json").read_text())
        path = Path(self.tmp.name) / "credentials.gmail.json"
        path.write_text(json.dumps({"schema_version": 1, "client_id": "synthetic-client", "client_secret": "synthetic-secret",
                                   "refresh_token": "synthetic-refresh", "expected_email": "demo@example.invalid", "scopes": [METADATA_SCOPE]}))
        path.chmod(0o600)
        values = [
            {"access_token": "synthetic-access", "token_type": "Bearer", "expires_in": 3600, "scope": METADATA_SCOPE},
            {"emailAddress": "demo@example.invalid"}, fixture["history"]["first"], fixture["history"]["synthetic-page-2"],
            fixture["labels"], fixture["messages"]["msg-a"], fixture["messages"]["msg-b"]]
        connections = []
        for value in values:
            conn = Mock()
            conn.getresponse.return_value = Response(json.dumps(value).encode())
            connections.append(conn)
        # В этом тесте оба модуля используют настоящий ограниченный exchange;
        # заменяется только HTTPSConnection, до каких-либо DNS/сокетов.
        with patch("mail_organizer.gmail_client.exchange", new=exchange), \
             patch("mail_organizer.gmail_http.http.client.HTTPSConnection", side_effect=connections) as factory, \
             patch("socket.socket", side_effect=AssertionError("Сеть запрещена")):
            session = refresh_session(path)
            reader = GmailHttpsReader(session, self.scope)
            with closing(OfflineState.create(Path(self.tmp.name) / "integration.sqlite3")) as state:
                state.bind_identity(self.scope, reader.identity_sha256)
                # Отправная точка остаётся искусственной: live bootstrap ещё не включён.
                rules = load_rules(root / "policy")
                state.commit_page(self.scope, Page("synthetic-bootstrap", Checkpoint(), "100", ()), rules,
                                  identity_sha256=reader.identity_sha256)
                self.assertEqual(sync_verified_history(state, reader, rules), "committed")
                self.assertEqual(state.checkpoint(self.scope), Checkpoint(2, "160"))
                self.assertEqual(state._conn.execute("SELECT status FROM decisions ORDER BY message_id").fetchall(),
                                 [("PROPOSED",), ("PROTECTED",)])
            self.assertEqual(factory.call_count, 7)
            self.assertEqual(connections[0].request.call_args.args[0], "POST")
            self.assertTrue(all(c.request.call_args.args[0] == "GET" for c in connections[1:]))


if __name__ == "__main__":
    unittest.main()
