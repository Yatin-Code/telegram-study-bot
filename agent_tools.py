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
import json
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

# Internal/system tables that must never be advertised to the LLM via
# get_schema.  These hold user transcripts, pending writes, draft state,
# health/probe data, or sync metadata — none are business tables the agent
# should query.  A prompt-injection via Notion page_content could otherwise
# steer the LLM to read other chats' pending states or write payloads.
_INTERNAL_TABLES = frozenset({
    "agent_pending_states",
    "drafts",
    "pending_clarifications",
    "pending_writes",
    "chat_qa_history",
    "bug_reports",
    "pending_doubt_resolutions",
    "pending_session_debrief",
    "pending_setting_edits",
    "reset_confirmations",
    "operational_schema_meta",
    "sync_meta",
    "reminder_events",
    "reminder_deliveries",
    "coaching_sync_runs",
    "llm_model_health",
    "llm_route_state",
    "llm_requests",
    "conversation_history",
    "chat_context",
    "commitment_checks",
    "op_execution_links",
    "sqlite_sequence",
})


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
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            names = [r["name"] for r in rows]
            visible = [n for n in names if n not in _SQL_OWNED_KEYS and n not in _INTERNAL_TABLES]
            if table is None:
                return {"tables": visible}

            allowed = set(visible)
            if table not in allowed:
                logger.warning("get_schema rejected non-whitelisted table %r", table)
                return {"error": True, "message": f"unknown table: {table}"}

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
# JEE analytics read tools (read-only, parameterized SELECTs only)
# ---------------------------------------------------------------------------

