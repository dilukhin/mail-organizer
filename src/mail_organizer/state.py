"""Минимальное SQLite-состояние v0.1; без текста писем и OAuth."""
from __future__ import annotations

import sqlite3
from .offline import Decision, MessageFeatures

SCHEMA_VERSION = 1


def open_memory_state() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript("""
        CREATE TABLE schema_version(version INTEGER NOT NULL CHECK (version = 1));
        INSERT INTO schema_version VALUES (1);
        CREATE TABLE provider_cursors(
            provider TEXT NOT NULL, account_alias TEXT NOT NULL,
            cursor_value TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY(provider, account_alias)
        );
        CREATE TABLE message_metadata(
            provider TEXT NOT NULL, account_alias TEXT NOT NULL,
            message_id TEXT NOT NULL, received_at TEXT NOT NULL,
            has_attachments INTEGER NOT NULL CHECK (has_attachments IN (0, 1)),
            PRIMARY KEY(provider, account_alias, message_id)
        );
        CREATE TABLE decisions(
            provider TEXT NOT NULL, account_alias TEXT NOT NULL,
            message_id TEXT NOT NULL, status TEXT NOT NULL,
            category TEXT, action_hint TEXT NOT NULL, reason_code TEXT NOT NULL,
            PRIMARY KEY(provider, account_alias, message_id),
            FOREIGN KEY(provider, account_alias, message_id)
              REFERENCES message_metadata(provider, account_alias, message_id)
        );
        CREATE TABLE actions(
            id INTEGER PRIMARY KEY,
            provider TEXT NOT NULL, account_alias TEXT NOT NULL,
            message_id TEXT NOT NULL, mode TEXT NOT NULL CHECK(mode = 'dry_run'),
            action_hint TEXT NOT NULL, result TEXT NOT NULL CHECK(result = 'proposed_only'),
            FOREIGN KEY(provider, account_alias, message_id)
              REFERENCES message_metadata(provider, account_alias, message_id)
        );
        CREATE TABLE incidents(id INTEGER PRIMARY KEY, reason_code TEXT NOT NULL, created_at TEXT NOT NULL);
    """)
    return conn


def record_decision(conn: sqlite3.Connection, message: MessageFeatures, decision: Decision) -> None:
    """Записывает только идентификаторы/метаданные и факт предложения."""
    key = (message.provider, message.account_alias, message.message_id)
    with conn:
        conn.execute("INSERT INTO message_metadata VALUES (?, ?, ?, ?, ?)",
                     (*key, message.received_at, int(message.has_attachments)))
        conn.execute("INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (*key, decision.status, decision.category, decision.action_hint, decision.reason_code))
        if decision.status == "PROPOSED":
            conn.execute("INSERT INTO actions(provider, account_alias, message_id, mode, action_hint, result) "
                         "VALUES (?, ?, ?, 'dry_run', ?, 'proposed_only')",
                         (*key, decision.action_hint))
