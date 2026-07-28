"""
User-defined scheduled jobs (/jobs) — the bot DOES things on a schedule.

Fundamentally different from remembering/storing: a job makes the bot act at
a time — either ANSWER a saved question from the study database and send the
result ("every weekday at 21:00 tell me my week's cognitive yield") or send
a fixed reminder text. Jobs are created from natural language (LLM-parsed,
then deterministically validated, then user-confirmed), listed/edited/
paused/deleted from the /jobs inline UI, and fired by a 60-second scan in
bot.py that dedups via reminders.claim so a restart never double-sends.

Schedule kinds: daily · weekdays (Mon-Fri) · weekly (one weekday) · once
(a date; auto-disables after firing). Times are local (USER_TIMEZONE).
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path
from typing import Any

from config import settings

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = PROJECT_ROOT / "sqlite_mirror.db"

JOBS_TABLE = "user_jobs"
EDIT_TABLE = "pending_job_edits"
EDIT_TTL_MINUTES = 5

SCHEDULE_KINDS = ("daily", "weekdays", "weekly", "once")
ACTION_KINDS = ("ask", "message")

MAX_JOBS = 25  # sanity cap for a single-user bot


def _connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {JOBS_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            schedule_kind TEXT NOT NULL,
            run_time TEXT NOT NULL,
            weekday INTEGER,
            run_date TEXT,
            action_kind TEXT NOT NULL,
            action_text TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            last_run TEXT
        )
    """)
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {EDIT_TABLE} (
            chat_id INTEGER PRIMARY KEY,
            job_id INTEGER NOT NULL,
            field TEXT NOT NULL,
            asked_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Validation (LLM output → safe row) and description
# ---------------------------------------------------------------------------

def validate_parsed(parsed: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Turn a parse_job result into safe row data, or (None, error)."""
    kind = str(parsed.get("schedule_kind") or "").strip().lower()
    if kind not in SCHEDULE_KINDS:
        return None, "schedule must be daily, weekdays, weekly or once"
    ok, time_or_err = settings.validate_hhmm(str(parsed.get("time") or ""))
    if not ok:
        return None, f"time: {time_or_err}"
    run_time = time_or_err
    weekday = parsed.get("weekday")
    if kind == "weekly":
        try:
            weekday = int(weekday)
        except (TypeError, ValueError):
            return None, "weekly jobs need a weekday (0=Mon … 6=Sun)"
        if not 0 <= weekday <= 6:
            return None, "weekday must be 0=Mon … 6=Sun"
    else:
        weekday = None
    run_date = None
    if kind == "once":
        raw = str(parsed.get("date") or "")[:10]
        try:
            run_date = dt.date.fromisoformat(raw).isoformat()
        except ValueError:
            return None, "one-time jobs need a date (YYYY-MM-DD)"
        import session_context
        if run_date < session_context.local_today_iso():
            return None, f"that date ({run_date}) is already past"
    action_kind = str(parsed.get("action_kind") or "").strip().lower()
    if action_kind not in ACTION_KINDS:
        return None, "action must be ask or message"
    action_text = str(parsed.get("action_text") or "").strip()
    if not action_text:
        return None, "the job needs something to ask or say"
    title = str(parsed.get("title") or "").strip() or action_text[:40]
    return {
        "title": title, "schedule_kind": kind, "run_time": run_time,
        "weekday": weekday, "run_date": run_date,
        "action_kind": action_kind, "action_text": action_text,
    }, None


def schedule_text(job: dict[str, Any]) -> str:
    kind = job["schedule_kind"]
    if kind == "daily":
        when = "every day"
    elif kind == "weekdays":
        when = "Mon-Fri"
    elif kind == "weekly":
        when = f"every {settings.WEEKDAY_NAMES[int(job['weekday'])]}"
    else:
        when = f"once on {job['run_date']}"
    return f"{when} at {job['run_time']}"


def describe(job: dict[str, Any]) -> str:
    verb = "🤖 ask" if job["action_kind"] == "ask" else "🔔 say"
    state = "" if job.get("enabled", 1) else " (paused)"
    return f"{schedule_text(job)} — {verb}: “{job['action_text']}”{state}"


def builtin_overlaps(data: dict[str, Any]) -> list[str]:
    """Deterministic clash check against the bot's built-in schedules."""
    builtins = [
        ("planning check", "daily", settings.planning_reminder_time(), None),
        ("morning commitment nudge", "daily", settings.commitment_nudge_time(), None),
        ("nightly commitment check", "daily", settings.commitment_check_time(), None),
        ("weekly growth report", "weekly", settings.weekly_report_time(),
         settings.timetable_reminder_weekday()),
        ("weekly timetable check", "weekly", "09:00",
         settings.timetable_reminder_weekday()),
    ]
    overlaps = []
    for name, kind, time, weekday in builtins:
        if data["run_time"] != time:
            continue
        days_clash = (
            kind == "daily" or data["schedule_kind"] in ("daily", "weekdays")
            or (data["schedule_kind"] == "weekly" and data.get("weekday") == weekday)
        )
        if days_clash:
            overlaps.append(f"the built-in {name} already fires at {time}")
    return overlaps


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def create_job(
    chat_id: int, data: dict[str, Any], *, db_path: str | Path = DEFAULT_DB_PATH
) -> dict[str, Any]:
    with _connect(db_path) as conn:
        count = conn.execute(
            f"SELECT COUNT(*) FROM {JOBS_TABLE} WHERE chat_id = ?", (chat_id,)
        ).fetchone()[0]
        if count >= MAX_JOBS:
            raise ValueError(f"job limit reached ({MAX_JOBS}) — delete one first")
        cur = conn.execute(
            f"INSERT INTO {JOBS_TABLE} (chat_id, title, schedule_kind, run_time, "
            "weekday, run_date, action_kind, action_text, enabled, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
            (chat_id, data["title"], data["schedule_kind"], data["run_time"],
             data.get("weekday"), data.get("run_date"),
             data["action_kind"], data["action_text"], _utc_now()),
        )
        conn.commit()
        return get_job(int(cur.lastrowid), db_path=db_path)


def get_job(job_id: int, *, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, Any] | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            f"SELECT * FROM {JOBS_TABLE} WHERE id = ?", (job_id,)
        ).fetchone()
    return dict(row) if row else None


def list_jobs(chat_id: int, *, db_path: str | Path = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM {JOBS_TABLE} WHERE chat_id = ? ORDER BY id", (chat_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def set_enabled(job_id: int, enabled: bool, *, db_path: str | Path = DEFAULT_DB_PATH) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            f"UPDATE {JOBS_TABLE} SET enabled = ? WHERE id = ?",
            (int(enabled), job_id),
        )
        conn.commit()


def delete_job(job_id: int, *, db_path: str | Path = DEFAULT_DB_PATH) -> None:
    with _connect(db_path) as conn:
        conn.execute(f"DELETE FROM {JOBS_TABLE} WHERE id = ?", (job_id,))
        conn.commit()


def matches_day(data: dict[str, Any], day: dt.date) -> bool:
    """Would this job's schedule fire on the given calendar day?"""
    kind = data["schedule_kind"]
    if kind == "daily":
        return True
    if kind == "weekdays":
        return day.weekday() < 5
    if kind == "weekly":
        return data.get("weekday") == day.weekday()
    return data.get("run_date") == day.isoformat()


def should_preclaim_today(data: dict[str, Any], now: dt.datetime) -> bool:
    """True when creating this job now would make it fire immediately today.

    due_jobs selects run_time <= now, so a job created after its slot has
    passed would fire at the very next scan — surprising for "daily at
    08:00" created at 14:00. The creator pre-claims today's dedup key so the
    first run lands on the next matching slot instead.
    """
    return matches_day(data, now.date()) and data["run_time"] <= f"{now:%H:%M}"


def update_field(
    job_id: int, field: str, value: str, *, db_path: str | Path = DEFAULT_DB_PATH
) -> tuple[bool, str]:
    """Edit a job's time or text. Returns (ok, message)."""
    if field == "time":
        ok, result = settings.validate_hhmm(value)
        if not ok:
            return False, result
        column, value = "run_time", result
    elif field == "text":
        value = value.strip()
        if not value:
            return False, "text cannot be empty"
        column = "action_text"
    else:
        return False, "unknown field"
    with _connect(db_path) as conn:
        cur = conn.execute(
            f"UPDATE {JOBS_TABLE} SET {column} = ? WHERE id = ?", (value, job_id)
        )
        conn.commit()
        if cur.rowcount == 0:
            return False, "job not found"
    return True, value


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------

def due_jobs(
    now: dt.datetime, *, db_path: str | Path = DEFAULT_DB_PATH
) -> list[dict[str, Any]]:
    """Jobs whose fire time has passed today. Caller dedups via reminders.claim
    (key user-job:<id>:<date>), so late bot starts still deliver once."""
    hhmm = f"{now:%H:%M}"
    today = now.date().isoformat()
    weekday = now.weekday()
    due = []
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM {JOBS_TABLE} WHERE enabled = 1 AND run_time <= ?",
            (hhmm,),
        ).fetchall()
    for row in rows:
        job = dict(row)
        kind = job["schedule_kind"]
        if kind == "daily":
            due.append(job)
        elif kind == "weekdays" and weekday < 5:
            due.append(job)
        elif kind == "weekly" and job.get("weekday") == weekday:
            due.append(job)
        elif kind == "once" and job.get("run_date") == today:
            due.append(job)
    return due


