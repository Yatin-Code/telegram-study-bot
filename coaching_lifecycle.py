"""Deterministic coaching class lifecycle notifications (Phase 7).

Reads only the cached ``coaching_classes`` / ``coaching_sync_runs`` tables that
``ntsc_sync`` maintains, plus the shared ``reminders.claim`` dedup store.  Every
candidate is derived purely from known data — class type/time/subjects and, when
safely queryable, unresolved doubts whose subject matches the class.  Nothing is
invented.

The module never sends messages itself.  ``scan_candidates`` returns candidates
for a scheduled bot integration, which must ``reminders.claim`` each candidate's
``event_key`` before delivery so a later scan cannot double-send the same
logical class.  ``reminders.release`` allows a retry when delivery fails.

Dedup semantics
---------------
``event_key`` is built from the deterministic class identity
``class_date|start_time|class_type`` (the cached ``source_id`` embeds a
per-sync index that is not stable across re-syncs, so it is deliberately not
used).  Two cache rows describing the same logical class therefore map to one
candidate per phase; a rescheduled class gets a new key and a fresh reminder.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import ntsc_coaching
import session_context
from config import settings


DEFAULT_DB_PATH = Path(__file__).resolve().parent / "sqlite_mirror.db"

PRE_CLASS_MIN_LEAD_MINUTES = 30
PRE_CLASS_MAX_LEAD_MINUTES = 90
POST_CLASS_MIN_ELAPSED_MINUTES = 15
POST_CLASS_MAX_ELAPSED_MINUTES = 240
FRESHNESS_MAX_AGE_MINUTES = 24 * 60
MAX_DOUBTS_IN_PRE_CLASS = 3

_CLOCK_24H_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::\d{2})?$")
_CLOCK_12H_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*([AaPp][Mm])$")


def _tz() -> ZoneInfo:
    name = settings.user_timezone()
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("UTC")


def _as_local(value: dt.datetime | None) -> dt.datetime:
    if value is None:
        value = session_context.local_now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=_tz())
    return value.astimezone(_tz())


def _parse_clock(text: Any) -> dt.time | None:
    """Parse HH:MM, HH:MM:SS, or H:MM AM/PM into a time of day."""
    text = str(text or "").strip()
    match = _CLOCK_12H_RE.match(text)
    if match:
        hour, minute, ampm = int(match.group(1)), int(match.group(2)), match.group(3).lower()
        if not (1 <= hour <= 12 and 0 <= minute <= 59):
            return None
        hour = 0 if hour == 12 else hour
        if ampm == "p":
            hour += 12
        return dt.time(hour, minute)
    match = _CLOCK_24H_RE.match(text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return dt.time(hour, minute)
    return None


def class_start_time(row: dict[str, Any], *, now: dt.datetime | None = None) -> dt.datetime | None:
    """Start of a cached class as an aware local datetime, or None when unknown."""
    date_text = str(row.get("class_date") or "").strip()[:10]
    clock = _parse_clock(row.get("start_time"))
    if not date_text or clock is None:
        return None
    try:
        day = dt.date.fromisoformat(date_text)
    except ValueError:
        return None
    return dt.datetime.combine(day, clock, tzinfo=_tz())


def class_end_time(row: dict[str, Any], *, now: dt.datetime | None = None) -> dt.datetime | None:
    """End of a cached class, or None when start or a positive duration is unknown."""
    start = class_start_time(row, now=now)
    if start is None:
        return None
    try:
        duration = int(row.get("duration_min") or 0)
    except (TypeError, ValueError):
        return None
    if duration <= 0:
        return None
    return start + dt.timedelta(minutes=duration)


def event_key(row: dict[str, Any], phase: str) -> str:
    """Stable, claimable key for one phase of one logical class."""
    date_text = str(row.get("class_date") or "").strip()[:10]
    time_text = str(row.get("start_time") or "").strip()
    type_text = str(row.get("class_type") or "").strip()
    return f"coaching-{phase}:{date_text}:{time_text}:{type_text}"


def fresh(*, now: dt.datetime | None = None, db_path: str | Path = DEFAULT_DB_PATH) -> bool:
    """True only when the coaching cache had a successful, recent sync.

    Guards against notifications when the portal data was never synced, the
    latest run failed, or the last success is older than ``FRESHNESS_MAX_AGE``.
    """
    now = _as_local(now)
    with ntsc_coaching._connect(db_path) as conn:
        row = conn.execute(
            "SELECT status, finished_at FROM coaching_sync_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if row is None or str(row["status"]) != "success":
        return False
    finished = str(row["finished_at"] or "")
    if not finished:
        return False
    try:
        stamp = dt.datetime.fromisoformat(finished)
    except ValueError:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    age_minutes = (now.astimezone(dt.timezone.utc) - stamp.astimezone(dt.timezone.utc)).total_seconds() / 60
    return 0 <= age_minutes <= FRESHNESS_MAX_AGE_MINUTES


def _classes(*, db_path: str | Path = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    with ntsc_coaching._connect(db_path) as conn:
        rows = conn.execute(
            """SELECT source_id,class_date,start_time,duration_min,class_type,subjects,live_class
               FROM coaching_classes ORDER BY class_date, start_time"""
        ).fetchall()
    return [dict(row) for row in rows]


def _open_doubts(*, db_path: str | Path = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    """All open doubts, or [] when the doubt store is not safely queryable."""
    try:
        import study_domain
        return study_domain.doubt_queue(db_path=db_path)
    except Exception:
        return []


def _subject_tokens(text: Any) -> set[str]:
    tokens: set[str] = set()
    for part in str(text or "").split(","):
        part = part.strip().lower()
        if part:
            tokens.add(part)
    return tokens


def _relevant_doubts(row: dict[str, Any], doubts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Unresolved doubts whose subject overlaps this class's subjects."""
    class_subjects = _subject_tokens(row.get("subjects"))
    if not class_subjects:
        return []
    matching: list[dict[str, Any]] = []
    for doubt in doubts:
        subject = str(doubt.get("subject") or "").strip().lower()
        if not subject:
            continue
        if any(token == subject or token in subject or subject in token for token in class_subjects):
            matching.append(doubt)
    return matching[:MAX_DOUBTS_IN_PRE_CLASS]


