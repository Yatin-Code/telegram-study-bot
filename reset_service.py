"""Guarded reset workflow for Telegram, SQLite and Notion page contents.

The strongest invariant in this module is that Notion *databases* are never
mutated.  The only Notion write it can issue is ``archive_page(page_id)`` for
pages returned by configured database queries.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import secrets
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Iterable

import httpx
import notion_client_wrapper as notion
from config import settings as config_settings


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = PROJECT_ROOT / "sqlite_mirror.db"
DEFAULT_SETTINGS_PATH = PROJECT_ROOT / "settings.json"
DEFAULT_BACKUP_ROOT = PROJECT_ROOT / "backups"
CONFIRM_TABLE = "reset_confirmations"
CONFIRM_TTL_MINUTES = 10
SCOPES = ("sqlite", "notion", "context", "everything")

SCOPE_LABELS = {
    "sqlite": "SQLite data",
    "notion": "Notion pages",
    "context": "Conversation context",
    "everything": "Everything",
}

_SENTENCES = {
    "sqlite": (
        "I understand this will permanently erase all SQLite data while "
        "preserving the SQLite file, tables, and schemas. {token}"
    ),
    "notion": (
        "I understand this will archive every page in every configured Notion "
        "database while preserving the databases and their schemas. {token}"
    ),
    "context": (
        "I understand this will permanently erase all bot conversation and "
        "active session context while preserving my study records. {token}"
    ),
    "everything": (
        "I understand this will archive every Notion page and permanently erase "
        "all local bot data while preserving the Notion databases and SQLite schemas. {token}"
    ),
}

# Ephemeral conversational state only.  Study evidence, preferences, jobs,
# settings and onboarding completion deliberately survive a context-only reset.
CONTEXT_TABLES = (
    "chat_context",
    "drafts",
    "pending_clarifications",
    "chat_qa_history",
    "pending_session_debrief",
    "pending_setting_edits",
    "pending_job_edits",
    "active_plan_state",
    "pending_readiness_resolution",
    CONFIRM_TABLE,
)


class ResetError(RuntimeError):
    pass


def _retryable_notion_error(exc: Exception) -> bool:
    status = getattr(exc, "status", None)
    return isinstance(exc, (TimeoutError, ConnectionError, httpx.RequestError)) or (
        isinstance(status, int) and (status == 429 or status >= 500)
    )


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {CONFIRM_TABLE} (
            chat_id TEXT PRIMARY KEY,
            scope TEXT NOT NULL,
            token TEXT NOT NULL,
            sentence TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def create_confirmation(
    chat_id: int | str, scope: str, *, ttl_minutes: int = CONFIRM_TTL_MINUTES,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    if scope not in SCOPES:
        raise ValueError(f"unknown reset scope: {scope!r}")
    token = secrets.token_hex(3).upper()
    now = _utc_now()
    expires = now + dt.timedelta(minutes=ttl_minutes)
    sentence = _SENTENCES[scope].format(token=token)
    with _connect(db_path) as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO {CONFIRM_TABLE} "
            "(chat_id, scope, token, sentence, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (str(chat_id), scope, token, sentence, now.isoformat(), expires.isoformat()),
        )
        conn.commit()
    return {
        "chat_id": str(chat_id), "scope": scope, "token": token,
        "sentence": sentence, "created_at": now.isoformat(),
        "expires_at": expires.isoformat(),
    }


def pending_confirmation(
    chat_id: int | str, *, db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any] | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            f"SELECT * FROM {CONFIRM_TABLE} WHERE chat_id=?", (str(chat_id),)
        ).fetchone()
        if row is None:
            return None
        if str(row["expires_at"]) <= _utc_now().isoformat():
            conn.execute(f"DELETE FROM {CONFIRM_TABLE} WHERE chat_id=?", (str(chat_id),))
            conn.commit()
            return None
    return dict(row)


def cancel_confirmation(
    chat_id: int | str, *, db_path: str | Path = DEFAULT_DB_PATH,
) -> None:
    with _connect(db_path) as conn:
        conn.execute(f"DELETE FROM {CONFIRM_TABLE} WHERE chat_id=?", (str(chat_id),))
        conn.commit()


def consume_confirmation(
    chat_id: int | str, text: str, *, db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any] | None:
    """Atomically consume a still-valid confirmation only on exact equality."""
    now = _utc_now().isoformat()
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            f"SELECT * FROM {CONFIRM_TABLE} WHERE chat_id=?", (str(chat_id),)
        ).fetchone()
        if row is None:
            conn.rollback()
            return None
        if str(row["expires_at"]) <= now:
            conn.execute(f"DELETE FROM {CONFIRM_TABLE} WHERE chat_id=?", (str(chat_id),))
            conn.commit()
            return None
        if text != str(row["sentence"]):
            conn.rollback()
            return None
        conn.execute(f"DELETE FROM {CONFIRM_TABLE} WHERE chat_id=?", (str(chat_id),))
        conn.commit()
    return dict(row)


def backup_state(
    db_path: Path, settings_path: Path, backup_root: Path, label: str,
    *, retain: int = 7,
) -> Path:
    """Create a WAL-consistent SQLite/settings backup before any mutation."""
    backup_root.mkdir(parents=True, exist_ok=True)
    destination_dir = backup_root / label
    destination_dir.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        destination = destination_dir / db_path.name
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.unlink(missing_ok=True)
        try:
            with sqlite3.connect(str(db_path)) as source, sqlite3.connect(str(temporary)) as target:
                if source.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ResetError("source SQLite database failed its integrity check")
                source.backup(target)
                if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ResetError("backup failed SQLite integrity check")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    if settings_path.exists():
        destination = destination_dir / settings_path.name
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        shutil.copy2(settings_path, temporary)
        os.replace(temporary, destination)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", label):
        kept = sorted(
            path for path in backup_root.iterdir()
            if path.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", path.name)
        )
    elif label.startswith("reset-"):
        kept = sorted(
            path for path in backup_root.iterdir()
            if path.is_dir() and path.name.startswith("reset-")
        )
    else:
        kept = sorted(path for path in backup_root.iterdir() if path.is_dir())
    for old in kept[:-max(1, retain)]:
        shutil.rmtree(old, ignore_errors=True)
    return destination_dir


def _application_tables(conn: sqlite3.Connection) -> list[str]:
    return [
        str(row[0]) for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    ]


def _schema_snapshot(conn: sqlite3.Connection) -> list[tuple[str, str, str, str]]:
    return [
        (str(row[0]), str(row[1]), str(row[2]), str(row[3]))
        for row in conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL "
            "ORDER BY type, name"
        ).fetchall()
    ]


def clear_sqlite_data(*, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    """Delete rows transactionally while preserving every table and index."""
    db_path = Path(db_path)
    with sqlite3.connect(str(db_path), timeout=30) as conn:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA secure_delete=ON")
        before_tables = _application_tables(conn)
        before_schema = _schema_snapshot(conn)
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN IMMEDIATE")
        counts: dict[str, int] = {}
        try:
            for table in before_tables:
                counts[table] = int(
                    conn.execute(f"SELECT COUNT(*) FROM {_quote(table)}").fetchone()[0]
                )
                conn.execute(f"DELETE FROM {_quote(table)}")
            sequence_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'"
            ).fetchone()
            if sequence_exists:
                conn.execute("DELETE FROM sqlite_sequence")
            remaining = {
                table: int(
                    conn.execute(f"SELECT COUNT(*) FROM {_quote(table)}").fetchone()[0]
                )
                for table in before_tables
            }
            if any(remaining.values()):
                raise ResetError(
                    "a SQLite trigger recreated rows during reset; transaction rolled back"
                )
            after_tables = _application_tables(conn)
            after_schema = _schema_snapshot(conn)
            if before_tables != after_tables or before_schema != after_schema:
                raise ResetError("SQLite schema changed during row reset")
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ResetError("SQLite failed integrity check during reset")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        # Remove deleted content from free pages and the WAL while keeping the
        # same database path and the exact table/index/trigger/view schema.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        conn.execute("VACUUM")
        if _schema_snapshot(conn) != before_schema:
            raise ResetError("SQLite schema changed while compacting reset data")
        if any(
            int(conn.execute(f"SELECT COUNT(*) FROM {_quote(table)}").fetchone()[0])
            for table in before_tables
        ):
            raise ResetError("SQLite rows reappeared after reset compaction")
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ResetError("SQLite failed integrity check after reset compaction")
    return {
        "tables_preserved": len(before_tables),
        "rows_deleted": sum(counts.values()),
        "rows_by_table": counts,
    }


def clear_context(
    *, db_path: str | Path = DEFAULT_DB_PATH, chat_id: int | str | None = None,
) -> dict[str, Any]:
    """Clear ephemeral interaction state, optionally only for one chat."""
    with sqlite3.connect(str(db_path), timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        existing = set(_application_tables(conn))
        conn.execute("BEGIN IMMEDIATE")
        counts: dict[str, int] = {}
        try:
            for table in CONTEXT_TABLES:
                if table not in existing:
                    continue
                columns = {
                    str(row[1]) for row in conn.execute(
                        f"PRAGMA table_info({_quote(table)})"
                    ).fetchall()
                }
                if chat_id is not None and "chat_id" in columns:
                    cur = conn.execute(
                        f"DELETE FROM {_quote(table)} WHERE CAST(chat_id AS TEXT)=?",
                        (str(chat_id),),
                    )
                else:
                    cur = conn.execute(f"DELETE FROM {_quote(table)}")
                counts[table] = max(0, int(cur.rowcount))
            if "onboarding_state" in existing:
                columns = {
                    str(row[1]) for row in conn.execute(
                        "PRAGMA table_info(onboarding_state)"
                    ).fetchall()
                }
                if {"section", "mode"} <= columns:
                    if chat_id is None:
                        cur = conn.execute(
                            "UPDATE onboarding_state SET section=NULL, mode=NULL"
                        )
                    else:
                        cur = conn.execute(
                            "UPDATE onboarding_state SET section=NULL, mode=NULL "
                            "WHERE CAST(chat_id AS TEXT)=?", (str(chat_id),)
                        )
                    counts["onboarding_state_active_section"] = max(
                        0, int(cur.rowcount)
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {"rows_deleted": sum(counts.values()), "rows_by_table": counts}


def _archive_with_retry(
    page_id: str, archive_page: Callable[[str], Any], *,
    sleep_func: Callable[[float], None], pause_seconds: float, retries: int = 3,
) -> None:
    error: Exception | None = None
    for attempt in range(retries):
        try:
            response = archive_page(page_id)
            if isinstance(response, dict) and response and not (
                response.get("archived") or response.get("in_trash")
            ):
                raise ResetError("Notion did not confirm that the page was archived")
            if pause_seconds:
                sleep_func(pause_seconds)
            return
        except Exception as exc:
            error = exc
            if _retryable_notion_error(exc) and attempt + 1 < retries:
                sleep_func(max(1.0, pause_seconds) * (2 ** attempt))
                continue
            raise
    assert error is not None
    raise error


def _query_with_retry(
    db_key: str, query_pages: Callable[..., Iterable[dict[str, Any]]], *,
    sleep_func: Callable[[float], None], pause_seconds: float, retries: int = 3,
) -> list[dict[str, Any]]:
    error: Exception | None = None
    for attempt in range(retries):
        try:
            pages = list(query_pages(db_key, page_size=100))
            if pause_seconds:
                sleep_func(pause_seconds)
            return pages
        except Exception as exc:
            error = exc
            if not _retryable_notion_error(exc) or attempt + 1 >= retries:
                raise
            sleep_func(max(1.0, pause_seconds) * (2 ** attempt))
    assert error is not None
    raise error


def archive_all_notion_pages(
    *, db_keys: Iterable[str] | None = None,
    query_pages: Callable[..., Iterable[dict[str, Any]]] = notion.query_database_iter,
    archive_page: Callable[[str], Any] = notion.archive_page,
    sleep_func: Callable[[float], None] = time.sleep,
    pause_seconds: float = 0.35,
) -> dict[str, Any]:
    """Archive active pages only; no database mutation endpoint is used."""
    selected = config_settings.configured_db_keys() if db_keys is None else db_keys
    keys = list(dict.fromkeys(selected))
    pages_by_db: dict[str, list[dict[str, Any]]] = {}
    failures: list[dict[str, str]] = []
    if not keys:
        return {
            "complete": False, "configured_databases": 0,
            "databases_scanned": 0, "pages_found": 0,
            "pages_archived": 0, "per_database": {},
            "failures": [{
                "database": "configuration", "page_id": "",
                "error": "no configured Notion databases; refusing to claim the page reset is complete",
            }],
            "databases_preserved": True,
        }

    # Preflight every database before the first archive.  A bad/missing ID
    # therefore cannot cause an avoidable half-reset.
    for db_key in keys:
        try:
            pages_by_db[db_key] = _query_with_retry(
                db_key, query_pages, sleep_func=sleep_func,
                pause_seconds=pause_seconds,
            )
        except Exception as exc:
            failures.append({
                "database": db_key, "page_id": "",
                "error": f"query failed: {type(exc).__name__}: {exc}",
            })
    if failures:
        return {
            "complete": False, "configured_databases": len(keys),
            "databases_scanned": len(pages_by_db), "pages_found": 0,
            "pages_archived": 0, "per_database": {}, "failures": failures,
            "databases_preserved": True,
        }

    per_database: dict[str, dict[str, int]] = {}
    archived_ids_by_db: dict[str, set[str]] = {}
    pages_found = sum(len(pages) for pages in pages_by_db.values())
    pages_archived = 0
    for db_key, pages in pages_by_db.items():
        archived = 0
        archived_ids: set[str] = set()
        for page in pages:
            page_id = str(page.get("id") or "")
            if not page_id:
                failures.append({
                    "database": db_key, "page_id": "",
                    "error": "query returned a page without an id",
                })
                continue
            try:
                _archive_with_retry(
                    page_id, archive_page, sleep_func=sleep_func,
                    pause_seconds=pause_seconds,
                )
                archived += 1
                pages_archived += 1
                archived_ids.add(page_id)
            except Exception as exc:
                failures.append({
                    "database": db_key, "page_id": page_id,
                    "error": f"{type(exc).__name__}: {exc}",
                })
        per_database[db_key] = {"found": len(pages), "archived": archived}
        archived_ids_by_db[db_key] = archived_ids

    # Verify through the same database query endpoint.  This catches a page
    # created concurrently during a long archive and avoids claiming a clean
    # reset while any active page remains.  It still never mutates a database.
    for db_key in keys:
        try:
            remaining = _query_with_retry(
                db_key, query_pages, sleep_func=sleep_func,
                pause_seconds=pause_seconds,
            )
        except Exception as exc:
            failures.append({
                "database": db_key, "page_id": "",
                "error": f"post-archive verification failed: {type(exc).__name__}: {exc}",
            })
            continue
        per_database[db_key]["remaining"] = len(remaining)
        if remaining:
            known_failed = {
                str(failure.get("page_id") or "")
                for failure in failures
                if failure.get("database") == db_key and failure.get("page_id")
            }
            unexpected = [
                page for page in remaining
                if not page.get("id")
                or (
                    str(page.get("id")) not in known_failed
                    and str(page.get("id")) not in archived_ids_by_db.get(db_key, set())
                )
            ]
            if unexpected:
                sample = ", ".join(
                    str(page.get("id") or "missing-id") for page in unexpected[:5]
                )
                failures.append({
                    "database": db_key, "page_id": sample,
                    "error": (
                        f"post-archive verification found {len(unexpected)} "
                        "unexpected active page(s)"
                    ),
                })
    return {
        "complete": not failures and pages_archived == pages_found,
        "configured_databases": len(keys), "databases_scanned": len(keys),
        "pages_found": pages_found, "pages_archived": pages_archived,
        "per_database": per_database, "failures": failures,
        "databases_preserved": True,
    }


def _backup_label() -> str:
    return "reset-" + _utc_now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


def execute(
    scope: str, *, chat_id: int | str | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
    settings_path: str | Path = DEFAULT_SETTINGS_PATH,
    backup_root: str | Path = DEFAULT_BACKUP_ROOT,
    db_keys: Iterable[str] | None = None,
    query_pages: Callable[..., Iterable[dict[str, Any]]] = notion.query_database_iter,
    archive_page: Callable[[str], Any] = notion.archive_page,
    sleep_func: Callable[[float], None] = time.sleep,
    pause_seconds: float = 0.35,
) -> dict[str, Any]:
    """Execute an already-confirmed reset, backing up before any mutation."""
    if scope not in SCOPES:
        raise ValueError(f"unknown reset scope: {scope!r}")
    db_path, settings_path, backup_root = Path(db_path), Path(settings_path), Path(backup_root)
    backup = backup_state(
        db_path, settings_path, backup_root / "resets", _backup_label()
    )
    result: dict[str, Any] = {
        "scope": scope, "backup": str(backup), "complete": False,
        "notion": None, "sqlite": None, "context": None,
        "settings_overrides_cleared": False,
    }

    if scope in ("notion", "everything"):
        notion_result = archive_all_notion_pages(
            db_keys=db_keys, query_pages=query_pages, archive_page=archive_page,
            sleep_func=sleep_func, pause_seconds=pause_seconds,
        )
        result["notion"] = notion_result
        if not notion_result["complete"]:
            result["blocked_after_notion_failure"] = scope == "everything"
            return result

    if scope == "context":
        result["context"] = clear_context(db_path=db_path, chat_id=chat_id)
    elif scope in ("sqlite", "everything"):
        result["sqlite"] = clear_sqlite_data(db_path=db_path)

    if scope == "everything" and settings_path.exists():
        settings_path.unlink()
        result["settings_overrides_cleared"] = True

    result["complete"] = True
    return result


def format_result(result: dict[str, Any]) -> str:
    scope = SCOPE_LABELS.get(str(result.get("scope")), str(result.get("scope")))
    lines = [f"{'✅' if result.get('complete') else '⚠️'} Reset result · {scope}"]
    lines.append(f"Backup: {result.get('backup')}")
    notion_result = result.get("notion")
    if notion_result:
        lines.append(
            f"Notion pages: {notion_result.get('pages_archived', 0)}/"
            f"{notion_result.get('pages_found', 0)} archived across "
            f"{notion_result.get('configured_databases', 0)} configured database(s)."
        )
        lines.append("Notion databases and schemas: preserved.")
        failures = notion_result.get("failures") or []
        if failures:
            lines.append(f"Failures: {len(failures)} (no failure was hidden).")
            for failure in failures[:5]:
                target = failure.get("page_id") or "database query"
                lines.append(
                    f"• {failure.get('database')} · {target}: {failure.get('error')}"
                )
    sqlite_result = result.get("sqlite")
    if sqlite_result:
        lines.append(
            f"SQLite: {sqlite_result.get('rows_deleted', 0)} row(s) erased; "
            f"{sqlite_result.get('tables_preserved', 0)} table(s) and their indexes preserved."
        )
        if result.get("scope") == "sqlite":
            lines.append(
                "Notion-backed mirror rows can return on the next sync because "
                "the Notion pages were intentionally preserved."
            )
    context_result = result.get("context")
    if context_result:
        lines.append(f"Context: {context_result.get('rows_deleted', 0)} ephemeral row(s) erased.")
    if result.get("settings_overrides_cleared"):
        lines.append("Runtime settings overrides cleared; .env and credentials preserved.")
    if result.get("blocked_after_notion_failure"):
        lines.append(
            "Everything stopped before SQLite/settings deletion because the Notion page archive was incomplete."
        )
    elif result.get("complete") and result.get("scope") == "everything":
        lines.append("Onboarding will start again on the next interaction.")
    elif result.get("complete") and result.get("scope") == "notion":
        lines.append("SQLite may show cached mirror rows until the next automatic sync.")
    return "\n".join(lines)
