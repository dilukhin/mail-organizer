"""Ограниченный HTTPS Gmail/OAuth без redirects, прокси окружения и повторов."""
from __future__ import annotations

import http.client
import json
import re
import ssl
import time
from urllib.parse import urlencode, urlsplit


class HttpFailure(ValueError):
    def __init__(self, status: int = 0):
        self.status = status
        super().__init__("HTTPS-обмен не подтверждён")


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Повтор JSON-поля")
        result[key] = value
    return result


def parse_json(data: bytes) -> dict:
    try:
        depth, quoted, escaped = 0, False, False
        for byte in data:
            if quoted:
                if escaped:
                    escaped = False
                elif byte == 92:
                    escaped = True
                elif byte == 34:
                    quoted = False
            elif byte == 34:
                quoted = True
            elif byte in (91, 123):
                depth += 1
                if depth > 32:
                    raise ValueError()
            elif byte in (93, 125):
                depth -= 1
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise HttpFailure() from None


def exchange(endpoint: str, path: str, *, bearer: str | None = None, form: dict | None = None) -> dict:
    """Закрытые узлы и методы; токены не попадают в URL или исключения."""
    headers = {"Accept": "application/json", "Accept-Encoding": "identity", "Connection": "close"}
    body = None
    if endpoint == "oauth" and path == "/token" and bearer is None and isinstance(form, dict):
        if set(form) != {"client_id", "client_secret", "refresh_token", "grant_type"} or form["grant_type"] != "refresh_token":
            raise HttpFailure()
        host, method, limit = "oauth2.googleapis.com", "POST", 65536
        body = urlencode(form).encode("ascii")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif endpoint == "gmail" and form is None and isinstance(bearer, str):
        parts = urlsplit(path)
        if parts.scheme or parts.netloc or parts.fragment or not re.fullmatch(
                r"/gmail/v1/users/me/(?:profile|history|labels|messages/[a-zA-Z0-9_-]{1,256})", parts.path):
            raise HttpFailure()
        if not re.fullmatch(r"[a-zA-Z0-9._~+/=-]{1,8192}", bearer):
            raise HttpFailure()
        host, method, limit = "gmail.googleapis.com", "GET", 1048576
        headers["Authorization"] = "Bearer " + bearer
    else:
        raise HttpFailure()
    conn = None
    try:
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.set_alpn_protocols(["http/1.1"])
        conn = http.client.HTTPSConnection(host, 443, timeout=10, context=context)
        conn.set_debuglevel(0)
        # http.client не следует redirects и не использует HTTP(S)_PROXY.
        deadline = time.monotonic() + 20
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        if response.status != 200:
            # Чувствительное тело ошибки даже не читается.
            raise HttpFailure(response.status)
        response_headers = {}
        for key, value in response.getheaders():
            key = key.casefold()
            if key in {"content-length", "content-type", "content-encoding", "transfer-encoding"}:
                if key in response_headers:
                    raise HttpFailure()
                response_headers[key] = value.strip().casefold()
        if response_headers.get("content-type", "").split(";")[0].strip() != "application/json":
            raise HttpFailure()
        if response_headers.get("content-encoding", "identity") != "identity":
            raise HttpFailure()
        length = response_headers.get("content-length")
        transfer = response_headers.get("transfer-encoding")
        if transfer is not None and (transfer != "chunked" or length is not None):
            raise HttpFailure()
        if length is not None and (not re.fullmatch(r"[0-9]{1,10}", length) or int(length) > limit):
            raise HttpFailure()
        data = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HttpFailure()
            if conn.sock is not None:
                conn.sock.settimeout(min(10, remaining))
            chunk = response.read1(min(65536, limit + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > limit:
                raise HttpFailure()
        if length is not None and len(data) != int(length):
            raise HttpFailure()
        result = parse_json(bytes(data))
        if time.monotonic() > deadline:
            raise HttpFailure()
        return result
    except HttpFailure:
        raise
    except Exception:
        raise HttpFailure() from None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                raise HttpFailure() from None
