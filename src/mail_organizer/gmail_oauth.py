"""Обновление собственного Gmail OAuth grant; первичное согласие ещё не реализовано."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time

from .gmail import METADATA_SCOPE
from .gmail_http import HttpFailure, exchange, parse_json


class OAuthFailure(ValueError):
    def __init__(self):
        super().__init__("OAuth-обновление не подтверждено; требуется диагностика")


def mailbox(value) -> str:
    if not isinstance(value, str) or len(value) > 320 or not re.fullmatch(r"[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9.-]+", value):
        raise OAuthFailure()
    if any(not part for part in value.rsplit("@", 1)[1].split(".")):
        raise OAuthFailure()
    return value.casefold()


def _secret(value, limit=8192):
    if not isinstance(value, str) or not 0 < len(value) <= limit or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise OAuthFailure()
    return value


def _read_fd(fd):
    os.lseek(fd, 0, os.SEEK_SET)
    data = bytearray()
    while True:
        chunk = os.read(fd, 65537 - len(data))
        if not chunk:
            return bytes(data)
        data.extend(chunk)
        if len(data) > 65536:
            raise OAuthFailure()


def _private_open(path: Path):
    if os.name != "posix":
        raise OAuthFailure()
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o077:
        raise OAuthFailure()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
            raise OAuthFailure()
    except Exception:
        os.close(fd)
        raise
    return fd


def _grant(data):
    grant = parse_json(data)
    if set(grant) != {"schema_version", "client_id", "client_secret", "refresh_token", "expected_email", "scopes"}:
        raise OAuthFailure()
    if type(grant["schema_version"]) is not int or grant["schema_version"] != 1 or grant["scopes"] != [METADATA_SCOPE]:
        raise OAuthFailure()
    for name in ("client_id", "client_secret", "refresh_token"):
        _secret(grant[name])
    grant["expected_email"] = mailbox(grant["expected_email"])
    return grant


@dataclass(frozen=True, repr=False)
class AccessSession:
    _access_token: str
    _expires_at: float
    _expected_email: str
    _client_id: str

    def bearer(self):
        if time.monotonic() >= self._expires_at:
            raise OAuthFailure()
        return self._access_token

    def identity_sha256(self):
        value = json.dumps(["gmail", self._expected_email, self._client_id, METADATA_SCOPE])
        return hashlib.sha256(value.encode()).hexdigest()


@contextmanager
def _locked(path: Path):
    import fcntl
    fd = _private_open(path)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (path.lstat().st_dev, path.lstat().st_ino) != (os.fstat(fd).st_dev, os.fstat(fd).st_ino):
            raise OAuthFailure()
        yield fd
    finally:
        os.close(fd)


def _fsync_directory(path: Path):
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _rotation_candidate(path: Path, grant: dict):
    # Кандидат остаётся в приватном каталоге при неопределённом результате записи.
    encoded = json.dumps(grant, ensure_ascii=True, sort_keys=True).encode()
    fd, name = tempfile.mkstemp(prefix=".mail-organizer-grant-", dir=path.parent)
    with os.fdopen(fd, "wb") as candidate:
        candidate.write(encoded)
        candidate.flush()
        os.fsync(candidate.fileno())
    _fsync_directory(path.parent)
    fd = _private_open(Path(name))
    try:
        if _read_fd(fd) != encoded:
            raise OAuthFailure()
    finally:
        os.close(fd)
    return name, encoded


def _install_rotation(path: Path, original: bytes, name: str, encoded: bytes):
    fd = _private_open(path)
    try:
        if _read_fd(fd) != original:
            raise OAuthFailure()
    finally:
        os.close(fd)
    os.replace(name, path)
    _fsync_directory(path.parent)
    fd = _private_open(path)
    try:
        if _read_fd(fd) != encoded:
            raise OAuthFailure()
    finally:
        os.close(fd)


def _pending_path(path: Path):
    return path.parent / (".mail-organizer-refresh-" + hashlib.sha256(path.name.encode()).hexdigest() + ".json")


def _read_private(path: Path):
    fd = _private_open(path)
    try:
        return _read_fd(fd)
    finally:
        os.close(fd)


def _replace_record(path: Path, expected: bytes, updated: bytes):
    fd, name = tempfile.mkstemp(prefix=".mail-organizer-refresh-stage-", dir=path.parent)
    with os.fdopen(fd, "wb") as output:
        output.write(updated)
        output.flush()
        os.fsync(output.fileno())
    if _read_private(path) != expected:
        raise OAuthFailure()
    os.replace(name, path)
    _fsync_directory(path.parent)
    if _read_private(path) != updated:
        raise OAuthFailure()


def _begin_refresh(path: Path, original: bytes):
    marker = _pending_path(path)
    data = json.dumps({"phase": "refresh_pending", "grant_sha256": hashlib.sha256(original).hexdigest()}).encode()
    if marker.exists() or marker.is_symlink():
        previous = _read_private(marker)
        record = parse_json(previous)
        if record != {"phase": "refresh_completed", "grant_sha256": hashlib.sha256(original).hexdigest()}:
            raise OAuthFailure()
        _replace_record(marker, previous, data)
    else:
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        _fsync_directory(path.parent)
        if _read_private(marker) != data:
            raise OAuthFailure()
    return marker, data


def _finish_refresh(path: Path, marker: Path, expected: bytes, final_grant: bytes):
    current = _read_private(path)
    if current != final_grant:
        raise OAuthFailure()
    completed = json.dumps({"phase": "refresh_completed", "grant_sha256": hashlib.sha256(current).hexdigest()}).encode()
    _replace_record(marker, expected, completed)


def refresh_session(path: Path) -> AccessSession:
    """Один явный POST refresh. Никакого retry, интерактивного согласия или revoke."""
    try:
        with _locked(path) as fd:
            original = _read_fd(fd)
            grant = _grant(original)
            marker, marker_data = _begin_refresh(path, original)
            started = time.monotonic()
            raw = exchange("oauth", "/token", form={"client_id": grant["client_id"], "client_secret": grant["client_secret"],
                           "refresh_token": grant["refresh_token"], "grant_type": "refresh_token"})
            rotated = raw.get("refresh_token", grant["refresh_token"])
            _secret(rotated)
            candidate = None
            if rotated != grant["refresh_token"]:
                # Сохраняем новый refresh token и при последующем отказе scope/expiry.
                candidate = _rotation_candidate(path, {**grant, "refresh_token": rotated})
            # Точное подтверждение scope требуется от endpoint, а не от локального флага.
            if raw.get("scope") != METADATA_SCOPE or raw.get("token_type") != "Bearer":
                raise OAuthFailure()
            token = _secret(raw.get("access_token"))
            if not re.fullmatch(r"[a-zA-Z0-9._~+/=-]+", token):
                raise OAuthFailure()
            lifetime = raw.get("expires_in")
            if type(lifetime) is not int or not 60 <= lifetime <= 86400:
                raise OAuthFailure()
            expires = started + lifetime - 30
            if time.monotonic() >= expires:
                raise OAuthFailure()
            if candidate is not None:
                _install_rotation(path, original, *candidate)
            _finish_refresh(path, marker, marker_data, candidate[1] if candidate is not None else original)
            return AccessSession(token, expires, grant["expected_email"], grant["client_id"])
    except Exception:
        raise OAuthFailure() from None
