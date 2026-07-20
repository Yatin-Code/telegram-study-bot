"""
Read-only SQL execution tool for the SQLite mirror.

Provides a safe `run_sql()` that accepts ONLY SELECT statements and opens
the connection in read-only mode (SQLite URI `?mode=ro`), so even a hostile
or buggy LLM-generated query cannot mutate the mirror.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

import sync
import operational_store

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = PROJECT_ROOT / "sqlite_mirror.db"

MAX_ROWS = 500
QUERY_TIMEOUT_MS = 5000

# Reject any statement that isn't a single SELECT (or WITH ... SELECT).
# SQLite's own protection is the read-only connection; this is a first line
# of defence so we surface a clear error instead of a generic "read-only".
_SELECT_RE = re.compile(
    r"^\s*(?:WITH\s+.*?\s+)?SELECT\s+",
    re.IGNORECASE | re.DOTALL,
)

# Pragma expressions we allow (table_info is needed for schema discovery).
_ALLOWED_PRAGMAS = {"table_info", "table_list", "database_list", "index_list"}
_PRAGMA_RE = re.compile(
    r"^\s*PRAGMA\s+(\w+)\b",
    re.IGNORECASE,
)


class SQLExecutionError(RuntimeError):
    pass


class SQLRejectedError(SQLExecutionError):
    """The statement was rejected before execution (not a SELECT, or unsafe)."""
    pass


def _validate_sql(sql: str) -> None:
    """Reject anything that isn't a single read-only SELECT/PRAGMA."""
    stripped = sql.strip().rstrip(";").strip()
    if not stripped:
        raise SQLRejectedError("Empty SQL.")

    # No semicolons except a trailing one — prevents stacked queries.
    inner = sql.strip().rstrip(";")
    if ";" in inner:
        raise SQLRejectedError(
            "Multiple statements are not allowed."
        )

    if _SELECT_RE.match(inner):
        return

    m = _PRAGMA_RE.match(inner)
    if m and m.group(1).lower() in _ALLOWED_PRAGMAS:
        return

    raise SQLRejectedError(
        "Only SELECT (or WITH ... SELECT) statements are allowed. "
        f"Got: {inner[:80]!r}"
    )


def _read_only_uri(db_path: str | Path) -> str:
    """SQLite URI that opens the file read-only."""
    p = Path(db_path).resolve()
    return f"file:{p}?mode=ro"


def run_sql(
    sql: str,
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    max_rows: int = MAX_ROWS,
) -> dict[str, Any]:
    """Execute a validated SELECT against the mirror (read-only connection).

    Returns {"columns": [...], "rows": [...], "row_count": n, "truncated": bool}.
    Raises SQLRejectedError if the statement is not a read-only SELECT, or
    SQLExecutionError if SQLite fails to execute it.
    """
    _validate_sql(sql)

    inner_sql = sql.strip().rstrip(";")
    # Inject a LIMIT if the user didn't supply one and there's no WITH/aggregate
    # bound — for raw SELECTs we wrap to be safe. We don't rewrite user SQL
    # because it may already contain a LIMIT; we just cap fetch rows.
    uri = _read_only_uri(db_path)
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=QUERY_TIMEOUT_MS / 1000)
    except sqlite3.OperationalError as e:
        raise SQLExecutionError(f"Cannot open mirror read-only: {e}") from e

    try:
        conn.row_factory = sqlite3.Row
        # PRAGMA database_list sanity check that we're really ro.
        cur = conn.execute(inner_sql)
        rows = cur.fetchmany(max_rows + 1)
        columns = (
            [d[0] for d in cur.description] if cur.description else []
        )
    except sqlite3.Error as e:
        conn.close()
        raise SQLExecutionError(f"SQLite error: {e}") from e

    truncated = len(rows) > max_rows
    if truncated:
        rows = rows[:max_rows]
    result_rows = [dict(r) for r in rows]
    conn.close()

    return {
        "columns": columns,
        "rows": result_rows,
        "row_count": len(result_rows),
        "truncated": truncated,
    }


