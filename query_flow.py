"""
Phase 8 — Retrieval / query flow.

Reads ONLY from the SQLite mirror (never Notion live) so listing is fast and
works even when Notion is unreachable. Turns a validated `query` Intent into a
SQL SELECT over the right mirror table, formats a compact Telegram-friendly
result, and always includes the Notion URL for each row so the user can jump in.

Supported query shapes (from the spec's test list):
- "what did I complete today"            -> ledger, date = today
- "show me all unresolved doubts"        -> doubts, status = Unresolved
- "which chapters are due for revision"  -> revision, next_execution_date <= today
- "physics doubts", "kinematics doubts"  -> doubts filtered by subject/keyword
- generic keyword search across the title + free-text columns.

Dates are compared in the user's timezone (via session_context.local_today_iso).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Optional

import session_context
import sync
from config import notion_schema

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = PROJECT_ROOT / "sqlite_mirror.db"

DEFAULT_LIMIT = 10

# Title column per DB (for display + keyword search).
TITLE_COL = {
    "ledger": "task",
    "doubts": "core_concept",
    "revision": "chapter_module",
}

# Extra free-text columns to include in a keyword search, per DB.
KEYWORD_COLS = {
    "ledger": ["task", "doubts", "key_points_notes", "exercise_type"],
    "doubts": ["core_concept", "concept_deficit_failure_reason", "failure_type"],
    "revision": ["chapter_module"],
}

# Columns shown in the one-line summary for each row, per DB (in order).
SUMMARY_COLS = {
    "ledger": ["date", "subject", "exercise_type", "questions_correct", "questions_attempted"],
    "doubts": ["status", "failure_type"],
    "revision": ["status", "mastery", "next_execution_date", "subject"],
}


class QueryError(RuntimeError):
    pass


def _connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _col_exists(conn: sqlite3.Connection, table: str, col: str) -> bool:
    rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    return any(r["name"] == col for r in rows)


def run_query(intent: Any, *, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    """Execute a query Intent against the mirror.

    Returns {"db_key", "count", "rows": [ {title, url, summary, page_id} ]}.
    """
    db_key = getattr(intent, "database", None)
    if db_key not in sync.DB_TABLES:
        # Infer from filters/keyword when the parser didn't set a database.
        db_key = _infer_db(intent)
    table = sync.DB_TABLES[db_key]
    filters = intent.filters

    where: list[str] = ["archived = 0"]
    params: list[Any] = []

    with _connect(db_path) as conn:
        # date filter -> today (ledger uses `date`, revision "due" uses
        # next_execution_date handled separately below).
        date_val = getattr(filters, "date", None)
        if date_val and db_key == "ledger" and _col_exists(conn, table, "date"):
            iso = session_context.local_today_iso() if str(date_val).lower() == "today" else str(date_val)[:10]
            where.append('date LIKE ?')
            params.append(f"{iso}%")

        subject = getattr(filters, "subject", None)
        if subject and _col_exists(conn, table, "subject"):
            where.append("UPPER(subject) = ?")
            params.append(str(subject).upper())

        status = getattr(filters, "status", None)
        if status and _col_exists(conn, table, "status"):
            where.append("LOWER(status) = ?")
            params.append(str(status).lower())

        # "due for revision" -> next_execution_date <= today (revision only).
        if db_key == "revision" and _is_due_query(intent) and _col_exists(conn, table, "next_execution_date"):
            where.append("(next_execution_date IS NOT NULL AND substr(next_execution_date,1,10) <= ?)")
            params.append(session_context.local_today_iso())

        keyword = getattr(filters, "keyword", None) or _keyword_from_chapter(intent)
        # Due-signal words drive the date filter above, not a literal text search.
        if keyword and keyword.lower() in _DUE_WORDS:
            keyword = None
        if keyword:
            cols = [c for c in KEYWORD_COLS[db_key] if _col_exists(conn, table, c)]
            if cols:
                ors = " OR ".join(f'{c} LIKE ?' for c in cols)
                where.append(f"({ors})")
                params.extend([f"%{keyword}%"] * len(cols))

        sort_col = "date" if (db_key == "ledger" and _col_exists(conn, table, "date")) else "last_edited_time"
        sql = (
            f'SELECT * FROM "{table}" WHERE {" AND ".join(where)} '
            f'ORDER BY {sort_col} DESC LIMIT ?'
        )
        params.append(DEFAULT_LIMIT)
        rows = conn.execute(sql, params).fetchall()

    result_rows = [_row_summary(db_key, dict(r)) for r in rows]
    return {"db_key": db_key, "count": len(result_rows), "rows": result_rows}


def _infer_db(intent: Any) -> str:
    text = " ".join(
        str(getattr(intent.filters, k, "") or "")
        for k in ("keyword", "status", "chapter")
    ).lower()
    if "doubt" in text:
        return "doubts"
    if "revis" in text:
        return "revision"
    return "ledger"


_DUE_WORDS = {"due", "overdue", "pending", "due for revision"}


def _is_due_query(intent: Any) -> bool:
    kw = (getattr(intent.filters, "keyword", None) or "").lower()
    if any(w in kw for w in _DUE_WORDS):
        return True
    # An explicit search term (keyword or chapter) means the user is looking
    # something up by name, not asking "what's due" — don't force the date filter.
    if kw or _keyword_from_chapter(intent):
        return False
    # A bare "revision" query with no keyword/status is treated as "what's due".
    return getattr(intent.filters, "status", None) is None


def _keyword_from_chapter(intent: Any) -> Optional[str]:
    """A `chapter` filter on a query behaves as a keyword search term."""
    return getattr(intent.filters, "chapter", None)


def _row_summary(db_key: str, row: dict[str, Any]) -> dict[str, Any]:
    schema = notion_schema.PROPERTIES_BY_DB[db_key]
    title = row.get(TITLE_COL[db_key]) or "(untitled)"
    bits: list[str] = []
    for col in SUMMARY_COLS[db_key]:
        val = row.get(col)
        if val in (None, ""):
            continue
        label = schema.get(col, {}).get("notion_name", col)
        bits.append(f"{label}={val}")
    return {
        "title": str(title).strip(),
        "url": row.get("notion_url"),
        "summary": " | ".join(bits),
        "page_id": row.get("notion_page_id"),
    }


def format_result(result: dict[str, Any]) -> str:
    """Render a query result as a compact Telegram message (Markdown)."""
    db_title = notion_schema.DATABASES[result["db_key"]]["title"]
    if result["count"] == 0:
        return f"No matching entries in *{db_title}*."
    lines = [f"*{db_title}* — {result['count']} result(s):", ""]
    for i, row in enumerate(result["rows"], 1):
        head = f"{i}. {row['title']}"
        if row["url"]:
            head = f"{i}. [{row['title']}]({row['url']})"
        lines.append(head)
        if row["summary"]:
            lines.append(f"   {row['summary']}")
    return "\n".join(lines)
