"""Authoritative SQLite storage for study-planning operational state.

Notion remains authoritative for the user-authored daily plan and the existing
Ledger, Doubts and Revision databases. Everything the bot itself owns lives in
the local tables defined here. The ``op_`` prefix keeps these tables separate
from old Notion mirror tables and makes the ownership boundary explicit.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Iterable

from config import notion_schema


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = PROJECT_ROOT / "sqlite_mirror.db"

OPERATIONAL_KEYS = (
    "work_items",
    "goals",
    "exams",
    "exam_questions",
    "doubt_attempts",
    "timetable",
)
OP_TABLES = {key: f"op_{key}" for key in OPERATIONAL_KEYS}
SCHEMA_VERSION = 1
EXECUTION_LINKS_TABLE = "op_execution_links"

_SQL_TYPES = {
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

_RESERVED = {
    "id": "TEXT PRIMARY KEY",
    # Kept as a compatibility alias for relation and query code. For local
    # records it equals id; it is not a remote Notion page identifier.
    "notion_page_id": "TEXT NOT NULL UNIQUE",
    "notion_url": "TEXT",
    "created_time": "TEXT NOT NULL",
    "last_edited_time": "TEXT NOT NULL",
    "last_synced_at": "TEXT",
    "archived": "INTEGER NOT NULL DEFAULT 0",
    "raw_json": "TEXT NOT NULL DEFAULT '{}'",
    "page_content": "TEXT",
}


class OperationalStoreError(RuntimeError):
    pass


def connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def table_for(db_key: str) -> str:
    try:
        return OP_TABLES[db_key]
    except KeyError as exc:
        raise OperationalStoreError(f"{db_key!r} is not SQLite-owned") from exc


def is_operational(db_key: str) -> bool:
    return db_key in OP_TABLES


def _quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _table_sql(db_key: str) -> str:
    columns = [f"{_quote(name)} {kind}" for name, kind in _RESERVED.items()]
    for name, definition in notion_schema.PROPERTIES_BY_DB[db_key].items():
        if name in _RESERVED:
            continue
        columns.append(
            f"{_quote(name)} {_SQL_TYPES.get(definition['type'], 'TEXT')}"
        )
    return (
        f"CREATE TABLE IF NOT EXISTS {_quote(table_for(db_key))} ("
        + ", ".join(columns)
        + ")"
    )


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS operational_schema_meta (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            schema_version INTEGER NOT NULL,
            migrated_at TEXT NOT NULL
        )
    """)
    for db_key in OPERATIONAL_KEYS:
        table = table_for(db_key)
        conn.execute(_table_sql(db_key))
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{table}_active ON {_quote(table)}(archived)"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{table}_edited ON {_quote(table)}(last_edited_time)"
        )
        if "operation_id" in notion_schema.PROPERTIES_BY_DB[db_key]:
            conn.execute(
                f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{table}_operation_id "
                f"ON {_quote(table)}(operation_id) WHERE operation_id IS NOT NULL"
            )
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {EXECUTION_LINKS_TABLE} (
            work_item_id TEXT NOT NULL,
            ledger_page_id TEXT NOT NULL UNIQUE,
            linked_at TEXT NOT NULL,
            PRIMARY KEY(work_item_id, ledger_page_id)
        )
    """)

    current = conn.execute(
        "SELECT schema_version FROM operational_schema_meta WHERE singleton=1"
    ).fetchone()
    if current is None:
        _import_legacy_notion_rows(conn)
        conn.execute(
            "INSERT INTO operational_schema_meta(singleton, schema_version, migrated_at) "
            "VALUES (1, ?, ?)",
            (SCHEMA_VERSION, _utc_now()),
        )
    elif int(current["schema_version"]) > SCHEMA_VERSION:
        raise OperationalStoreError(
            "database schema is newer than this application build"
        )
    conn.commit()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row["name"])
        for row in conn.execute(f"PRAGMA table_info({_quote(table)})").fetchall()
    }


def _import_legacy_notion_rows(conn: sqlite3.Connection) -> None:
    """Import operational records from old Notion mirror tables once.

    Empty setup databases therefore cost nothing, while any real records made
    during testing survive the ownership migration.
    """
    now = _utc_now()
    for db_key in OPERATIONAL_KEYS:
        legacy = db_key
        destination = table_for(db_key)
        if not _table_exists(conn, legacy):
            continue
        source_columns = _columns(conn, legacy)
        destination_columns = _columns(conn, destination)
        shared = sorted((source_columns & destination_columns) - {"id"})
        if "notion_page_id" not in shared:
            continue
        target_columns = ["id", *shared]
        expressions = ["notion_page_id"]
        params: list[str] = []
        for name in shared:
            if name in ("created_time", "last_edited_time"):
                expressions.append(f"COALESCE({_quote(name)}, ?)")
                params.append(now)
            elif name == "raw_json":
                expressions.append(f"COALESCE({_quote(name)}, '{{}}')")
            else:
                expressions.append(_quote(name))
        # Tolerate partial legacy fixtures without timestamp columns.
        for timestamp in ("created_time", "last_edited_time"):
            if timestamp not in shared:
                target_columns.append(timestamp)
                expressions.append("?")
                params.append(now)
        conn.execute(
            f"INSERT OR IGNORE INTO {_quote(destination)} "
            f"({', '.join(_quote(name) for name in target_columns)}) "
            f"SELECT {', '.join(expressions)} FROM {_quote(legacy)} "
            "WHERE notion_page_id IS NOT NULL",
            tuple(params),
        )


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _value(value: Any) -> Any:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return value


def create(
    db_key: str,
    properties: dict[str, Any],
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    table = table_for(db_key)
    props = dict(properties)
    operation_id = str(props.get("operation_id") or uuid.uuid4().hex)
    props["operation_id"] = operation_id
    with connect(db_path) as conn:
        init_db(conn)
        existing = conn.execute(
            f"SELECT * FROM {_quote(table)} WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if existing is not None:
            return _result(dict(existing))
        record_id = uuid.uuid4().hex
        now = _utc_now()
        allowed = _columns(conn, table) - set(_RESERVED)
        clean = {key: _value(value) for key, value in props.items() if key in allowed}
        row = {
            "id": record_id,
            "notion_page_id": record_id,
            "created_time": now,
            "last_edited_time": now,
            "last_synced_at": None,
            "archived": 0,
            "raw_json": "{}",
            **clean,
        }
        names = list(row)
        conn.execute(
            f"INSERT INTO {_quote(table)} "
            f"({', '.join(_quote(name) for name in names)}) "
            f"VALUES ({', '.join('?' for _ in names)})",
            tuple(row[name] for name in names),
        )
        conn.commit()
        stored = conn.execute(
            f"SELECT * FROM {_quote(table)} WHERE id=?", (record_id,)
        ).fetchone()
    return _result(dict(stored))


def update(
    db_key: str,
    record_id: str,
    properties: dict[str, Any],
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    table = table_for(db_key)
    with connect(db_path) as conn:
        init_db(conn)
        allowed = _columns(conn, table) - set(_RESERVED)
        clean = {
            key: _value(value)
            for key, value in properties.items()
            if key in allowed
        }
        clean["last_edited_time"] = _utc_now()
        assignments = ", ".join(f"{_quote(name)}=?" for name in clean)
        cur = conn.execute(
            f"UPDATE {_quote(table)} SET {assignments} "
            "WHERE id=? OR notion_page_id=?",
            (*clean.values(), str(record_id), str(record_id)),
        )
        if cur.rowcount != 1:
            raise OperationalStoreError(f"no active {db_key} record matches {record_id!r}")
        conn.commit()
        row = conn.execute(
            f"SELECT * FROM {_quote(table)} WHERE id=? OR notion_page_id=?",
            (str(record_id), str(record_id)),
        ).fetchone()
    return _result(dict(row))


def rows(
    db_key: str,
    where: str = "1=1",
    params: Iterable[Any] = (),
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    table = table_for(db_key)
    with connect(db_path) as conn:
        init_db(conn)
        result = conn.execute(
            f"SELECT * FROM {_quote(table)} WHERE {where}", tuple(params)
        ).fetchall()
    return [dict(row) for row in result]


def _result(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, "url": row.get("notion_url")}


def health(db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    with connect(db_path) as conn:
        init_db(conn)
        integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
        version = conn.execute(
            "SELECT schema_version FROM operational_schema_meta WHERE singleton=1"
        ).fetchone()[0]
        counts = {
            key: conn.execute(
                f"SELECT COUNT(*) FROM {_quote(table)} WHERE archived=0"
            ).fetchone()[0]
            for key, table in OP_TABLES.items()
        }
    return {"integrity": integrity, "schema_version": version, "counts": counts}


def link_execution(
    work_item_id: str,
    ledger_page_id: str,
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> None:
    with connect(db_path) as conn:
        init_db(conn)
        work = conn.execute(
            f"SELECT 1 FROM {_quote(table_for('work_items'))} "
            "WHERE (id=? OR notion_page_id=?) AND archived=0",
            (str(work_item_id), str(work_item_id)),
        ).fetchone()
        if work is None:
            raise OperationalStoreError(f"unknown work item {work_item_id!r}")
        conn.execute(
            f"INSERT OR IGNORE INTO {EXECUTION_LINKS_TABLE} "
            "(work_item_id, ledger_page_id, linked_at) VALUES (?, ?, ?)",
            (str(work_item_id), str(ledger_page_id), _utc_now()),
        )
        conn.commit()


def execution_links(
    work_item_id: str,
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> list[str]:
    with connect(db_path) as conn:
        init_db(conn)
        rows = conn.execute(
            f"SELECT ledger_page_id FROM {EXECUTION_LINKS_TABLE} "
            "WHERE work_item_id=? ORDER BY linked_at",
            (str(work_item_id),),
        ).fetchall()
    return [str(row["ledger_page_id"]) for row in rows]
