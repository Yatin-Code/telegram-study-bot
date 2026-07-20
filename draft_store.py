"""
SQLite-backed draft store for pending log entries (Gap 2).

A "draft" is the state between the bot showing a Confirm/Edit/Cancel preview
and the user tapping one of those buttons. Previously this lived in
python-telegram-bot's in-memory chat_data dict (lost on restart, no TTL).

This module stores drafts in a `drafts` table in sqlite_mirror.db with:
- draft_id (short UUID token, used in Telegram callback_data)
- chat_id
- payload (the WritePlan.to_payload() dict as JSON)
- preview_lines (for re-rendering after edits)
- message_id (the Telegram message with the inline keyboard, for expiry edits)
- created_at / expires_at (TTL-based expiry)

The bot writes a draft when it sends a preview, reads it on button tap,
updates it on field edits, and deletes it on Confirm/Cancel/expiry.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = PROJECT_ROOT / "sqlite_mirror.db"

DRAFTS_TABLE = "drafts"
DEFAULT_TTL_MINUTES = 15


def _connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init(conn: sqlite3.Connection) -> None:
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {DRAFTS_TABLE} (
            draft_id TEXT PRIMARY KEY,
            chat_id INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            preview_lines_json TEXT NOT NULL,
            message_id INTEGER,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            editing_field TEXT
        )
    """)
    conn.commit()


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def create_draft(
    chat_id: int,
    payload: dict[str, Any],
    preview_lines: list[str],
    message_id: int | None = None,
    *,
    ttl_minutes: int = DEFAULT_TTL_MINUTES,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> str:
    """Store a draft and return its draft_id (12-char hex token)."""
    draft_id = uuid.uuid4().hex[:12]
    now = _utc_now()
    expires = now + dt.timedelta(minutes=ttl_minutes)
    with _connect(db_path) as conn:
        _init(conn)
        conn.execute(
            f"INSERT INTO {DRAFTS_TABLE} "
            "(draft_id, chat_id, payload_json, preview_lines_json, message_id, "
            "created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (draft_id, chat_id,
             json.dumps(payload, ensure_ascii=False),
             json.dumps(preview_lines, ensure_ascii=False),
             message_id, now.isoformat(), expires.isoformat()),
        )
        conn.commit()
    return draft_id


