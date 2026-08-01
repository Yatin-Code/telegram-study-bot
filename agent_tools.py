"""Constrained tools for the study agent.

The agent never touches raw APIs. It gets a small set of validated tools:

Read tools (executed immediately by the agent loop):
  - get_context()         — active session context (chat_id injected by the loop)
  - get_schema(table?)    — mirror table list / columns + sample rows
  - sql_select(sql)       — validated read-only SELECT (sql_tool.run_sql)

Write tools (validated at preview time, executed only after user confirmation):
  - set_context(...)           — session subject/chapter/block/exercise
  - log_study_session(...)     — Notion Ledger entry (via logging_flow)
  - log_doubt(...)             — Doubts DB entry (via logging_flow)
  - log_revision(...)          — Revision DB entry (via logging_flow)
  - create_goal(...)           — study_domain.create_goal
  - create_exam(...)           — study_domain.create_exam
  - record_exam_result(...)    — study_domain.record_exam_summary
  - create_work_item(...)      — study_domain.create_work_item
  - add_plan_item(...)         — study_domain.create_plan_item
  - create_timetable_entry(...)— study_domain.create_timetable_entry
  - resolve_doubt(...)         — study_domain.resolve_doubt
  - dismiss_doubt(...)         — study_domain.dismiss_doubt
  - schedule_reminder(...)     — user_jobs (validated remind/ask schedule)

Flow for writes:
  1. Agent loop calls prepare_write(name, args, chat_id):
       - validates arguments (enums, numbers, dates, required fields),
       - builds a human preview,
       - returns a serialisable "run" plan.
     Invalid calls return {"ok": False, "error": ..., "hint": ...} (or a
     "clarification" question) — the loop feeds that back to the model so it
     can self-correct before the user ever sees a broken Confirm card.
  2. The loop shows the preview; on confirm it calls
     run_prepared_write(name, run, chat_id) which performs the real write.
     The wrapped domain functions re-validate authoritatively; failures are
     returned as error dicts, never raised into the Telegram layer.
"""

from __future__ import annotations

import datetime as dt
import logging
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Optional

import session_context
import sql_tool
from config import notion_schema
from config.ownership import SQL_OWNED_KEYS as _SQL_OWNED_KEYS

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "sqlite_mirror.db"


class ToolArgError(ValueError):
    """Invalid tool arguments — message is shown to the model for self-correction."""


# ---------------------------------------------------------------------------
# Small validation helpers (mirror study_domain semantics so the preview-time
# check can't diverge from the confirm-time authoritative one)
# ---------------------------------------------------------------------------

def _req_str(args: dict[str, Any], key: str) -> str:
    value = str(args.get(key) or "").strip()
    if not value:
        raise ToolArgError(f"{key} is required")
    return value


def _opt_str(args: dict[str, Any], key: str) -> Optional[str]:
    value = args.get(key)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _enum(key: str, value: Any, options: list[str], *, default: Optional[str] = None) -> Optional[str]:
    if value is None or value == "":
        return default
    text = str(value).strip()
    for option in options:
        if text.lower() == option.lower():
            return option
    raise ToolArgError(
        f"invalid {key}: {value!r}. Valid options: {', '.join(options)}"
    )


def _number(key: str, value: Any, *, required: bool = False) -> Optional[float]:
    if value is None or value == "":
        if required:
            raise ToolArgError(f"{key} is required")
        return None
    try:
        import math
        num = float(value)
    except (TypeError, ValueError):
        raise ToolArgError(f"{key} must be a number, got {value!r}")
    if not math.isfinite(num):
        raise ToolArgError(f"{key} must be a finite number")
    return int(num) if num.is_integer() else num


def _nonneg(key: str, value: Any, *, required: bool = False, integer: bool = False) -> Optional[float]:
    num = _number(key, value, required=required)
    if num is None:
        return None
    if num < 0:
        raise ToolArgError(f"{key} cannot be negative")
    if integer:
        if not float(num).is_integer():
            raise ToolArgError(f"{key} must be a whole number")
        return int(num)
    return num


def _iso_date(key: str, value: Any, *, required: bool = False) -> Optional[str]:
    if value is None or value == "":
        if required:
            raise ToolArgError(f"{key} is required (YYYY-MM-DD)")
        return None
    text = str(value).strip()[:10]
    try:
        dt.date.fromisoformat(text)
    except ValueError:
        raise ToolArgError(f"{key} must be a date (YYYY-MM-DD), got {value!r}")
    return text


