"""Подготовка серверного Gmail-адаптера; без ChatGPT Connector и OAuth-клиента."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from email import policy
from email.header import decode_header, make_header
from email.parser import HeaderParser
import hashlib
import json
import re
from typing import Literal, Protocol

from .incremental import Checkpoint, OfflineState, Page, Scope
from .offline import MessageFeatures, PolicyRule, parse_message_data

METADATA_SCOPE = "https://www.googleapis.com/auth/gmail.metadata"
HISTORY_SCOPE_KEY = "gmail-history-all-v1"
Operation = Literal["labels.list", "history.list", "messages.get"]


class GmailError(ValueError):
    """Только фиксированный код причины, без ответа сервиса, адресов и токенов."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__("Обработка Gmail остановлена: " + reason)


class ReadFailure(Exception):
    """Будущий транспорт передаёт статус отдельно от чувствительного тела ошибки."""

    def __init__(self, status: int):
        self.status = status
        super().__init__("Ошибка чтения Gmail API")


class GmailReader(Protocol):
    """Контракт собственного серверного транспорта. В этом срезе реализован тестовой заменой."""

    def read(self, operation: Operation, parameters: Mapping[str, object]) -> dict: ...


def _object(value, allowed: set[str], required: set[str] = frozenset()) -> dict:
    if not isinstance(value, dict) or set(value) - allowed or required - set(value):
        raise GmailError("invalid_response")
    return value


def _items(value, limit: int) -> list:
    if not isinstance(value, list) or len(value) > limit:
        raise GmailError("response_limit")
    return value


def _text(value, limit: int) -> str:
    if not isinstance(value, str) or not 0 < len(value) <= limit or any(ord(c) < 32 for c in value):
        raise GmailError("invalid_response")
    return value


def _id(value) -> str:
    value = _text(value, 256)
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", value):
        raise GmailError("invalid_message_id")
    return value


def _history_id(value) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,31}", value):
        raise GmailError("invalid_history_id")
    return value


def _read(reader: GmailReader, operation: Operation, **parameters) -> dict:
    try:
        return reader.read(operation, {"userId": "me", **parameters})
    except ReadFailure as exc:
        if exc.status in (401, 403):
            reason = "access_denied"
        elif exc.status == 404 and operation == "history.list":
            reason = "cursor_expired"
        elif exc.status == 404 and operation == "messages.get":
            reason = "message_disappeared"
        else:
            reason = "source_unavailable"
        raise GmailError(reason) from None
    except Exception:
        raise GmailError("transport_failure") from None


def parse_labels(raw: dict) -> dict[str, str]:
    raw = _object(raw, {"labels"})
    labels = {}
    names = set()
    for item in _items(raw.get("labels", []), 10000):
        item = _object(item, {"id", "name"}, {"id", "name"})
        ident, name = _id(item["id"]), _text(item["name"], 200)
        if ident in labels or name in names:
            raise GmailError("duplicate_label")
        labels[ident] = name
        names.add(name)
    return labels


def _domain(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        header = HeaderParser(policy=policy.default).parsestr("From: " + value + "\n\n")["From"]
        if header.defects or len(header.addresses) != 1:
            return None
        address = header.addresses[0]
        domain = address.domain.encode("idna").decode("ascii").casefold()
        if not address.username or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,253}[a-z0-9])?", domain):
            return None
        return domain
    except (ValueError, UnicodeError, AttributeError):
        return None


def _subject(value: str | None) -> str | None:
    if value is None:
        return ""
    try:
        text = str(make_header(decode_header(value)))
        if len(text) > 500 or any(ord(c) < 32 for c in text):
            return None
        return text
    except (ValueError, UnicodeError, LookupError):
        return None


def parse_metadata(raw: dict, message_id: str, scope: Scope, labels: dict[str, str]) -> MessageFeatures:
    raw = _object(raw, {"id", "internalDate", "labelIds", "payload"}, {"id", "internalDate"})
    if _id(raw["id"]) != message_id:
        raise GmailError("message_identity_mismatch")
    stamp = raw["internalDate"]
    if not isinstance(stamp, str) or not re.fullmatch(r"[0-9]{1,16}", stamp):
        raise GmailError("invalid_timestamp")
    try:
        received = (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=int(stamp))).isoformat()
    except OverflowError:
        raise GmailError("invalid_timestamp") from None
    names = []
    for ident in _items(raw.get("labelIds", []), 32):
        ident = _id(ident)
        if ident not in labels or ident in names:
            raise GmailError("unknown_label")
        names.append(ident)
    payload = _object(raw.get("payload", {}), {"headers"})
    headers = {}
    for header in _items(payload.get("headers", []), 2):
        header = _object(header, {"name", "value"}, {"name", "value"})
        name = _text(header["name"], 32).casefold()
        if name not in {"from", "subject"} or name in headers:
            raise GmailError("ambiguous_headers")
        # Пустая тема допустима. Никаких CR/LF или исполнения содержимого заголовка.
        value = header["value"]
        headers[name] = "" if value == "" else _text(value, 4096)
    message = {"provider": "gmail", "account_alias": scope.account_alias, "message_id": message_id,
               "received_at": received, "sender_domain": _domain(headers.get("from")),
               "subject": _subject(headers.get("subject")), "labels": [labels[v] for v in names],
               "has_attachments": None}
    return parse_message_data([message])[0]


