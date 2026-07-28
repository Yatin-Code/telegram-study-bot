"""Conversation history for the agentic loop.

SQLite-backed rolling window (default last 15 turns) so the bot remembers
recent user/assistant/tool turns across messages. Tool results are stored
as short summaries to keep context meaningful without flooding the LLM.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "sqlite_mirror.db"
TABLE = "conversation_history"
DEFAULT_WINDOW = 15


def _init(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            tool_name TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_conv_chat_created
        ON {TABLE}(chat_id, created_at DESC)
        """
    )
    conn.commit()


def _summarize_tool_result(role: str, content: str, tool_name: str | None = None) -> str:
    """Summarize tool results so the context window stays useful."""
    if role != "tool":
        return content
    try:
        data = json.loads(content)
    except Exception:
        return content
    # Shorten success/error results to 1-2 lines.
    if isinstance(data, dict):
        if "error" in data:
            return f"[tool result] error: {data['error']}"
        if "cancelled" in data:
            return "[tool result] write cancelled by user"
        if "rows" in data:
            rows = data["rows"]
            preview = json.dumps(rows[:3], ensure_ascii=False)
            return f"[tool result] {len(rows)} row(s): {preview}"
        return "[tool result] " + json.dumps(data, ensure_ascii=False)[:200]
    return content


def save_message(
    chat_id: int,
    role: str,
    content: str,
    *,
    tool_name: Optional[str] = None,
    db_path: str | Path | None = None,
) -> None:
    """Persist one message/turn."""
    path = db_path or DEFAULT_DB_PATH
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(path))
        conn.execute("PRAGMA journal_mode=WAL")
        _init(conn)
        content = _summarize_tool_result(role, content, tool_name)
        conn.execute(
            f"INSERT INTO {TABLE} (chat_id, role, content, tool_name, created_at) VALUES (?, ?, ?, ?, ?)",
            (chat_id, role, content, tool_name, dt.datetime.now(dt.timezone.utc).isoformat()),
        )
        conn.commit()
    except Exception:
        logger.exception("failed to save conversation message")
    finally:
        if conn is not None:
            conn.close()


def save_turn(
    chat_id: int,
    user_text: str,
    assistant_text: str,
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> None:
    """Convenience: save a full user→assistant exchange."""
    save_message(chat_id, "user", user_text, db_path=db_path)
    save_message(chat_id, "assistant", assistant_text, db_path=db_path)


def recent_messages(
    chat_id: int,
    limit: int = DEFAULT_WINDOW,
    *,
    db_path: str | Path | None = None,
) -> list[dict[str, str]]:
    """Return most recent messages oldest-first, excluding tool/system."""
    path = db_path or DEFAULT_DB_PATH
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        _init(conn)
        rows = conn.execute(
            f"SELECT role, content FROM {TABLE} WHERE chat_id = ? ORDER BY created_at DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
        return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]
    except Exception:
        logger.exception("failed to load conversation history")
        return []
    finally:
        if conn is not None:
            conn.close()


def clear_history(chat_id: int, *, db_path: str | Path | None = None) -> None:
    path = db_path or DEFAULT_DB_PATH
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(path))
        conn.execute("PRAGMA journal_mode=WAL")
        _init(conn)
        conn.execute(f"DELETE FROM {TABLE} WHERE chat_id = ?", (chat_id,))
        conn.commit()
    except Exception:
        logger.exception("failed to clear conversation history")
    finally:
        if conn is not None:
            conn.close()
