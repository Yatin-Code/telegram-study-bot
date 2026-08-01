"""
SQLite mirror for Notion-owned databases (ledger, doubts, revision).

Notion is authoritative only for Ledger, Doubts and Revision.
Bot-owned operational state (including daily plan) uses operational_store.py.
Queries/listing flows should read from SQLite, never hit Notion live.

Usage:
    python sync.py --once
    python sync.py --loop --interval-seconds 240
    python sync.py --counts

Bot write path (Phase 7) should call `sync_once(db_keys=(...))` immediately
after a successful Notion write so the write is queryable without waiting for
the scheduled loop.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import math
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

import formulas
from config import notion_schema
from config.ownership import NOTION_OWNED_KEYS, SQL_OWNED_KEYS
from notion_client_wrapper import page_plain_text, parse_page, query_database_iter


logger = logging.getLogger(__name__)


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = PROJECT_ROOT / "sqlite_mirror.db"
SYNC_META_TABLE = "sync_meta"

# One process-wide, thread-safe lock serialises every mirror sync.  Sync is
# initiated by async handlers, synchronous write helpers, and the background
# scheduler, so an asyncio.Lock would leave two of those paths unprotected.
# RLock keeps the public sync_once() safe even when a locked wrapper calls it.
_sync_lock = threading.RLock()

# SQLite-owned domains (goals, work_items, exams, exam_questions, doubt_attempts,
# timetable, daily_plan) live ONLY in op_* tables; Notion never mirrors them.
# Restricting DB_TABLES to NOTION_OWNED_KEYS prevents sync.init_db from creating
# empty bare mirror tables for SQLite-owned keys, which used to confuse the agent
# into querying "goals" instead of "op_goals".
DB_TABLES: dict[str, str] = {key: key for key in NOTION_OWNED_KEYS}
NOTION_SOURCE_KEYS = NOTION_OWNED_KEYS

# Legacy bare mirror tables for SQLite-owned domains that older versions of this
# bot created as Notion mirrors. These were emptied once the operational data
# moved to op_* tables (see operational_store._import_legacy_notion_rows).
# init_db() now drops them when they are empty so the agent cannot be confused
# into querying the wrong table.
_LEGACY_BARE_SQL_KEYS: tuple[str, ...] = SQL_OWNED_KEYS


SQLITE_TYPE_BY_NOTION_TYPE = {
    "title": "TEXT",
    "rich_text": "TEXT",
    "number": "REAL",
    "select": "TEXT",
    "status": "TEXT",
    "date": "TEXT",
    "checkbox": "INTEGER",
    "relation": "TEXT",
    "formula": "TEXT",
    "rollup": "TEXT",
    "unique_id": "TEXT",
    "people": "TEXT",
}


RESERVED_COLUMNS = {
    "notion_page_id": "TEXT PRIMARY KEY",
    "notion_url": "TEXT",
    "created_time": "TEXT",
    "last_edited_time": "TEXT",
    "last_synced_at": "TEXT NOT NULL",
    "archived": "INTEGER NOT NULL DEFAULT 0",
    "raw_json": "TEXT NOT NULL",
    "page_content": "TEXT",
}

# Formula columns materialised locally from formulas.py during sync, so they're
# real queryable columns (not just Notion's opaque computed values stored as
# text). The key is the human column name; the value is the SQLite type. Only
# ledger has these — doubts/revision formula columns aren't replicated.
COMPUTED_COLUMNS: dict[str, dict[str, str]] = {
    "ledger": {
        "cognitive_yield": "INTEGER",
        "theory_yield": "INTEGER",
        "accuracy_ratio": "REAL",
        "mins_per_question": "REAL",
    },
}


def connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create mirror tables if they don't exist, and migrate computed columns."""
    for db_key, table in DB_TABLES.items():
        conn.execute(_create_table_sql(db_key, table))
        _migrate_computed_columns(conn, db_key, table)
        _ensure_column(conn, table, "page_content", "TEXT")
        computed_types = COMPUTED_COLUMNS.get(db_key, {})
        for human_name, prop_def in notion_schema.PROPERTIES_BY_DB[db_key].items():
            sql_type = computed_types.get(
                human_name,
                SQLITE_TYPE_BY_NOTION_TYPE.get(prop_def["type"], "TEXT"),
            )
            _ensure_column(conn, table, human_name, sql_type)
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{table}_last_edited_time "
            f"ON {table}(last_edited_time)"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{table}_archived "
            f"ON {table}(archived)"
        )

    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SYNC_META_TABLE} (
            db_key TEXT PRIMARY KEY,
            last_started_at TEXT,
            last_completed_at TEXT,
            last_row_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT
        )
        """
    )

    # Drop legacy bare mirror tables for SQLite-owned domains when they are
    # empty, or when every row has already been copied into the op_* table by
    # operational_store._import_legacy_notion_rows. Rows that have NOT been
    # migrated are left in place so the import can still pick them up — this
    # drop can never lose data.
    for legacy_key in _LEGACY_BARE_SQL_KEYS:
        if not _table_exists(conn, legacy_key):
            continue
        try:
            n = conn.execute(
                f'SELECT COUNT(*) FROM {_quote_ident(legacy_key)}'
            ).fetchone()[0]
        except Exception:
            continue
        if n == 0:
            conn.execute(f'DROP TABLE {_quote_ident(legacy_key)}')
            continue
        op_table = f"op_{legacy_key}"
        if not _table_exists(conn, op_table):
            continue
        op_cols = {str(r[1]) for r in conn.execute(
            f'PRAGMA table_info({_quote_ident(op_table)})'
        ).fetchall()}
        if "notion_page_id" not in op_cols:
            continue
        try:
            unmigrated = conn.execute(
                f'SELECT COUNT(*) FROM {_quote_ident(legacy_key)} l '
                f"WHERE l.notion_page_id IS NOT NULL AND NOT EXISTS ("
                f'SELECT 1 FROM {_quote_ident(op_table)} o '
                f"WHERE o.notion_page_id = l.notion_page_id)"
            ).fetchone()[0]
        except Exception:
            continue
        if unmigrated == 0:
            conn.execute(f'DROP TABLE {_quote_ident(legacy_key)}')

    conn.commit()


def _create_table_sql(db_key: str, table: str) -> str:
    schema = notion_schema.PROPERTIES_BY_DB[db_key]
    computed_types = COMPUTED_COLUMNS.get(db_key, {})
    columns: list[str] = []
    for name, sql_type in RESERVED_COLUMNS.items():
        columns.append(f"{_quote_ident(name)} {sql_type}")
    for human_name, prop_def in schema.items():
        if human_name in computed_types:
            sql_type = computed_types[human_name]
        else:
            sql_type = SQLITE_TYPE_BY_NOTION_TYPE.get(prop_def["type"], "TEXT")
        columns.append(f"{_quote_ident(human_name)} {sql_type}")
    cols_sql = ",\n            ".join(columns)
    return f"""
        CREATE TABLE IF NOT EXISTS {_quote_ident(table)} (
            {cols_sql}
        )
    """


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def _ensure_column(
    conn: sqlite3.Connection, table: str, column: str, sql_type: str
) -> None:
    """ALTER TABLE ADD COLUMN if missing (CREATE IF NOT EXISTS won't add it)."""
    info = conn.execute(f"PRAGMA table_info({_quote_ident(table)})").fetchall()
    if column not in {r["name"] for r in info}:
        conn.execute(
            f"ALTER TABLE {_quote_ident(table)} "
            f"ADD COLUMN {_quote_ident(column)} {sql_type}"
        )


def _migrate_computed_columns(
    conn: sqlite3.Connection, db_key: str, table: str
) -> None:
    """Recreate a table if its computed columns have the wrong type.

    SQLite's CREATE TABLE IF NOT EXISTS won't update column types on an
    existing table. When the schema changes (e.g. TEXT -> REAL for computed
    columns), we rebuild the table so the new types take effect. Data is
    preserved by copying from the old table; a re-sync from Notion fills
    the computed values correctly.
    """
    computed = COMPUTED_COLUMNS.get(db_key)
    if not computed:
        return
    info = conn.execute(f'PRAGMA table_info({_quote_ident(table)})').fetchall()
    col_types = {r["name"]: r["type"] for r in info}
    needs_rebuild = any(
        col_types.get(col) != want_type
        for col, want_type in computed.items()
        if col in col_types
    )
    if not needs_rebuild:
        return
    tmp = f"_migration_{table}"
    conn.execute(_create_table_sql(db_key, tmp))
    # Copy over the non-computed columns we can carry; computed columns get
    # re-materialised on the next sync, so we leave them NULL in the copy.
    shared = [c for c in col_types if c in {r["name"] for r in conn.execute(f'PRAGMA table_info({_quote_ident(tmp)})').fetchall()}]
    col_list = ", ".join(_quote_ident(c) for c in shared)
    conn.execute(f'INSERT INTO {_quote_ident(tmp)} ({col_list}) SELECT {col_list} FROM {_quote_ident(table)}')
    conn.execute(f'DROP TABLE {_quote_ident(table)}')
    conn.execute(f'ALTER TABLE {_quote_ident(tmp)} RENAME TO {_quote_ident(table)}')


def sync_once(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    db_keys: Sequence[str] | None = None,
) -> dict[str, int]:
    """Sync selected Notion DBs into SQLite. Returns {db_key: upserted_count}."""
    with _sync_lock:
        if db_keys is None:
            from config import settings
            configured = set(settings.configured_db_keys())
            db_keys = tuple(key for key in NOTION_SOURCE_KEYS if key in configured)
        else:
            db_keys = tuple(key for key in db_keys if key in NOTION_SOURCE_KEYS)
        counts: dict[str, int] = {}
        for db_key in db_keys:
            try:
                # Keep each source database in an independent transaction.  If
                # a fetch or upsert fails, rollback its partial mirror changes
                # before recording the error through a brand-new connection.
                with connect(db_path) as conn:
                    init_db(conn)
                    counts[db_key] = sync_database(conn, db_key)
                    conn.commit()
            except Exception as exc:
                _persist_sync_error(db_path, db_key, exc)
                raise
        return counts


def sync_once_locked_sync(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    db_keys: Sequence[str] | None = None,
) -> dict[str, int]:
    """Synchronous counterpart for write hooks and the scheduler.

    sync_once() already owns the re-entrant lock; this explicit entry point
    makes it clear that callers running outside an event loop are protected.
    """
    with _sync_lock:
        return sync_once(db_path=db_path, db_keys=db_keys)


async def sync_once_locked(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    db_keys: Sequence[str] | None = None,
) -> dict[str, int]:
    """Run one lock-protected sync without blocking the Telegram event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, lambda: sync_once_locked_sync(db_path=db_path, db_keys=db_keys)
    )


def sync_database(conn: sqlite3.Connection, db_key: str) -> int:
    if db_key not in DB_TABLES:
        raise ValueError(f"Unknown db_key: {db_key!r}")

    started_at = _utc_now_iso()
    _record_sync_start(conn, db_key, started_at)

    table = DB_TABLES[db_key]
    count = 0
    seen_page_ids: set[str] = set()
    try:
        for page in query_database_iter(db_key, page_size=100):
            upsert_page(conn, db_key, page)
            count += 1
            pid = page.get("id")
            if pid:
                seen_page_ids.add(pid)
        # Notion's /query endpoints (both /databases and /data_sources) refuse
        # to return archived pages — `is_archived=true` is accepted but yields
        # an empty result set. So the only way to detect "archived in Notion
        # since last sync" is a diff: any mirror row currently marked
        # archived=0 that didn't appear in this full successful fetch has
        # been archived (or hard-deleted) in Notion. Mark it archived=1 so
        # the read path (query_flow, resolve_relation) stops surfacing it.
        # This only runs after a fully successful pagination — a mid-sync
        # error raises and skips this block, so partial fetches can't cause
        # false-positive archival marking. An empty successful fetch means
        # nothing is active in Notion anymore, so every active mirror row is
        # marked archived (handled by the no-`NOT IN` branch below).
        if seen_page_ids:
            placeholders = ", ".join("?" for _ in seen_page_ids)
            conn.execute(
                f'UPDATE {_quote_ident(table)} SET archived = 1 '
                f"WHERE archived = 0 AND notion_page_id NOT IN ({placeholders})",
                tuple(seen_page_ids),
            )
        elif count == 0:
            # Notion returned 200 with an empty result set. This can happen
            # during partial regional outages. Refuse to archive-sweep — a
            # genuinely empty database is indistinguishable from a silent API
            # failure, and wiping the mirror is unrecoverable until next sync.
            logger.warning(
                "sync %s: Notion returned 0 pages — skipping archived sweep "
                "to protect mirror data", db_key
            )
        else:
            # count > 0 but seen_page_ids is empty only if every page lacked
            # an 'id' field — extremely unlikely, but safer to skip sweep.
            logger.warning(
                "sync %s: %d pages fetched but none had an id — skipping archived sweep",
                db_key, count,
            )
        _record_sync_success(conn, db_key, count)
        return count
    except Exception:
        # sync_once() rolls this transaction back, then persists the failure
        # independently so the diagnostic cannot disappear with the rollback.
        raise


def upsert_page(conn: sqlite3.Connection, db_key: str, page_obj: dict[str, Any]) -> None:
    table = DB_TABLES[db_key]
    parsed = parse_page(page_obj)
    now = _utc_now_iso()

    row: dict[str, Any] = {
        "notion_page_id": parsed.get("_page_id"),
        "notion_url": parsed.get("_url"),
        "created_time": parsed.get("_created_time"),
        "last_edited_time": parsed.get("_last_edited_time"),
        "last_synced_at": now,
        "archived": 1 if parsed.get("_archived") else 0,
        "raw_json": json.dumps(page_obj, ensure_ascii=False, sort_keys=True),
    }
    row["page_content"] = _page_content_for(conn, table, row)

    for human_name, prop_def in notion_schema.PROPERTIES_BY_DB[db_key].items():
        row[human_name] = _sqlite_value(parsed.get(human_name), prop_def["type"])

    _materialise_computed_columns(db_key, row)

    cols = list(row.keys())
    placeholders = ", ".join("?" for _ in cols)
    col_sql = ", ".join(_quote_ident(c) for c in cols)
    update_sql = ", ".join(
        f"{_quote_ident(c)} = excluded.{_quote_ident(c)}"
        for c in cols
        if c != "notion_page_id"
    )

    conn.execute(
        f"""
        INSERT INTO {_quote_ident(table)} ({col_sql})
        VALUES ({placeholders})
        ON CONFLICT(notion_page_id) DO UPDATE SET
            {update_sql}
        """,
        [row[c] for c in cols],
    )


def _page_content_for(
    conn: sqlite3.Connection, table: str, row: dict[str, Any]
) -> str | None:
    """Return the page body text, fetching from Notion only when the page changed.

    A body edit bumps the page's last_edited_time, so an unchanged timestamp
    means the stored content is still current — no API call needed. Fetch
    failures are non-fatal: the old content (or NULL) is kept and the next
    sync retries.
    """
    existing = conn.execute(
        f"SELECT last_edited_time, page_content FROM {_quote_ident(table)} "
        f"WHERE notion_page_id = ?",
        (row["notion_page_id"],),
    ).fetchone()
    if (
        existing is not None
        and existing["page_content"] is not None
        and existing["last_edited_time"] == row["last_edited_time"]
    ):
        return existing["page_content"]
    try:
        return page_plain_text(row["notion_page_id"])
    except Exception as exc:
        logger.warning(
            "page_content fetch failed for %s: %r", row["notion_page_id"], exc
        )
        return existing["page_content"] if existing is not None else None


def _materialise_computed_columns(db_key: str, row: dict[str, Any]) -> None:
    """Overwrite Notion's formula values with locally computed ones (formulas.py).

    Only ledger has replicated formulas. The Notion values stored in these
    columns are opaque strings; we replace them with real numbers so the
    columns are usable in SQL (ORDER BY, aggregates, comparisons).
    """
    computed = COMPUTED_COLUMNS.get(db_key)
    if not computed:
        return
    # A missing source field is not an intentional zero-output block. Keep all
    # derived metrics NULL until the entry is complete, so reports can exclude
    # incomplete records instead of treating them as failed work.
    source_values = tuple(_num(row.get(name)) for name in (
        "actual_time_min", "questions_attempted", "questions_correct",
    ))
    if any(value is None for value in source_values):
        for column in computed:
            row[column] = None
        return
    actual_time, attempted, correct = source_values
    row["cognitive_yield"] = formulas.cognitive_yield(
        row.get("subject"),
        row.get("exercise_type"),
        actual_time,
        attempted,
        correct,
    )
    row["theory_yield"] = formulas.theory_yield(
        row.get("subject"),
        row.get("exercise_type"),
        actual_time,
        attempted,
        correct,
    )
    row["accuracy_ratio"] = formulas.accuracy_ratio(
        attempted,
        correct,
    )
    row["mins_per_question"] = formulas.mins_per_question(
        actual_time,
        attempted,
    )


def _num(val: Any) -> float | None:
    """Coerce a stored SQLite value to float, or None."""
    if val is None or val == "":
        return None
    try:
        number = float(val)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def _sqlite_value(value: Any, notion_type: str) -> Any:
    if value is None:
        return None
    if notion_type == "checkbox":
        return 1 if value else 0
    if notion_type == "number":
        return value
    if notion_type in ("title", "rich_text", "select", "status", "date", "unique_id"):
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def row_counts(db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, int]:
    with connect(db_path) as conn:
        init_db(conn)
        return {
            db_key: conn.execute(
                f"SELECT COUNT(*) AS n FROM {_quote_ident(table)}"
            ).fetchone()["n"]
            for db_key, table in DB_TABLES.items()
        }


def table_samples(db_path: str | Path = DEFAULT_DB_PATH, *, limit: int = 3) -> dict[str, list[dict[str, Any]]]:
    with connect(db_path) as conn:
        init_db(conn)
        samples: dict[str, list[dict[str, Any]]] = {}
        for db_key, table in DB_TABLES.items():
            rows = conn.execute(
                f"SELECT * FROM {_quote_ident(table)} ORDER BY last_edited_time DESC LIMIT ?",
                (limit,),
            ).fetchall()
            samples[db_key] = [dict(r) for r in rows]
        return samples


def scheduled_loop(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    interval_seconds: int = 240,
) -> None:
    """Simple forever loop for scheduled sync every 3-5 minutes."""
    while True:
        started = time.monotonic()
        try:
            counts = sync_once_locked_sync(db_path=db_path)
            print(f"[{_utc_now_iso()}] synced: {counts}", flush=True)
        except Exception:
            # A transient Notion/SQLite fault must not kill the only scheduled
            # sync worker.  sync_once has already persisted per-DB diagnostics.
            logger.exception("scheduled sync failed")
        elapsed = time.monotonic() - started
        time.sleep(max(1, interval_seconds - int(elapsed)))


def _record_sync_start(conn: sqlite3.Connection, db_key: str, started_at: str) -> None:
    conn.execute(
        f"""
        INSERT INTO {SYNC_META_TABLE} (db_key, last_started_at, last_error)
        VALUES (?, ?, NULL)
        ON CONFLICT(db_key) DO UPDATE SET
            last_started_at = excluded.last_started_at,
            last_error = NULL
        """,
        (db_key, started_at),
    )


def _record_sync_success(conn: sqlite3.Connection, db_key: str, count: int) -> None:
    conn.execute(
        f"""
        UPDATE {SYNC_META_TABLE}
        SET last_completed_at = ?, last_row_count = ?, last_error = NULL
        WHERE db_key = ?
        """,
        (_utc_now_iso(), count, db_key),
    )


def _record_sync_error(conn: sqlite3.Connection, db_key: str, exc: Exception) -> None:
    conn.execute(
        f"""
        INSERT INTO {SYNC_META_TABLE} (db_key, last_started_at, last_error)
        VALUES (?, ?, ?)
        ON CONFLICT(db_key) DO UPDATE SET
            last_started_at = excluded.last_started_at,
            last_error = excluded.last_error
        """,
        (db_key, _utc_now_iso(), repr(exc)),
    )


def _persist_sync_error(
    db_path: str | Path, db_key: str, exc: Exception
) -> None:
    """Persist a sync failure outside the rolled-back work transaction."""
    try:
        with connect(db_path) as error_conn:
            init_db(error_conn)
            _record_sync_error(error_conn, db_key, exc)
            error_conn.commit()
    except Exception:
        # Never mask the original sync failure.  The logger still gives the
        # operator a clear signal if the mirror is too unhealthy to log it.
        logger.exception("could not persist sync error for %s", db_key)


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync Notion study DBs to SQLite")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--once", action="store_true", help="Run one sync (default if no mode specified)")
    parser.add_argument("--loop", action="store_true", help="Run a scheduled sync loop")
    parser.add_argument("--interval-seconds", type=int, default=240)
    parser.add_argument("--counts", action="store_true", help="Print current SQLite row counts")
    parser.add_argument("--db-key", action="append", choices=tuple(DB_TABLES), help="Limit sync to one DB; repeatable")
    args = parser.parse_args(argv)

    db_path = Path(args.db_path)

    if args.counts:
        print(row_counts(db_path))
        return 0

    if args.loop:
        scheduled_loop(db_path=db_path, interval_seconds=args.interval_seconds)
        return 0

    db_keys = tuple(args.db_key) if args.db_key else tuple(DB_TABLES)
    counts = sync_once(db_path=db_path, db_keys=db_keys)
    print(counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
