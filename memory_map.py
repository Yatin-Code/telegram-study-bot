"""
/memory backend: a complete, honest map of everything the bot stores.

Three truth categories, rendered so a non-programmer can steer the bot for
years without touching code:

1. ALWAYS IN CONTEXT (variable, user-editable): session context, the QA
   follow-up window, active commitments (+streaks) and preferences — the only
   things injected into every LLM prompt. A char budget watches their growth.
2. ON DEMAND: the Notion mirror + op_* tables (goals, coaching timetable,
   exams, ledger...). The model sees only a fixed schema digest and writes
   SQL to fetch rows when a question needs them — zero context cost.
3. INTERNAL: plumbing tables that never reach a prompt.

Everything here is a pure function over SQLite so the whole surface is
unit-testable without Telegram. bot.py turns keyboard() tuples into
InlineKeyboardButtons and wires the memory:* callbacks.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import advisor
import commitments
import draft_store
import session_context
import study_domain
from config import settings

DEFAULT_DB_PATH = commitments.DEFAULT_DB_PATH

# Telegram hard limit is 4096 chars/message; keep headroom.
MAX_RENDER_CHARS = 3900
MAX_PREFS_SHOWN = 15
BUTTON_LABEL_CHARS = 28
CALLBACK_DATA_LIMIT = 64

_MIRROR_TABLES = [
    ("ledger", "ledger"),
    ("doubts", "doubts"),
    ("revision", "revision"),
]
_OP_TABLES = [
    ("op_goals", "goals"),
    ("op_work_items", "work items"),
    ("op_exams", "exams"),
    ("op_exam_questions", "exam questions"),
    ("op_doubt_attempts", "doubt attempts"),
    ("op_timetable", "coaching schedule"),
    ("op_daily_plan", "daily plan"),
    ("op_execution_links", "execution links"),
]
_INTERNAL_TABLES = [
    ("drafts", "pending previews"),
    ("pending_clarifications", "pending clarifications"),
    ("commitment_checks", "adherence checks"),
    ("reminder_events", "reminder dedup keys"),
    ("pending_writes", "queued Notion writes"),
    ("active_plan_state", "active plan markers"),
]


def _count(conn: sqlite3.Connection, table: str) -> int:
    try:
        return int(conn.execute(
            f'SELECT COUNT(*) FROM "{table}" WHERE archived = 0'
        ).fetchone()[0])
    except sqlite3.OperationalError:
        try:
            return int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        except sqlite3.OperationalError:
            return 0


def _study_data(db_path: str | Path) -> dict[str, Any]:
    facts: list[str] = []
    gaps: list[str] = []
    try:
        pace = study_domain.adaptive_target(db_path=db_path)
        if pace["days_to_exam"] is not None:
            facts.append(
                f"Next exam: {pace['nearest_exam']} in {pace['days_to_exam']} day(s) "
                f"— phase: {pace['phase']}"
            )
        else:
            gaps.append(
                "no upcoming exam date recorded — planner stays in Foundation phase. "
                "Add: /exam JEE Main on YYYY-MM-DD"
            )
    except Exception:
        pass
    try:
        timetable = study_domain._rows("timetable", "archived=0", db_path=db_path)
        if timetable:
            facts.append(f"Coaching schedule: {len(timetable)} timetable entr(ies) — edit: /timetable")
        else:
            gaps.append("coaching timetable is empty — add classes: /timetable add …")
    except Exception:
        pass
    try:
        marked = study_domain._rows(
            "exams", "archived=0 AND actual_marks IS NOT NULL", db_path=db_path
        )
        finished = study_domain._rows(
            "exams", "archived=0 AND status IN ('Analysing','Analysed','Completed')",
            db_path=db_path,
        )
        if marked:
            latest = max(marked, key=lambda r: str(r.get("exam_date") or ""))
            facts.append(
                f"Latest marks: {latest.get('title')} — {latest.get('actual_marks'):g}"
                + (f"/{latest.get('max_marks'):g}" if latest.get("max_marks") is not None else "")
            )
        elif finished:
            gaps.append("a finished exam has no marks yet — record: /exam_summary …")
    except Exception:
        pass
    try:
        goals = study_domain._rows("goals", "archived=0 AND status='Active'", db_path=db_path)
        facts.append(f"Active goals: {len(goals)} — manage: /goal, /remember")
    except Exception:
        pass
    return {"facts": facts, "gaps": gaps}


def report(chat_id: int | None, *, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    ctx = session_context.get_context(chat_id) if chat_id is not None else None
    ctx_line = ", ".join(
        f"{key}={ctx[key]}" for key in session_context.CONTEXT_KEYS if ctx and ctx.get(key)
    ) if ctx else ""
    qa = draft_store.recent_qa(chat_id, db_path=db_path) if chat_id is not None else []
    qa_chars = sum(len(t["question"]) + len(t["answer"]) for t in qa)
    memory_block = ""
    try:
        memory_block = advisor.memory_prompt_block(chat_id, db_path=db_path)
    except Exception:
        pass
    today = session_context.local_today_iso()
    commitment_rows = []
    for goal in study_domain._rows(
        "goals", "archived=0 AND status='Active' AND period IN ('Daily','Weekly')",
        db_path=db_path,
    ):
        entry = {
            "goal_id": goal.get("notion_page_id"),
            "title": str(goal.get("title") or "Untitled"),
            "target": float(goal.get("target") or 0),
            "metric": str(goal.get("metric") or ""),
            "period": str(goal.get("period") or ""),
            "streak": None, "adherence": None,
        }
        if entry["goal_id"] and entry["period"] == "Daily":
            entry["streak"] = commitments.streak(entry["goal_id"], as_of=today, db_path=db_path)
            stats = commitments.adherence(entry["goal_id"], as_of=today, db_path=db_path)
            if stats["total"]:
                entry["adherence"] = stats
        commitment_rows.append(entry)
    prefs = commitments.active_prefs(chat_id, db_path=db_path) if chat_id is not None else []
    try:
        budget = settings.memory_context_budget()
    except Exception:
        budget = 4000
    persistent_chars = len(memory_block)
    schema_chars = None
    try:
        import sql_tool
        schema_chars = len(sql_tool.schema_digest())
    except Exception:
        pass
    with commitments._connect(db_path) as conn:
        on_demand = {label: _count(conn, table) for table, label in _MIRROR_TABLES + _OP_TABLES}
        internal = {label: _count(conn, table) for table, label in _INTERNAL_TABLES}
    return {
        "session_ctx": ctx_line,
        "qa_pairs": len(qa),
        "qa_chars": qa_chars,
        "commitments": commitment_rows,
        "prefs": prefs,
        "sizes": {
            "persistent_chars": persistent_chars,
            "est_tokens": persistent_chars // 4,
            "budget": budget,
            "over_budget": persistent_chars > budget,
            "schema_chars": schema_chars,
        },
        "study_data": _study_data(db_path),
        "on_demand": on_demand,
        "internal": internal,
    }


def render(rep: dict[str, Any]) -> str:
    sizes = rep["sizes"]
    if sizes["over_budget"]:
        meter = (
            f"⚠️ ~{sizes['persistent_chars']} chars (~{sizes['est_tokens']} tokens) — "
            f"OVER the {sizes['budget']} budget. Remove items below."
        )
    else:
        meter = (
            f"~{sizes['persistent_chars']} chars (~{sizes['est_tokens']} tokens) "
            f"of {sizes['budget']} budget ✅"
        )
    lines = ["🧠 Always in the model's context (editable)", f"Memory size: {meter}", ""]
    lines.append(
        f"Session context: {rep['session_ctx'] or 'none'} (clears at midnight / 🧹)"
    )
    ttl = None
    try:
        ttl = settings.qa_history_ttl_minutes()
    except Exception:
        pass
    lines.append(
        f"Conversation window: {rep['qa_pairs']} Q&A pair(s)"
        + (f", {ttl}-min TTL" if ttl else "") + " (🧹 to clear)"
    )
    if rep["commitments"]:
        lines.append("Commitments (⏸ to pause):")
        for i, c in enumerate(rep["commitments"], 1):
            entry = (
                f"{i}. {c['title']} — "
                f"{commitments.format_target(c['target'], c['metric'], c['period'])}"
            )
            if c["streak"] is not None:
                entry += f" · streak {c['streak']}"
            if c["adherence"]:
                entry += f" · 7d {c['adherence']['met']}/{c['adherence']['total']}"
            lines.append(entry)
    if rep["prefs"]:
        lines.append("Preferences (🗑 to remove):")
        shown = rep["prefs"][:MAX_PREFS_SHOWN]
        for p in shown:
            lines.append(f"#{p['id']} {p['text']}")
        hidden = len(rep["prefs"]) - len(shown)
        if hidden > 0:
            lines.append(f"…+{hidden} more (see Raw view)")
    if not rep["commitments"] and not rep["prefs"]:
        lines.append("No commitments or preferences yet — try /remember …")
    study = rep["study_data"]
    if study["facts"] or study["gaps"]:
        lines.append("")
        lines.append("📌 Study data status")
        lines.extend(study["facts"])
        lines.extend(f"⚠️ {g}" for g in study["gaps"])
    lines.append("")
    lines.append("📚 Stored, fetched on demand — NOT in context (the model writes SQL when needed):")
    lines.append(" · ".join(f"{label} {count}" for label, count in rep["on_demand"].items()))
    if rep["sizes"]["schema_chars"]:
        lines.append(
            f"(the model always carries only the table map, ~{rep['sizes']['schema_chars'] // 1000}k chars — fixed, never grows)"
        )
    lines.append("")
    lines.append("⚙️ Internal (never in context): "
                 + " · ".join(f"{label} {count}" for label, count in rep["internal"].items()))
    text = "\n".join(lines)
    if len(text) > MAX_RENDER_CHARS:
        text = text[:MAX_RENDER_CHARS - 12] + "\n…truncated"
    return text


def render_raw(chat_id: int | None, *, db_path: str | Path = DEFAULT_DB_PATH) -> str:
    """The literal text injected into prompts — exactly what the model sees."""
    try:
        block = advisor.memory_prompt_block(chat_id, db_path=db_path)
    except Exception:
        block = ""
    ctx = session_context.get_context(chat_id) if chat_id is not None else None
    ctx_line = ", ".join(
        f"{key}={ctx[key]}" for key in session_context.CONTEXT_KEYS if ctx and ctx.get(key)
    ) if ctx else ""
    parts = ["📄 Raw memory — the exact text injected into the model's prompts:", ""]
    parts.append("— Answer-loop memory block —")
    parts.append(block or "(empty — nothing injected)")
    parts.append("")
    parts.append("— Intent-parser session context —")
    parts.append(ctx_line or "(none set)")
    text = "\n".join(parts)
    if len(text) > MAX_RENDER_CHARS:
        text = text[:MAX_RENDER_CHARS - 12] + "\n…truncated"
    return text


def _label(text: str) -> str:
    text = str(text).strip()
    return text if len(text) <= BUTTON_LABEL_CHARS else text[:BUTTON_LABEL_CHARS - 1] + "…"


def keyboard(rep: dict[str, Any]) -> list[list[tuple[str, str]]]:
    rows: list[list[tuple[str, str]]] = []
    for pref in rep["prefs"][:MAX_PREFS_SHOWN]:
        data = f"memory:delpref:{pref['id']}"
        if len(data.encode()) <= CALLBACK_DATA_LIMIT:
            rows.append([(f"🗑 {_label(pref['text'])}", data)])
    for c in rep["commitments"]:
        if not c["goal_id"]:
            continue
        data = f"memory:pausegoal:{c['goal_id']}"
        if len(data.encode()) <= CALLBACK_DATA_LIMIT:
            rows.append([(f"⏸ {_label(c['title'])}", data)])
    rows.append([("🧹 Clear context", "memory:clearctx"),
                 ("🧹 Clear history", "memory:clearhist")])
    rows.append([("📄 Raw view", "memory:raw"), ("🔄 Refresh", "memory:refresh")])
    return rows


def budget_warning(
    chat_id: int | None, *, db_path: str | Path = DEFAULT_DB_PATH
) -> str | None:
    try:
        block = advisor.memory_prompt_block(chat_id, db_path=db_path)
        budget = settings.memory_context_budget()
    except Exception:
        return None
    if len(block) > budget:
        return (
            f"⚠️ In-context memory is ~{len(block)} chars (budget {budget}). "
            "Open /memory to review and remove items."
        )
    return None
