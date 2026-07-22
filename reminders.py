"""Durable reminder decisions for planning, exams and teacher opportunities."""

from __future__ import annotations

import datetime as dt
import hashlib
import sqlite3
from pathlib import Path
from typing import Any

import session_context
import study_domain
import planner


DEFAULT_DB_PATH = Path(__file__).resolve().parent / "sqlite_mirror.db"
EVENT_TABLE = "reminder_events"
TEACHER_PREP_MINUTES = 45


def _connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {EVENT_TABLE} (
            event_key TEXT PRIMARY KEY,
            sent_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def claim(event_key: str, *, db_path: str | Path = DEFAULT_DB_PATH) -> bool:
    """Atomically claim a reminder. False means it was already sent."""
    with _connect(db_path) as conn:
        cur = conn.execute(
            f"INSERT OR IGNORE INTO {EVENT_TABLE} (event_key, sent_at) VALUES (?, ?)",
            (event_key, dt.datetime.now(dt.timezone.utc).isoformat()),
        )
        conn.commit()
        return cur.rowcount == 1


def release(event_key: str, *, db_path: str | Path = DEFAULT_DB_PATH) -> None:
    """Release a claim when delivery failed, so a later scan can retry."""
    with _connect(db_path) as conn:
        conn.execute(
            f"DELETE FROM {EVENT_TABLE} WHERE event_key = ?", (event_key,)
        )
        conn.commit()


def planning_message(*, now: dt.datetime | None = None, db_path: str | Path = DEFAULT_DB_PATH) -> str:
    now = now or session_context.local_now()
    facts = planner.analyze(now.date().isoformat(), db_path=db_path)
    if not facts["items"]:
        return (
            "Planning cutoff reached. Write today's ordered schedule in the "
            "Study Bot - Daily Plan database in Notion, then use /today and sleep."
        )
    if facts["errors"]:
        return "Today's Notion plan is blocked:\n" + "\n".join(f"- {x}" for x in facts["errors"])
    if facts["warnings"]:
        return "Today's Notion plan needs a quick check:\n" + "\n".join(f"- {x}" for x in facts["warnings"])
    return f"Today's sequence is ready: {len(facts['items'])} items, expected CY {facts['expected_cy']:g}. Sleep now."


def settled_plan_change(*, now: dt.datetime | None = None, db_path: str | Path = DEFAULT_DB_PATH) -> tuple[str, str] | None:
    """Return (event_key, analysis) after a Notion plan has been quiet for 3 min."""
    now = now or session_context.local_now()
    rows = study_domain._rows(
        "daily_plan",
        "archived=0 AND substr(COALESCE(plan_date,''),1,10)=?",
        (now.date().isoformat(),), db_path=db_path,
    )
    if not rows:
        return None
    stamps = []
    for row in rows:
        try:
            stamp = dt.datetime.fromisoformat(str(row.get("last_edited_time") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=dt.timezone.utc)
        stamps.append(stamp)
    if not stamps:
        return None
    latest = max(stamps)
    now_utc = now.astimezone(dt.timezone.utc)
    if (now_utc - latest.astimezone(dt.timezone.utc)).total_seconds() < 180:
        return None
    event_key = f"plan-analysis:{now.date().isoformat()}:{latest.isoformat()}"
    return event_key, planning_message(now=now, db_path=db_path)


def weekly_timetable_message() -> str:
    return (
        "Weekly timetable check: are your classes and teacher doubt windows "
        "still correct in Study Bot - Class & Teacher Timetable? Use /timetable to review them."
    )


def due_exams(*, now: dt.datetime | None = None, db_path: str | Path = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    now = now or session_context.local_now()
    result: list[dict[str, Any]] = []
    for row in study_domain._rows("exams", "archived=0 AND status='Planned'", db_path=db_path):
        raw = row.get("exam_date")
        if not raw:
            continue
        try:
            stamp = dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=now.tzinfo)
            due = stamp <= now
        except ValueError:
            try:
                # A date-only exam has no finish time.  Do not ask whether it
                # has finished at midnight before the paper has even started.
                due = dt.date.fromisoformat(str(raw)[:10]) < now.date()
            except ValueError:
                continue
        if due:
            result.append(row)
    return result


def teacher_opportunities(*, now: dt.datetime | None = None, db_path: str | Path = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    now = now or session_context.local_now()
    doubts = study_domain.doubt_queue(db_path=db_path)
    if not doubts:
        return []
    current = study_domain.next_plan_item(now.date().isoformat(), db_path=db_path)
    opportunities: list[dict[str, Any]] = []
    for window in study_domain.upcoming_teacher_windows(now=now, days=0, db_path=db_path):
        if window["ends_at"] <= now:
            continue
        minutes_to_start = max(0.0, (window["starts_at"] - now).total_seconds() / 60)
        active = minutes_to_start <= 0
        if not active and minutes_to_start > TEACHER_PREP_MINUTES:
            continue
        subject = str(window.get("subject") or "").lower()
        matching = [
            doubt for doubt in doubts
            if not subject or subject in str(doubt.get("subject") or "").lower()
        ]
        # The one-attempt exception exists only in the immediate teacher-window
        # context; the general dashboard must not call it teacher-ready.
        matching = [
            {**doubt, "readiness": "expedited"}
            if doubt.get("readiness") == "attempting" and doubt.get("serious_attempt")
            else doubt
            for doubt in matching
        ]
        if not matching:
            continue
        if not active:
            decision = {
                "phase": "prepare",
                "interrupt": False,
                "minutes_to_start": round(minutes_to_start, 1),
                "minutes_left": round((window["ends_at"] - now).total_seconds() / 60, 1),
                "reason": "prepare evidence before the teacher window opens",
            }
            opportunities.append({
                "window": window, "doubts": matching, "decision": decision,
                "current": current,
            })
            continue
        # During the window, strict two-attempt doubts and one-attempt serious
        # doubts are actionable. Brand-new/short-attempt doubts remain visible
        # only in the advance preparation card.
        actionable = [
            doubt for doubt in matching if doubt.get("readiness") in ("ready", "expedited")
        ]
        if not actionable:
            continue
        decision = study_domain.interruption_decision(
            current_priority=int((current or {}).get("priority") or 0),
            current_interruptible=bool((current or {}).get("interruptible", True)),
            window=window, now=now,
        )
        decision["phase"] = "interrupt" if decision["interrupt"] else "open"
        opportunities.append({"window": window, "doubts": actionable, "decision": decision, "current": current})
    return opportunities


def teacher_event_key(
    window: dict[str, Any], decision: dict[str, Any],
    doubts: list[dict[str, Any]] | None = None,
) -> str:
    """Deduplicate by phase and evidence set, so a newly logged doubt is seen."""
    phase = decision.get("phase") or ("interrupt" if decision.get("interrupt") else "open")
    evidence = ""
    if doubts:
        raw = "|".join(sorted(
            f"{row.get('notion_page_id')}:{row.get('readiness')}:{row.get('valid_attempts', 0)}"
            for row in doubts
        ))
        evidence = ":" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"teacher:{phase}:{window['notion_page_id']}:{window['ends_at'].isoformat()}{evidence}"
