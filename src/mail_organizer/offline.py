"""Чистая детерминированная классификация без сетевых провайдеров и ИИ."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
from typing import Any

ALLOWED_PROVIDERS = frozenset({"gmail", "outlook"})
ALLOWED_CATEGORIES = frozenset({"receipts", "notifications", "other"})
PROTECTED_ROOT = "chatgpt-control"


@dataclass(frozen=True)
class MessageFeatures:
    provider: str
    account_alias: str
    message_id: str
    received_at: str
    sender_domain: str
    subject: str
    labels: tuple[str, ...]
    has_attachments: bool


@dataclass(frozen=True)
class PolicyRule:
    identifier: str
    sender_domain: str
    subject_contains: str
    category: str
    ownership: str


@dataclass(frozen=True)
class Decision:
    status: str
    category: str | None
    action_hint: str
    reason_code: str


def _object(value: Any, expected: set[str], required: set[str], context: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - expected or required - set(value):
        raise ValueError(f"Некорректный набор полей: {context}")
    return value


def _text(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str) or not 0 < len(value) <= limit or any(c in value for c in "\r\n\x00"):
        raise ValueError(f"Некорректное поле: {field}")
    return value


def parse_messages(path: Path, max_messages: int = 100) -> list[MessageFeatures]:
    """Фикстура содержит только признаки; body/HTML/secret fields отвергаются."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    return parse_message_data(raw, max_messages)


def parse_message_data(raw: Any, max_messages: int = 100) -> list[MessageFeatures]:
    """Проверяет те же признаки для страницы автономной синхронизации."""
    if not isinstance(raw, list) or len(raw) > max_messages:
        raise ValueError("Фикстура должна содержать не более 100 сообщений")
    allowed = {"provider", "account_alias", "message_id", "received_at", "sender_domain", "subject", "labels", "has_attachments"}
    output = []
    seen = set()
    for item in raw:
        item = _object(item, allowed, allowed, "сообщение")
        provider = _text(item["provider"], "provider", 16).casefold()
        if provider not in ALLOWED_PROVIDERS:
            raise ValueError("Провайдер не поддерживается")
        alias = _text(item["account_alias"], "account_alias", 64)
        msg_id = _text(item["message_id"], "message_id", 256)
        key = (provider, alias, msg_id)
        if key in seen:
            raise ValueError("Повторяющийся идентификатор сообщения")
        seen.add(key)
        received = _text(item["received_at"], "received_at", 64)
        domain = _text(item["sender_domain"], "sender_domain", 255).casefold().strip(".")
        subject = _text(item["subject"], "subject", 500)
        labels = item["labels"]
        if not isinstance(labels, list) or len(labels) > 32:
            raise ValueError("Некорректные labels")
        clean_labels = tuple(_text(v, "label", 200) for v in labels)
        flag = item["has_attachments"]
        if type(flag) is not bool:
            raise ValueError("Некорректное has_attachments")
        output.append(MessageFeatures(provider, alias, msg_id, received, domain, subject, clean_labels, flag))
    return output


def load_rules(directory: Path) -> list[PolicyRule]:
    """Начальный ограниченный формат: JSON (допустимый поднабор YAML 1.2)."""
    raw = json.loads((directory / "rules.yaml").read_text(encoding="utf-8"))
    return parse_rule_data(raw)


def parse_rule_data(raw: Any) -> list[PolicyRule]:
    """Общий строгий разбор правил для CLI и автономного состояния."""
    _object(raw, {"schema_version", "rules"}, {"schema_version", "rules"}, "rules.yaml")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise ValueError("Версия policy не поддерживается")
    items = raw["rules"]
    if not isinstance(items, list) or len(items) > 100:
        raise ValueError("Слишком много правил")
    output = []
    ids = set()
    for item in items:
        item = _object(item, {"id", "sender_domain", "subject_contains", "category", "ownership"},
                       {"id", "sender_domain", "subject_contains", "category", "ownership"}, "rule")
        ident = _text(item["id"], "id", 64)
        if ident in ids:
            raise ValueError("Повторяющийся идентификатор правила")
        ids.add(ident)
        domain = _text(item["sender_domain"], "sender_domain", 255).casefold().strip(".")
        fragment = item["subject_contains"]
        if not isinstance(fragment, str) or len(fragment) > 200:
            raise ValueError("Некорректный subject_contains")
        category = _text(item["category"], "category", 64)
        ownership = _text(item["ownership"], "ownership", 16)
        if category not in ALLOWED_CATEGORIES or ownership != "managed":
            raise ValueError("Недопустимая категория или ownership; external правила только наблюдаются")
        output.append(PolicyRule(ident, domain, fragment.casefold(), category, ownership))
    return sorted(output, key=lambda r: r.identifier)


def is_protected(message: MessageFeatures) -> bool:
    for label in message.labels:
        norm = label.replace("\\", "/").strip("/").casefold()
        if norm == PROTECTED_ROOT or norm.startswith(PROTECTED_ROOT + "/"):
            return True
    return False


def classify(message: MessageFeatures, rules: list[PolicyRule]) -> Decision:
    if is_protected(message):
        return Decision("PROTECTED", None, "none", "protected_control_scope")
    applicable = [r for r in rules if message.sender_domain == r.sender_domain
                  and (not r.subject_contains or r.subject_contains in message.subject.casefold())]
    if not applicable:
        return Decision("REVIEW_REQUIRED", None, "none", "no_matching_rule")
    categories = {r.category for r in applicable}
    if len(categories) != 1:
        return Decision("REVIEW_REQUIRED", None, "none", "conflicting_rules")
    return Decision("PROPOSED", applicable[0].category, "label_candidate", "matched_managed_rule")
