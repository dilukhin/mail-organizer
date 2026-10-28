"""Автономные страницы и файловая SQLite: без OAuth, сети и изменения почты."""
from __future__ import annotations

from contextlib import closing
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat

from .offline import (ALLOWED_PROVIDERS, MessageFeatures, PolicyRule, classify,
                      parse_message_data, parse_rule_data)
from .state import SCHEMA_VERSION, create_schema, open_memory_state


class StateError(ValueError):
    """Состояние не подтверждено; автоматическое восстановление запрещено."""


class StateDrift(StateError):
    """Ожидаемая версия или содержимое страницы не совпали."""


def _name(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", value):
        raise StateError("Некорректный локальный псевдоним или идентификатор")
    return value


def _cursor(value: str) -> str:
    if not isinstance(value, str) or not 0 < len(value) <= 4096 or any(ord(c) < 32 for c in value):
        raise StateError("Некорректный курсор")
    return value


@dataclass(frozen=True)
class Scope:
    provider: str
    account_alias: str
    scope_key: str

    def __post_init__(self):
        if not isinstance(self.provider, str) or self.provider not in ALLOWED_PROVIDERS:
            raise StateError("Провайдер не поддерживается")
        _name(self.account_alias)
        _name(self.scope_key)


@dataclass(frozen=True)
class Checkpoint:
    revision: int = 0
    cursor: str | None = None

    def __post_init__(self):
        if type(self.revision) is not int or not 0 <= self.revision < 2**63 - 1:
            raise StateError("Некорректная версия курсора")
        if self.revision == 0:
            if self.cursor is not None:
                raise StateError("Начальная версия не имеет курсора")
        else:
            _cursor(self.cursor)


@dataclass(frozen=True)
class Page:
    batch_id: str
    expected: Checkpoint
    next_cursor: str
    messages: tuple[MessageFeatures, ...]


def _digest(value) -> str:
    data = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def load_pages(path: Path) -> tuple[Scope, list[Page]]:
    """Закрытый формат фикстуры; произвольные ошибки API/тела писем не принимаются."""
    with path.open("rb") as source:
        data = source.read(2 * 1024 * 1024 + 1)
    if len(data) > 2 * 1024 * 1024:
        raise StateError("Фикстура страниц превышает 2 МиБ")
    raw = json.loads(data.decode("utf-8"))
    fields = {"schema_version", "provider", "account_alias", "scope_key", "pages"}
    if not isinstance(raw, dict) or set(raw) != fields or type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise StateError("Некорректная фикстура страниц")
    scope = Scope(raw["provider"], raw["account_alias"], raw["scope_key"])
    if not isinstance(raw["pages"], list) or not 1 <= len(raw["pages"]) <= 100:
        raise StateError("Нужно от 1 до 100 страниц")
    pages = []
    for item in raw["pages"]:
        if not isinstance(item, dict) or set(item) != {"batch_id", "expected_revision", "expected_cursor", "next_cursor", "messages"}:
            raise StateError("Некорректная страница")
        page = Page(_name(item["batch_id"]), Checkpoint(item["expected_revision"], item["expected_cursor"]),
                    _cursor(item["next_cursor"]), tuple(parse_message_data(item["messages"])))
        pages.append(_validate_page(scope, page))
    return scope, pages


def _validate_page(scope: Scope, page: Page) -> Page:
    _name(page.batch_id)
    if not isinstance(page.expected, Checkpoint):
        raise StateError("Некорректное ожидаемое состояние")
    _cursor(page.next_cursor)
    if page.next_cursor == page.expected.cursor:
        raise StateError("Страница не продвигает курсор")
    # Повторная проверка нужна и для вызова Python API без файловой фикстуры.
    raw = [{**asdict(m), "labels": list(m.labels)} for m in page.messages]
    messages = tuple(parse_message_data(raw))
    if any((m.provider, m.account_alias) != (scope.provider, scope.account_alias) for m in messages):
        raise StateError("Страница содержит сообщения другого ящика")
    return Page(page.batch_id, page.expected, page.next_cursor, messages)


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.absolute().as_uri() + "?mode=rw", uri=True, isolation_level=None, timeout=0)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = FULL")
    except sqlite3.Error:
        conn.close()
        raise StateError("Соединение с состоянием не настроено") from None
    return conn