class GmailHistoryAdapter:
    """Собирает ограниченное полное окно history в памяти до единственной SQLite-записи."""

    def __init__(self, reader: GmailReader):
        self.reader = reader

    def prepare(self, scope: Scope, expected: Checkpoint) -> Page | None:
        if scope.provider != "gmail" or scope.scope_key != HISTORY_SCOPE_KEY:
            raise GmailError("scope_mismatch")
        if expected.revision == 0:
            raise GmailError("initial_inventory_required")
        start = _history_id(expected.cursor)
        ids: set[str] = set()
        tokens: set[str] = set()
        token = None
        previous = int(start)
        total_records = 0
        for _ in range(10):
            parameters = {"startHistoryId": start, "maxResults": 100,
                          "fields": "history(id,messages(id),messagesAdded(message(id)),messagesDeleted(message(id)),labelsAdded(message(id),labelIds),labelsRemoved(message(id),labelIds)),nextPageToken,historyId"}
            if token is not None:
                parameters["pageToken"] = token
            raw = _object(_read(self.reader, "history.list", **parameters),
                          {"history", "nextPageToken", "historyId"}, {"historyId"})
            end = _history_id(raw["historyId"])
            records = _items(raw.get("history", []), 100)
            total_records += len(records)
            if total_records > 100:
                raise GmailError("history_limit")
            for record in records:
                record = _object(record, {"id", "messages", "messagesAdded", "messagesDeleted", "labelsAdded", "labelsRemoved"}, {"id"})
                ident = int(_history_id(record["id"]))
                if ident <= previous or ident > int(end):
                    raise GmailError("history_order")
                previous = ident
                deleted = _items(record.get("messagesDeleted", []), 100)
                if deleted:
                    raise GmailError("deletion_requires_inventory")
                # messages — дублирующее краткое представление; читаем только типизированные события.
                record_ids = set()
                for event in ("messagesAdded", "labelsAdded", "labelsRemoved"):
                    for change in _items(record.get(event, []), 100):
                        change = _object(change, {"message", "labelIds"}, {"message"})
                        message = _object(change["message"], {"id"}, {"id"})
                        record_ids.add(_id(message["id"]))
                        for label in _items(change.get("labelIds", []), 100):
                            _id(label)
                summary_ids = {_id(_object(m, {"id"}, {"id"})["id"])
                               for m in _items(record.get("messages", []), 100)}
                if not record_ids or not summary_ids.issubset(record_ids):
                    raise GmailError("unsupported_history")
                ids.update(record_ids)
                if len(ids) > 100:
                    raise GmailError("message_limit")
            if int(end) < previous:
                raise GmailError("history_order")
            token = raw.get("nextPageToken")
            if token is None:
                break
            token = _text(token, 2048)
            if token in tokens:
                raise GmailError("pagination_cycle")
            tokens.add(token)
        else:
            raise GmailError("page_limit")
        if end == start:
            return None
        labels = parse_labels(_read(self.reader, "labels.list", fields="labels(id,name)"))
        messages = tuple(parse_metadata(_read(self.reader, "messages.get", id=ident, format="metadata",
                                             metadataHeaders=("From", "Subject"),
                                             fields="id,internalDate,labelIds,payload(headers(name,value))"),
                                        ident, scope, labels) for ident in sorted(ids))
        identity = json.dumps({"scope": asdict(scope), "expected": asdict(expected), "end": end}, sort_keys=True)
        batch_id = "gmail-" + hashlib.sha256(identity.encode()).hexdigest()[:32]
        return Page(batch_id, expected, end, messages)


def sync_history(state: OfflineState, scope: Scope, adapter: GmailHistoryAdapter, rules: list[PolicyRule]) -> str:
    """Ни одной SQLite-записи, пока всё окно history и метаданные не подготовлены."""
    expected = state.checkpoint(scope)
    page = adapter.prepare(scope, expected)
    return "no_changes" if page is None else state.commit_page(scope, page, rules)
