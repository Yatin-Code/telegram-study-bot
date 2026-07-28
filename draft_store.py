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

from config import settings as _settings

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
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({DRAFTS_TABLE})")}
    if "editing_field" not in cols:
        conn.execute(f"ALTER TABLE {DRAFTS_TABLE} ADD COLUMN editing_field TEXT")
    conn.commit()


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def create_draft(
    chat_id: int,
    payload: dict[str, Any],
    preview_lines: list[str],
    message_id: int | None = None,
    *,
    ttl_minutes: int | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> str:
    """Store a draft and return its draft_id (12-char hex token)."""
    if ttl_minutes is None:
        try:
            ttl_minutes = _settings.draft_ttl_minutes()
        except Exception:
            ttl_minutes = DEFAULT_TTL_MINUTES
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


# ---------------------------------------------------------------------------
# Conversation history for the ask/query answer loop (follow-up context)
# ---------------------------------------------------------------------------
# A rolling window of the last few (question, answer) turns per chat, so a
# follow-up like "what about physics?" can be answered against the prior
# question. This is deliberately separate from the three existing context
# layers and never feeds them:
#   - session context (subject/chapter/... for logging inheritance)
#   - pending clarification (bot-asked follow-up merge)
#   - the within-question SQL loop's ephemeral message list
# Only the ask/query path writes here; only sql_query_flow reads it.

QA_HISTORY_TABLE = "chat_qa_history"
QA_HISTORY_MAX_PAIRS = 5            # 5 user + 5 assistant = 10 messages max
QA_HISTORY_TTL_MINUTES = 60         # ignore turns older than this on read
QA_ANSWER_MAX_CHARS = 1500          # bound stored answers so the prompt stays small


def _init_qa_history(conn: sqlite3.Connection) -> None:
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {QA_HISTORY_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{QA_HISTORY_TABLE}_chat "
        f"ON {QA_HISTORY_TABLE}(chat_id, id)"
    )
    conn.commit()


def record_qa(
    chat_id: int,
    question: str,
    answer: str,
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> None:
    """Append a (question, answer) turn, then trim to the newest N pairs."""
    question = (question or "").strip()
    answer = (answer or "").strip()
    if not question or not answer:
        return
    if len(answer) > QA_ANSWER_MAX_CHARS:
        answer = answer[:QA_ANSWER_MAX_CHARS] + "…"
    with _connect(db_path) as conn:
        _init_qa_history(conn)
        conn.execute(
            f"INSERT INTO {QA_HISTORY_TABLE} "
            "(chat_id, question, answer, created_at) VALUES (?, ?, ?, ?)",
            (chat_id, question, answer, _utc_now().isoformat()),
        )
        # Keep only the newest QA_HISTORY_MAX_PAIRS rows for this chat.
        conn.execute(
            f"DELETE FROM {QA_HISTORY_TABLE} WHERE chat_id = ? AND id NOT IN ("
            f"  SELECT id FROM {QA_HISTORY_TABLE} WHERE chat_id = ? "
            f"  ORDER BY id DESC LIMIT ?"
            f")",
            (chat_id, chat_id, QA_HISTORY_MAX_PAIRS),
        )
        conn.commit()


def recent_qa(
    chat_id: int,
    *,
    limit_pairs: int = QA_HISTORY_MAX_PAIRS,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> list[dict[str, str]]:
    """Return recent turns oldest-first as [{"question":..., "answer":...}].

    Turns older than the QA-history TTL (settings, default 60 min) are skipped
    (and pruned) so a follow-up never binds to a stale, unrelated earlier
    conversation.
    """
    try:
        ttl_minutes = _settings.qa_history_ttl_minutes()
    except Exception:
        ttl_minutes = QA_HISTORY_TTL_MINUTES
    cutoff = (
        _utc_now() - dt.timedelta(minutes=ttl_minutes)
    ).isoformat()
    with _connect(db_path) as conn:
        _init_qa_history(conn)
        conn.execute(
            f"DELETE FROM {QA_HISTORY_TABLE} WHERE chat_id = ? AND created_at < ?",
            (chat_id, cutoff),
        )
        conn.commit()
        rows = conn.execute(
            f"SELECT question, answer FROM {QA_HISTORY_TABLE} "
            "WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, max(1, limit_pairs)),
        ).fetchall()
    return [{"question": r["question"], "answer": r["answer"]} for r in reversed(rows)]


def clear_qa_history(chat_id: int, *, db_path: str | Path = DEFAULT_DB_PATH) -> None:
    with _connect(db_path) as conn:
        _init_qa_history(conn)
        conn.execute(
            f"DELETE FROM {QA_HISTORY_TABLE} WHERE chat_id = ?", (chat_id,)
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Block-close debrief (doubts + key points right after a confirmed ledger log)
# ---------------------------------------------------------------------------
# One row per chat: which just-logged ledger page we're debriefing and which
# stage we're at ("doubts" loop, then a single "notes" answer).

DEBRIEF_TABLE = "pending_session_debrief"
DEBRIEF_TTL_MINUTES = 10


def _init_debrief(conn: sqlite3.Connection) -> None:
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {DEBRIEF_TABLE} (
            chat_id INTEGER PRIMARY KEY,
            ledger_page_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            asked_at TEXT NOT NULL
        )
    """)
    conn.commit()


def set_session_debrief(
    chat_id: int, ledger_page_id: str, *, db_path: str | Path = DEFAULT_DB_PATH
) -> None:
    with _connect(db_path) as conn:
        _init_debrief(conn)
        conn.execute(
            f"INSERT OR REPLACE INTO {DEBRIEF_TABLE} "
            "(chat_id, ledger_page_id, stage, asked_at) VALUES (?, ?, 'doubts', ?)",
            (chat_id, ledger_page_id, _utc_now().isoformat()),
        )
        conn.commit()


def get_session_debrief(
    chat_id: int, *, db_path: str | Path = DEFAULT_DB_PATH
) -> Optional[dict[str, str]]:
    """Current debrief state (NOT cleared on read — the doubts stage loops)."""
    with _connect(db_path) as conn:
        _init_debrief(conn)
        row = conn.execute(
            f"SELECT * FROM {DEBRIEF_TABLE} WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        if row is None:
            return None
        asked = dt.datetime.fromisoformat(row["asked_at"])
        if (_utc_now() - asked).total_seconds() > DEBRIEF_TTL_MINUTES * 60:
            conn.execute(f"DELETE FROM {DEBRIEF_TABLE} WHERE chat_id = ?", (chat_id,))
            conn.commit()
            return None
    return {"ledger_page_id": row["ledger_page_id"], "stage": row["stage"]}


def advance_session_debrief(
    chat_id: int, *, db_path: str | Path = DEFAULT_DB_PATH
) -> None:
    """Move from the doubts loop to the single notes question (refreshes TTL)."""
    with _connect(db_path) as conn:
        _init_debrief(conn)
        conn.execute(
            f"UPDATE {DEBRIEF_TABLE} SET stage = 'notes', asked_at = ? WHERE chat_id = ?",
            (_utc_now().isoformat(), chat_id),
        )
        conn.commit()


def clear_session_debrief(
    chat_id: int, *, db_path: str | Path = DEFAULT_DB_PATH
) -> None:
    with _connect(db_path) as conn:
        _init_debrief(conn)
        conn.execute(f"DELETE FROM {DEBRIEF_TABLE} WHERE chat_id = ?", (chat_id,))
        conn.commit()
# ---------------------------------------------------------------------------
# Pending /settings edit (which setting a chat is currently typing a value for)
# ---------------------------------------------------------------------------
# Mirrors the pending-clarification pattern: one row per chat, short TTL, the
# next text message is consumed as the new value for the stored setting key.

SETTING_EDIT_TABLE = "pending_setting_edits"
SETTING_EDIT_TTL_MINUTES = 5


def _init_setting_edit(conn: sqlite3.Connection) -> None:
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {SETTING_EDIT_TABLE} (
            chat_id INTEGER PRIMARY KEY,
            setting_key TEXT NOT NULL,
            asked_at TEXT NOT NULL
        )
    """)
    conn.commit()


def set_pending_setting_edit(
    chat_id: int, setting_key: str, *, db_path: str | Path = DEFAULT_DB_PATH
) -> None:
    with _connect(db_path) as conn:
        _init_setting_edit(conn)
        conn.execute(
            f"INSERT OR REPLACE INTO {SETTING_EDIT_TABLE} "
            "(chat_id, setting_key, asked_at) VALUES (?, ?, ?)",
            (chat_id, setting_key, _utc_now().isoformat()),
        )
        conn.commit()


def get_pending_setting_edit(
    chat_id: int, *, db_path: str | Path = DEFAULT_DB_PATH
) -> Optional[str]:
    """Fetch-and-clear the setting key awaiting a value. None if absent/stale."""
    with _connect(db_path) as conn:
        _init_setting_edit(conn)
        row = conn.execute(
            f"SELECT * FROM {SETTING_EDIT_TABLE} WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            f"DELETE FROM {SETTING_EDIT_TABLE} WHERE chat_id = ?", (chat_id,)
        )
        conn.commit()
    asked = dt.datetime.fromisoformat(row["asked_at"])
    if (_utc_now() - asked).total_seconds() > SETTING_EDIT_TTL_MINUTES * 60:
        return None
    return row["setting_key"]


def clear_pending_setting_edit(
    chat_id: int, *, db_path: str | Path = DEFAULT_DB_PATH
) -> None:
    with _connect(db_path) as conn:
        _init_setting_edit(conn)
        conn.execute(
            f"DELETE FROM {SETTING_EDIT_TABLE} WHERE chat_id = ?", (chat_id,)
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Bug reports (7-day soak instrumentation: /bug <text>, /bugs)
# ---------------------------------------------------------------------------

BUGS_TABLE = "bug_reports"


def _init_bugs(conn: sqlite3.Connection) -> None:
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {BUGS_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open'
        )
    """)
    conn.commit()


def add_bug_report(
    chat_id: int, text: str, *, db_path: str | Path = DEFAULT_DB_PATH
) -> int:
    with _connect(db_path) as conn:
        _init_bugs(conn)
        cur = conn.execute(
            f"INSERT INTO {BUGS_TABLE} (chat_id, text, created_at) VALUES (?, ?, ?)",
            (chat_id, text.strip(), _utc_now().isoformat()),
        )
        conn.commit()
        return int(cur.lastrowid)


def list_bug_reports(
    chat_id: int, *, open_only: bool = True, db_path: str | Path = DEFAULT_DB_PATH
) -> list[dict[str, Any]]:
    with _connect(db_path) as conn:
        _init_bugs(conn)
        where = "chat_id = ?" + (" AND status = 'open'" if open_only else "")
        rows = conn.execute(
            f"SELECT * FROM {BUGS_TABLE} WHERE {where} ORDER BY id", (chat_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def close_bug_report(
    chat_id: int, bug_id: int, *, db_path: str | Path = DEFAULT_DB_PATH
) -> bool:
    with _connect(db_path) as conn:
        _init_bugs(conn)
        cur = conn.execute(
            f"UPDATE {BUGS_TABLE} SET status = 'closed' "
            "WHERE id = ? AND chat_id = ? AND status = 'open'",
            (bug_id, chat_id),
        )
        conn.commit()
        return cur.rowcount == 1


# ---------------------------------------------------------------------------
# Pending doubt resolution: conversational verification before marking solved
# ---------------------------------------------------------------------------

PENDING_RESOLVE_TABLE = "pending_doubt_resolutions"
PENDING_RESOLVE_TTL_MINUTES = 30


def _init_pending_resolve(conn: sqlite3.Connection) -> None:
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {PENDING_RESOLVE_TABLE} (
            chat_id INTEGER PRIMARY KEY,
            doubt_id TEXT NOT NULL,
            doubt_title TEXT NOT NULL,
            resolution TEXT NOT NULL,
            teacher_asked INTEGER NOT NULL DEFAULT 0,
            verification_question TEXT NOT NULL,
            asked_at TEXT NOT NULL
        )
    """)
    conn.commit()


def set_pending_doubt_resolution(
    chat_id: int,
    doubt_id: str,
    doubt_title: str,
    resolution: str,
    teacher_asked: bool,
    verification_question: str,
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> None:
    with _connect(db_path) as conn:
        _init_pending_resolve(conn)
        conn.execute(
            f"INSERT OR REPLACE INTO {PENDING_RESOLVE_TABLE} "
            "(chat_id, doubt_id, doubt_title, resolution, teacher_asked, "
            "verification_question, asked_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chat_id, doubt_id, doubt_title, resolution,
             int(teacher_asked), verification_question, _utc_now().isoformat()),
        )
        conn.commit()


def get_pending_doubt_resolution(
    chat_id: int, *, db_path: str | Path = DEFAULT_DB_PATH
) -> dict[str, Any] | None:
    with _connect(db_path) as conn:
        _init_pending_resolve(conn)
        row = conn.execute(
            f"SELECT * FROM {PENDING_RESOLVE_TABLE} WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        if row is None:
            return None
        asked = dt.datetime.fromisoformat(row["asked_at"])
        if (_utc_now() - asked).total_seconds() > PENDING_RESOLVE_TTL_MINUTES * 60:
            conn.execute(
                f"DELETE FROM {PENDING_RESOLVE_TABLE} WHERE chat_id = ?", (chat_id,)
            )
            conn.commit()
            return None
    return dict(row)


def clear_pending_doubt_resolution(
    chat_id: int, *, db_path: str | Path = DEFAULT_DB_PATH
) -> None:
    with _connect(db_path) as conn:
        _init_pending_resolve(conn)
        conn.execute(
            f"DELETE FROM {PENDING_RESOLVE_TABLE} WHERE chat_id = ?", (chat_id,)
        )
        conn.commit()