def _hhmm(key: str, value: Any, *, required: bool = True) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        if required:
            raise ToolArgError(f"{key} is required (HH:MM)")
        return None
    parts = text.split(":")
    if len(parts) != 2:
        raise ToolArgError(f"{key} must be HH:MM, got {value!r}")
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        raise ToolArgError(f"{key} must be HH:MM, got {value!r}")
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ToolArgError(f"{key} must be a valid 24h time, got {value!r}")
    return f"{h:02d}:{m:02d}"


def _err(exc: Exception) -> dict[str, Any]:
    return {"error": True, "message": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------

def _with_session_conn(db_path: str | Path, fn, *args, **kwargs):
    """Run a session_context function against a specific DB file."""
    conn = session_context.connect(db_path)
    try:
        return fn(*args, conn=conn, **kwargs)
    finally:
        conn.close()


def get_context(chat_id: int | str, *, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    """Return the current session context for a chat (chat_id is injected)."""
    ctx = _with_session_conn(db_path, session_context.get_context, chat_id)
    if ctx is None:
        return {"subject": None, "chapter": None, "block": None, "exercise": None}
    return {
        "subject": ctx.get("subject"),
        "chapter": ctx.get("chapter"),
        "block": ctx.get("block"),
        "exercise": ctx.get("exercise"),
    }


def sql_select(sql: str, *, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    """Execute a validated read-only SELECT against the SQLite mirror.

    Hard-enforced read-only: the connection opens with ?mode=ro and only
    SELECT/WITH...SELECT/PRAGMA statements pass validation, so a mistake
    here can never mutate data (no confirmation needed).
    """
    try:
        return sql_tool.run_sql(sql, db_path=db_path)
    except (sql_tool.SQLRejectedError, sql_tool.SQLExecutionError) as exc:
        logger.warning("sql_select rejected/failed: %s", exc)
        return _err(exc)
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("sql_select unexpected failure")
        return _err(exc)


def get_schema(table: Optional[str] = None, *, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    """Return schema information for SQLite tables.

    If `table` is given, return columns and sample rows for that table.
    If `table` is None, return a list of all tables.

    Legacy bare mirror tables for SQLite-owned domains (goals, work_items,
    exams, exam_questions, doubt_attempts, timetable, daily_plan) are hidden
    from the listing — the agent must use op_<key> for those domains.
    """
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            if table is None:
                rows = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                ).fetchall()
                names = [
                    r["name"] for r in rows if r["name"] not in _SQL_OWNED_KEYS
                ]
                return {"tables": names}

            cols = conn.execute(f"PRAGMA table_info('{table}')").fetchall()
            columns = [
                {"name": r["name"], "type": r["type"], "notnull": r["notnull"], "pk": r["pk"]}
                for r in cols
            ]

            sample_rows: list[dict[str, Any]] = []
            try:
                sample = conn.execute(
                    f'SELECT * FROM "{table}" LIMIT 3'
                ).fetchall()
                sample_rows = [dict(r) for r in sample]
            except Exception:
                pass

            return {"table": table, "columns": columns, "sample_rows": sample_rows}
    except Exception as exc:
        logger.exception("get_schema failed: %s", table)
        return _err(exc)


# ---------------------------------------------------------------------------
# Write tool: set_context  (merge semantics — omitted fields keep their values)
# ---------------------------------------------------------------------------

_CLEAR_TOKENS = {"", "none", "null", "clear", "-"}
_CTX_KEYS = ("subject", "chapter", "block", "exercise")


def _prep_set_context(args: dict[str, Any], chat_id: int | str, db_path: str | Path) -> dict[str, Any]:
    current = get_context(chat_id, db_path=db_path)
    merged: dict[str, Any] = {}
    changes: list[str] = []
    for key in _CTX_KEYS:
        if key in args:
            raw = args.get(key)
            if raw is None or str(raw).strip().lower() in _CLEAR_TOKENS:
                merged[key] = None
                changes.append(f"• {key}: (cleared)")
            else:
                merged[key] = str(raw).strip()
                changes.append(f"• {key}: {merged[key]}")
        else:
            merged[key] = current.get(key)  # keep
    if not changes:
        return {
            "ok": False,
            "error": "nothing to change — pass at least one of subject/chapter/block/exercise",
        }
    preview = "📝 Set session context\n" + "\n".join(changes)
    return {"ok": True, "preview": preview, "run": {"context": merged}}


def _run_set_context(run: dict[str, Any], chat_id: int | str, db_path: str | Path) -> dict[str, Any]:
    return _with_session_conn(db_path, session_context.set_context, chat_id, **run["context"])


# ---------------------------------------------------------------------------
# Write tools: logging flow (Ledger / Doubts / Revision) — full build_write_plan
# validation at preview time (read-only), commit_write at confirm time.
# ---------------------------------------------------------------------------

_LOG_ACTION_BY_TOOL = {
    "log_study_session": "log_execution",
    "log_doubt": "log_doubt",
    "log_revision": "log_revision",
}


def _prep_logging(tool: str, args: dict[str, Any], chat_id: int | str, db_path: str | Path) -> dict[str, Any]:
    import logging_flow
    from intent_parser import Intent, IntentFilters

    action = _LOG_ACTION_BY_TOOL[tool]
    fields = {k: v for k, v in (args or {}).items() if v is not None and v != ""}
    if tool == "log_doubt":
        _req_str(fields, "core_concept")
    if tool == "log_revision":
        _req_str(fields, "chapter_module")
    intent = Intent(
        action=action,
        database=logging_flow.ACTION_DB[action],
        fields=fields,
        filters=IntentFilters(),
    )
    try:
        plan = logging_flow.build_write_plan(intent, chat_id, db_path=db_path)
    except Exception as exc:
        logger.exception("build_write_plan failed for %s", tool)
        return {"ok": False, "error": f"could not prepare the write: {exc}"}
    if plan.needs_clarification:
        return {
            "ok": False,
            "clarification": plan.clarification_question
            or "More information is needed — ask the user for the missing detail.",
        }
    lines = [f"📝 Log ({plan.db_key})"] + plan.preview_lines
    if plan.warnings:
        lines.extend(f"⚠ {w}" for w in plan.warnings)
    return {"ok": True, "preview": "\n".join(lines), "run": {"payload": plan.to_payload()}}


def _run_logging(run: dict[str, Any], chat_id: int | str, db_path: str | Path) -> dict[str, Any]:
    import logging_flow
    return logging_flow.commit_write(run["payload"], db_path=db_path)


# ---------------------------------------------------------------------------
# Write tools: study_domain ops (operational SQLite tables + doubt lifecycle)
# ---------------------------------------------------------------------------

def _preview(title: str, fields: dict[str, Any]) -> str:
    lines = [f"📝 {title}"]
    for key, value in fields.items():
        if key == "operation_id":
            continue
        if value is not None and value != "":
            lines.append(f"• {key}: {value}")
    return "\n".join(lines)


def _prep_create_goal(args: dict[str, Any], chat_id: int | str, db_path: str | Path) -> dict[str, Any]:
    data = {
        "title": _req_str(args, "title"),
        "goal_type": _enum("goal_type", args.get("goal_type"), notion_schema.GOAL_TYPE_OPTIONS, default="Custom"),
        "metric": _opt_str(args, "metric"),
        "target": _nonneg("target", args.get("target"), required=True),
        "minimum": _nonneg("minimum", args.get("minimum")),
        "period": _enum("period", args.get("period"), notion_schema.GOAL_PERIOD_OPTIONS, default="Deadline"),
        "subject": _enum("subject", args.get("subject"), notion_schema.SUBJECT_OPTIONS),
        "deadline": _iso_date("deadline", args.get("deadline")),
        "priority": _nonneg("priority", args.get("priority"), integer=True),
        "notes": _opt_str(args, "notes"),
    }
    if data["minimum"] is not None and data["minimum"] > data["target"]:
        raise ToolArgError("minimum cannot exceed target")
    # Stamp now so a double-confirm of the same prepared write is a no-op
    # (operational_store.create dedupes on operation_id).
    data["operation_id"] = f"agent-goal:{uuid.uuid4().hex}"
    return {"ok": True, "preview": _preview("Create goal", data), "run": {"data": data}}


def _prep_create_exam(args: dict[str, Any], chat_id: int | str, db_path: str | Path) -> dict[str, Any]:
    data = {
        "title": _req_str(args, "title"),
        "kind": _enum("kind", args.get("kind"), notion_schema.EXAM_KIND_OPTIONS, default="Other"),
        "exam_date": _iso_date("exam_date", args.get("exam_date"), required=True),
        "date_confidence": _enum("date_confidence", args.get("date_confidence"), notion_schema.DATE_CONFIDENCE_OPTIONS, default="Tentative"),
        "syllabus": _opt_str(args, "syllabus"),
        "max_marks": _nonneg("max_marks", args.get("max_marks")),
        "target_marks": _nonneg("target_marks", args.get("target_marks")),
        "notes": _opt_str(args, "notes"),
    }
    if data["max_marks"] is not None and data["target_marks"] is not None and data["target_marks"] > data["max_marks"]:
        raise ToolArgError("target_marks cannot exceed max_marks")
    data["operation_id"] = f"agent-exam:{uuid.uuid4().hex}"
    return {"ok": True, "preview": _preview("Create exam", data), "run": {"data": data}}


def _prep_record_exam_result(args: dict[str, Any], chat_id: int | str, db_path: str | Path) -> dict[str, Any]:
    import study_domain
    exam = _req_str(args, "exam")
    data = {
        "actual_marks": _number("actual_marks", args.get("actual_marks"), required=True),
        "attempted": _nonneg("attempted", args.get("attempted"), integer=True),
        "correct": _nonneg("correct", args.get("correct"), integer=True),
        "incorrect": _nonneg("incorrect", args.get("incorrect"), integer=True),
        "unattempted": _nonneg("unattempted", args.get("unattempted"), integer=True),
    }
    if data["attempted"] is not None and data["correct"] is not None and data["correct"] > data["attempted"]:
        raise ToolArgError("correct cannot exceed attempted")
    try:
        match = study_domain._title_match("exams", exam, db_path=db_path)
    except Exception as exc:
        raise ToolArgError(str(exc))
    if match is None:
        raise ToolArgError(f"no exam matches {exam!r}")
    preview = _preview(f"Record result — {exam}", data)
    return {"ok": True, "preview": preview, "run": {"exam": exam, "data": data}}


def _prep_create_work_item(args: dict[str, Any], chat_id: int | str, db_path: str | Path) -> dict[str, Any]:
    data = {
        "title": _req_str(args, "title"),
        "kind": _enum("kind", args.get("kind"), notion_schema.WORK_KIND_OPTIONS, default="Other"),
        "subject": _enum("subject", args.get("subject"), notion_schema.SUBJECT_OPTIONS),
        "due_date": _iso_date("due_date", args.get("due_date")),
        "priority": _nonneg("priority", args.get("priority"), integer=True),
        "estimated_min": _nonneg("estimated_min", args.get("estimated_min"), integer=True),
    }
    data["operation_id"] = f"agent-work:{uuid.uuid4().hex}"
    return {"ok": True, "preview": _preview("Create backlog item", data), "run": {"data": data}}


def _prep_add_plan_item(args: dict[str, Any], chat_id: int | str, db_path: str | Path) -> dict[str, Any]:
    sequence = _nonneg("sequence", args.get("sequence"), integer=True)
    plan_date = _iso_date("plan_date", args.get("plan_date")) or session_context.local_today_iso()
    data = {
        "title": _req_str(args, "title"),
        "plan_date": plan_date,
        "sequence": 0 if sequence is None else sequence,
        "subject": _enum("subject", args.get("subject"), notion_schema.SUBJECT_OPTIONS),
        "kind": _enum("kind", args.get("kind"), notion_schema.WORK_KIND_OPTIONS),
        "estimated_min": _nonneg("estimated_min", args.get("estimated_min"), integer=True),
    }
    data["operation_id"] = f"agent-plan:{uuid.uuid4().hex}"
    return {"ok": True, "preview": _preview("Add to daily plan", data), "run": {"data": data}}


def _prep_create_timetable_entry(args: dict[str, Any], chat_id: int | str, db_path: str | Path) -> dict[str, Any]:
    start = _hhmm("start_time", args.get("start_time"))
    end = _hhmm("end_time", args.get("end_time"))
    if end <= start:
        raise ToolArgError("end_time must be after start_time")
    data = {
        "subject": _enum("subject", args.get("subject"), notion_schema.SUBJECT_OPTIONS),
        "weekday": _enum("weekday", args.get("weekday"), notion_schema.WEEKDAY_OPTIONS),
        "start_time": start,
        "end_time": end,
        "kind": _enum("kind", args.get("kind"), notion_schema.TIMETABLE_KIND_OPTIONS, default="Class"),
        "title": _opt_str(args, "title"),
        "teacher": _opt_str(args, "teacher"),
        "location": _opt_str(args, "location"),
    }
    if data["subject"] is None:
        raise ToolArgError("subject is required. Valid options: " + ", ".join(notion_schema.SUBJECT_OPTIONS))
    if data["weekday"] is None:
        raise ToolArgError("weekday is required. Valid options: " + ", ".join(notion_schema.WEEKDAY_OPTIONS))
    data["operation_id"] = f"agent-tt:{uuid.uuid4().hex}"
    return {"ok": True, "preview": _preview("Create timetable entry", data), "run": {"data": data}}


def _check_doubt_match(query: str, db_path: str | Path) -> None:
    import study_domain
    try:
        row = study_domain._title_match("doubts", query, db_path=db_path)
    except Exception as exc:
        raise ToolArgError(str(exc))
    if row is None:
        raise ToolArgError(f"no doubt matches {query!r}")


def _prep_resolve_doubt(args: dict[str, Any], chat_id: int | str, db_path: str | Path) -> dict[str, Any]:
    doubt = _req_str(args, "doubt")
    resolution = _req_str(args, "resolution")
    _check_doubt_match(doubt, db_path)
    run = {
        "doubt": doubt,
        "resolution": resolution,
        "teacher_asked": bool(args.get("teacher_asked", False)),
    }
    return {"ok": True, "preview": _preview(f"Resolve doubt — {doubt}", {"resolution": resolution, "teacher_asked": run["teacher_asked"]}), "run": run}


def _prep_dismiss_doubt(args: dict[str, Any], chat_id: int | str, db_path: str | Path) -> dict[str, Any]:
    doubt = _req_str(args, "doubt")
    reason = _req_str(args, "reason")
    _check_doubt_match(doubt, db_path)
    run = {"doubt": doubt, "reason": reason}
    return {"ok": True, "preview": _preview(f"Dismiss doubt — {doubt}", {"reason": reason}), "run": run}


def _run_domain(fn, *fn_args, **fn_kwargs) -> dict[str, Any]:
    """Run a study_domain op, converting failures into error dicts."""
    import study_domain
    try:
        result = fn(*fn_args, **fn_kwargs)
    except study_domain.DomainError as exc:
        return {"error": True, "message": str(exc)}
    except Exception as exc:
        logger.exception("domain write failed: %s", fn)
        return _err(exc)
    if isinstance(result, dict):
        return result
    return {"status": "saved", "result": str(result)}


def _run_create_goal(run: dict[str, Any], chat_id: int | str, db_path: str | Path) -> dict[str, Any]:
    import study_domain
    return _run_domain(study_domain.create_goal, run["data"], db_path=db_path)


def _run_create_exam(run: dict[str, Any], chat_id: int | str, db_path: str | Path) -> dict[str, Any]:
    import study_domain
    return _run_domain(study_domain.create_exam, run["data"], db_path=db_path)


def _run_record_exam_result(run: dict[str, Any], chat_id: int | str, db_path: str | Path) -> dict[str, Any]:
    import study_domain
    return _run_domain(study_domain.record_exam_summary, run["exam"], run["data"], db_path=db_path)


def _run_create_work_item(run: dict[str, Any], chat_id: int | str, db_path: str | Path) -> dict[str, Any]:
    import study_domain
    return _run_domain(study_domain.create_work_item, run["data"], db_path=db_path)


def _run_add_plan_item(run: dict[str, Any], chat_id: int | str, db_path: str | Path) -> dict[str, Any]:
    import study_domain
    return _run_domain(study_domain.create_plan_item, run["data"], db_path=db_path)


def _run_create_timetable_entry(run: dict[str, Any], chat_id: int | str, db_path: str | Path) -> dict[str, Any]:
    import study_domain
    return _run_domain(study_domain.create_timetable_entry, run["data"], db_path=db_path)


def _run_resolve_doubt(run: dict[str, Any], chat_id: int | str, db_path: str | Path) -> dict[str, Any]:
    import study_domain
    return _run_domain(
        study_domain.resolve_doubt, run["doubt"], run["resolution"],
        teacher_asked=run["teacher_asked"], db_path=db_path,
    )


def _run_dismiss_doubt(run: dict[str, Any], chat_id: int | str, db_path: str | Path) -> dict[str, Any]:
    import study_domain
    return _run_domain(study_domain.dismiss_doubt, run["doubt"], run["reason"], db_path=db_path)


# ---------------------------------------------------------------------------
# Write tool: schedule_reminder (user_jobs)
# ---------------------------------------------------------------------------

def _prep_schedule_reminder(args: dict[str, Any], chat_id: int | str, db_path: str | Path) -> dict[str, Any]:
    import user_jobs

    parsed = {
        "title": _opt_str(args, "title"),
        "schedule_kind": _opt_str(args, "schedule_kind"),
        "time": _opt_str(args, "time"),
        "weekday": args.get("weekday"),
        "date": _opt_str(args, "date"),
        "action_kind": _opt_str(args, "action_kind"),
        "action_text": _opt_str(args, "action_text"),
    }
    data, err = user_jobs.validate_parsed(parsed)
    if err:
        raise ToolArgError(err)

    overlaps = user_jobs.builtin_overlaps(data)
    lines = [
        "📝 Schedule reminder",
        f"• {user_jobs.describe(dict(data, enabled=1))}",
    ]
    for warning in overlaps:
        lines.append(f"⚠ {warning}")
    return {"ok": True, "preview": "\n".join(lines), "run": {"data": data}}


def _run_schedule_reminder(run: dict[str, Any], chat_id: int | str, db_path: str | Path) -> dict[str, Any]:
    import user_jobs
    import reminders

    try:
        job = user_jobs.create_job(int(chat_id), run["data"], db_path=db_path)
    except ValueError as exc:
        return {"error": True, "message": str(exc)}
    # A slot that already passed today shouldn't fire at the next 60s scan.
    now = session_context.local_now()
    if user_jobs.should_preclaim_today(job, now):
        reminders.claim(f"user-job:{job['id']}:{now.date().isoformat()}", db_path=db_path)
    return {"status": "saved", "job": user_jobs.describe(job)}


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

_PREP_HANDLERS = {
    "set_context": _prep_set_context,
    "log_study_session": lambda a, c, d: _prep_logging("log_study_session", a, c, d),
    "log_doubt": lambda a, c, d: _prep_logging("log_doubt", a, c, d),
    "log_revision": lambda a, c, d: _prep_logging("log_revision", a, c, d),
    "create_goal": _prep_create_goal,
    "create_exam": _prep_create_exam,
    "record_exam_result": _prep_record_exam_result,
    "create_work_item": _prep_create_work_item,
    "add_plan_item": _prep_add_plan_item,
    "create_timetable_entry": _prep_create_timetable_entry,
    "resolve_doubt": _prep_resolve_doubt,
    "dismiss_doubt": _prep_dismiss_doubt,
    "schedule_reminder": _prep_schedule_reminder,
}

_RUN_HANDLERS = {
    "set_context": _run_set_context,
    "log_study_session": _run_logging,
    "log_doubt": _run_logging,
    "log_revision": _run_logging,
    "create_goal": _run_create_goal,
    "create_exam": _run_create_exam,
    "record_exam_result": _run_record_exam_result,
    "create_work_item": _run_create_work_item,
    "add_plan_item": _run_add_plan_item,
    "create_timetable_entry": _run_create_timetable_entry,
    "resolve_doubt": _run_resolve_doubt,
    "dismiss_doubt": _run_dismiss_doubt,
    "schedule_reminder": _run_schedule_reminder,
}

WRITE_TOOLS = frozenset(_PREP_HANDLERS)
READ_TOOLS = frozenset({"get_context", "get_schema", "sql_select"})


def prepare_write(
    name: str,
    arguments: dict[str, Any],
    *,
    chat_id: int | str,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Validate a write call and build its preview + run plan.

    Returns {"ok": True, "preview": str, "run": {...}} or
    {"ok": False, "error"/"clarification": str}. Never raises.
    """
    handler = _PREP_HANDLERS.get(name)
    if handler is None:
        return {"ok": False, "error": f"unknown write tool: {name}"}
    try:
        return handler(dict(arguments or {}), chat_id, Path(db_path))
    except ToolArgError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        logger.exception("prepare_write failed for %s", name)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def run_prepared_write(
    name: str,
    run: dict[str, Any],
    *,
    chat_id: int | str,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Execute a previously prepared (user-confirmed) write. Never raises."""
    handler = _RUN_HANDLERS.get(name)
    if handler is None:
        return {"error": True, "message": f"unknown write tool: {name}"}
    try:
        return handler(dict(run or {}), chat_id, Path(db_path))
    except Exception as exc:
        logger.exception("run_prepared_write failed for %s", name)
        return _err(exc)


# ---------------------------------------------------------------------------
# Tool specs (system prompt) — enums inlined so the model is steered before
# validation ever runs.
# ---------------------------------------------------------------------------

TOOL_SPECS = [
    {
        "name": "get_context",
        "description": (
            "Get the current study session context (subject, chapter, block, exercise). "
            "No arguments — the chat is identified automatically."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "get_schema",
        "description": (
            "Inspect the local SQLite mirror schema. Call with no arguments to list all tables, "
            "or with a table name for columns + 3 sample rows. Use before writing SQL."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "table": {"type": "string", "description": "Table name to inspect, or omit to list all tables."},
            },
        },
    },
    {
        "name": "sql_select",
        "description": (
            "Read-only SELECT query against the local SQLite mirror (ledger, doubts, revision, "
            "op_work_items, op_goals, op_exams, op_daily_plan, op_timetable, user_jobs, commitments, ...). "
            "Use only columns from the schema block or get_schema. Always filter archived = 0 "
            "unless the user asks for archived entries."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "A single SELECT (or WITH ... SELECT) statement."},
            },
            "required": ["sql"],
        },
    },
    {
        "name": "set_context",
        "description": (
            "Update the current study session context. Omitted fields keep their current values; "
            "pass \"none\" to clear a field. The user confirms before it applies."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "chapter": {"type": "string"},
                "block": {"type": "string"},
                "exercise": {"type": "string"},
            },
        },
    },
    {
        "name": "log_study_session",
        "description": (
            "Log a study session to the Ledger. Session context (subject/chapter/block/exercise) "
            "is merged automatically — only pass fields the user stated. Include 'doubts' text to "
            "cross-log a doubt raised during the session. The user confirms before it saves."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "What was done (verb + scope)"},
                "subject": {"type": "string", "enum": sorted(notion_schema.SUBJECT_OPTIONS)},
                "chapter": {"type": "string"},
                "block": {"type": "string"},
                "exercise_type": {"type": "string"},
                "questions_attempted": {"type": "integer"},
                "questions_correct": {"type": "integer"},
                "actual_time_min": {"type": "integer"},
                "date": {"type": "string", "description": "YYYY-MM-DD, 'today' or 'tomorrow'"},
                "doubts": {"type": "string", "description": "Doubt text to cross-log"},
                "key_points_notes": {"type": "string"},
            },
        },
    },
    {
        "name": "log_doubt",
        "description": (
            "Log a doubt / error. It is auto-linked to the most likely ledger session; "
            "pass ledger_entry (session title) to link explicitly. Confirmed before saving."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "core_concept": {"type": "string", "description": "The exact concept that is unclear"},
                "ledger_entry": {"type": "string", "description": "Optional ledger session title to link"},
            },
            "required": ["core_concept"],
        },
    },
    {
        "name": "log_revision",
        "description": "Log a revision of a chapter/module. Confirmed before saving.",
        "parameters": {
            "type": "object",
            "properties": {
                "chapter_module": {"type": "string"},
                "subject": {"type": "string", "enum": sorted(notion_schema.SUBJECT_OPTIONS)},
                "exercises": {"type": "string"},
                "status": {"type": "string", "enum": sorted(notion_schema.REVISION_STATUS_OPTIONS)},
                "mastery": {"type": "string", "enum": sorted(notion_schema.MASTERY_STATUS_OPTIONS)},
                "next_execution_date": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": ["chapter_module"],
        },
    },
    {
        "name": "create_goal",
        "description": "Create a measurable goal. Confirmed before saving.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "goal_type": {"type": "string", "enum": sorted(notion_schema.GOAL_TYPE_OPTIONS)},
                "target": {"type": "number", "description": "The numeric target (e.g. 300 questions, 2 hours)"},
                "metric": {"type": "string", "description": "Unit, e.g. 'questions', 'hours'. Defaults to goal_type."},
                "period": {"type": "string", "enum": sorted(notion_schema.GOAL_PERIOD_OPTIONS)},
                "subject": {"type": "string", "enum": sorted(notion_schema.SUBJECT_OPTIONS)},
                "deadline": {"type": "string", "description": "YYYY-MM-DD"},
                "minimum": {"type": "number"},
                "priority": {"type": "integer", "description": "0-100"},
                "notes": {"type": "string"},
            },
            "required": ["title", "target"],
        },
    },
    {
        "name": "create_exam",
        "description": "Schedule an exam / mock test. Confirmed before saving.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "kind": {"type": "string", "enum": sorted(notion_schema.EXAM_KIND_OPTIONS)},
                "exam_date": {"type": "string", "description": "YYYY-MM-DD"},
                "date_confidence": {"type": "string", "enum": sorted(notion_schema.DATE_CONFIDENCE_OPTIONS)},
                "syllabus": {"type": "string"},
                "max_marks": {"type": "number"},
                "target_marks": {"type": "number"},
                "notes": {"type": "string"},
            },
            "required": ["title", "exam_date"],
        },
    },
    {
        "name": "record_exam_result",
        "description": (
            "Record the result of an existing exam (fuzzy name match). "
            "Marks the exam as Analysing. Confirmed before saving."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "exam": {"type": "string", "description": "Exam title (fuzzy matched)"},
                "actual_marks": {"type": "number"},
                "attempted": {"type": "integer"},
                "correct": {"type": "integer"},
                "incorrect": {"type": "integer"},
                "unattempted": {"type": "integer"},
            },
            "required": ["exam", "actual_marks"],
        },
    },
    {
        "name": "create_work_item",
        "description": "Add an item to the work backlog. Confirmed before saving.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "kind": {"type": "string", "enum": sorted(notion_schema.WORK_KIND_OPTIONS)},
                "subject": {"type": "string", "enum": sorted(notion_schema.SUBJECT_OPTIONS)},
                "due_date": {"type": "string", "description": "YYYY-MM-DD"},
                "priority": {"type": "integer", "description": "0-100"},
                "estimated_min": {"type": "integer"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "add_plan_item",
        "description": "Add an item to a day's plan (default today). Confirmed before saving.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "plan_date": {"type": "string", "description": "YYYY-MM-DD, defaults to today"},
                "sequence": {"type": "integer", "description": "Order in the day, default 0"},
                "subject": {"type": "string", "enum": sorted(notion_schema.SUBJECT_OPTIONS)},
                "kind": {"type": "string", "enum": sorted(notion_schema.WORK_KIND_OPTIONS)},
                "estimated_min": {"type": "integer"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "create_timetable_entry",
        "description": "Add a weekly timetable slot. Confirmed before saving.",
        "parameters": {
            "type": "object",
            "properties": {
                "subject": {"type": "string", "enum": sorted(notion_schema.SUBJECT_OPTIONS)},
                "weekday": {"type": "string", "enum": sorted(notion_schema.WEEKDAY_OPTIONS)},
                "start_time": {"type": "string", "description": "HH:MM"},
                "end_time": {"type": "string", "description": "HH:MM"},
                "kind": {"type": "string", "enum": sorted(notion_schema.TIMETABLE_KIND_OPTIONS)},
                "title": {"type": "string"},
                "teacher": {"type": "string"},
                "location": {"type": "string"},
            },
            "required": ["subject", "weekday", "start_time", "end_time"],
        },
    },
    {
        "name": "resolve_doubt",
        "description": (
            "Resolve an existing doubt (fuzzy name match) with evidence. "
            "teacher_asked=true requires two prior valid attempts. Confirmed before saving."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "doubt": {"type": "string", "description": "Doubt title (fuzzy matched)"},
                "resolution": {"type": "string", "description": "How it was resolved (evidence)"},
                "teacher_asked": {"type": "boolean"},
            },
            "required": ["doubt", "resolution"],
        },
    },
    {
        "name": "dismiss_doubt",
        "description": "Dismiss an existing doubt as not-worth-solving. Confirmed before saving.",
        "parameters": {
            "type": "object",
            "properties": {
                "doubt": {"type": "string", "description": "Doubt title (fuzzy matched)"},
                "reason": {"type": "string"},
            },
            "required": ["doubt", "reason"],
        },
    },
    {
        "name": "schedule_reminder",
        "description": (
            "Schedule a reminder/job: a message at a time ('message') or a question the bot asks "
            "and answers from your data ('ask'). Confirmed before saving."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "schedule_kind": {"type": "string", "enum": ["daily", "weekdays", "weekly", "once"]},
                "time": {"type": "string", "description": "HH:MM (24h)"},
                "weekday": {"type": "integer", "description": "weekly only: 0=Mon … 6=Sun"},
                "date": {"type": "string", "description": "once only: YYYY-MM-DD (not in the past)"},
                "action_kind": {"type": "string", "enum": ["ask", "message"]},
                "action_text": {"type": "string", "description": "What to ask or say"},
            },
            "required": ["schedule_kind", "time", "action_kind", "action_text"],
        },
    },
]


def execute_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    chat_id: int | str = "",
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Execute a READ tool by name. Writes must go through prepare/preview/confirm."""
    try:
        if name == "get_context":
            return get_context(chat_id, db_path=db_path)
        if name == "get_schema":
            return get_schema(db_path=db_path, **dict(arguments or {}))
        if name == "sql_select":
            return sql_select(db_path=db_path, **dict(arguments or {}))
        if name in WRITE_TOOLS:
            return {
                "error": True,
                "message": f"{name} is a write tool — the runtime handles it via the "
                           "confirmation preview. Do not try to execute it directly.",
            }
        return {"error": True, "message": f"Unknown tool: {name}"}
    except TypeError as exc:
        # Bad argument names/types from the model — feed back for self-correction.
        return {"error": True, "message": f"invalid arguments for {name}: {exc}"}
