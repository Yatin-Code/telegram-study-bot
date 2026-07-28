"""Durable reminder decisions for planning, exams and teacher opportunities."""

from __future__ import annotations

import datetime as dt
import hashlib
import sqlite3
import statistics
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import session_context
import study_domain
import planner
from config import settings


DEFAULT_DB_PATH = Path(__file__).resolve().parent / "sqlite_mirror.db"
EVENT_TABLE = "reminder_events"
DELIVERY_TABLE = "reminder_deliveries"
TEACHER_PREP_MINUTES = 45
RESPONSE_WINDOW_MINUTES = 6 * 60
MIN_TIMING_SAMPLES = 4


def _connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {EVENT_TABLE} (
            event_key TEXT PRIMARY KEY,
            sent_at TEXT NOT NULL
        )
    """)
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {DELIVERY_TABLE} (
            event_key TEXT PRIMARY KEY,
            chat_id TEXT NOT NULL,
            reminder_kind TEXT NOT NULL,
            scheduled_local TEXT,
            sent_at TEXT NOT NULL,
            responded_at TEXT,
            response_latency_min REAL
        )
    """)
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{DELIVERY_TABLE}_kind_response "
        f"ON {DELIVERY_TABLE}(chat_id, reminder_kind, responded_at)"
    )
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