def get_draft(draft_id: str, *, db_path: str | Path = DEFAULT_DB_PATH) -> Optional[dict[str, Any]]:
    """Fetch a draft by ID. Returns None if not found or expired."""
    with _connect(db_path) as conn:
        _init(conn)
        row = conn.execute(
            f"SELECT * FROM {DRAFTS_TABLE} WHERE draft_id = ?", (draft_id,)
        ).fetchone()
    if row is None:
        return None
    if row["expires_at"] < _utc_now().isoformat():
        delete_draft(draft_id, db_path=db_path)
        return None
    return {
        "draft_id": row["draft_id"],
        "chat_id": row["chat_id"],
        "payload": json.loads(row["payload_json"]),
        "preview_lines": json.loads(row["preview_lines_json"]),
        "message_id": row["message_id"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "editing_field": row["editing_field"],
    }


def update_draft_payload(
    draft_id: str,
    payload: dict[str, Any],
    preview_lines: list[str],
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> None:
    """Update a draft's payload and preview after a field edit."""
    with _connect(db_path) as conn:
        _init(conn)
        conn.execute(
            f"UPDATE {DRAFTS_TABLE} SET payload_json = ?, preview_lines_json = ?, "
            "editing_field = NULL WHERE draft_id = ?",
            (json.dumps(payload, ensure_ascii=False),
             json.dumps(preview_lines, ensure_ascii=False),
             draft_id),
        )
        conn.commit()


def set_editing_field(
    draft_id: str, field_name: str | None, *, db_path: str | Path = DEFAULT_DB_PATH
) -> None:
    """Mark which field is being edited (or clear with None)."""
    with _connect(db_path) as conn:
        _init(conn)
        conn.execute(
            f"UPDATE {DRAFTS_TABLE} SET editing_field = ? WHERE draft_id = ?",
            (field_name, draft_id),
        )
        conn.commit()


def set_message_id(
    draft_id: str, message_id: int | None, *, db_path: str | Path = DEFAULT_DB_PATH
) -> None:
    with _connect(db_path) as conn:
        _init(conn)
        conn.execute(
            f"UPDATE {DRAFTS_TABLE} SET message_id = ? WHERE draft_id = ?",
            (message_id, draft_id),
        )
        conn.commit()


def delete_draft(draft_id: str, *, db_path: str | Path = DEFAULT_DB_PATH) -> None:
    with _connect(db_path) as conn:
        _init(conn)
        conn.execute(f"DELETE FROM {DRAFTS_TABLE} WHERE draft_id = ?", (draft_id,))
        conn.commit()


def get_editing_draft_for_chat(
    chat_id: int, *, db_path: str | Path = DEFAULT_DB_PATH
) -> Optional[dict[str, Any]]:
    """Find a draft in editing mode for this chat (editing_field IS NOT NULL)."""
    with _connect(db_path) as conn:
        _init(conn)
        row = conn.execute(
            f"SELECT * FROM {DRAFTS_TABLE} WHERE chat_id = ? AND editing_field IS NOT NULL "
            "ORDER BY created_at DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
    if row is None:
        return None
    if row["expires_at"] < _utc_now().isoformat():
        delete_draft(row["draft_id"], db_path=db_path)
        return None
    return {
        "draft_id": row["draft_id"],
        "chat_id": row["chat_id"],
        "payload": json.loads(row["payload_json"]),
        "preview_lines": json.loads(row["preview_lines_json"]),
        "message_id": row["message_id"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "editing_field": row["editing_field"],
    }


def expire_stale_drafts(*, db_path: str | Path = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    """Delete expired drafts and return their info (for editing the original message)."""
    now = _utc_now().isoformat()
    with _connect(db_path) as conn:
        _init(conn)
        rows = conn.execute(
            f"SELECT draft_id, chat_id, message_id FROM {DRAFTS_TABLE} WHERE expires_at < ?",
            (now,),
        ).fetchall()
        expired = [dict(r) for r in rows]
        if expired:
            conn.execute(
                f"DELETE FROM {DRAFTS_TABLE} WHERE expires_at < ?", (now,),
            )
            conn.commit()
    return expired


# ---------------------------------------------------------------------------
# Gap 5: Pending clarification store
# ---------------------------------------------------------------------------
# When the bot asks a clarification question (from intent_parser or
# build_write_plan), we stash the original message + question so the user's
# next reply is interpreted in context rather than parsed cold.

CLARIFICATION_TABLE = "pending_clarifications"
CLARIFICATION_TTL_MINUTES = 15


def _init_clarification(conn: sqlite3.Connection) -> None:
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {CLARIFICATION_TABLE} (
            chat_id INTEGER PRIMARY KEY,
            original_message TEXT NOT NULL,
            question TEXT NOT NULL,
            asked_at TEXT NOT NULL
        )
    """)
    conn.commit()


def set_pending_clarification(
    chat_id: int,
    original_message: str,
    question: str,
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> None:
    """Store a pending clarification for a chat (replaces any previous one)."""
    with _connect(db_path) as conn:
        _init_clarification(conn)
        conn.execute(
            f"INSERT OR REPLACE INTO {CLARIFICATION_TABLE} "
            "(chat_id, original_message, question, asked_at) VALUES (?, ?, ?, ?)",
            (chat_id, original_message, question, _utc_now().isoformat()),
        )
        conn.commit()


def get_pending_clarification(
    chat_id: int, *, db_path: str | Path = DEFAULT_DB_PATH
) -> Optional[dict[str, str]]:
    """Fetch and clear the pending clarification for a chat.

    Returns None and discards the row if it's stale (older than
    CLARIFICATION_TTL_MINUTES) — prevents merging a stale clarification
    into an unrelated fresh message.
    """
    with _connect(db_path) as conn:
        _init_clarification(conn)
        row = conn.execute(
            f"SELECT * FROM {CLARIFICATION_TABLE} WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        if row is None:
            return None
        # TTL check: if asked long ago, the user has moved on — discard
        # rather than risk merging into an unrelated message.
        asked = dt.datetime.fromisoformat(row["asked_at"])
        if (_utc_now() - asked).total_seconds() > CLARIFICATION_TTL_MINUTES * 60:
            conn.execute(
                f"DELETE FROM {CLARIFICATION_TABLE} WHERE chat_id = ?", (chat_id,)
            )
            conn.commit()
            return None
        conn.execute(
            f"DELETE FROM {CLARIFICATION_TABLE} WHERE chat_id = ?", (chat_id,)
        )
        conn.commit()
    return {
        "original_message": row["original_message"],
        "question": row["question"],
        "asked_at": row["asked_at"],
    }


def clear_pending_clarification(
    chat_id: int, *, db_path: str | Path = DEFAULT_DB_PATH
) -> None:
    with _connect(db_path) as conn:
        _init_clarification(conn)
        conn.execute(
            f"DELETE FROM {CLARIFICATION_TABLE} WHERE chat_id = ?", (chat_id,)
        )
        conn.commit()