def pre_class_message(row: dict[str, Any], *, doubts: list[dict[str, Any]] | None = None) -> str:
    """Concise pre-class nudge built only from known class fields and doubts."""
    subjects = str(row.get("subjects") or "class").strip()
    class_type = str(row.get("class_type") or "Class").strip()
    start_time = str(row.get("start_time") or "unknown").strip()
    lines = [f"📚 {class_type} at {start_time}: {subjects}."]
    relevant = list(doubts or [])
    if relevant:
        lines.extend(("", "Unresolved doubts to carry in:"))
        for doubt in relevant:
            concept = str(doubt.get("core_concept") or "Untitled doubt").strip()
            attempts = int(doubt.get("valid_attempts") or 0)
            lines.append(f"• {concept} — {attempts}/2 attempts")
    lines.extend(("", "Attend, then log the session afterwards."))
    return "\n".join(lines)


def post_class_message(row: dict[str, Any], *, now: dt.datetime | None = None) -> str:
    """Fixed follow-up checklist; never invents topics, homework, or answers."""
    end = class_end_time(row, now=now)
    end_label = end.strftime("%H:%M") if end else "ended"
    subjects = str(row.get("subjects") or "class").strip()
    class_type = str(row.get("class_type") or "Class").strip()
    return "\n".join([
        f"🏁 {class_type} · {subjects} ended at {end_label}.",
        "",
        "Quick check-in:",
        "• Did you attend? (yes / no)",
        "• Topics covered?",
        "• Homework assigned?",
        "• Any doubts? Log them with /doubt.",
    ])


def _candidate(
    row: dict[str, Any], phase: str, *, now: dt.datetime, doubts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    now = _as_local(now)
    candidate: dict[str, Any] = {
        "phase": phase,
        "event_key": event_key(row, phase),
        "class_date": str(row.get("class_date") or "")[:10],
        "start_time": str(row.get("start_time") or ""),
        "class_type": str(row.get("class_type") or ""),
        "subjects": str(row.get("subjects") or ""),
        "duration_min": row.get("duration_min"),
        "message": "",
    }
    if phase == "pre":
        start = class_start_time(row, now=now)
        candidate["minutes_to_start"] = round((start - now).total_seconds() / 60, 1)
        candidate["doubts"] = list(doubts or [])
        candidate["message"] = pre_class_message(row, doubts=candidate["doubts"])
    else:
        end = class_end_time(row, now=now)
        candidate["minutes_elapsed"] = round((now - end).total_seconds() / 60, 1)
        candidate["message"] = post_class_message(row, now=now)
    return candidate


def _dedup(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for candidate in candidates:
        if candidate["event_key"] in seen:
            continue
        seen.add(candidate["event_key"])
        result.append(candidate)
    return result


def pre_class_candidates(
    *, now: dt.datetime | None = None, db_path: str | Path = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    """Upcoming pre-class candidates: classes starting in [30, 90] minutes."""
    if not fresh(now=now, db_path=db_path):
        return []
    now = _as_local(now)
    doubts = _open_doubts(db_path=db_path)
    candidates: list[dict[str, Any]] = []
    for row in _classes(db_path=db_path):
        start = class_start_time(row, now=now)
        if start is None or start <= now:
            continue
        minutes = (start - now).total_seconds() / 60
        if not (PRE_CLASS_MIN_LEAD_MINUTES <= minutes <= PRE_CLASS_MAX_LEAD_MINUTES):
            continue
        candidates.append(_candidate(row, "pre", now=now, doubts=_relevant_doubts(row, doubts)))
    return _dedup(candidates)


def post_class_candidates(
    *, now: dt.datetime | None = None, db_path: str | Path = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    """Post-class follow-up candidates: classes that ended [15, 240] minutes ago."""
    if not fresh(now=now, db_path=db_path):
        return []
    now = _as_local(now)
    candidates: list[dict[str, Any]] = []
    for row in _classes(db_path=db_path):
        end = class_end_time(row, now=now)
        if end is None or end >= now:
            continue
        minutes = (now - end).total_seconds() / 60
        if not (POST_CLASS_MIN_ELAPSED_MINUTES <= minutes <= POST_CLASS_MAX_ELAPSED_MINUTES):
            continue
        candidates.append(_candidate(row, "post", now=now))
    return _dedup(candidates)


def scan_candidates(
    *, now: dt.datetime | None = None, db_path: str | Path = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    """All due candidates (pre then post) for a scheduled bot integration.

    The integration must ``reminders.claim`` each ``candidate["event_key"]``
    before sending, and ``reminders.release`` it if delivery fails.
    """
    return pre_class_candidates(now=now, db_path=db_path) + post_class_candidates(now=now, db_path=db_path)
