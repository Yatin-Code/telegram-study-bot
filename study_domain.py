"""Deterministic study-domain services backed by Notion + SQLite mirror.

This module is deliberately free of LLM decisions. It owns state transitions,
numeric validation, teacher eligibility, exam review creation and planner facts.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Iterable

import notion_client_wrapper as notion
import operational_store
import session_context
import sync
from config import notion_schema


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = PROJECT_ROOT / "sqlite_mirror.db"
ACTIVE_PLAN_TABLE = "active_plan_state"


class DomainError(ValueError):
    pass


def _require_enum(name: str, value: Any, options: list[str], *, default: str | None = None) -> str | None:
    chosen = default if value is None or value == "" else str(value)
    if chosen is None:
        return None
    for option in options:
        if chosen.strip().lower() == option.lower():
            return option
    raise DomainError(f"invalid {name}: {value!r}")


def _connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    sync.init_db(conn)
    operational_store.init_db(conn)
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {ACTIVE_PLAN_TABLE} (
            chat_id TEXT PRIMARY KEY,
            plan_item_id TEXT NOT NULL,
            work_item_id TEXT,
            activated_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def _rows(db_key: str, where: str = "1=1", params: Iterable[Any] = (), *, db_path: str | Path = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    if operational_store.is_operational(db_key):
        return operational_store.rows(db_key, where, params, db_path=db_path)
    with _connect(db_path) as conn:
        return [dict(r) for r in operational_store._safe_select_rows(
            conn, db_key, where, params
        )]


def _row(db_key: str, where: str, params: Iterable[Any] = (), *, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, Any] | None:
    rows = _rows(db_key, where, params, db_path=db_path)
    return rows[0] if rows else None


def _sync(keys: Iterable[str], db_path: str | Path = DEFAULT_DB_PATH) -> None:
    notion_keys = tuple(
        key for key in dict.fromkeys(keys)
        if not operational_store.is_operational(key)
    )
    if not notion_keys:
        return
    try:
        # A Notion write is never mirrored partially: sync ALL Notion-owned
        # DBs immediately so cross-log relations, rollups and computed columns
        # are queryable at the exact second the write lands.
        sync.sync_all_notion(db_path=db_path)
    except Exception:
        # A successful Notion write remains durable; the normal mirror job will
        # catch up. Callers surface the write result, not a misleading failure.
        pass


def _require_nonnegative(name: str, value: Any, *, integer: bool = False) -> int | float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise DomainError(f"{name} cannot be boolean")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise DomainError(f"{name} must be numeric") from exc
    if not math.isfinite(number):
        raise DomainError(f"{name} must be finite")
    if number < 0:
        raise DomainError(f"{name} cannot be negative")
    if integer and not number.is_integer():
        raise DomainError(f"{name} must be a whole number")
    return int(number) if number.is_integer() else number


def _require_number(name: str, value: Any) -> int | float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise DomainError(f"{name} cannot be boolean")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise DomainError(f"{name} must be numeric") from exc
    if not math.isfinite(number):
        raise DomainError(f"{name} must be finite")
    return int(number) if number.is_integer() else number


def _require_priority(value: Any) -> int:
    priority = _require_nonnegative("priority", value, integer=True)
    if priority is None or not 1 <= priority <= 100:
        raise DomainError("priority must be between 1 and 100")
    return int(priority)


def _require_bool(name: str, value: Any, *, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("yes", "true", "on", "1", "allowed", "doubts"):
        return True
    if text in ("no", "false", "off", "0", "not allowed"):
        return False
    raise DomainError(f"{name} must be yes or no")


def _iso_date(value: Any, *, required: bool = False) -> str | None:
    if value is None or value == "":
        if required:
            raise DomainError("date is required")
        return None
    text = str(value).strip()
    try:
        dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DomainError(f"invalid ISO date: {value!r}") from exc
    return text


def _title_match(db_key: str, query: str, *, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, Any] | None:
    query = query.strip().lower()
    if not query:
        return None
    title_col = {
        "ledger": "task", "revision": "chapter_module",
        "work_items": "title", "goals": "title", "exams": "title",
        "exam_questions": "title", "doubt_attempts": "title",
        "timetable": "title", "daily_plan": "title", "doubts": "core_concept",
    }.get(db_key, "title")
    table = operational_store.table_for(db_key) if operational_store.is_operational(db_key) else db_key
    with _connect(db_path) as conn:
        rows = conn.execute(
            f'SELECT * FROM "{table}" WHERE archived=0 AND LOWER(COALESCE("{title_col}","")) LIKE ? LIMIT 20',
            (f"%{query}%",),
        ).fetchall()
    if len(rows) == 1:
        return dict(rows[0])
    exact = [r for r in rows if str(r[title_col] or "").strip().lower() == query]
    if len(exact) == 1:
        return dict(exact[0])
    if len(rows) > 1:
        raise DomainError(f"ambiguous {db_key} query {query!r}")
    return None


def _create(db_key: str, props: dict[str, Any], *, sync_keys: Iterable[str] | None = None, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    props = dict(props)
    if not props.get("operation_id"):
        props["operation_id"] = uuid.uuid4().hex
    if operational_store.is_operational(db_key):
        return operational_store.create(db_key, props, db_path=db_path)
    existing = notion.query_database(
        db_key,
        filter={"property": "Operation ID", "rich_text": {"equals": props["operation_id"]}},
        max_pages=1,
    )
    page = existing[0] if existing else notion.create_page(db_key, props)
    _sync(sync_keys or (db_key,), db_path)
    return page


def create_work_item(props: dict[str, Any], *, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    props = dict(props)
    props.setdefault("status", "Inbox")
    props.setdefault("priority", 50)
    props.setdefault("interruptible", True)
    props["kind"] = _require_enum("kind", props.get("kind"), notion_schema.WORK_KIND_OPTIONS, default="Other")
    props["status"] = _require_enum("status", props.get("status"), notion_schema.WORK_STATUS_OPTIONS, default="Inbox")
    props["priority"] = _require_priority(props["priority"])
    for key in ("estimated_min", "expected_cy", "remaining_units", "total_units"):
        if key in props:
            props[key] = _require_nonnegative(key, props[key])
    if props.get("remaining_units") is not None and props.get("total_units") is not None:
        if props["remaining_units"] > props["total_units"]:
            raise DomainError("remaining_units cannot exceed total_units")
    for key in ("due_date", "planned_date"):
        if key in props:
            props[key] = _iso_date(props[key])
    if not props.get("title"):
        raise DomainError("work item needs a title")
    return _create("work_items", props, db_path=db_path)


def update_work_item_status(
    item: str, status: str, *, db_path: str | Path = DEFAULT_DB_PATH
) -> dict[str, Any]:
    chosen = _require_enum("status", status, notion_schema.WORK_STATUS_OPTIONS)
    row = _title_match("work_items", item, db_path=db_path)
    if not row:
        raise DomainError(f"no work item matches {item!r}")
    return operational_store.update(
        "work_items", row["notion_page_id"], {"status": chosen}, db_path=db_path
    )


def create_timetable_entry(
    data: dict[str, Any], *, db_path: str | Path = DEFAULT_DB_PATH
) -> dict[str, Any]:
    weekday = _require_enum(
        "weekday", data.get("weekday"), notion_schema.WEEKDAY_OPTIONS
    )
    kind = _require_enum(
        "kind", data.get("kind"), notion_schema.TIMETABLE_KIND_OPTIONS,
        default="Class",
    )
    start = str(data.get("start_time") or "").strip()
    end = str(data.get("end_time") or "").strip()
    start_parts, end_parts = _parse_hhmm(start), _parse_hhmm(end)
    if end_parts <= start_parts:
        raise DomainError("timetable end time must be after start time")
    subject = _require_enum(
        "subject", data.get("subject"), notion_schema.SUBJECT_OPTIONS
    )
    title = str(data.get("title") or "").strip()
    if not title:
        title = f"{weekday} {start} {kind}"
    questions_allowed = _require_bool(
        "questions_allowed", data.get("questions_allowed"),
        default=(kind == "Doubt Window"),
    )
    return _create("timetable", {
        "title": title,
        "weekday": weekday,
        "start_time": f"{start_parts[0]:02d}:{start_parts[1]:02d}",
        "end_time": f"{end_parts[0]:02d}:{end_parts[1]:02d}",
        "subject": subject,
        "teacher": data.get("teacher"),
        "kind": kind,
        "questions_allowed": questions_allowed,
        "location": data.get("location"),
        "effective_from": _iso_date(data.get("effective_from")),
        "effective_to": _iso_date(data.get("effective_to")),
        "active": bool(data.get("active", True)),
        "operation_id": data.get("operation_id"),
    }, db_path=db_path)


def set_timetable_active(
    entry: str, active: bool, *, db_path: str | Path = DEFAULT_DB_PATH
) -> dict[str, Any]:
    row = _title_match("timetable", entry, db_path=db_path)
    if not row:
        raise DomainError(f"no timetable entry matches {entry!r}")
    return operational_store.update(
        "timetable", row["notion_page_id"], {"active": bool(active)},
        db_path=db_path,
    )


def set_timetable_questions_allowed(
    entry: str, allowed: bool, *, db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    row = _title_match("timetable", entry, db_path=db_path)
    if not row:
        raise DomainError(f"no timetable entry matches {entry!r}")
    return operational_store.update(
        "timetable", row["notion_page_id"], {"questions_allowed": bool(allowed)},
        db_path=db_path,
    )


def ensure_system_goals(*, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    """Create the internal AIR-1 operating target once, without predicting rank."""
    return create_goal({
        "title": "JEE 2028 AIR 1 operating target",
        "goal_type": "Rank",
        "metric": "AIR",
        "target": 1,
        "period": "Deadline",
        "priority": 100,
        "hard_constraint": True,
        "notes": "Planning objective, not a rank prediction. Recommendations remain evidence-gated.",
        "operation_id": "system-goal:jee-2028-air-1",
    }, db_path=db_path)


def create_goal(data: dict[str, Any], *, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    target = _require_nonnegative("target", data.get("target"))
    if target is None:
        raise DomainError("goal target is required")
    minimum = _require_nonnegative("minimum", data.get("minimum"))
    if minimum is not None and minimum > target:
        raise DomainError("minimum cannot exceed target")
    goal_type = _require_enum("goal_type", data.get("goal_type"), notion_schema.GOAL_TYPE_OPTIONS, default="Custom")
    period = _require_enum("period", data.get("period"), notion_schema.GOAL_PERIOD_OPTIONS, default="Deadline")
    props = {
        "title": str(data["title"]).strip(),
        "goal_type": goal_type,
        "status": _require_enum("status", data.get("status"), notion_schema.GOAL_STATUS_OPTIONS, default="Active"),
        "metric": data.get("metric") or goal_type,
        "target": target,
        "period": period,
        "subject": data.get("subject"),
        "deadline": _iso_date(data.get("deadline")),
        "priority": _require_priority(50 if data.get("priority") is None else data.get("priority")),
        "minimum": minimum,
        "hard_constraint": bool(data.get("hard_constraint", False)),
        "source_text": data.get("source_text"),
        "notes": data.get("notes"),
        "operation_id": data.get("operation_id"),
    }
    if not props["title"]:
        raise DomainError("goal title is required")
    return _create("goals", props, db_path=db_path)


def _goal_match_for_status_change(
    query: str, *, db_path: str | Path = DEFAULT_DB_PATH
) -> dict[str, Any] | None:
    """Title-match a goal, preferring live goals.

    Cancelled/Completed goals keep their titles forever; without this
    preference, cancelling "Daily PYQs", recreating it, and cancelling again
    is permanently "ambiguous".
    """
    query_l = query.strip().lower()
    if not query_l:
        return None
    table = operational_store.table_for("goals")
    with _connect(db_path) as conn:
        rows = [dict(r) for r in conn.execute(
            f'SELECT * FROM "{table}" WHERE archived=0 '
            'AND LOWER(COALESCE(title, "")) LIKE ? LIMIT 20',
            (f"%{query_l}%",),
        ).fetchall()]
    live = [r for r in rows if r.get("status") not in ("Cancelled", "Completed")]
    pool = live or rows
    if len(pool) == 1:
        return pool[0]
    exact = [r for r in pool if str(r.get("title") or "").strip().lower() == query_l]
    if len(exact) == 1:
        return exact[0]
    if len(pool) > 1:
        raise DomainError(f"ambiguous goals query {query!r}")
    return None


def update_goal_status(
    goal: str, status: str, *, db_path: str | Path = DEFAULT_DB_PATH
) -> dict[str, Any]:
    if status not in notion_schema.GOAL_STATUS_OPTIONS:
        raise DomainError(f"invalid goal status: {status!r}")
    row = _goal_match_for_status_change(goal, db_path=db_path)
    if not row:
        raise DomainError(f"no goal matches {goal!r}")
    operational_store.update(
        "goals", row["notion_page_id"], {"status": status}, db_path=db_path
    )
    return {"goal_id": row["notion_page_id"], "status": status}


def create_exam(data: dict[str, Any], *, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    max_marks = _require_nonnegative("max_marks", data.get("max_marks"))
    target_marks = _require_nonnegative("target_marks", data.get("target_marks"))
    if max_marks is not None and target_marks is not None and target_marks > max_marks:
        raise DomainError("target marks cannot exceed maximum marks")
    props = {
        "title": str(data["title"]).strip(),
        "kind": _require_enum("kind", data.get("kind"), notion_schema.EXAM_KIND_OPTIONS, default="Other"),
        "status": "Planned",
        "exam_date": _iso_date(data.get("exam_date"), required=True),
        "date_confidence": _require_enum("date_confidence", data.get("date_confidence"), notion_schema.DATE_CONFIDENCE_OPTIONS, default="Tentative"),
        "syllabus": data.get("syllabus"),
        "max_marks": max_marks,
        "target_marks": target_marks,
        "source_url": data.get("source_url"),
        "notes": data.get("notes"),
        "operation_id": data.get("operation_id"),
    }
    return _create("exams", props, db_path=db_path)


def finish_exam(exam: str, *, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    """Move an exam into analysis and create the mandatory paper-review task."""
    row = _title_match("exams", exam, db_path=db_path)
    if not row:
        raise DomainError(f"no exam matches {exam!r}")
    exam_id = row["notion_page_id"]
    operational_store.update(
        "exams", exam_id,
        {"status": "Analysing", "analysis_complete": False}, db_path=db_path,
    )
    work = create_work_item({
        "title": f"Complete and analyse {row.get('title')}",
        "kind": "Exam Review", "status": "Planned", "priority": 100,
        "planned_date": session_context.local_today_iso(),
        "exam": exam_id, "interruptible": False,
        "exit_condition": "Re-solve the full paper, then record summary and every mistake",
        "operation_id": f"exam-review-work:{exam_id}",
    }, db_path=db_path)
    create_plan_item({
        "title": f"Paper analysis: {row.get('title')}",
        "plan_date": session_context.local_today_iso(), "sequence": 0,
        "work_item": work.get("id"), "kind": "Exam Review", "priority": 100,
        "interruptible": False,
        "exit_condition": "Complete full-paper reconstruction before other optional work",
        "operation_id": f"exam-review-plan:{exam_id}",
    }, db_path=db_path)
    _sync(("daily_plan",), db_path)
    return {"exam_id": exam_id, "work_item_id": work.get("id"), "status": "Analysing"}


def record_exam_summary(exam: str, data: dict[str, Any], *, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    row = _title_match("exams", exam, db_path=db_path)
    if not row:
        raise DomainError(f"no exam matches {exam!r}")
    values = {"actual_marks": _require_number("actual_marks", data.get("actual_marks"))}
    values.update({
        key: _require_nonnegative(key, data.get(key), integer=True)
        for key in ("attempted", "correct", "incorrect", "unattempted")
    })
    if values["actual_marks"] is None:
        raise DomainError("actual marks are required")
    if row.get("max_marks") is not None and values["actual_marks"] > row["max_marks"]:
        raise DomainError("actual marks cannot exceed maximum marks")
    if row.get("max_marks") is not None and values["actual_marks"] < -float(row["max_marks"]):
        raise DomainError("actual marks are below the supported exam range")
    attempted = values.get("attempted")
    correct = values.get("correct")
    incorrect = values.get("incorrect")
    if attempted is not None and correct is not None and correct > attempted:
        raise DomainError("correct cannot exceed attempted")
    if attempted is not None and incorrect is not None and incorrect > attempted:
        raise DomainError("incorrect cannot exceed attempted")
    if attempted is not None and correct is not None and incorrect is not None:
        if correct + incorrect > attempted:
            raise DomainError("correct + incorrect cannot exceed attempted")
    props = {key: value for key, value in values.items() if value is not None}
    props["status"] = "Analysing"
    operational_store.update("exams", row["notion_page_id"], props, db_path=db_path)
    return {"exam_id": row["notion_page_id"], **props}


def record_question_review(exam: str, data: dict[str, Any], *, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    exam_row = _title_match("exams", exam, db_path=db_path)
    if not exam_row:
        raise DomainError(f"no exam matches {exam!r}")
    qno = str(data.get("question_no") or "").strip()
    if not qno:
        raise DomainError("question number is required")
    attempted = bool(data.get("attempted", False))
    correct = bool(data.get("correct", False))
    if correct and not attempted:
        raise DomainError("a correct question must be marked attempted")
    if not correct and not str(data.get("root_cause") or "").strip():
        raise DomainError("an incorrect/unattempted question needs a root cause")
    title = f"{exam_row.get('title')} Q{qno}"
    existing = _row(
        "exam_questions", "archived=0 AND exam LIKE ? AND question_no=?",
        (f"%{exam_row['notion_page_id']}%", qno), db_path=db_path,
    )
    if existing:
        raise DomainError(f"question {qno} is already recorded for this exam")
    props = {
        "title": title, "exam": exam_row["notion_page_id"], "question_no": qno,
        "subject": data.get("subject"), "chapter": data.get("chapter"),
        "attempted": attempted,
        "correct": correct,
        "marks_awarded": _require_number("marks_awarded", data.get("marks_awarded")),
        "marks_lost": _require_nonnegative("marks_lost", data.get("marks_lost")),
        "time_min": _require_nonnegative("time_min", data.get("time_min")),
        "failure_type": data.get("failure_type"), "root_cause": data.get("root_cause"),
        "correct_approach": data.get("correct_approach"),
        "reattempt_status": "Pending" if not correct else None,
        "reattempt_date": _iso_date(data.get("reattempt_date")),
        "operation_id": f"exam-question:{exam_row['notion_page_id']}:{qno}",
    }
    return _create("exam_questions", props, db_path=db_path)


def complete_exam_analysis(exam: str, *, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    row = _title_match("exams", exam, db_path=db_path)
    if not row:
        raise DomainError(f"no exam matches {exam!r}")
    questions = _rows(
        "exam_questions", "archived=0 AND exam LIKE ?",
        (f"%{row['notion_page_id']}%",), db_path=db_path,
    )
    if not questions:
        raise DomainError("record at least one question review before completing analysis")
    operational_store.update(
        "exams", row["notion_page_id"],
        {"status": "Analysed", "analysis_complete": True}, db_path=db_path,
    )
    return {"exam_id": row["notion_page_id"], "status": "Analysed", "questions_reviewed": len(questions)}


def create_plan_item(props: dict[str, Any], *, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    sequence = _require_nonnegative("sequence", props.get("sequence"), integer=True)
    if sequence is None:
        raise DomainError("plan sequence is required")
    plan_date = _iso_date(props.get("plan_date"), required=True)
    work_item = props.get("work_item")
    planner_note = props.get("planner_note")
    if work_item:
        local_work = _row("work_items", "notion_page_id=?", (str(work_item),), db_path=db_path)
        if local_work:
            planner_note = f"SQLite work item: {work_item}" + (f"; {planner_note}" if planner_note else "")
            work_item = None
    return _create("daily_plan", {
        "title": str(props.get("title") or props.get("exit_condition") or "Plan item"),
        "plan_date": plan_date,
        "sequence": sequence,
        "work_item": work_item,
        "subject": props.get("subject"),
        "kind": props.get("kind"),
        "expected_cy": _require_nonnegative("expected_cy", props.get("expected_cy")),
        "estimated_min": _require_nonnegative("estimated_min", props.get("estimated_min")),
        "status": props.get("status") or "Planned",
        "priority": _require_priority(50 if props.get("priority") is None else props.get("priority")),
        "interruptible": bool(props.get("interruptible", True)),
        "exit_condition": props.get("exit_condition"),
        "planner_note": planner_note,
        "operation_id": props.get("operation_id"),
    }, db_path=db_path)


def _doubt_id(query: str, *, db_path: str | Path = DEFAULT_DB_PATH) -> str:
    row = _title_match("doubts", query, db_path=db_path)
    if not row:
        raise DomainError(f"no doubt matches {query!r}")
    return str(row["notion_page_id"])


def _attempt_rows(doubt_id: str, *, db_path: str | Path = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    return _rows("doubt_attempts", "archived=0 AND doubt LIKE ?", (f"%{doubt_id}%",), db_path=db_path)


def record_doubt_attempt(
    doubt: str,
    *,
    duration_min: Any,
    approach: str,
    stuck_point: str,
    outcome: str = "Unsolved",
    ledger_entry: str | None = None,
    attempted_at: dt.datetime | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    duration = _require_nonnegative("duration_min", duration_min)
    if duration is None or duration < 1:
        raise DomainError("a doubt attempt needs at least one minute")
    if not str(approach or "").strip() or not str(stuck_point or "").strip():
        raise DomainError("record the approach and the exact stuck point")
    if outcome not in notion_schema.ATTEMPT_OUTCOME_OPTIONS:
        raise DomainError(f"invalid attempt outcome: {outcome!r}")
    doubt_id = _doubt_id(doubt, db_path=db_path)
    current_doubt = _row("doubts", "notion_page_id=?", (doubt_id,), db_path=db_path)
    if current_doubt and str(current_doubt.get("workflow_state") or "") in (
        "Resolved", "Solved Independently", "Dismissed"
    ):
        raise DomainError("this doubt is already closed; reopen it before another attempt")
    previous = _attempt_rows(doubt_id, db_path=db_path)
    attempted_at = attempted_at or session_context.local_now()
    valid = outcome not in ("Solution Viewed", "Dismissed")
    # Two rapid submissions are one attempt, not two independent encounters.
    latest_valid = None
    for old in previous:
        if old.get("valid") and old.get("attempted_at"):
            try:
                stamp = dt.datetime.fromisoformat(str(old["attempted_at"]).replace("Z", "+00:00"))
            except ValueError:
                continue
            if latest_valid is None or stamp > latest_valid:
                latest_valid = stamp
    if latest_valid is not None:
        comparable = attempted_at
        if comparable.tzinfo is None and latest_valid.tzinfo is not None:
            comparable = comparable.replace(tzinfo=latest_valid.tzinfo)
        if (comparable - latest_valid).total_seconds() < 30 * 60:
            valid = False
    attempt_no = len(previous) + 1
    props: dict[str, Any] = {
        "title": f"Attempt {attempt_no} — {doubt}",
        "doubt": doubt_id,
        "attempt_no": attempt_no,
        "attempted_at": attempted_at.isoformat(),
        "duration_min": duration,
        "approach": approach.strip(),
        "stuck_point": stuck_point.strip(),
        "outcome": outcome,
        "valid": valid,
        "ledger_entry": ledger_entry,
        "operation_id": f"doubt-attempt:{doubt_id}:{attempt_no}",
    }
    page = _create("doubt_attempts", props, sync_keys=("doubt_attempts", "doubts"), db_path=db_path)
    # Count from the refreshed mirror when possible; include this write if the
    # remote sync is temporarily unavailable.
    expected_count = sum(1 for row in previous if row.get("valid")) + (1 if valid else 0)
    refreshed = _attempt_rows(doubt_id, db_path=db_path)
    valid_count = max(expected_count, sum(1 for row in refreshed if row.get("valid")))
    if outcome == "Solved Independently":
        workflow = "Solved Independently"
        status = "Resolved"
        teacher_ready = False
    elif valid_count >= 2:
        workflow = "Eligible for Teacher"
        status = "Unresolved"
        teacher_ready = True
    else:
        workflow = "Attempting"
        status = "Unresolved"
        teacher_ready = False
    summary_pending = False
    try:
        notion.update_page(doubt_id, {
            "workflow_state": workflow,
            "valid_attempts": valid_count,
            "teacher_ready": teacher_ready,
            "status": status,
            "teacher_asked": False,
        })
        _sync(("doubts",), db_path)
    except Exception:
        # The authoritative attempt is already durable in SQLite. Eligibility
        # is computed from it, so a temporary Notion outage cannot lose work.
        summary_pending = True
    return {
        "page_id": page.get("id"), "doubt_id": doubt_id, "attempt_no": attempt_no,
        "valid_attempts": valid_count, "workflow_state": workflow,
        "teacher_ready": teacher_ready, "notion_summary_pending": summary_pending,
    }


def eligible_doubts(*, db_path: str | Path = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    return [row for row in doubt_queue(db_path=db_path) if row["readiness"] == "ready"]


def doubt_queue(*, db_path: str | Path = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    """All open doubts with evidence-derived readiness, including legacy rows."""
    result: list[dict[str, Any]] = []
    for doubt in _rows(
        "doubts",
        "archived=0 AND LOWER(COALESCE(status,'')) NOT IN ('resolved','dismissed')",
        db_path=db_path,
    ):
        attempts = [
            attempt for attempt in _attempt_rows(doubt["notion_page_id"], db_path=db_path)
            if attempt.get("valid")
        ]
        valid_count = len(attempts)
        def _duration(row: dict[str, Any]) -> float:
            try:
                value = float(row.get("duration_min") or 0)
                return value if math.isfinite(value) else 0
            except (TypeError, ValueError):
                return 0

        serious = any(_duration(attempt) >= 10 for attempt in attempts)
        readiness = "ready" if valid_count >= 2 else ("attempting" if valid_count else "new")
        status = str(doubt.get("status") or "").strip()
        workflow = str(doubt.get("workflow_state") or "").strip()
        result.append({
            **doubt,
            "valid_attempts": valid_count,
            "teacher_ready": int(valid_count >= 2),
            "serious_attempt": serious,
            "readiness": readiness,
            "metadata_incomplete": not bool(status and workflow),
        })
    order = {"ready": 0, "expedited": 1, "attempting": 2, "new": 3}
    return sorted(result, key=lambda row: (
        order.get(str(row.get("readiness")), 9),
        str(row.get("subject") or ""), str(row.get("core_concept") or ""),
    ))


def dismiss_doubt(query: str, reason: str, *, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    if not reason.strip():
        raise DomainError("dismissal needs a reason")
    row = _doubt_id(query, db_path=db_path)
    notion.update_page(row, {
        "workflow_state": "Dismissed", "status": "Resolved", "dismissed_reason": reason.strip(),
    })
    _sync(("doubts",), db_path)
    return {"doubt_id": row, "workflow_state": "Dismissed"}


def resolve_doubt(
    query: str, resolution: str, *, teacher_asked: bool = False,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    if not resolution.strip():
        raise DomainError("resolution evidence is required")
    row = _title_match("doubts", query, db_path=db_path)
    if not row:
        raise DomainError(f"no doubt matches {query!r}")
    return resolve_doubt_id(
        str(row["notion_page_id"]), resolution, teacher_asked=teacher_asked,
        db_path=db_path,
    )


def resolve_doubt_id(
    doubt_id: str, resolution: str, *, teacher_asked: bool = False,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Resolve one exact doubt ID after recording real resolution evidence."""
    if not resolution.strip():
        raise DomainError("resolution evidence is required")
    row = _row(
        "doubts", "notion_page_id=? AND archived=0", (str(doubt_id),),
        db_path=db_path,
    )
    if not row:
        raise DomainError("that doubt no longer exists")
    valid_attempts = sum(
        1 for attempt in _attempt_rows(row["notion_page_id"], db_path=db_path)
        if attempt.get("valid")
    )
    if teacher_asked and valid_attempts < 2:
        raise DomainError("teacher escalation requires two valid attempts first")
    workflow = "Resolved" if teacher_asked else "Solved Independently"
    notion.update_page(row["notion_page_id"], {
        "workflow_state": workflow, "status": "Resolved",
        "resolution": resolution.strip(),
        "resolved_at": session_context.local_now().isoformat(),
        "teacher_asked": bool(teacher_asked),
        "teacher_ready": False,
    })
    _sync(("doubts",), db_path)
    return {"doubt_id": row["notion_page_id"], "workflow_state": workflow}