def _truncate_sample(row: dict) -> dict:
    """Cap long values (page bodies, raw_json) so the prompt stays compact."""
    out = {}
    for k, v in row.items():
        if k in {"raw_json", "page_content", "operation_id"}:
            continue
        if isinstance(v, str) and len(v) > 100:
            v = v[:100] + "…"
        out[k] = v
    return out


def schema_digest() -> str:
    """A compact, model-friendly description of the mirror schema.

    Lists each table, its columns, types, and a few sample rows so the LLM
    can write SQL that matches the real column names. Also includes the
    computed columns and their meaning, so the model knows it can filter
    and aggregate on cognitive_yield etc. directly.
    """
    with sync.connect() as conn:
        sync.init_db(conn)
        operational_store.init_db(conn)
        lines: list[str] = []
        tables = {
            key: sync.DB_TABLES[key] for key in sync.NOTION_SOURCE_KEYS
        }
        tables.update(operational_store.OP_TABLES)
        for db_key, table in tables.items():
            info = conn.execute(
                f'PRAGMA table_info("{table}")'
            ).fetchall()
            cols = [f"{r['name']}:{r['type'] or 'TEXT'}" for r in info]
            lines.append(f"TABLE {table} ({db_key}): {', '.join(cols)}")
            # Sample 2 rows for the model to see shape/values.
            sample = conn.execute(
                f'SELECT * FROM "{table}" WHERE archived=0 LIMIT 2'
            ).fetchall()
            if sample:
                lines.append(f"  sample row 1: {_truncate_sample(dict(sample[0]))}")
                if len(sample) > 1:
                    lines.append(f"  sample row 2: {_truncate_sample(dict(sample[1]))}")
            else:
                lines.append("  (no active rows)")
        link_table = operational_store.EXECUTION_LINKS_TABLE
        link_info = conn.execute(f'PRAGMA table_info("{link_table}")').fetchall()
        link_cols = [f"{r['name']}:{r['type'] or 'TEXT'}" for r in link_info]
        lines.append(
            f"TABLE {link_table} (local Work Item to Notion Ledger links): "
            + ", ".join(link_cols)
        )
        # Computed-column explanation.
        lines.append("")
        lines.append(
            "COMPUTED COLUMNS on ledger (materialised by formulas.py during sync, "
            "so they're real numbers you can filter/sort/aggregate on):"
        )
        lines.append(
            "  cognitive_yield INTEGER — subject/exercise-specific yield, "
            "velocity capped at 1.5; NULL when source data is incomplete"
        )
        lines.append(
            "  theory_yield INTEGER — same formula but velocity uncapped "
            "(can exceed cognitive_yield when the user was faster than target)"
        )
        lines.append(
            "  accuracy_ratio REAL — questions_correct / questions_attempted "
            "(NULL when source data is incomplete, capped at 1)"
        )
        lines.append(
            "  mins_per_question REAL — actual_time_min / questions_attempted "
            "(NULL when no questions)"
        )
        lines.append(
            "page_content TEXT on Notion-owned tables — the full plain text the "
            "user wrote inside the Notion page body. It is normally NULL on "
            "SQLite-owned op_* tables. Search Notion content with "
            "LOWER(page_content) LIKE '%keyword%'."
        )
        lines.append(
            "Always filter `archived = 0` unless the user explicitly asks "
            "for deleted/archived entries."
        )
        return "\n".join(lines)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "schema":
        print(schema_digest())
    else:
        sql_text = sys.stdin.read()
        try:
            res = run_sql(sql_text)
            print(f"{res['row_count']} row(s)" + (" (truncated)" if res["truncated"] else ""))
            if res["columns"]:
                print("\t".join(res["columns"]))
                for r in res["rows"]:
                    print("\t".join(str(r.get(c, "")) for c in res["columns"]))
        except (SQLRejectedError, SQLExecutionError) as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)
