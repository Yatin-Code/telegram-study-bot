"""Raw tools for the study agent.

The agent gets a tiny number of powerful tools:
  - notion_api(...): any Notion REST call
  - sqlite_query(sql): read-only SQL
  - sqlite_execute(sql): write SQL (writes are gated by the agent loop)
  - get_schema(table?): introspect a table
  - get_context(chat_id): current session context
  - set_context(...): update session context

No guardrails here — the agent loop shows every write to the user for
confirmation before it executes.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Optional

import httpx
import session_context
from config import settings

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "sqlite_mirror.db"
NOTION_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


def _notion_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.notion_token()}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# Public tool: notion_api
# ---------------------------------------------------------------------------

def notion_api(method: str, path: str, body: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Make a raw Notion API call and return the JSON response.

    Examples:
        notion_api("GET", "/databases/abc123")
        notion_api("POST", "/databases/abc123/query", {"filter": {...}})
        notion_api("POST", "/pages", {"parent": {...}, "properties": {...}})
        notion_api("PATCH", "/pages/def456", {"properties": {...}})
    """
    url = NOTION_BASE + (path if path.startswith("/") else "/" + path)
    try:
        with httpx.Client(timeout=60.0) as client:
            resp = client.request(method, url, headers=_notion_headers(), json=body)
        if resp.status_code >= 400:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            return {
                "error": True,
                "status": resp.status_code,
                "message": str(detail)[:500],
            }
        return resp.json() if resp.text else {"status": resp.status_code}
    except Exception as exc:
        logger.exception("notion_api failed: %s %s", method, path)
        return {"error": True, "message": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# Public tool: sqlite_query
# ---------------------------------------------------------------------------

def sqlite_query(sql: str, *, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    """Execute a read-only SQL query against the local SQLite mirror.

    This is intended for SELECT only. INSERT/UPDATE/DELETE should go through
    sqlite_execute (after user confirmation in the agent loop).
    """
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql).fetchall()
            return {
                "rows": [dict(r) for r in rows],
                "row_count": len(rows),
            }
    except Exception as exc:
        logger.exception("sqlite_query failed: %s", sql)
        return {"error": True, "message": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# Public tool: sqlite_execute
# ---------------------------------------------------------------------------

def sqlite_execute(sql: str, *, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    """Execute a write SQL statement against the local SQLite database.

    The caller (agent.py) is responsible for showing the statement to the user
    and getting confirmation before calling this tool. This function itself does
    not guardrail — it just runs the SQL.
    """
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(sql)
            conn.commit()
            return {
                "row_count": cur.rowcount,
                "last_row_id": cur.lastrowid,
                "message": "executed",
            }
    except Exception as exc:
        logger.exception("sqlite_execute failed: %s", sql)
        return {"error": True, "message": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# Public tool: get_schema
# ---------------------------------------------------------------------------

def get_schema(table: Optional[str] = None, *, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    """Return schema information for SQLite tables.

    If `table` is given, return columns and sample rows for that table.
    If `table` is None, return a list of all tables.
    """
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            if table is None:
                rows = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                ).fetchall()
                return {"tables": [r["name"] for r in rows]}

            # Columns
            cols = conn.execute(f"PRAGMA table_info('{table}')").fetchall()
            columns = [
                {"name": r["name"], "type": r["type"], "notnull": r["notnull"], "pk": r["pk"]}
                for r in cols
            ]

            # Sample rows
            sample_rows: list[dict[str, Any]] = []
            try:
                sample = conn.execute(
                    f"SELECT * FROM \"{table}\" LIMIT 3"
                ).fetchall()
                sample_rows = [dict(r) for r in sample]
            except Exception:
                pass

            return {"table": table, "columns": columns, "sample_rows": sample_rows}
    except Exception as exc:
        logger.exception("get_schema failed: %s", table)
        return {"error": True, "message": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# Public tools: get_context / set_context
# ---------------------------------------------------------------------------

def get_context(chat_id: int | str) -> dict[str, Any]:
    """Return the current session context for a chat."""
    ctx = session_context.get_context(chat_id)
    if ctx is None:
        return {"subject": None, "chapter": None, "block": None, "exercise": None}
    return {
        "subject": ctx.get("subject"),
        "chapter": ctx.get("chapter"),
        "block": ctx.get("block"),
        "exercise": ctx.get("exercise"),
    }


def set_context(
    chat_id: int | str,
    subject: Optional[str] = None,
    chapter: Optional[str] = None,
    block: Optional[str] = None,
    exercise: Optional[str] = None,
) -> dict[str, Any]:
    """Update the session context for a chat."""
    return session_context.set_context(chat_id, subject=subject, chapter=chapter, block=block, exercise=exercise)


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

TOOL_SPECS = [
    {
        "name": "notion_api",
        "description": (
            "Call any Notion REST API endpoint. Use it to read pages, query databases, "
            "create/update/delete pages. First call GET /databases/{id} or GET /databases/{id}/query "
            "to discover page structure. POST /pages creates a page. PATCH /pages/{id} updates properties."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "method": {"type": "string", "enum": ["GET", "POST", "PATCH", "DELETE"]},
                "path": {"type": "string", "description": "Notion API path, e.g. /databases/abc123/query or /pages/abc123"},
                "body": {"type": "object", "description": "Optional JSON body for POST/PATCH"},
            },
            "required": ["method", "path"],
        },
    },
    {
        "name": "sqlite_query",
        "description": (
            "Read-only SELECT query against the local SQLite mirror. "
            "Use this to inspect study data: ledger (study sessions), doubts, exams, "
            "work_items (backlog), daily_plan, chat_context (session subject/chapter), "
            "commitments, learner_profiles, etc. "
            "ALWAYS use only column names shown in the SQLite schema block in your prompt — "
            "never invent column names."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "SELECT statement only. No INSERT/UPDATE/DELETE."},
            },
            "required": ["sql"],
        },
    },
    {
        "name": "sqlite_execute",
        "description": (
            "Write SQL: INSERT/UPDATE/DELETE. This ALWAYS triggers a user-confirmation "
            "preview before execution — never refuse to call it. "
            "Use this to create study records, update ledger, create/update doubts, "
            "manage goals, commitments, backlog items, exams, etc. "
            "ALWAYS use only column names from the schema block."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "INSERT/UPDATE/DELETE statement only."},
            },
            "required": ["sql"],
        },
    },
    {
        "name": "get_schema",
        "description": (
            "Inspect the local SQLite mirror schema. Call with no arguments to list all tables. "
            "Call with a table name to get column names, types, and 3 sample rows. "
            "Use this whenever you are unsure about column names or a table exists. "
            "Known important tables: ledger, doubts, exams, work_items, daily_plan, "
            "chat_context, commitments, learner_profiles, active_plan_state."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "table": {"type": "string", "description": "Table name to inspect, or omit to list all tables."},
            },
        },
    },
    {
        "name": "get_context",
        "description": (
            "Get the current study session context: subject, chapter, block, exercise for the user. "
            "Use this to know what the user is currently studying."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "chat_id": {"type": "integer", "description": "Telegram chat ID"},
            },
            "required": ["chat_id"],
        },
    },
    {
        "name": "set_context",
        "description": (
            "Update the current study session context. Call this when the user says they are "
            "studying something, or to set/clear subject, chapter, block, exercise. "
            "All fields optional — omit a field to leave it unchanged, pass null to clear it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "chat_id": {"type": "integer"},
                "subject": {"type": "string"},
                "chapter": {"type": "string"},
                "block": {"type": "string"},
                "exercise": {"type": "string"},
            },
            "required": ["chat_id"],
        },
    },
]


def execute_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Execute a tool by name with the given arguments."""
    if name == "notion_api":
        return notion_api(**arguments)
    if name == "sqlite_query":
        return sqlite_query(**arguments)
    if name == "sqlite_execute":
        return sqlite_execute(**arguments)
    if name == "get_schema":
        return get_schema(**arguments)
    if name == "get_context":
        return get_context(**arguments)
    if name == "set_context":
        return set_context(**arguments)
    return {"error": True, "message": f"Unknown tool: {name}"}