def reopen_doubt(query: str, *, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    row = _title_match("doubts", query, db_path=db_path)
    if not row:
        raise DomainError(f"no doubt matches {query!r}")
    notion.update_page(row["notion_page_id"], {
        "workflow_state": "Attempting", "status": "Unresolved",
        "teacher_ready": sum(
            1 for attempt in _attempt_rows(row["notion_page_id"], db_path=db_path)
            if attempt.get("valid")
        ) >= 2,
    })
    _sync(("doubts",), db_path)
    return {"doubt_id": row["notion_page_id"], "workflow_state": "Attempting"}


def _parse_hhmm(value: Any) -> tuple[int, int]:
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(value or ""))
    if not match:
        raise DomainError(f"invalid time: {value!r}")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise DomainError(f"invalid time: {value!r}")
    return hour, minute


def upcoming_teacher_windows(*, now: dt.datetime | None = None, days: int = 7, db_path: str | Path = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    now = now or session_context.local_now()
    windows: list[dict[str, Any]] = []
    for row in _rows(
        "timetable",
        "archived=0 AND active=1",
        db_path=db_path,
    ):
        if row.get("kind") != "Doubt Window" and not row.get("questions_allowed"):
            continue
        try:
            weekday = notion_schema.WEEKDAY_OPTIONS.index(row.get("weekday"))
            hour, minute = _parse_hhmm(row.get("start_time"))
            end_hour, end_minute = _parse_hhmm(row.get("end_time"))
        except (ValueError, DomainError):
            continue
        for offset in range(days + 1):
            date = (now.date() + dt.timedelta(days=offset))
            if date.weekday() != weekday:
                continue
            try:
                effective_from = (
                    dt.date.fromisoformat(str(row["effective_from"])[:10])
                    if row.get("effective_from") else None
                )
                effective_to = (
                    dt.date.fromisoformat(str(row["effective_to"])[:10])
                    if row.get("effective_to") else None
                )
            except ValueError:
                continue
            if effective_from and date < effective_from:
                continue
            if effective_to and date > effective_to:
                continue
            start = dt.datetime.combine(date, dt.time(hour, minute), tzinfo=now.tzinfo)
            end = dt.datetime.combine(date, dt.time(end_hour, end_minute), tzinfo=now.tzinfo)
            if end <= start:
                end += dt.timedelta(days=1)
            if end <= now:
                continue
            if start < now:
                start = now
            windows.append({**row, "starts_at": start, "ends_at": end})
    return sorted(windows, key=lambda item: item["starts_at"])


def interruption_decision(
    *, current_priority: int | None, current_interruptible: bool,
    window: dict[str, Any], now: dt.datetime | None = None,
    cooldown_until: dt.datetime | None = None,
) -> dict[str, Any]:
    now = now or session_context.local_now()
    minutes_left = (window["ends_at"] - now).total_seconds() / 60
    allowed = (
        current_interruptible
        and (current_priority or 0) < 80
        and minutes_left <= 25
        and minutes_left > 0
        and (cooldown_until is None or now >= cooldown_until)
    )
    return {"interrupt": allowed, "minutes_left": round(minutes_left, 1), "reason": (
        "teacher window closes soon and current work is interruptible" if allowed
        else "continue current sequence until a boundary"
    )}


def adaptive_target(*, today: str | None = None, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    """Return an exam-aware CY target, gated by demonstrated capacity.

    Exam proximity can raise the requested pace, but only complete recent
    blocks with acceptable accuracy unlock the increase. Otherwise the planner
    reprioritises within the baseline instead of fabricating extra capacity.
    """
    from config.settings import daily_cy_baseline, daily_cy_ceiling

    day = dt.date.fromisoformat((today or session_context.local_today_iso())[:10])
    baseline, ceiling = daily_cy_baseline(), daily_cy_ceiling()
    nearest = None
    for exam in _rows("exams", "archived=0 AND status='Planned'", db_path=db_path):
        raw = exam.get("exam_date")
        if not raw:
            continue
        try:
            exam_day = dt.date.fromisoformat(str(raw)[:10])
        except ValueError:
            continue
        days = (exam_day - day).days
        if days < 0:
            continue
        if nearest is None or days < nearest[0]:
            nearest = (days, exam)

    days_to_exam = nearest[0] if nearest else None
    if days_to_exam is None or days_to_exam > 180:
        phase, requested_boost = "Foundation", 0
    elif days_to_exam > 90:
        phase, requested_boost = "Consolidation", 10
    elif days_to_exam > 30:
        phase, requested_boost = "Timed practice", 25
    else:
        phase, requested_boost = "Exam simulation", 50

    start = (day - dt.timedelta(days=13)).isoformat()
    with _connect(db_path) as conn:
        recent = conn.execute("""
            SELECT COUNT(*) blocks, AVG(cognitive_yield) avg_block_cy,
                   SUM(questions_attempted) attempted, SUM(questions_correct) correct,
                   COUNT(DISTINCT substr(date,1,10)) active_days
            FROM ledger
            WHERE archived=0 AND substr(COALESCE(date,''),1,10) BETWEEN ? AND ?
              AND cognitive_yield IS NOT NULL AND accuracy_ratio IS NOT NULL
        """, (start, day.isoformat())).fetchone()
    attempted, correct = recent["attempted"] or 0, recent["correct"] or 0
    accuracy = (correct / attempted) if attempted else None
    capacity_proven = recent["blocks"] >= 5 and accuracy is not None and accuracy >= 0.65
    boost = requested_boost if capacity_proven else 0
    target = min(ceiling, baseline + boost)
    avg_block = float(recent["avg_block_cy"] or 0)
    blocks = max(1, round(target / avg_block)) if avg_block > 0 else None
    return {
        "baseline": baseline, "ceiling": ceiling, "target": target,
        "phase": phase, "days_to_exam": days_to_exam,
        "nearest_exam": nearest[1].get("title") if nearest else None,
        "date_confidence": nearest[1].get("date_confidence") if nearest else None,
        "requested_boost": requested_boost, "applied_boost": boost,
        "capacity_proven": capacity_proven, "recent_blocks": recent["blocks"],
        "recent_accuracy": accuracy, "recommended_blocks": blocks,
    }


def _normalised_words(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _goal_scope_kind(goal: dict[str, Any]) -> str | None:
    """Map a goal's human metric/title onto a deterministic plan kind."""
    text = _normalised_words(f"{goal.get('metric') or ''} {goal.get('title') or ''}")
    aliases = (
        ("PYQ", ("pyq", "previousyearquestion")),
        ("Short Notes", ("shortnote",)),
        ("Coaching Homework", ("coachinghomework", "classhomework")),
        ("Doubt Reattempt", ("doubtreattempt",)),
        ("Exam Review", ("examreview", "paperanalysis")),
        ("Revision", ("revision", "revise")),
        ("Current Syllabus", ("currentsyllabus",)),
    )
    for kind, needles in aliases:
        if any(needle in text for needle in needles):
            return kind
    return None


def _goal_items(goal: dict[str, Any], items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    subject = str(goal.get("subject") or "").strip().lower()
    kind = _goal_scope_kind(goal)
    result: list[dict[str, Any]] = []
    for item in items:
        if subject and str(item.get("subject") or "").strip().lower() != subject:
            continue
        if kind:
            item_kind = str(item.get("kind") or "").strip().lower()
            if item_kind != kind.lower():
                continue
        result.append(item)
    return result


def _goal_planned_value(goal: dict[str, Any], items: list[dict[str, Any]]) -> float | None:
    scoped = _goal_items(goal, items)
    goal_type = goal.get("goal_type")
    if goal_type == "CY":
        return sum(float(row.get("expected_cy") or 0) for row in scoped)
    if goal_type == "Duration":
        return sum(float(row.get("estimated_min") or 0) for row in scoped)
    if goal_type == "Coverage" and _normalised_words(goal.get("metric")) in {
        "block", "blocks", "session", "sessions", "item", "items",
    }:
        return float(len(scoped))
    return None


_ACTIVE_PLAN_STATUSES = {"", "planned", "active"}


def _plan_item_is_active(row: dict[str, Any]) -> bool:
    return str(row.get("status") or "").strip().lower() in _ACTIVE_PLAN_STATUSES


def _chapter_key(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _linked_plan_work_item(
    item: dict[str, Any], *, db_path: str | Path
) -> dict[str, Any] | None:
    work_id = _relation_id(item.get("work_item"))
    if not work_id:
        marker = re.search(
            r"SQLite work item:\s*([A-Za-z0-9-]+)",
            str(item.get("planner_note") or ""),
            re.IGNORECASE,
        )
        work_id = marker.group(1) if marker else None
    if not work_id:
        return None
    return _row("work_items", "notion_page_id=?", (work_id,), db_path=db_path)


def _planned_revision_keys(
    items: list[dict[str, Any]], *, db_path: str | Path
) -> set[str]:
    """Exact chapter evidence represented by active plan items."""
    keys: set[str] = set()
    for item in items:
        title = _chapter_key(item.get("title"))
        if title:
            keys.add(title)
        linked = _linked_plan_work_item(item, db_path=db_path)
        if linked:
            for field in ("chapter", "title"):
                key = _chapter_key(linked.get(field))
                if key:
                    keys.add(key)
    return keys


def plan_facts(plan_date: str | None = None, *, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    plan_date = (plan_date or session_context.local_today_iso())[:10]
    items = sorted(
        _rows("daily_plan", "archived=0 AND substr(COALESCE(plan_date,''),1,10)=?", (plan_date,), db_path=db_path),
        key=lambda row: (row.get("sequence") is None, row.get("sequence") or 0),
    )
    goals = _rows("goals", "archived=0 AND status='Active'", db_path=db_path)
    backlog = _rows("work_items", "archived=0 AND status IN ('Backlog','Inbox')", db_path=db_path)
    homework = _rows(
        "work_items",
        "archived=0 AND kind='Coaching Homework' "
        "AND status NOT IN ('Completed','Dismissed')",
        db_path=db_path,
    )
    due_revision = _rows(
        "revision",
        "archived=0 AND next_execution_date IS NOT NULL AND substr(next_execution_date,1,10) <= ?",
        (plan_date,), db_path=db_path,
    )
    active_items = [row for row in items if _plan_item_is_active(row)]
    expected_cy = sum(float(row.get("expected_cy") or 0) for row in active_items)
    planned_minutes = sum(float(row.get("estimated_min") or 0) for row in active_items)
    warnings: list[str] = []
    errors: list[str] = []
    signals: list[dict[str, Any]] = []

    def add_signal(
        code: str, severity: str, message: str, **evidence: Any
    ) -> None:
        signals.append({
            "code": code,
            "severity": severity,
            "message": message,
            "evidence": evidence,
        })
        (errors if severity == "error" else warnings).append(message)

    missing_sequence = [row for row in active_items if row.get("sequence") is None]
    if missing_sequence:
        add_signal(
            "missing_sequence", "error",
            "one or more plan items have no sequence number",
            count=len(missing_sequence),
            items=[row.get("title") for row in missing_sequence],
        )
    seen: set[int] = set()
    duplicate_sequences: set[int] = set()
    for row in active_items:
        seq = row.get("sequence")
        if seq is not None and int(seq) in seen:
            duplicate_sequences.add(int(seq))
        if seq is not None:
            seen.add(int(seq))
    for seq in sorted(duplicate_sequences):
        add_signal(
            "duplicate_sequence", "error", f"duplicate sequence {seq}",
            sequence=seq,
        )
    pace = adaptive_target(today=plan_date, db_path=db_path)
    if expected_cy > pace["ceiling"]:
        add_signal(
            "capacity_exceeded", "error",
            f"expected CY {expected_cy:g} exceeds ceiling {pace['ceiling']}",
            expected_cy=expected_cy, ceiling=pace["ceiling"],
        )
    elif expected_cy < pace["target"]:
        add_signal(
            "below_adaptive_target", "warning",
            f"expected CY {expected_cy:g} is below adaptive target {pace['target']}",
            expected_cy=expected_cy, target=pace["target"],
        )
    missing_revision: list[dict[str, Any]] = []
    if due_revision:
        planned_chapters = _planned_revision_keys(active_items, db_path=db_path)
        missing_revision = [
            row for row in due_revision
            if _chapter_key(row.get("chapter_module")) not in planned_chapters
        ]
        if missing_revision:
            add_signal(
                "overdue_revision_unplanned", "warning",
                f"{len(missing_revision)} overdue revision item(s) are not in the plan",
                count=len(missing_revision),
                chapters=[row.get("chapter_module") for row in missing_revision],
            )
    planned_work_ids = {
        relation_id for relation_id in (_relation_id(row.get("work_item")) for row in active_items)
        if relation_id
    }
    unplanned_backlog = [
        row for row in backlog if row.get("notion_page_id") not in planned_work_ids
    ]
    has_backlog_block = any(row.get("kind") == "Backlog" for row in active_items)
    if unplanned_backlog and not has_backlog_block:
        add_signal(
            "backlog_unplanned", "warning",
            f"{len(unplanned_backlog)} backlog item(s) exist but no backlog work is linked to the plan",
            count=len(unplanned_backlog),
            items=[row.get("title") for row in unplanned_backlog],
        )

    def homework_is_current(row: dict[str, Any]) -> bool:
        date_value = row.get("due_date") or row.get("planned_date")
        if not date_value:
            return True
        return str(date_value)[:10] <= plan_date

    current_homework = [row for row in homework if homework_is_current(row)]
    unplanned_homework = [
        row for row in current_homework
        if row.get("notion_page_id") not in planned_work_ids
    ]
    if unplanned_homework:
        add_signal(
            "homework_unplanned", "warning",
            f"{len(unplanned_homework)} current coaching homework item(s) are not linked to the plan",
            count=len(unplanned_homework),
            items=[row.get("title") for row in unplanned_homework],
        )
    active_goal_gaps: list[dict[str, Any]] = []
    unverifiable_goals: list[str] = []
    for goal in goals:
        period = goal.get("period")
        if period != "Daily":
            continue
        target = float(goal.get("target") or 0)
        planned = _goal_planned_value(goal, active_items)
        if planned is None:
            unverifiable_goals.append(str(goal.get("title") or "Untitled goal"))
        elif planned < target:
            active_goal_gaps.append({"goal": goal.get("title"), "target": target, "planned": planned})
    if active_goal_gaps:
        add_signal(
            "active_goal_gap", "warning",
            f"{len(active_goal_gaps)} active daily goal(s) are not covered",
            count=len(active_goal_gaps), gaps=active_goal_gaps,
        )
    if unverifiable_goals:
        add_signal(
            "goal_unverifiable", "warning",
            f"{len(unverifiable_goals)} daily goal(s) need a measurable CY, duration, or block metric",
            count=len(unverifiable_goals), goals=unverifiable_goals,
        )
    return {
        "plan_date": plan_date, "items": items, "active_items": active_items,
        "expected_cy": expected_cy,
        "planned_minutes": planned_minutes, "backlog_count": len(backlog),
        "unplanned_backlog_count": len(unplanned_backlog),
        "homework_pending_count": len(current_homework),
        "homework_planned_count": len(current_homework) - len(unplanned_homework),
        "capacity_headroom_cy": max(0, pace["ceiling"] - expected_cy),
        "due_revision_count": len(due_revision),
        "missing_revision_count": len(missing_revision),
        "active_goal_gaps": active_goal_gaps,
        "unverifiable_goals": unverifiable_goals,
        "warnings": warnings, "errors": errors, "signals": signals,
        "structured_warnings": [s for s in signals if s["severity"] == "warning"],
        "structured_errors": [s for s in signals if s["severity"] == "error"],
        "pace": pace,
        "outcome": "blocked" if errors else ("needs_adjustment" if warnings else "ready"),
        "iterations": min(3, 1 + len(errors)),
    }


def next_plan_item(plan_date: str | None = None, *, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, Any] | None:
    facts = plan_facts(plan_date, db_path=db_path)
    for row in facts["items"]:
        if row.get("status") in (None, "Planned", "Active"):
            return row
    return None


def _relation_id(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, list):
        return str(value[0]) if value else None
    text = str(value)
    try:
        decoded = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return text
    if isinstance(decoded, list):
        return str(decoded[0]) if decoded else None
    return str(decoded) if decoded else None


def active_plan(chat_id: int | str, *, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, Any] | None:
    with _connect(db_path) as conn:
        state = conn.execute(
            f"SELECT * FROM {ACTIVE_PLAN_TABLE} WHERE chat_id=?", (str(chat_id),)
        ).fetchone()
        if state is None:
            return None
        plan_item_id = state["plan_item_id"]
        work_item_id = state["work_item_id"]
    item = _row(
        "daily_plan",
        "archived=0 AND (id=? OR notion_page_id=?)",
        (plan_item_id, plan_item_id),
        db_path=db_path,
    )
    if item is None or item.get("status") not in (None, "Planned", "Active"):
        with _connect(db_path) as conn:
            conn.execute(f"DELETE FROM {ACTIVE_PLAN_TABLE} WHERE chat_id=?", (str(chat_id),))
            conn.commit()
        return None
    result = dict(item)
    result["work_item_id"] = work_item_id
    return result


def activate_next_plan(
    chat_id: int | str, plan_date: str | None = None, *, db_path: str | Path = DEFAULT_DB_PATH
) -> dict[str, Any] | None:
    current = active_plan(chat_id, db_path=db_path)
    if current:
        return current
    item = next_plan_item(plan_date, db_path=db_path)
    if not item:
        return None
    work_id = _relation_id(item.get("work_item"))
    if not work_id:
        marker = re.search(
            r"SQLite work item:\s*([a-f0-9]{16,})",
            str(item.get("planner_note") or ""), re.I,
        )
        work_id = marker.group(1) if marker else None
    local_work = (
        _row("work_items", "notion_page_id=?", (work_id,), db_path=db_path)
        if work_id else None
    )
    if local_work is None:
        work = create_work_item({
            "title": item.get("title") or "Plan item",
            "kind": item.get("kind") or "Other",
            "status": "Active",
            "subject": item.get("subject"),
            "source": "Daily Plan",
            "planned_date": item.get("plan_date"),
            "sequence": item.get("sequence"),
            "priority": item.get("priority") or 50,
            "estimated_min": item.get("estimated_min"),
            "expected_cy": item.get("expected_cy"),
            "interruptible": bool(item.get("interruptible", True)),
            "exit_condition": item.get("exit_condition"),
            "operation_id": f"plan-work:{item['notion_page_id']}",
        }, db_path=db_path)
        work_id = str(work.get("id") or "") or None
        if not work_id:
            raise DomainError("could not create a Work Item for this plan row")
    else:
        operational_store.update(
            "work_items", work_id, {"status": "Active"}, db_path=db_path
        )
    existing_note = str(item.get("planner_note") or "")
    marker_note = f"SQLite work item: {work_id}"
    if marker_note not in existing_note:
        existing_note = marker_note + (f"; {existing_note}" if existing_note else "")
    plan_id = str(item.get("id") or item.get("notion_page_id") or "")
    operational_store.update(
        "daily_plan",
        plan_id,
        {"status": "Active", "planner_note": existing_note},
        db_path=db_path,
    )
    with _connect(db_path) as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO {ACTIVE_PLAN_TABLE} "
            "(chat_id, plan_item_id, work_item_id, activated_at) VALUES (?,?,?,?)",
            (str(chat_id), plan_id, work_id, session_context.local_now().isoformat()),
        )
        conn.commit()
    return active_plan(chat_id, db_path=db_path) or {**item, "work_item_id": work_id, "status": "Active"}


def complete_active_plan(
    chat_id: int | str, *, carry_to_backlog: bool = False,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    item = active_plan(chat_id, db_path=db_path)
    if not item:
        raise DomainError("no active plan item")
    plan_status = "Moved" if carry_to_backlog else "Completed"
    plan_id = str(item.get("id") or item.get("notion_page_id") or "")
    operational_store.update("daily_plan", plan_id, {"status": plan_status}, db_path=db_path)
    work_id = item.get("work_item_id")
    if work_id:
        operational_store.update("work_items", work_id, {
            "status": "Backlog" if carry_to_backlog else "Completed",
            "planned_date": None if carry_to_backlog else session_context.local_today_iso(),
        }, db_path=db_path)
    with _connect(db_path) as conn:
        conn.execute(f"DELETE FROM {ACTIVE_PLAN_TABLE} WHERE chat_id=?", (str(chat_id),))
        conn.commit()
    return {"plan_item_id": plan_id, "work_item_id": work_id, "status": plan_status}


def weak_points(*, db_path: str | Path = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    """Merge exam mistakes, doubts and execution exposure into weakness evidence."""
    with _connect(db_path) as conn:
        exam_rows = conn.execute(f"""
            SELECT COALESCE(chapter, '(uncategorised)') AS chapter,
                   COUNT(*) AS mistakes, SUM(COALESCE(marks_lost,0)) AS marks_lost,
                   COUNT(DISTINCT exam) AS exams
            FROM {operational_store.table_for('exam_questions')}
            WHERE archived=0 AND (failure_type IS NOT NULL OR marks_lost > 0)
            GROUP BY COALESCE(chapter, '(uncategorised)')
        """).fetchall()
        doubt_rows = conn.execute("""
            SELECT COALESCE(NULLIF(chapter,''), NULLIF(subject,''), '(uncategorised)') chapter,
                   COUNT(*) unresolved_doubts
            FROM doubts
            WHERE archived=0 AND LOWER(COALESCE(status,''))='unresolved'
            GROUP BY COALESCE(NULLIF(chapter,''), NULLIF(subject,''), '(uncategorised)')
        """).fetchall()
        ledger_rows = conn.execute("""
            SELECT COALESCE(NULLIF(chapter_text,''),
                            COALESCE(subject,'Unknown') || ' / ' || COALESCE(exercise_type,'Unknown')) chapter,
                   COUNT(*) blocks, AVG(accuracy_ratio) avg_accuracy
            FROM ledger
            WHERE archived=0 AND accuracy_ratio IS NOT NULL
            GROUP BY COALESCE(NULLIF(chapter_text,''),
                              COALESCE(subject,'Unknown') || ' / ' || COALESCE(exercise_type,'Unknown'))
        """).fetchall()

    def clean(value: Any) -> str:
        text = str(value or "(uncategorised)")
        try:
            decoded = json.loads(text)
            if isinstance(decoded, list):
                text = ", ".join(str(x) for x in decoded if x)
        except json.JSONDecodeError:
            pass
        return text.strip() or "(uncategorised)"

    merged: dict[str, dict[str, Any]] = {}
    for row in exam_rows:
        key = clean(row["chapter"])
        merged.setdefault(key, {"chapter": key, "mistakes": 0, "marks_lost": 0, "exams": 0, "unresolved_doubts": 0, "blocks": 0, "avg_accuracy": None})
        merged[key].update({"mistakes": row["mistakes"], "marks_lost": row["marks_lost"] or 0, "exams": row["exams"]})
    for row in doubt_rows:
        key = clean(row["chapter"])
        merged.setdefault(key, {"chapter": key, "mistakes": 0, "marks_lost": 0, "exams": 0, "unresolved_doubts": 0, "blocks": 0, "avg_accuracy": None})
        merged[key]["unresolved_doubts"] += row["unresolved_doubts"]
    for row in ledger_rows:
        key = clean(row["chapter"])
        merged.setdefault(key, {"chapter": key, "mistakes": 0, "marks_lost": 0, "exams": 0, "unresolved_doubts": 0, "blocks": 0, "avg_accuracy": None})
        merged[key]["blocks"] += row["blocks"]
        merged[key]["avg_accuracy"] = row["avg_accuracy"]

    result = []
    for item in merged.values():
        accuracy_penalty = 0.0
        if item["avg_accuracy"] is not None and item["blocks"] >= 2:
            accuracy_penalty = max(0.0, 0.75 - float(item["avg_accuracy"])) * item["blocks"] * 20
        score = (
            float(item["marks_lost"]) * 2 + int(item["mistakes"]) * 3
            + int(item["unresolved_doubts"]) * 2 + accuracy_penalty
        )
        evidence = int(item["mistakes"]) + int(item["unresolved_doubts"]) + int(item["blocks"])
        if score <= 0:
            continue
        item["score"] = round(score, 2)
        item["confidence"] = "high" if evidence >= 6 else ("medium" if evidence >= 3 else "low")
        result.append(item)
    return sorted(result, key=lambda row: (-row["score"], row["chapter"]))


def weekly_report(*, end_date: str | None = None, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    end = dt.date.fromisoformat((end_date or session_context.local_today_iso())[:10])
    start = end - dt.timedelta(days=6)
    previous_end = start - dt.timedelta(days=1)
    previous_start = previous_end - dt.timedelta(days=6)
    with _connect(db_path) as conn:
        ledger = conn.execute("""
            SELECT COUNT(*) blocks, COALESCE(SUM(cognitive_yield),0) cy,
                   COALESCE(SUM(questions_attempted),0) attempted,
                   COALESCE(SUM(questions_correct),0) correct,
                   COALESCE(SUM(actual_time_min),0) minutes
            FROM ledger WHERE archived=0 AND substr(COALESCE(date,''),1,10) BETWEEN ? AND ?
        """, (start.isoformat(), end.isoformat())).fetchone()
        previous = conn.execute("""
            SELECT COUNT(*) blocks, COALESCE(SUM(cognitive_yield),0) cy,
                   COALESCE(SUM(questions_attempted),0) attempted,
                   COALESCE(SUM(questions_correct),0) correct,
                   COALESCE(SUM(actual_time_min),0) minutes
            FROM ledger WHERE archived=0 AND substr(COALESCE(date,''),1,10) BETWEEN ? AND ?
        """, (previous_start.isoformat(), previous_end.isoformat())).fetchone()
        completed = conn.execute(f"""
            SELECT COUNT(*) n FROM {operational_store.table_for('work_items')}
            WHERE archived=0 AND status='Completed' AND substr(COALESCE(planned_date,''),1,10) BETWEEN ? AND ?
        """, (start.isoformat(), end.isoformat())).fetchone()["n"]
        backlog = conn.execute(f"""
            SELECT COUNT(*) n FROM {operational_store.table_for('work_items')} WHERE archived=0 AND status IN ('Backlog','Inbox')
        """).fetchone()["n"]
        attempts = conn.execute(f"""
            SELECT COUNT(*) n FROM {operational_store.table_for('doubt_attempts')}
            WHERE archived=0 AND valid=1 AND substr(COALESCE(attempted_at,''),1,10) BETWEEN ? AND ?
        """, (start.isoformat(), end.isoformat())).fetchone()["n"]
        mistakes = conn.execute(f"""
            SELECT COUNT(*) n, COALESCE(SUM(marks_lost),0) marks_lost FROM {operational_store.table_for('exam_questions')}
            WHERE archived=0 AND substr(COALESCE(created_time,''),1,10) BETWEEN ? AND ?
        """, (start.isoformat(), end.isoformat())).fetchone()
        top_failure = conn.execute(f"""
            SELECT failure_type, COUNT(*) n, COALESCE(SUM(marks_lost),0) marks_lost
            FROM {operational_store.table_for('exam_questions')}
            WHERE archived=0 AND failure_type IS NOT NULL
              AND substr(COALESCE(created_time,''),1,10) BETWEEN ? AND ?
            GROUP BY failure_type ORDER BY marks_lost DESC, n DESC LIMIT 1
        """, (start.isoformat(), end.isoformat())).fetchone()
    current_dict, previous_dict = dict(ledger), dict(previous)
    cy_delta = float(current_dict.get("cy") or 0) - float(previous_dict.get("cy") or 0)
    return {
        "start": start.isoformat(), "end": end.isoformat(), "ledger": current_dict,
        "previous": previous_dict, "cy_delta": cy_delta,
        "completed_work": completed, "backlog": backlog, "valid_doubt_attempts": attempts,
        "exam_mistakes": dict(mistakes), "top_failure": dict(top_failure) if top_failure else None,
    }