def _jee_conn(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def get_jee_patterns(
    subject: str | None = None, chapter: str | None = None, limit: int = 10,
    *, db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Top repeating JEE patterns, filtered by subject/chapter, by frequency."""
    try:
        limit = max(1, min(int(limit), 20))
    except (TypeError, ValueError):
        limit = 10
    where = []
    params: list[Any] = []
    if subject:
        where.append("LOWER(subject) = LOWER(?)")
        params.append(str(subject).strip())
    if chapter:
        where.append("LOWER(chapter) = LOWER(?)")
        params.append(str(chapter).strip())
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    try:
        with _jee_conn(db_path) as conn:
            rows = conn.execute(
                f"SELECT pattern_id, subject, chapter, sub_topic, frequency, "
                f"years_json, exams_json, core_concept, key_formula, common_trap, "
                f"difficulty, question_type FROM op_jee_patterns{clause} "
                f"ORDER BY frequency DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
    except sqlite3.Error as exc:
        logger.warning("get_jee_patterns failed: %s", exc)
        return _err(exc)
    return {
        "patterns": [
            {
                "pattern_id": row["pattern_id"],
                "subject": row["subject"],
                "chapter": row["chapter"],
                "sub_topic": row["sub_topic"],
                "frequency": row["frequency"],
                "years": _load_json(row["years_json"]),
                "exams": _load_json(row["exams_json"]),
                "core_concept": row["core_concept"],
                "key_formula": row["key_formula"],
                "common_trap": row["common_trap"],
                "difficulty": row["difficulty"],
                "question_type": row["question_type"],
            }
            for row in rows
        ],
    }


def get_chapter_roi(
    exam_type: str = "mains", subject: str | None = None, limit: int = 10,
    *, db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Chapters ranked by importance (ROI) for an exam type."""
    try:
        limit = max(1, min(int(limit), 20))
    except (TypeError, ValueError):
        limit = 10
    exam_type = str(exam_type or "mains").strip().lower()
    where = ["LOWER(exam_type) = LOWER(?)", "LOWER(COALESCE(chapter,'')) <> 'unclassified'"]
    params: list[Any] = [exam_type]
    if subject:
        where.append("LOWER(subject) = LOWER(?)")
        params.append(str(subject).strip())
    clause = " WHERE " + " AND ".join(where)
    try:
        with _jee_conn(db_path) as conn:
            rows = conn.execute(
                f"SELECT subject, chapter, exam_type, total_questions, repeat_ratio, "
                f"easy_ratio, importance_score FROM op_jee_chapter_stats{clause} "
                f"ORDER BY importance_score DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
    except sqlite3.Error as exc:
        logger.warning("get_chapter_roi failed: %s", exc)
        return _err(exc)
    return {
        "chapters": [
            {
                "subject": row["subject"],
                "chapter": row["chapter"],
                "exam_type": row["exam_type"],
                "total_questions": row["total_questions"],
                "repeat_ratio": row["repeat_ratio"],
                "easy_ratio": row["easy_ratio"],
                "importance_score": row["importance_score"],
            }
            for row in rows
        ],
    }


def get_exam_weightage(
    subject: str | None = None, chapter: str | None = None,
    *, db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Per-chapter weightage (SUM of total_questions) across exam types."""
    where = ["LOWER(COALESCE(chapter,'')) <> 'unclassified'"]
    params: list[Any] = []
    if subject:
        where.append("LOWER(subject) = LOWER(?)")
        params.append(str(subject).strip())
    if chapter:
        where.append("LOWER(chapter) = LOWER(?)")
        params.append(str(chapter).strip())
    clause = " WHERE " + " AND ".join(where)
    try:
        with _jee_conn(db_path) as conn:
            rows = conn.execute(
                f"SELECT subject, chapter, SUM(total_questions) AS total_questions "
                f"FROM op_jee_chapter_stats{clause} GROUP BY subject, chapter "
                f"ORDER BY total_questions DESC",
                params,
            ).fetchall()
    except sqlite3.Error as exc:
        logger.warning("get_exam_weightage failed: %s", exc)
        return _err(exc)
    return {
        "weightage": [
            {
                "subject": row["subject"],
                "chapter": row["chapter"],
                "total_questions": row["total_questions"],
            }
            for row in rows
        ],
    }


def _load_json(value: Any) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


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
        plan = logging_flow.build_write_plan(intent, chat_id, db_path=db_path, first_round=False)
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


def _prep_record_doubt_attempt(args: dict[str, Any], chat_id: int | str, db_path: str | Path) -> dict[str, Any]:
    """Preview a durable doubt-attempt write (confirmed before it runs)."""
    import study_domain
    doubt = _req_str(args, "doubt")
    _check_doubt_match(doubt, db_path)
    duration = _number("duration_min", args.get("duration_min"), required=True)
    if duration is None or duration < 1:
        raise ToolArgError("duration_min must be at least 1")
    data = {
        "duration_min": int(duration),
        "approach": _req_str(args, "approach"),
        "stuck_point": _req_str(args, "stuck_point"),
        "outcome": _enum("outcome", args.get("outcome"), notion_schema.ATTEMPT_OUTCOME_OPTIONS, default="Unsolved"),
    }
    preview = _preview(f"Record doubt attempt — {doubt}", data)
    return {"ok": True, "preview": preview, "run": {"doubt": doubt, **data}}


def _run_record_doubt_attempt(run: dict[str, Any], chat_id: int | str, db_path: str | Path) -> dict[str, Any]:
    import study_domain
    return _run_domain(
        study_domain.record_doubt_attempt,
        run["doubt"],
        duration_min=run["duration_min"],
        approach=run["approach"],
        stuck_point=run["stuck_point"],
        outcome=run["outcome"],
        db_path=db_path,
    )


# ---------------------------------------------------------------------------
# Write tool: update_progress (coaching_progress — evidence-aware chapter/topic
# progress). Preview via prepare_progress_write, confirm via run_progress_write.
# ---------------------------------------------------------------------------

def _prep_update_progress(args: dict[str, Any], chat_id: int | str, db_path: str | Path) -> dict[str, Any]:
    import coaching_progress
    record = {k: v for k, v in (args or {}).items() if v is not None and v != ""}
    result = coaching_progress.prepare_progress_write(record, db_path=db_path)
    if not result.get("ok"):
        return {"ok": False, "error": "; ".join(result.get("errors") or ["invalid progress record"])}
    return {
        "ok": True,
        "preview": result["preview"],
        "run": {"record": result["run"]["record"]},
    }


def _run_update_progress(run: dict[str, Any], chat_id: int | str, db_path: str | Path) -> dict[str, Any]:
    import coaching_progress
    result = coaching_progress.run_progress_write(run, db_path=db_path)
    if not result.get("ok"):
        return {"error": True, "message": "; ".join(result.get("errors") or ["progress write failed"])}
    return result


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
    "record_doubt_attempt": _prep_record_doubt_attempt,
    "update_progress": _prep_update_progress,
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
    "record_doubt_attempt": _run_record_doubt_attempt,
    "update_progress": _run_update_progress,
    "schedule_reminder": _run_schedule_reminder,
}

WRITE_TOOLS = frozenset(_PREP_HANDLERS)
READ_TOOLS = frozenset({
    "get_context", "get_schema", "sql_select", "get_coaching_schedule",
    "get_coaching_snapshot", "get_upcoming_syllabus", "get_next_class",
    "get_plan_suggestions", "get_chapter_progress", "get_next_doubt",
    "get_doubt_interaction", "get_score_prediction", "get_backlog_status",
    "get_today_blocks", "get_current_block", "get_chapter_classifications",
    "get_jee_patterns", "get_chapter_roi", "get_exam_weightage",
})


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
        "name": "get_coaching_schedule",
        "description": (
            "Read the synced Narayana coaching timetable for an exact ISO date. "
            "Use this for today, tomorrow, or relative-date class questions after "
            "resolving the date. Returns freshness and class times."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "Target date, YYYY-MM-DD"},
            },
            "required": ["date"],
        },
    },
    {
        "name": "get_coaching_snapshot",
        "description": (
            "Read the compact current Narayana coaching snapshot: profile, today's "
            "and tomorrow's classes, next tests, latest result, and sync freshness."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "get_upcoming_syllabus",
        "description": (
            "Read upcoming coaching tests with their normalized syllabus topics and "
            "per-topic coverage flags (covered / has_doubt) matched against the ledger "
            "and doubts mirror. Use for 'what is in the next test', coverage, or "
            "progress-on-syllabus questions."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "How many upcoming tests to include (default 5)"},
            },
        },
    },
    {
        "name": "get_next_class",
        "description": (
            "Read the next upcoming coaching classes (and their times) from the synced "
            "timetable. Use for 'when is the next class / what is next on the schedule'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "How many upcoming classes to include (default 3)"},
            },
        },
    },
    {
        "name": "get_plan_suggestions",
        "description": (
            "Build deterministic read-only coaching plan suggestions for tomorrow (or a "
            "wider window): fixed classes, pre/post-class prep, revision, homework, test "
            "prep, and doubt work placed into free time. Returns blocks, unplaced items "
            "with reasons, per-day capacity, and warnings. Use for 'plan my day/week' or "
            "'what should I do tomorrow'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Number of days to plan (default 1 = tomorrow)"},
                "start_date": {"type": "string", "description": "Optional start date YYYY-MM-DD (default tomorrow)"},
            },
        },
    },
    {
        "name": "get_chapter_progress",
        "description": (
            "Read deterministic chapter/topic coverage across upcoming coaching tests "
            "and the evidence-aware progress rows (coverage summary + missing questions). "
            "Use for 'how much of the syllabus have I covered', progress gaps, or what "
            "progress data is still missing. Read-only; nothing is written."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "How many upcoming tests to include (default 5)"},
            },
        },
    },
    {
        "name": "get_chapter_classifications",
        "description": (
            "Read the confirmed chapter classifications (mastery/revision/hard) "
            "with the per-chapter accuracy ratio and cognitive yield that drove "
            "each tag, plus any still-pending proposals. Use for 'which chapters "
            "are done and how did I do on them'. Read-only; nothing is written."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "get_next_doubt",
        "description": (
            "Read the deterministic next-doubt ranking: the highest-priority open doubt "
            "plus a short queue, each with its bucket, reason and confidence. Use for "
            "'which doubt should I work on next'. Read-only."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "get_doubt_interaction",
        "description": (
            "Begin (or resume) a local doubt-attempt interaction session for one open "
            "doubt_id (from get_next_doubt). Returns the session state and prompt. This "
            "starts a local interaction only — durable attempt/resolution writes use the "
            "confirmed write tools record_doubt_attempt / resolve_doubt / dismiss_doubt."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "doubt_id": {"type": "string", "description": "Doubt id from get_next_doubt"},
            },
            "required": ["doubt_id"],
        },
    },
    {
        "name": "get_score_prediction",
        "description": (
            "Read a deterministic bounded score-range projection for the nearest upcoming "
            "coaching test (normalized to percentages, with conservative/likely/stretch "
            "bands, confidence and bounded actions). Never claims a rank/AIR. Read-only; "
            "nothing is stored."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "test_id": {"type": "string", "description": "Optional specific upcoming test id to project"},
            },
        },
    },
    {
        "name": "get_backlog_status",
        "description": (
            "Read the deterministic backlog escalation verdict (normal/growing/critical/"
            "impossible), its metrics, plan adherence/headroom, and per-dataset data "
            "freshness. Use for 'is my backlog out of control' or 'should I add more time'. "
            "Read-only; never writes a plan."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "get_today_blocks",
        "description": (
            "Read today's execution-discipline blocks (from the resolved daily template) "
            "with each block's confirmation status (pending/started/skipped/completed), a "
            "'current' marker on the active block, and a short what-to-do hint. Use for "
            "'what is my schedule today' or 'what should I be doing now'. Read-only."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "get_current_block",
        "description": (
            "Read the execution-discipline block active right now: its title, window, "
            "confirmation status, whether the ledger has evidence for it, plus (best-effort) "
            "days to the next test and the backlog level. Returns a small 'no current block' "
            "dict when between windows. Read-only."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "get_jee_patterns",
        "description": (
            "Read the top repeating JEE question patterns (mined from 414 past papers, "
            "2016-2026), filtered by subject/chapter, ordered by frequency. Use for "
            "'which questions repeat most in X', 'what patterns come up in chapter Y'. "
            "Read-only. If it returns an empty list, the JEE analytics are not loaded."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "subject": {"type": "string", "description": "Optional subject: Physics, Chemistry or Mathematics"},
                "chapter": {"type": "string", "description": "Optional exact chapter name"},
                "limit": {"type": "integer", "description": "How many patterns to return (default 10, max 20)"},
            },
        },
    },
    {
        "name": "get_chapter_roi",
        "description": (
            "Read JEE chapters ranked by importance (ROI score) for an exam type "
            "(mains or advanced), optionally filtered by subject. Use for 'which "
            "chapters give the easiest marks', 'best chapters for Advanced'. Read-only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "exam_type": {"type": "string", "description": "mains or advanced (default mains)"},
                "subject": {"type": "string", "description": "Optional subject: Physics, Chemistry or Mathematics"},
                "limit": {"type": "integer", "description": "How many chapters to return (default 10, max 20)"},
            },
        },
    },
    {
        "name": "get_exam_weightage",
        "description": (
            "Read per-chapter JEE weightage (total historical question counts) across "
            "exam types, optionally filtered by subject/chapter. Use for 'what is the "
            "weightage of Electrostatics', 'which chapters carry the most marks'. "
            "Read-only. If it returns an empty list, the JEE analytics are not loaded."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "subject": {"type": "string", "description": "Optional subject: Physics, Chemistry or Mathematics"},
                "chapter": {"type": "string", "description": "Optional exact chapter name"},
            },
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
        "name": "record_doubt_attempt",
        "description": (
            "Record a durable attempt on an existing open doubt (fuzzy name match). "
            "Requires duration_min>=1, the approach you tried and the exact stuck point. "
            "Confirmed before saving."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "doubt": {"type": "string", "description": "Doubt title (fuzzy matched)"},
                "duration_min": {"type": "integer", "description": "Minutes spent (>=1)"},
                "approach": {"type": "string", "description": "What you tried"},
                "stuck_point": {"type": "string", "description": "Exactly where you got stuck"},
                "outcome": {"type": "string", "enum": sorted(notion_schema.ATTEMPT_OUTCOME_OPTIONS), "description": "Attempt outcome"},
            },
            "required": ["doubt", "duration_min", "approach", "stuck_point"],
        },
    },
    {
        "name": "update_progress",
        "description": (
            "Record evidence-aware chapter/topic progress for an upcoming-syllabus item "
            "(subject + topic required, chapter optional): exercise/mle/pyq done+total, "
            "confidence 0-100, mastery, verification_source, last_verified, notes. Weaker "
            "evidence never overrides stronger stored evidence. Confirmed before saving."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "subject": {"type": "string", "description": "Canonical subject, e.g. Physics"},
                "topic": {"type": "string", "description": "Topic name"},
                "chapter": {"type": "string", "description": "Optional chapter name"},
                "exercise_done": {"type": "integer"},
                "exercise_total": {"type": "integer"},
                "mle_done": {"type": "integer"},
                "mle_total": {"type": "integer"},
                "pyq_done": {"type": "integer"},
                "pyq_total": {"type": "integer"},
                "confidence": {"type": "integer", "description": "0-100"},
                "mastery": {"type": "string", "enum": sorted(notion_schema.MASTERY_STATUS_OPTIONS)},
                "verification_source": {"type": "string", "enum": ["unknown", "self_reported", "partially_evidenced", "evidence_backed"]},
                "last_verified": {"type": "string", "description": "YYYY-MM-DD"},
                "notes": {"type": "string"},
            },
            "required": ["subject", "topic"],
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


def _compact_doubt(entry: dict[str, Any] | None) -> dict[str, Any] | None:
    """Reduce one ranked-doubt entry to its privacy-safe, compact shape."""
    if entry is None:
        return None
    doubt = entry.get("doubt") or {}
    return {
        "doubt_id": entry.get("doubt_id") or doubt.get("notion_page_id"),
        "concept": entry.get("concept"),
        "subject": entry.get("subject"),
        "bucket": entry.get("bucket_label"),
        "score": entry.get("score"),
        "confidence": entry.get("confidence"),
        "reason": entry.get("reason"),
    }


def coaching_progress_relevant_freshness(db_path: str | Path) -> dict[str, Any]:
    """Compact freshness of the datasets chapter-progress reads depend on."""
    try:
        import coaching_policy
        fresh = coaching_policy.classify_freshness(db_path=db_path)
        return {k: fresh[k]["status"] for k in ("coaching", "ledger", "doubts")}
    except Exception:
        return {}


def _discipline_hint(now: dt.datetime, block: dict[str, Any], db_path: str | Path) -> str | None:
    """Short 'what to do' hint from build_llm_context's plan/coverage slice.

    Best-effort: any failure (empty db, missing tables) yields None so the
    read tool never breaks on a hint.
    """
    try:
        import execution_discipline
        ctx = execution_discipline.build_llm_context(now, block, db_path=db_path)
        items = ctx.get("plan_items") or []
        if items:
            return f"Plan: {items[0].get('title')}"
        uncovered = ctx.get("uncovered_topics") or []
        if uncovered:
            return f"Uncovered: {uncovered[0].get('test')}"
    except Exception:
        pass
    return None


def _run_get_today_blocks(chat_id: int | str, db_path: str | Path) -> dict[str, Any]:
    """Today's execution-discipline blocks with status + current marker + hint."""
    import execution_discipline
    now = session_context.local_now()
    today_iso = session_context.local_today_iso()
    blocks = execution_discipline.blocks_for_date(today_iso, db_path=db_path)
    current = execution_discipline.current_block(now, db_path=db_path)
    current_key = current["block_key"] if current else None
    hint_block = current or (blocks[0] if blocks else None)
    hint = _discipline_hint(now, hint_block, db_path) if hint_block else None
    out: list[dict[str, Any]] = []
    for block in blocks:
        state = execution_discipline.get_state(today_iso, block["block_key"], db_path=db_path)
        entry: dict[str, Any] = {
            "block_key": block["block_key"],
            "title": block["title"],
            "kind": block["kind"],
            "window": f"{block['start_hhmm']}-{block['end_hhmm']}",
            "status": (state or {}).get("status") or "pending",
            "current": block["block_key"] == current_key,
        }
        if hint and block["block_key"] == current_key:
            entry["hint"] = hint
        out.append(entry)
    return {"date": today_iso, "blocks": out, "generated_with": "deterministic"}


def _run_get_current_block(chat_id: int | str, db_path: str | Path) -> dict[str, Any]:
    """The block active now + status + ledger evidence + best-effort test/backlog."""
    import execution_discipline
    now = session_context.local_now()
    block = execution_discipline.current_block(now, db_path=db_path)
    if block is None:
        return {
            "current": None,
            "message": "No active block right now (between windows).",
            "generated_with": "deterministic",
        }
    state = execution_discipline.get_state(
        block["local_date"], block["block_key"], db_path=db_path,
    )
    result: dict[str, Any] = {
        "block_key": block["block_key"],
        "title": block["title"],
        "kind": block["kind"],
        "window": f"{block['start_hhmm']}-{block['end_hhmm']}",
        "status": (state or {}).get("status") or "pending",
        "has_ledger_evidence": execution_discipline.has_ledger_evidence(
            block["local_date"], block, db_path=db_path,
        ),
        "generated_with": "deterministic",
    }
    try:
        import ntsc_coaching
        snap = ntsc_coaching.context_snapshot(db_path=db_path)
        tests = snap.get("next_tests") or []
        if tests:
            first = tests[0]
            result["next_test"] = first.get("title")
            test_date = str(first.get("test_date") or "")[:10]
            if test_date:
                result["days_to_test"] = (
                    dt.date.fromisoformat(test_date)
                    - dt.date.fromisoformat(block["local_date"])
                ).days
    except Exception:
        pass
    try:
        import coaching_policy
        level = coaching_policy.backlog_escalation(
            today=block["local_date"], db_path=db_path,
        ).get("level")
        if level:
            result["backlog_level"] = level
    except Exception:
        pass
    return result


def _run_get_chapter_classifications(chat_id: int | str, db_path: str | Path) -> dict[str, Any]:
    """Confirmed/proposed chapter tags (mastery/revision/hard) — read-only.

    Returns only subject/chapter/tag/metrics/decided_at — never the raw
    payloads. An empty or missing table yields empty lists, never an error.
    """
    import chapter_classification
    confirmed: list[dict[str, Any]] = []
    proposed: list[dict[str, Any]] = []
    try:
        if not Path(db_path).exists():
            return {
                "confirmed": confirmed,
                "proposed": proposed,
                "generated_with": "deterministic",
            }
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            table = chapter_classification.CLASSIFICATIONS_TABLE
            rows = conn.execute(
                f"SELECT subject, chapter, tag, accuracy_ratio, cognitive_yield, "
                f"decided_at, status FROM {table} ORDER BY decided_at"
            ).fetchall()
        for row in rows:
            entry = {
                "subject": row["subject"],
                "chapter": row["chapter"],
                "tag": row["tag"],
                "accuracy_ratio": row["accuracy_ratio"],
                "cognitive_yield": row["cognitive_yield"],
                "decided_at": row["decided_at"],
            }
            if row["status"] == "confirmed":
                confirmed.append(entry)
            elif row["status"] == "proposed":
                proposed.append(entry)
    except sqlite3.Error:
        pass
    return {
        "confirmed": confirmed,
        "proposed": proposed,
        "generated_with": "deterministic",
    }


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
        if name == "get_coaching_schedule":
            import ntsc_coaching
            date = str((arguments or {}).get("date") or "")[:10]
            if len(date) != 10:
                return {"error": True, "message": "date must be YYYY-MM-DD"}
            return {
                "date": date,
                "classes": ntsc_coaching.classes_for_date(date, db_path=db_path),
                "freshness": ntsc_coaching.freshness(db_path=db_path),
            }
        if name == "get_coaching_snapshot":
            import ntsc_coaching
            return ntsc_coaching.context_snapshot(db_path=db_path)
        if name == "get_upcoming_syllabus":
            import coaching_syllabus
            import ntsc_coaching
            try:
                limit = max(1, min(int((arguments or {}).get("limit") or 5), 20))
            except (TypeError, ValueError):
                limit = 5
            tests = coaching_syllabus.coverage_snapshot(limit=limit, db_path=db_path)
            return {
                "tests": [
                    {
                        "source_id": t.get("source_id"),
                        "title": t.get("title"),
                        "test_date": t.get("test_date"),
                        "coverage": t.get("coverage"),
                        "topics": [
                            {
                                "subject": r.get("subject"),
                                "chapter": r.get("chapter"),
                                "topic": r.get("topic"),
                                "covered": r.get("covered"),
                                "has_doubt": r.get("has_doubt"),
                            }
                            for r in (t.get("syllabus_records") or [])
                        ],
                    }
                    for t in tests
                ],
                "freshness": ntsc_coaching.freshness(db_path=db_path),
            }
        if name == "get_next_class":
            import ntsc_coaching
            try:
                limit = max(1, min(int((arguments or {}).get("limit") or 3), 10))
            except (TypeError, ValueError):
                limit = 3
            return {
                "classes": ntsc_coaching.next_classes(limit=limit, db_path=db_path),
                "freshness": ntsc_coaching.freshness(db_path=db_path),
            }
        if name == "get_plan_suggestions":
            import coaching_planner
            try:
                days = max(1, min(int((arguments or {}).get("days") or 1), 14))
            except (TypeError, ValueError):
                days = 1
            start_date = (arguments or {}).get("start_date")
            if start_date not in (None, ""):
                start_date = str(start_date)[:10]
                try:
                    dt.date.fromisoformat(start_date)
                except ValueError:
                    return {
                        "error": True,
                        "message": "start_date must be YYYY-MM-DD",
                    }
            plan = coaching_planner.build_plan(
                target_date=start_date,
                days=days,
                db_path=db_path,
            )
            return {
                "plan_type": plan["plan_type"],
                "start_date": plan["start_date"],
                "end_date": plan["end_date"],
                "blocks": [
                    {
                        "kind": b["kind"],
                        "title": b["title"],
                        "date": b["date"],
                        "start": b.get("start"),
                        "end": b.get("end"),
                        "duration_min": b.get("duration_min"),
                        "priority": b.get("priority"),
                        "reason": b.get("reason"),
                    }
                    for b in plan["blocks"] if b.get("placed")
                ],
                "unplaced": [
                    {
                        "kind": u["kind"],
                        "title": u["title"],
                        "date": u["date"],
                        "skip_reason": u.get("skip_reason"),
                    }
                    for u in plan["unplaced"]
                ],
                "capacity": plan["capacity"],
                "warnings": plan["warnings"],
            }
        if name == "get_chapter_progress":
            import coaching_progress
            try:
                limit = max(1, min(int((arguments or {}).get("limit") or 5), 20))
            except (TypeError, ValueError):
                limit = 5
            return {
                "coverage": coaching_progress.coverage_summary(limit=limit, db_path=db_path),
                "missing_questions": coaching_progress.missing_data_questions(
                    limit=limit, db_path=db_path
                ),
                "freshness": coaching_progress_relevant_freshness(db_path),
                "generated_with": "deterministic",
            }
        if name == "get_next_doubt":
            import coaching_doubts
            ranked = coaching_doubts.ranked_doubts(db_path=db_path)
            top = ranked[0] if ranked else None
            return {
                "open_doubt_count": len(ranked),
                "next": _compact_doubt(top),
                "queue": [_compact_doubt(d) for d in ranked[:5]],
                "generated_with": "deterministic",
            }
        if name == "get_doubt_interaction":
            import coaching_doubts
            doubt_id = str((arguments or {}).get("doubt_id") or "")
            if not doubt_id:
                return {"error": True, "message": "doubt_id is required (see get_next_doubt)"}
            try:
                session = coaching_doubts.begin_doubt(chat_id, doubt_id, db_path=db_path)
            except ValueError as exc:
                return {"error": True, "message": str(exc)}
            return {
                "session_id": session["id"],
                "state": session["state"],
                "doubt_id": session["doubt_id"],
                "doubt_concept": session.get("doubt_concept"),
                "subject": session.get("subject"),
                "message": session["message"],
                "write_plan": session.get("write_plan"),
                "note": (
                    "This begins a local interaction state only. Durable doubt "
                    "attempt/resolution writes require the confirmed write tools "
                    "(record_doubt_attempt / resolve_doubt / dismiss_doubt)."
                ),
                "generated_with": "deterministic",
            }
        if name == "get_score_prediction":
            import coaching_prediction
            test_id = (arguments or {}).get("test_id")
            snapshot = coaching_prediction.project_coaching_score(
                test_id=test_id or None, db_path=db_path, store=False,
            )
            return {
                "status": snapshot["status"],
                "test_title": snapshot.get("test_title"),
                "test_date": snapshot.get("test_date"),
                "days_to_test": snapshot.get("days_to_test"),
                "confidence": snapshot.get("confidence"),
                "total": snapshot.get("total"),
                "subjects": [
                    {k: s.get(k) for k in ("subject", "status", "mean_pct", "pct", "trend_direction")}
                    for s in (snapshot.get("subjects") or [])
                ],
                "risks": (snapshot.get("risks") or [])[:6],
                "actions": (snapshot.get("actions") or [])[:6],
                "missing": snapshot.get("missing", []),
                "rank_statement": snapshot.get("rank_statement"),
                "generated_with": "deterministic",
            }
        if name == "get_backlog_status":
            import coaching_policy
            escalation = coaching_policy.backlog_escalation(db_path=db_path)
            return {
                "level": escalation["level"],
                "metrics": escalation["metrics"],
                "plan": escalation["plan"],
                "escalation": escalation["escalation"],
                "reasons": escalation["reasons"],
                "recommendation": escalation["recommendation"],
                "freshness": coaching_policy.classify_freshness(db_path=db_path),
                "generated_with": "deterministic",
            }
        if name == "get_today_blocks":
            return _run_get_today_blocks(chat_id, db_path)
        if name == "get_current_block":
            return _run_get_current_block(chat_id, db_path)
        if name == "get_chapter_classifications":
            return _run_get_chapter_classifications(chat_id, db_path)
        if name == "get_jee_patterns":
            return get_jee_patterns(db_path=db_path, **dict(arguments or {}))
        if name == "get_chapter_roi":
            return get_chapter_roi(db_path=db_path, **dict(arguments or {}))
        if name == "get_exam_weightage":
            return get_exam_weightage(db_path=db_path, **dict(arguments or {}))
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