def _signature(conn: sqlite3.Connection):
    return conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name").fetchall()


class OfflineState:
    """Один владелец соединения. При ошибке записи продолжения блокируются."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._failed = False

    @classmethod
    def create(cls, path: Path) -> OfflineState:
        # Только новый файл в уже существующем каталоге; без overwrite/cleanup.
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(fd)
        conn = _connect(path)
        try:
            create_schema(conn)
        except sqlite3.Error:
            conn.close()
            raise StateError("Создание состояния не подтверждено; файл сохранён для диагностики") from None
        return cls(conn)

    @classmethod
    def open(cls, path: Path) -> OfflineState:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise StateError("Нужен обычный файл состояния без символической ссылки")
        if os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise StateError("Файл состояния должен принадлежать пользователю и иметь права 0600")
        conn = _connect(path)
        try:
            with closing(open_memory_state()) as expected:
                if _signature(conn) != _signature(expected) or conn.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise StateError("Схема или целостность состояния не подтверждена")
            if conn.execute("SELECT version FROM schema_version").fetchall() != [(SCHEMA_VERSION,)]:
                raise StateError("Версия состояния не поддерживается")
        except (sqlite3.Error, StateError):
            conn.close()
            raise StateError("Состояние не открыто; пересоздание и миграция не выполнялись") from None
        return cls(conn)

    def close(self) -> None:
        self._failed = True
        self._conn.close()

    def checkpoint(self, scope: Scope) -> Checkpoint:
        if self._failed:
            raise StateError("Соединение заблокировано; нужна диагностика")
        row = self._conn.execute("SELECT revision, cursor_value FROM provider_cursors "
                                 "WHERE provider=? AND account_alias=? AND scope_key=?", tuple(asdict(scope).values())).fetchone()
        return Checkpoint(*row) if row else Checkpoint()

    def bind_identity(self, scope: Scope, identity_sha256: str) -> None:
        """Связывает только новый пустой ящик; чужое/непривязанное старое состояние не принимается."""
        if self._failed or not isinstance(identity_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", identity_sha256):
            raise StateError("Привязка учётной записи не подтверждена")
        db = self._conn
        key = (scope.provider, scope.account_alias)
        expected = (scope.scope_key, identity_sha256)
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT scope_key, identity_sha256 FROM account_bindings WHERE provider=? AND account_alias=?", key).fetchone()
            if row is not None:
                if row != expected:
                    raise StateDrift("Привязка учётной записи изменилась")
                db.execute("ROLLBACK")
                return
            for table in ("provider_cursors", "message_metadata", "sync_batches"):
                if db.execute(f"SELECT 1 FROM {table} WHERE provider=? AND account_alias=? LIMIT 1", key).fetchone():
                    raise StateDrift("Существующее состояние не имеет доказанной привязки")
            db.execute("INSERT INTO account_bindings VALUES (?, ?, ?, ?)", (*key, *expected))
            db.execute("COMMIT")
            if db.execute("SELECT scope_key, identity_sha256 FROM account_bindings WHERE provider=? AND account_alias=?", key).fetchone() != expected:
                raise StateError("Привязка не подтверждена чтением")
        except (sqlite3.Error, StateError) as exc:
            if db.in_transaction:
                try:
                    db.execute("ROLLBACK")
                except sqlite3.Error:
                    self._failed = True
            if isinstance(exc, StateDrift) and not self._failed:
                raise
            self._failed = True
            raise StateError("Привязка не подтверждена; обработка остановлена") from None

    def commit_page(self, scope: Scope, page: Page, rules: list[PolicyRule], *, identity_sha256: str | None = None) -> str:
        """Фиксирует страницу целиком. Возвращает committed либо already_committed."""
        if self._failed:
            raise StateError("Соединение заблокировано; нужна диагностика")
        page = _validate_page(scope, page)
        checked = parse_rule_data({"schema_version": 1, "rules": [
            {"id": r.identifier, "sender_domain": r.sender_domain, "subject_contains": r.subject_contains,
             "category": r.category, "ownership": r.ownership} for r in rules]})
        policy_hash = _digest([asdict(r) for r in checked])
        payload_hash = _digest({"page": asdict(page), "policy": policy_hash})
        key = (scope.provider, scope.account_alias, scope.scope_key)
        db = self._conn
        try:
            db.execute("BEGIN IMMEDIATE")
            binding = db.execute("SELECT scope_key, identity_sha256 FROM account_bindings WHERE provider=? AND account_alias=?", key[:2]).fetchone()
            if binding is not None:
                if binding != (scope.scope_key, identity_sha256):
                    raise StateDrift("Нет совпадающей привязки учётной записи")
            elif identity_sha256 is not None:
                raise StateDrift("Ящик ещё не связан с учётной записью")
            old = db.execute("SELECT payload_sha256 FROM sync_batches WHERE provider=? AND account_alias=? "
                             "AND scope_key=? AND batch_id=?", (*key, page.batch_id)).fetchone()
            if old:
                if old[0] != payload_hash:
                    raise StateDrift("Повторная страница отличается от сохранённой")
                db.execute("ROLLBACK")
                return "already_committed"
            if self.checkpoint(scope) != page.expected:
                raise StateDrift("Ожидаемое состояние курсора не совпало")
            old_policy = db.execute("SELECT policy_sha256 FROM provider_cursors WHERE provider=? "
                                    "AND account_alias=? AND scope_key=?", key).fetchone()
            if old_policy and old_policy[0] != policy_hash:
                raise StateDrift("Изменилась policy; нужен отдельный план пересчёта")
            old_scope = db.execute("SELECT scope_key FROM provider_cursors WHERE provider=? AND account_alias=?", key[:2]).fetchone()
            if old_scope and old_scope[0] != scope.scope_key:
                raise StateDrift("Изменился scope ящика; нужен отдельный план")
            for msg in page.messages:
                decision = classify(msg, checked)
                msg_key = (msg.provider, msg.account_alias, msg.message_id)
                db.execute("INSERT INTO message_metadata VALUES (?, ?, ?, ?, ?) ON CONFLICT(provider, account_alias, message_id) "
                           "DO UPDATE SET received_at=excluded.received_at, has_attachments=excluded.has_attachments",
                           (*msg_key, msg.received_at, None if msg.has_attachments is None else int(msg.has_attachments)))
                db.execute("INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(provider, account_alias, message_id) "
                           "DO UPDATE SET status=excluded.status, category=excluded.category, action_hint=excluded.action_hint, reason_code=excluded.reason_code",
                           (*msg_key, decision.status, decision.category, decision.action_hint, decision.reason_code))
                db.execute("DELETE FROM actions WHERE provider=? AND account_alias=? AND message_id=?", msg_key)
                if decision.status == "PROPOSED":
                    db.execute("INSERT INTO actions(provider, account_alias, message_id, mode, action_hint, result) "
                               "VALUES (?, ?, ?, 'dry_run', ?, 'proposed_only')", (*msg_key, decision.action_hint))
            revision = page.expected.revision + 1
            db.execute("INSERT INTO provider_cursors VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(provider, account_alias, scope_key) "
                       "DO UPDATE SET cursor_value=excluded.cursor_value, revision=excluded.revision, policy_sha256=excluded.policy_sha256",
                       (*key, page.next_cursor, revision, policy_hash))
            db.execute("INSERT INTO sync_batches VALUES (?, ?, ?, ?, ?, ?)", (*key, page.batch_id, payload_hash, revision))
            db.execute("COMMIT")
            if self.checkpoint(scope) != Checkpoint(revision, page.next_cursor):
                raise StateError("Сохранение курсора не подтверждено чтением")
            return "committed"
        except (sqlite3.Error, StateError) as exc:
            if db.in_transaction:
                try:
                    db.execute("ROLLBACK")
                except sqlite3.Error:
                    self._failed = True
            if isinstance(exc, StateDrift) and not self._failed:
                raise
            self._failed = True
            raise StateError("Запись страницы не подтверждена; обработка остановлена") from None
