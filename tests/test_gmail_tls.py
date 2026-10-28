"""Настоящий TLS на loopback с одноразовым искусственным сертификатом."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from mail_organizer.gmail_http import HttpFailure, exchange


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.do_GET()

    def do_GET(self):
        self.server.observed.append(self.path)
        self.send_response(self.server.reply_status)
        self.send_header("Content-Type", "application/json")
        if self.server.reply_status == 302:
            self.send_header("Location", "https://other.invalid/")
        body = json.dumps({"emailAddress": "demo@example.invalid"}).encode()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class LocalTlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        openssl = shutil.which("openssl")
        if openssl is None:
            raise RuntimeError("Для обязательной локальной TLS-проверки нужен openssl")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        root = Path(cls.tmp.name)
        cls.cert, key = root / "synthetic-cert.pem", root / "synthetic-key.pem"
        subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-keyout", str(key), "-out", str(cls.cert), "-subj", "/CN=gmail.googleapis.com",
                        "-addext", "subjectAltName=DNS:gmail.googleapis.com"],
                       check=True, capture_output=True, timeout=20)
        key.chmod(0o600)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cls.cert, key)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.socket = context.wrap_socket(cls.server.socket, server_side=True)
        cls.server.observed = []
        cls.server.reply_status = 200
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.addClassCleanup(cls.stop_server)

    @classmethod
    def stop_server(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def setUp(self):
        self.server.observed.clear()
        self.server.reply_status = 200
        real_connect = socket.create_connection
        def loopback_only(address, *args, **kwargs):
            self.assertIn(address, (("gmail.googleapis.com", 443), ("oauth2.googleapis.com", 443)))
            return real_connect(("127.0.0.1", self.server.server_port), *args, **kwargs)
        self.route = patch("socket.create_connection", side_effect=loopback_only)
        self.connect = self.route.start()
        self.addCleanup(self.route.stop)

    def trusted_context(self):
        context = ssl.create_default_context()
        context.load_verify_locations(cafile=self.cert)
        return context

    def get(self):
        return exchange("gmail", "/gmail/v1/users/me/profile?fields=emailAddress", bearer="synthetic-access")

    def test_real_handshake_succeeds_only_with_trusted_matching_certificate(self):
        context = self.trusted_context()
        with patch("mail_organizer.gmail_http.ssl.create_default_context", return_value=context):
            self.assertEqual(self.get(), {"emailAddress": "demo@example.invalid"})
        self.assertEqual(len(self.server.observed), 1)
        self.assertEqual(self.connect.call_count, 1)

    def test_untrusted_self_signed_certificate_rejected_before_http(self):
        with self.assertRaises(HttpFailure):
            self.get()
        self.assertEqual(self.server.observed, [])

    def test_trusted_but_wrong_hostname_rejected_before_sending_oauth_secret(self):
        context = self.trusted_context()
        with patch("mail_organizer.gmail_http.ssl.create_default_context", return_value=context):
            with self.assertRaises(HttpFailure):
                exchange("oauth", "/token", form={"client_id": "synthetic-client", "client_secret": "synthetic-secret",
                                                  "refresh_token": "synthetic-refresh", "grant_type": "refresh_token"})
        self.assertEqual(self.server.observed, [])

    def test_real_redirect_is_not_followed(self):
        self.server.reply_status = 302
        context = self.trusted_context()
        with patch("mail_organizer.gmail_http.ssl.create_default_context", return_value=context):
            with self.assertRaises(HttpFailure) as error:
                self.get()
        self.assertEqual(error.exception.status, 302)
        self.assertEqual(self.connect.call_count, 1)
        self.assertEqual(len(self.server.observed), 1)


if __name__ == "__main__":
    unittest.main()