def _aware(value: dt.datetime | None = None) -> dt.datetime:
    value = value or dt.datetime.now(dt.timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value


def record_delivery(
    event_key: str,
    *,
    chat_id: int | str,
    reminder_kind: str,
    scheduled_local: str | None = None,
    sent_at: dt.datetime | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> bool:
    """Record one successfully delivered reminder for response-time learning."""
    kind = str(reminder_kind or "").strip().lower()
    if not kind:
        raise ValueError("reminder_kind is required")
    stamp = _aware(sent_at).astimezone(dt.timezone.utc).isoformat()
    with _connect(db_path) as conn:
        cur = conn.execute(
            f"INSERT OR IGNORE INTO {DELIVERY_TABLE} "
            "(event_key,chat_id,reminder_kind,scheduled_local,sent_at) "
            "VALUES (?,?,?,?,?)",
            (str(event_key), str(chat_id), kind, scheduled_local, stamp),
        )
        conn.commit()
        return cur.rowcount == 1


def record_response(
    chat_id: int | str,
    *,
    responded_at: dt.datetime | None = None,
    event_key: str | None = None,
    reminder_kind: str | None = None,
    max_latency_minutes: int = RESPONSE_WINDOW_MINUTES,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any] | None:
    """Attach a user response to the latest eligible unanswered reminder.

    Handler integration can call this once Phase 2 routing is settled. Passing
    an event key is exact; otherwise the latest reminder in the response window
    is selected, optionally restricted by kind.
    """
    response = _aware(responded_at).astimezone(dt.timezone.utc)
    cutoff = response - dt.timedelta(minutes=max_latency_minutes)
    clauses = ["chat_id=?", "responded_at IS NULL", "sent_at BETWEEN ? AND ?"]
    params: list[Any] = [str(chat_id), cutoff.isoformat(), response.isoformat()]
    if event_key is not None:
        clauses.append("event_key=?")
        params.append(str(event_key))
    if reminder_kind is not None:
        clauses.append("reminder_kind=?")
        params.append(str(reminder_kind).strip().lower())
    with _connect(db_path) as conn:
        row = conn.execute(
            f"SELECT * FROM {DELIVERY_TABLE} WHERE {' AND '.join(clauses)} "
            "ORDER BY sent_at DESC LIMIT 1",
            tuple(params),
        ).fetchone()
        if row is None:
            return None
        sent = dt.datetime.fromisoformat(str(row["sent_at"]))
        latency = max(0.0, (response - sent).total_seconds() / 60)
        conn.execute(
            f"UPDATE {DELIVERY_TABLE} SET responded_at=?, response_latency_min=? "
            "WHERE event_key=?",
            (response.isoformat(), round(latency, 2), row["event_key"]),
        )
        conn.commit()
    return {**dict(row), "responded_at": response.isoformat(),
            "response_latency_min": round(latency, 2)}


def _minute_of_day(value: dt.datetime, timezone: ZoneInfo) -> int:
    local = value.astimezone(timezone)
    return local.hour * 60 + local.minute


def _hhmm(minutes: int) -> str:
    minutes %= 24 * 60
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _parse_hhmm(value: str) -> int:
    hour, minute = (int(part) for part in str(value).split(":", 1))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("time must be HH:MM")
    return hour * 60 + minute


def timing_recommendation(
    chat_id: int | str,
    reminder_kind: str,
    default_time: str,
    *,
    as_of: dt.datetime | None = None,
    lookback_days: int = 60,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Recommend a conservative local reminder time from response evidence."""
    default_minute = _parse_hhmm(default_time)
    now = _aware(as_of)
    cutoff = now.astimezone(dt.timezone.utc) - dt.timedelta(days=lookback_days)
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT sent_at, responded_at, response_latency_min "
            f"FROM {DELIVERY_TABLE} "
            "WHERE chat_id=? AND reminder_kind=? AND responded_at IS NOT NULL "
            "AND sent_at>=? ORDER BY sent_at",
            (str(chat_id), str(reminder_kind).strip().lower(), cutoff.isoformat()),
        ).fetchall()
    try:
        timezone = ZoneInfo(settings.user_timezone())
    except Exception:
        timezone = ZoneInfo("UTC")
    response_minutes = [
        _minute_of_day(dt.datetime.fromisoformat(str(row["responded_at"])), timezone)
        for row in rows
    ]
    distinct_days = {
        dt.datetime.fromisoformat(str(row["responded_at"])).astimezone(timezone).date()
        for row in rows
    }
    samples = len(response_minutes)
    if samples < MIN_TIMING_SAMPLES or len(distinct_days) < 3:
        return {
            "default_time": default_time, "recommended_time": default_time,
            "samples": samples, "confidence": "insufficient",
            "median_response_time": None,
            "reason": "not enough response evidence across distinct days",
        }
    median_response = int(statistics.median(response_minutes))
    deviations = [abs(value - median_response) for value in response_minutes]
    mad = float(statistics.median(deviations))
    if mad > 120:
        return {
            "default_time": default_time, "recommended_time": default_time,
            "samples": samples, "confidence": "low",
            "median_response_time": _hhmm(median_response),
            "reason": "response times are too scattered to move the reminder safely",
        }
    # Aim shortly before the user's normal response window, then blend toward
    # the configured default. Evidence can move at most 75% of the distance.
    learned = median_response - 15
    delta = learned - default_minute
    if delta > 720:
        delta -= 1440
    elif delta < -720:
        delta += 1440
    weight = min(0.75, 0.35 + samples * 0.04)
    recommended = round((default_minute + delta * weight) / 5) * 5
    # Avoid sleep-hostile automatic shifts even if noisy timestamps suggest it.
    recommended = max(5 * 60, min(23 * 60, recommended))
    confidence = "high" if samples >= 10 and mad <= 45 else (
        "medium" if samples >= 6 and mad <= 90 else "low"
    )
    return {
        "default_time": default_time,
        "recommended_time": _hhmm(recommended),
        "samples": samples,
        "confidence": confidence,
        "median_response_time": _hhmm(median_response),
        "median_absolute_deviation_min": round(mad, 1),
        "reason": "blended configured time with the observed response window",
    }


def effective_time(
    chat_id: int | str, reminder_kind: str, default_time: str,
    *, db_path: str | Path = DEFAULT_DB_PATH,
) -> str:
    recommendation = timing_recommendation(
        chat_id, reminder_kind, default_time, db_path=db_path
    )
    if recommendation["confidence"] in {"medium", "high"}:
        return str(recommendation["recommended_time"])
    return default_time


def _profile_planning_hint(
    chat_id: int | str | None, *, db_path: str | Path
) -> str | None:
    if chat_id is None:
        return None
    try:
        import learner_profile
        profile = learner_profile.latest(chat_id, db_path=db_path) or learner_profile.refresh(
            chat_id, db_path=db_path
        )
    except Exception:
        return None
    focus = profile.get("coaching_focus") or []
    if focus:
        item = focus[0]
        if item.get("subject"):
            return f"Protect one focused {item['subject']} block: {item['reason']}."
        if item.get("goal"):
            return f"Protect a recovery block for {item['goal']}: {item['reason']}."
        return str(item.get("reason") or "").strip() or None
    window = (profile.get("rhythm") or {}).get("best_window")
    if window:
        return f"Reserve the hardest block for your best evidenced {window} window."
    return None


def planning_message(
    *, now: dt.datetime | None = None, chat_id: int | str | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> str:
    now = now or session_context.local_now()
    facts = planner.analyze(
        now.date().isoformat(), chat_id=chat_id, db_path=db_path
    )
    hint = _profile_planning_hint(chat_id, db_path=db_path)
    if not facts["items"]:
        message = (
            "Planning cutoff reached. Write today's ordered schedule in the "
            "Study Bot - Daily Plan database in Notion, then use /today and sleep."
        )
    elif facts["errors"]:
        message = "Today's Notion plan is blocked:\n" + "\n".join(
            f"- {x}" for x in facts["errors"]
        )
    elif facts["warnings"]:
        message = "Today's plan needs a quick check:\n" + "\n".join(
            f"- {x}" for x in facts["warnings"]
        )
    else:
        message = (
            f"Today's sequence is ready: {len(facts['active_items'])} active items, "
            f"expected CY {facts['expected_cy']:g}. Sleep now."
        )
    return message + (f"\n\nPersonalized focus: {hint}" if hint else "")


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