def mark_ran(
    job_id: int, *, consume_once: bool = True, db_path: str | Path = DEFAULT_DB_PATH
) -> None:
    """Record a run. consume_once=False (manual ▶ Run now) keeps a 'once' job
    armed for its real scheduled fire."""
    with _connect(db_path) as conn:
        if consume_once:
            conn.execute(
                f"UPDATE {JOBS_TABLE} SET last_run = ?, "
                "enabled = CASE WHEN schedule_kind = 'once' THEN 0 ELSE enabled END "
                "WHERE id = ?",
                (_utc_now(), job_id),
            )
        else:
            conn.execute(
                f"UPDATE {JOBS_TABLE} SET last_run = ? WHERE id = ?",
                (_utc_now(), job_id),
            )
        conn.commit()


# ---------------------------------------------------------------------------
# Pending edit state (which job field a chat is typing a new value for)
# ---------------------------------------------------------------------------

def set_pending_edit(
    chat_id: int, job_id: int, field: str, *, db_path: str | Path = DEFAULT_DB_PATH
) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO {EDIT_TABLE} (chat_id, job_id, field, asked_at) "
            "VALUES (?, ?, ?, ?)",
            (chat_id, job_id, field, _utc_now()),
        )
        conn.commit()


def get_pending_edit(
    chat_id: int, *, db_path: str | Path = DEFAULT_DB_PATH
) -> tuple[int, str] | None:
    """Fetch-and-clear (job_id, field). None if absent or stale."""
    with _connect(db_path) as conn:
        row = conn.execute(
            f"SELECT * FROM {EDIT_TABLE} WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        if row is None:
            return None
        conn.execute(f"DELETE FROM {EDIT_TABLE} WHERE chat_id = ?", (chat_id,))
        conn.commit()
    asked = dt.datetime.fromisoformat(row["asked_at"])
    if (dt.datetime.now(dt.timezone.utc) - asked).total_seconds() > EDIT_TTL_MINUTES * 60:
        return None
    return int(row["job_id"]), row["field"]


def clear_pending_edit(chat_id: int, *, db_path: str | Path = DEFAULT_DB_PATH) -> None:
    with _connect(db_path) as conn:
        conn.execute(f"DELETE FROM {EDIT_TABLE} WHERE chat_id = ?", (chat_id,))
        conn.commit()
