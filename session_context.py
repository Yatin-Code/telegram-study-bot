"""
Phase 6 — Session/context state.

Per-chat memory of what the user is currently studying (subject / chapter /
block), so a bare "doubt: sign of relative velocity" inherits that context
instead of forcing the user to re-specify it every time.

Design
------
- SQLite-backed (same mirror DB file as Phase 3) so state survives bot
  restarts. One row per chat_id.
- Auto-clears at the user's LOCAL midnight: context is stamped with the local
  date it was set on; a read on a later local date returns nothing (expired).
  This is evaluated lazily on read (no background job needed) using
  USER_TIMEZONE from config.
- set_context intents call `set_context()`; log_* intents call
  `merge_into_intent()` to backfill missing subject/chapter/block before the
  Phase 7 logging flow decides whether it still needs to ask the user.

The context values are the loose strings the parser produced (e.g. "PHYSICS",
"kinematics", "EB-1"). Normalisation against real Notion option lists happens
later in the Phase 7 confirm flow, not here.
"""

from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from config import settings

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = PROJECT_ROOT / "sqlite_mirror.db"
CONTEXT_TABLE = "chat_context"

# Context fields we track and inherit. Order matters only for display.
CONTEXT_KEYS = ("subject", "chapter", "block", "exercise")


def _tz() -> ZoneInfo:
    """User's timezone; falls back to UTC if the configured name is unknown."""
    name = settings.user_timezone()
    try:
        return ZoneInfo(name)
    except Exception:
        logger.warning("Unknown USER_TIMEZONE %r, falling back to UTC", name)
        return ZoneInfo("UTC")


def _now_local() -> dt.datetime:
    return dt.datetime.now(_tz())


def local_now() -> dt.datetime:
    """Public: current time in the user's timezone."""
    return _now_local()


def local_today_iso() -> str:
    """Public: today's date (user timezone) as an ISO string, e.g. 2026-07-19."""
    return _now_local().date().isoformat()


def connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create the context table if it doesn't exist."""
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {CONTEXT_TABLE} (
            chat_id TEXT PRIMARY KEY,
            subject TEXT,
            chapter TEXT,
            block TEXT,
            exercise TEXT,
            session_started_at TEXT NOT NULL,
            local_date TEXT NOT NULL
        )
        """
    )
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({CONTEXT_TABLE})")}
    if "exercise" not in cols:
        conn.execute(f"ALTER TABLE {CONTEXT_TABLE} ADD COLUMN exercise TEXT")
    conn.commit()


def _row_to_context(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "subject": row["subject"],
        "chapter": row["chapter"],
        "block": row["block"],
        "exercise": row["exercise"],
        "session_started_at": row["session_started_at"],
    }


def set_context(
    chat_id: int | str,
    subject: Optional[str] = None,
    chapter: Optional[str] = None,
    block: Optional[str] = None,
    exercise: Optional[str] = None,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> dict[str, Any]:
    """Create/replace the active study context for a chat.

    Any field left as None is cleared. Returns the stored context dict.
    """
    own = conn is None
    conn = conn or connect()
    try:
        init_db(conn)
        now = _now_local()
        conn.execute(
            f"""
            INSERT INTO {CONTEXT_TABLE}
                (chat_id, subject, chapter, block, exercise, session_started_at, local_date)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                subject=excluded.subject,
                chapter=excluded.chapter,
                block=excluded.block,
                exercise=excluded.exercise,
                session_started_at=excluded.session_started_at,
                local_date=excluded.local_date
            """,
            (
                str(chat_id),
                subject,
                chapter,
                block,
                exercise,
                now.isoformat(),
                now.date().isoformat(),
            ),
        )
        conn.commit()
        logger.info(
            "context set chat_id=%s subject=%r chapter=%r block=%r exercise=%r",
            chat_id, subject, chapter, block, exercise,
        )
        return {
            "subject": subject,
            "chapter": chapter,
            "block": block,
            "exercise": exercise,
            "session_started_at": now.isoformat(),
        }
    finally:
        if own:
            conn.close()


def get_context(
    chat_id: int | str,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[dict[str, Any]]:
    """Return the active context for a chat, or None if unset/expired.

    Context expires at local midnight: if it was set on an earlier local date
    than today, it is deleted and None is returned.
    """
    own = conn is None
    conn = conn or connect()
    try:
        init_db(conn)
        row = conn.execute(
            f"SELECT * FROM {CONTEXT_TABLE} WHERE chat_id = ?",
            (str(chat_id),),
        ).fetchone()
        if row is None:
            return None
        today = _now_local().date().isoformat()
        if row["local_date"] != today:
            logger.info(
                "context expired chat_id=%s set_on=%s today=%s",
                chat_id, row["local_date"], today,
            )
            conn.execute(
                f"DELETE FROM {CONTEXT_TABLE} WHERE chat_id = ?", (str(chat_id),)
            )
            conn.commit()
            return None
        return _row_to_context(row)
    finally:
        if own:
            conn.close()


def clear_context(
    chat_id: int | str,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """Forget the active context for a chat (e.g. /newsession)."""
    own = conn is None
    conn = conn or connect()
    try:
        init_db(conn)
        conn.execute(
            f"DELETE FROM {CONTEXT_TABLE} WHERE chat_id = ?", (str(chat_id),)
        )
        conn.commit()
        logger.info("context cleared chat_id=%s", chat_id)
    finally:
        if own:
            conn.close()


def elapsed_minutes(
    chat_id: int | str,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[float]:
    """Minutes since the active session timer started, or None if no context."""
    ctx = get_context(chat_id, conn=conn)
    if not ctx or not ctx.get("session_started_at"):
        return None
    try:
        started = dt.datetime.fromisoformat(ctx["session_started_at"])
    except ValueError:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=_tz())
    return (_now_local() - started).total_seconds() / 60.0


def restart_timer(
    chat_id: int | str,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """Reset session_started_at to now, keeping the rest of the context.

    Called after a confirmed ledger commit so the next stint in the same
    block gets its own elapsed time. No-op when no context is active.
    """
    own = conn is None
    conn = conn or connect()
    try:
        init_db(conn)
        now = _now_local()
        conn.execute(
            f"UPDATE {CONTEXT_TABLE} SET session_started_at = ?, local_date = ? "
            f"WHERE chat_id = ?",
            (now.isoformat(), now.date().isoformat(), str(chat_id)),
        )
        conn.commit()
    finally:
        if own:
            conn.close()


def context_for_parser(chat_id: int | str) -> Optional[dict[str, Any]]:
    """Shape the stored context for `intent_parser.parse_message`.

    Returns only the non-empty subject/chapter/block keys so the parser prompt
    inherits them; None when there is nothing active.
    """
    ctx = get_context(chat_id)
    if not ctx:
        return None
    shaped = {k: ctx.get(k) for k in CONTEXT_KEYS if ctx.get(k)}
    return shaped or None


def merge_into_intent(
    intent: Any,
    chat_id: int | str,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> dict[str, Any]:
    """Backfill missing subject/chapter/block on a log_* intent from context.

    The parser usually inherits context itself (it's in the system prompt), but
    this is the authoritative merge the logging flow relies on: for each of
    subject/chapter/block, use the intent's own value if present, otherwise the
    stored context value. Returns the effective {subject, chapter, block} dict.

    Only meaningful for log_* actions; callers decide when to invoke it.
    """
    ctx = get_context(chat_id, conn=conn) or {}
    filters = intent.filters
    effective: dict[str, Any] = {}
    for key in CONTEXT_KEYS:
        own_val = getattr(filters, key, None)
        effective[key] = own_val if own_val else ctx.get(key)
    return effective
