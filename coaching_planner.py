"""Phase 8 core — deterministic coaching plan suggestions (read-only).

Builds a validated daily/weekly timeline of candidate blocks by combining:

  * cached portal coaching classes (``coaching_classes``)
  * the local ``op_daily_plan`` (committed items count toward capacity)
  * ``op_work_items`` homework / backlog
  * revision due dates
  * unresolved doubts aligned to teacher windows (``op_timetable``)
  * the nearest coaching test / syllabus (``coaching_tests`` + ``op_exams``)

Placement is deterministic and offline. Fixed coaching classes and teacher
windows anchor the day; the remaining free gaps are filled by priority
(overdue revision → coaching homework → pre/post-class → test prep → backlog →
doubts). Every day is bounded by the CY ceiling
(``study_domain.adaptive_target``) and the committed-minute budget
(``config.settings.max_daily_committed_minutes``).

Nothing is written and no LLM is involved. Blocks that cannot be placed (no
gap, or the day is already over capacity) are returned under ``unplaced`` with
an explicit reason; capacity and warnings are surfaced on the result.

Usage::

    plan = build_plan(target_date="2026-08-02", days=1, db_path=...)
    plan = plan_tomorrow(db_path=...)
    plan = plan_week(days=7, db_path=...)
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any, Iterable

import ntsc_coaching
import session_context
import study_domain
from config import settings

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "sqlite_mirror.db"

DAY_START = "08:00"
DAY_END = "22:00"
DEFAULT_CLASS_MINUTES = 60

KINDS: tuple[str, ...] = (
    "Coaching Class",
    "Doubt Window",
    "Pre-Class Prep",
    "Post-Class Consolidation",
    "Revision",
    "Coaching Homework",
    "Backlog",
    "Test Prep",
    "Mock Prep",
    "Doubt Work",
    "Doubt Reattempt",
    "Planned Item",
)

# Deterministic priority tiers (higher = placed first into free gaps).
PRIO_FIXED = 100
PRIO_MOCK_PREP = 90
PRIO_REVISION = 88
PRIO_HOMEWORK = 84
PRIO_TEST_PREP = 78
PRIO_PREPOST = 76
PRIO_DOUBT_WINDOW = 74
PRIO_PLANNED = 70
PRIO_BACKLOG = 66
PRIO_DOUBT_REATTEMPT = 55

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HHMM_RE = re.compile(r"^\d{2}:\d{2}$")
_CLOCK_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::\d{2})?\s*([AaPp][Mm])?$")


# ---------------------------------------------------------------------------
# small numeric / parsing helpers
# ---------------------------------------------------------------------------

def _positive_int(value: Any, default: int | None = None) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _to_min(text: str) -> int:
    match = _HHMM_RE.match(str(text).strip())
    if not match:
        raise ValueError(f"invalid time {text!r}")
    return int(match.group(0)[:2]) * 60 + int(match.group(0)[3:5])


def _clock_to_hhmm(text: Any) -> str | None:
    """Normalize a portal clock (H:MM, HH:MM[:SS], H:MM AM/PM) to HH:MM.

    The portal cache stores ``start_time`` verbatim, so it may arrive as
    ``9:30`` or ``09:30 AM``. The lifecycle module already parses those; the
    planner must not crash on them.
    """
    match = _CLOCK_RE.match(str(text or "").strip())
    if not match:
        return None
    hour, minute, ampm = int(match.group(1)), int(match.group(2)), (match.group(3) or "").lower()
    if not 0 <= minute <= 59:
        return None
    if ampm:
        if not 1 <= hour <= 12:
            return None
        if hour == 12:
            hour = 0
        if ampm == "p":
            hour += 12
    elif hour > 23:
        return None
    return f"{hour:02d}:{minute:02d}"


def _to_hhmm(minutes: int) -> str:
    minutes %= 24 * 60
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _parse_date(value: Any) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _relation_id(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, list):
        return str(value[0]) if value else None
    text = str(value)
    try:
        decoded = json.loads(text)
    except (TypeError, ValueError):
        return text
    if isinstance(decoded, list):
        return str(decoded[0]) if decoded else None
    return str(decoded) if decoded else None


def _stable_id(source: Any) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", str(source)).strip("-")


def _rows(
    db_key: str, where: str, params: Iterable[Any] = (),
    *, db_path: str | Path = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    return study_domain._rows(db_key, where, params, db_path=db_path)


# ---------------------------------------------------------------------------
# block construction + validation
# ---------------------------------------------------------------------------

def _block(
    *, kind: str, title: str, date: str, priority: int, reason: str,
    evidence: dict[str, Any], source: str, start: str | None = None,
    end: str | None = None, duration_min: int | None = None,
    expected_cy: int | float | None = None, placed: bool = True,
    skip_reason: str | None = None,
) -> dict[str, Any]:
    if duration_min is None and start is not None and end is not None:
        duration_min = _to_min(end) - _to_min(start)
    if duration_min is not None and end is None and start is not None:
        end = _to_hhmm(_to_min(start) + duration_min)
    block = {
        "id": _stable_id(source),
        "kind": kind,
        "title": str(title).strip(),
        "date": date,
        "start": start,
        "end": end,
        "duration_min": duration_min,
        "priority": priority,
        "reason": reason,
        "evidence": dict(evidence or {}),
        "source": source,
        "expected_cy": expected_cy,
        "placed": placed,
        "skip_reason": skip_reason,
    }
    return _validate_block(block)


def _validate_block(block: dict[str, Any]) -> dict[str, Any]:
    kind = block.get("kind")
    if kind not in KINDS:
        raise ValueError(f"unknown block kind {kind!r}")
    if not str(block.get("title") or "").strip():
        raise ValueError("block needs a title")
    date = block.get("date")
    if not isinstance(date, str) or not _ISO_DATE_RE.match(date):
        raise ValueError(f"invalid block date {date!r}")
    start, end = block.get("start"), block.get("end")
    if start is not None and not _HHMM_RE.match(str(start)):
        raise ValueError(f"invalid block start time {start!r}")
    if end is not None and not _HHMM_RE.match(str(end)):
        raise ValueError(f"invalid block end time {end!r}")
    if start is None and end is not None:
        raise ValueError("block has an end time but no start time")
    if (
        start is not None and end is not None
        and _to_min(end) <= _to_min(start)
        and block.get("duration_min") is None
    ):
        raise ValueError("block end time must be after the start time")
    duration = block.get("duration_min")
    if duration is not None and (not isinstance(duration, int) or duration <= 0):
        raise ValueError(f"invalid block duration {duration!r}")
    priority = block.get("priority")
    if not isinstance(priority, int) or not 1 <= priority <= 100:
        raise ValueError(f"invalid block priority {priority!r}")
    return block


# ---------------------------------------------------------------------------
# per-source block builders
# ---------------------------------------------------------------------------

def _class_title(row: dict[str, Any]) -> str:
    subjects = str(row.get("subjects") or "").strip()
    class_type = str(row.get("class_type") or "Class").strip()
    return f"{subjects} {class_type}".strip() or "Coaching class"


def _class_block(row: dict[str, Any], index: int) -> dict[str, Any]:
    date = str(row["class_date"])[:10]
    start = _clock_to_hhmm(row.get("start_time")) or ""
    duration = _positive_int(row.get("duration_min"))
    end = None
    if start:
        if duration is not None:
            end = _to_hhmm(_to_min(start) + duration)
        else:
            end = _to_hhmm(_to_min(start) + DEFAULT_CLASS_MINUTES)
    return _block(
        kind="Coaching Class",
        title=_class_title(row),
        date=date,
        start=start or None,
        end=end,
        duration_min=duration if duration is not None else (DEFAULT_CLASS_MINUTES if start else None),
        priority=PRIO_FIXED,
        reason="Fixed coaching class from the portal schedule",
        evidence={
            "class_type": row.get("class_type"),
            "subjects": row.get("subjects"),
            "live_class": bool(row.get("live_class")),
            "duration_defaulted": duration is None,
        },
        source=f"class:{date}|{start}|{index}",
    )


def _window_block(window: dict[str, Any], date: str) -> dict[str, Any] | None:
    start = window["starts_at"].strftime("%H:%M")
    end = window["ends_at"].strftime("%H:%M")
    duration = _positive_int(
        int((window["ends_at"] - window["starts_at"]).total_seconds() // 60)
    )
    if duration is None or _to_min(end) <= _to_min(start):
        return None
    return _block(
        kind="Doubt Window",
        title=str(window.get("title") or "Teacher doubt window").strip(),
        date=date,
        start=start,
        end=end,
        duration_min=duration,
        priority=PRIO_FIXED,
        reason="Scheduled teacher doubt window",
        evidence={
            "subject": window.get("subject"),
            "teacher": window.get("teacher"),
            "questions_allowed": bool(window.get("questions_allowed")),
        },
        source=f"window:{window.get('notion_page_id')}",
    )


def _plan_item_block(row: dict[str, Any]) -> dict[str, Any]:
    date = str(row.get("plan_date") or "")[:10]
    return _block(
        kind="Planned Item",
        title=str(row.get("title") or "Plan item").strip(),
        date=date,
        duration_min=_positive_int(row.get("estimated_min")),
        expected_cy=_positive_int(row.get("expected_cy")) or 0,
        priority=_positive_int(row.get("priority"), PRIO_PLANNED),
        reason="Committed daily-plan item; consumes capacity even without a fixed time",
        evidence={
            "sequence": row.get("sequence"),
            "status": row.get("status"),
            "work_item": _relation_id(row.get("work_item")),
        },
        source=f"plan:{row.get('notion_page_id') or row.get('id')}",
    )


def _revision_block(row: dict[str, Any], date: str, cfg: dict[str, Any]) -> dict[str, Any]:
    chapter = str(row.get("chapter_module") or "revision item").strip()
    due = str(row.get("next_execution_date") or "")[:10]
    return _block(
        kind="Revision",
        title=f"Revision: {chapter}",
        date=date,
        duration_min=int(cfg["revision_minutes"]),
        priority=PRIO_REVISION,
        reason=f"Revision is due ({due})",
        evidence={
            "chapter_module": chapter,
            "subject": row.get("subject"),
            "next_execution_date": row.get("next_execution_date"),
            "mastery": row.get("mastery"),
        },
        source=f"revision:{row.get('notion_page_id') or row.get('id')}",
    )


def _homework_block(row: dict[str, Any], date: str, cfg: dict[str, Any]) -> dict[str, Any]:
    estimated = _positive_int(row.get("estimated_min"))
    duration = estimated or int(cfg["homework_minutes"])
    return _block(
        kind="Coaching Homework",
        title=str(row.get("title") or "Coaching homework").strip(),
        date=date,
        duration_min=duration,
        expected_cy=_positive_int(row.get("expected_cy")),
        priority=max(_positive_int(row.get("priority"), 0), PRIO_HOMEWORK),
        reason="Current coaching homework",
        evidence={
            "due_date": row.get("due_date"),
            "planned_date": row.get("planned_date"),
            "subject": row.get("subject"),
            "duration_defaulted": estimated is None,
        },
        source=f"homework:{row.get('notion_page_id') or row.get('id')}",
    )


def _backlog_block(row: dict[str, Any], date: str, cfg: dict[str, Any]) -> dict[str, Any]:
    estimated = _positive_int(row.get("estimated_min"))
    duration = estimated or int(cfg["backlog_minutes"])
    return _block(
        kind="Backlog",
        title=str(row.get("title") or "Backlog item").strip(),
        date=date,
        duration_min=duration,
        expected_cy=_positive_int(row.get("expected_cy")),
        priority=max(_positive_int(row.get("priority"), 0), PRIO_BACKLOG),
        reason="Tracked backlog / inbox work item",
        evidence={
            "status": row.get("status"),
            "priority": row.get("priority"),
            "subject": row.get("subject"),
            "due_date": row.get("due_date"),
        },
        source=f"backlog:{row.get('notion_page_id') or row.get('id')}",
    )


def _test_prep_block(test: dict[str, Any], date: str, cfg: dict[str, Any]) -> dict[str, Any]:
    days_to = (test["test_date_day"] - dt.date.fromisoformat(date)).days
    syllabus = str(test.get("syllabus") or "").strip()
    reason = f"Prepare for {test['title']} on {test['test_date']}"
    evidence: dict[str, Any] = {
        "test": test["title"],
        "test_date": test["test_date"],
        "days_to_test": days_to,
        "origin": test["origin"],
    }
    if syllabus:
        reason = f"{reason} — {syllabus[:120]}"
        evidence["syllabus"] = syllabus
    return _block(
        kind="Test Prep",
        title=f"Test prep: {test['title']}",
        date=date,
        duration_min=int(cfg["test_prep_minutes"]),
        priority=PRIO_TEST_PREP,
        reason=reason,
        evidence=evidence,
        source=f"test-prep:{test['source_id']}:{date}",
    )


def _mock_prep_block(test: dict[str, Any], date: str, cfg: dict[str, Any]) -> dict[str, Any]:
    days_to = (test["test_date_day"] - dt.date.fromisoformat(date)).days
    syllabus = str(test.get("syllabus") or "").strip()
    reason = f"Mock prep for {test['title']} on {test['test_date']}"
    evidence: dict[str, Any] = {
        "test": test["title"],
        "test_date": test["test_date"],
        "days_to_test": days_to,
        "origin": test["origin"],
    }
    if syllabus:
        reason = f"{reason} — {syllabus[:120]}"
        evidence["syllabus"] = syllabus
    return _block(
        kind="Mock Prep",
        title=f"Mock Prep: {test['title']}",
        date=date,
        duration_min=int(cfg["mock_prep_minutes"]),
        priority=PRIO_MOCK_PREP,
        reason=reason,
        evidence=evidence,
        source=f"mock-prep:{test['source_id']}:{date}",
    )


def _doubt_prep_block(
    window: dict[str, Any], doubts: list[dict[str, Any]], date: str, cfg: dict[str, Any],
    day_start_min: int,
) -> dict[str, Any] | None:
    window_min = _to_min(window["starts_at"].strftime("%H:%M"))
    prep = int(cfg["doubt_prep_minutes"])
    if window_min - prep < day_start_min:
        return None
    block = _block(
        kind="Doubt Work",
        title="Prepare teacher doubts",
        date=date,
        duration_min=prep,
        priority=PRIO_DOUBT_WINDOW,
        reason=f"Teacher window at {window['starts_at'].strftime('%H:%M')} — prepare {len(doubts)} ready doubt(s)",
        evidence={
            "window": window.get("title"),
            "window_start": window["starts_at"].strftime("%H:%M"),
            "doubts": [d.get("notion_page_id") for d in doubts],
            "concepts": [d.get("core_concept") or d.get("title") for d in doubts[:3]],
        },
        source=f"doubt-window:{window.get('notion_page_id')}",
    )
    block["_slot"] = (window_min - prep, window_min)
    return block


def _doubt_reattempt_block(
    doubts: list[dict[str, Any]], date: str, cfg: dict[str, Any],
) -> dict[str, Any]:
    concepts = [d.get("core_concept") or d.get("title") for d in doubts[:3]]
    return _block(
        kind="Doubt Reattempt",
        title="Reattempt unresolved doubts",
        date=date,
        duration_min=int(cfg["doubt_reattempt_minutes"]),
        priority=PRIO_DOUBT_REATTEMPT,
        reason=f"{len(doubts)} unresolved doubt(s) remain open",
        evidence={
            "doubts": [d.get("notion_page_id") for d in doubts],
            "concepts": concepts,
        },
        source=f"doubt-reattempt:{date}",
    )


def _pre_post_blocks(
    class_block: dict[str, Any], cfg: dict[str, Any],
    day_start_min: int, day_end_min: int,
) -> list[dict[str, Any]]:
    start, end = class_block.get("start"), class_block.get("end")
    if not start or not end:
        return []
    class_start = _to_min(start)
    class_end = _to_min(end)
    prep = int(cfg["pre_post_minutes"])
    blocks: list[dict[str, Any]] = []
    if class_start - prep >= day_start_min:
        pre = _block(
            kind="Pre-Class Prep",
            title=f"Pre-class prep: {class_block['title']}",
            date=class_block["date"],
            duration_min=prep,
            priority=PRIO_PREPOST,
            reason="Prepare the subject right before the fixed coaching class",
            evidence={"class": class_block["source"], "subjects": class_block["evidence"].get("subjects")},
            source=f"pre:{class_block['source']}",
        )
        pre["_slot"] = (class_start - prep, class_start)
        blocks.append(pre)
    if class_end + prep <= day_end_min:
        post = _block(
            kind="Post-Class Consolidation",
            title=f"Consolidate: {class_block['title']}",
            date=class_block["date"],
            duration_min=prep,
            priority=PRIO_PREPOST,
            reason="Consolidate notes and doubts immediately after the fixed coaching class",
            evidence={"class": class_block["source"], "subjects": class_block["evidence"].get("subjects")},
            source=f"post:{class_block['source']}",
        )
        post["_slot"] = (class_end, class_end + prep)
        blocks.append(post)
    return blocks


# ---------------------------------------------------------------------------
# day placement
# ---------------------------------------------------------------------------

def _free_gaps(
    fixed: list[dict[str, Any]], day_start_min: int, day_end_min: int,
) -> list[tuple[int, int]]:
    timed = sorted(
        (
            b for b in fixed
            if b.get("start") is not None and b.get("end") is not None
        ),
        key=lambda b: (_to_min(b["start"]), str(b["id"])),
    )
    gaps: list[tuple[int, int]] = []
    cursor = day_start_min
    for block in timed:
        start = _to_min(block["start"])
        end = _to_min(block["end"])
        if end <= start:
            continue
        if start > cursor:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < day_end_min:
        gaps.append((cursor, day_end_min))
    return gaps


def _place_day(
    *, date: str, fixed: list[dict[str, Any]], committed: list[dict[str, Any]],
    anchored: list[dict[str, Any]], generic: list[dict[str, Any]],
    cfg: dict[str, Any], day_start_min: int, day_end_min: int,
    cy_capacity: int, warnings: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    placed: list[dict[str, Any]] = list(fixed) + list(committed)
    gaps = _free_gaps(fixed, day_start_min, day_end_min)
    unplaced: list[dict[str, Any]] = []

    budget = int(cfg["max_daily_minutes"])
    planned_minutes = sum(b.get("duration_min") or 0 for b in placed)
    planned_cy = float(sum(b.get("expected_cy") or 0 for b in placed))

    def fits(block: dict[str, Any]) -> bool:
        return (
            planned_minutes + (block.get("duration_min") or 0) <= budget
            and planned_cy + float(block.get("expected_cy") or 0) <= cy_capacity
        )

    def mark(block: dict[str, Any], reason: str) -> dict[str, Any]:
        out = {k: v for k, v in block.items() if k != "_slot"}
        out["placed"] = False
        out["skip_reason"] = reason
        return out

    def consume(start: int, end: int) -> None:
        nonlocal gaps
        merged: list[tuple[int, int]] = []
        for gap_start, gap_end in gaps:
            if end <= gap_start or start >= gap_end:
                merged.append((gap_start, gap_end))
                continue
            if gap_start < start:
                merged.append((gap_start, start))
            if gap_end > end:
                merged.append((end, gap_end))
        gaps = [g for g in merged if g[1] > g[0]]

    ordered_anchored = sorted(
        anchored, key=lambda c: (c.get("_slot", (24 * 60, 24 * 60))[0], str(c["id"]))
    )
    for candidate in ordered_anchored:
        slot = candidate.pop("_slot", None)
        if not slot:
            unplaced.append(mark(candidate, "no feasible slot"))
            continue
        slot_start, slot_end = slot
        if not any(gap_start <= slot_start and slot_end <= gap_end for gap_start, gap_end in gaps):
            unplaced.append(mark(candidate, "no free gap adjacent to the class"))
            continue
        if not fits(candidate):
            unplaced.append(mark(candidate, "capacity exceeded"))
            continue
        candidate["start"] = _to_hhmm(slot_start)
        candidate["end"] = _to_hhmm(slot_end)
        placed.append(candidate)
        consume(slot_start, slot_end)
        planned_minutes += candidate["duration_min"]
        planned_cy += float(candidate.get("expected_cy") or 0)

    ordered_generic = sorted(
        generic, key=lambda c: (-int(c["priority"]), str(c["id"]))
    )
    for candidate in ordered_generic:
        if not fits(candidate):
            unplaced.append(mark(candidate, "capacity exceeded"))
            continue
        duration = candidate.get("duration_min") or 0
        chosen = next(
            (
                (gap_start, gap_end)
                for gap_start, gap_end in gaps if gap_end - gap_start >= duration
            ),
            None,
        )
        if chosen is None:
            unplaced.append(mark(candidate, f"no free gap of {duration} minutes"))
            continue
        slot_start = chosen[0]
        slot_end = slot_start + duration
        candidate["start"] = _to_hhmm(slot_start)
        candidate["end"] = _to_hhmm(slot_end)
        placed.append(candidate)
        consume(slot_start, slot_end)
        planned_minutes += duration
        planned_cy += float(candidate.get("expected_cy") or 0)

    placed.sort(key=lambda b: (_to_min(b.get("start") or "00:00"), str(b["id"])))
    if planned_minutes > budget:
        warnings.append(
            f"{date} is already over the {budget} minute budget "
            f"(planned {planned_minutes} minutes)"
        )
    if planned_cy > cy_capacity:
        warnings.append(
            f"{date} is already over the {cy_capacity} CY ceiling "
            f"(planned {planned_cy:g} CY)"
        )
    capacity_row = {
        "fixed_minutes": sum(b.get("duration_min") or 0 for b in fixed),
        "committed_minutes": sum(b.get("duration_min") or 0 for b in committed),
        "planned_minutes": planned_minutes,
        "budget_minutes": budget,
        "minutes_headroom": max(0, budget - planned_minutes),
        "planned_cy": round(planned_cy, 1),
        "cy_capacity": cy_capacity,
        "cy_headroom": max(0, round(cy_capacity - planned_cy, 1)),
        "placed_blocks": len(placed),
        "unplaced_blocks": len(unplaced),
    }
    return placed, unplaced, capacity_row


# ---------------------------------------------------------------------------
# data gathering
# ---------------------------------------------------------------------------

def _nearest_test(start_day: dt.date, *, db_path: str | Path) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for row in ntsc_coaching.next_tests(today=start_day.isoformat(), limit=20, db_path=db_path):
        day = _parse_date(row.get("test_date"))
        if day is None:
            continue
        candidates.append({
            "source_id": f"portal:{row.get('source_id')}",
            "title": str(row.get("title") or "Coaching test").strip(),
            "test_date": day.isoformat(),
            "test_date_day": day,
            "syllabus": row.get("syllabus"),
            "origin": "portal",
        })
    for row in _rows(
        "exams", "archived=0 AND status='Planned' AND kind='Coaching Test'", db_path=db_path
    ):
        day = _parse_date(row.get("exam_date"))
        if day is None:
            continue
        candidates.append({
            "source_id": f"exam:{row.get('notion_page_id') or row.get('id')}",
            "title": str(row.get("title") or "Coaching test").strip(),
            "test_date": day.isoformat(),
            "test_date_day": day,
            "syllabus": row.get("syllabus"),
            "origin": "exams",
        })
    future = [c for c in candidates if c["test_date_day"] >= start_day]
    if not future:
        return None
    future.sort(key=lambda c: (c["test_date_day"], c["origin"]))
    return future[0]


def _due_day(
    row: dict[str, Any], start_day: dt.date, end_day: dt.date,
) -> dt.date | None:
    day = _parse_date(row.get("due_date") or row.get("planned_date"))
    if day is None:
        return start_day
    if day < start_day:
        return start_day
    if day > end_day:
        return None
    return day


def _plan_work_ids(plan_items: list[dict[str, Any]]) -> set[str]:
    ids: set[str] = set()
    for item in plan_items:
        linked = _relation_id(item.get("work_item"))
        if linked:
            ids.add(linked)
        marker = re.search(
            r"SQLite work item:\s*([A-Za-z0-9-]+)",
            str(item.get("planner_note") or ""),
            re.IGNORECASE,
        )
        if marker:
            ids.add(marker.group(1))
    return ids


def _planned_chapter_keys(
    plan_items: list[dict[str, Any]], *, db_path: str | Path,
) -> set[str]:
    keys: set[str] = set()
    for item in plan_items:
        title = re.sub(r"\s+", " ", str(item.get("title") or "").strip().lower())
        if title:
            keys.add(title)
        linked = _relation_id(item.get("work_item"))
        if linked:
            work = study_domain._row("work_items", "notion_page_id=?", (linked,), db_path=db_path)
            if work:
                for field in ("chapter", "title"):
                    value = re.sub(r"\s+", " ", str(work.get(field) or "").strip().lower())
                    if value:
                        keys.add(value)
    return keys


def capacity_settings(
    *, capacity: dict[str, Any] | None = None, db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Effective capacity knobs; a caller-supplied dict overrides the defaults."""
    base: dict[str, Any] = {
        "day_start": DAY_START,
        "day_end": DAY_END,
        "max_daily_minutes": settings.max_daily_committed_minutes(),
        "pre_post_minutes": 30,
        "revision_minutes": 30,
        "homework_minutes": 45,
        "backlog_minutes": 45,
        "test_prep_minutes": 45,
        "mock_prep_minutes": 60,
        "test_prep_days": 3,
        "doubt_prep_minutes": 20,
        "doubt_reattempt_minutes": 15,
        "cy_capacity": None,
    }
    if capacity:
        base.update({str(k): v for k, v in capacity.items()})
    return base


# ---------------------------------------------------------------------------
# public entry points
# ---------------------------------------------------------------------------

def build_plan(
    *, target_date: str | None = None, days: int = 1,
    chat_id: int | str | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
    capacity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a deterministic plan for ``days`` starting at ``target_date``.

    ``target_date`` defaults to today (local). ``days=1`` produces a daily
    plan; more produces a weekly plan. Returns validated ``blocks`` (placed)
    plus ``unplaced`` candidates, per-day ``capacity``, and ``warnings``.
    Read-only: nothing is written and no LLM is involved.
    """
    if not isinstance(days, int) or days < 1:
        raise ValueError("days must be a positive integer")
    today = session_context.local_today_iso()
    start_day = dt.date.fromisoformat((target_date or today)[:10])
    end_day = start_day + dt.timedelta(days=days - 1)
    dates = [(start_day + dt.timedelta(days=i)).isoformat() for i in range(days)]
    cfg = capacity_settings(capacity=capacity, db_path=db_path)
    day_start_min = _to_min(cfg["day_start"])
    day_end_min = _to_min(cfg["day_end"])

    classes_by_date: dict[str, list[dict[str, Any]]] = {}
    for date in dates:
        classes_by_date[date] = ntsc_coaching.classes_for_date(date, db_path=db_path)

    now = session_context.local_now()
    window_days = max(0, (end_day - now.date()).days)
    windows_by_date: dict[str, list[dict[str, Any]]] = {}
    for window in study_domain.upcoming_teacher_windows(
        now=now, days=window_days, db_path=db_path
    ):
        windows_by_date.setdefault(window["starts_at"].date().isoformat(), []).append(window)

    plan_items_by_date: dict[str, list[dict[str, Any]]] = {}
    planned_work_ids: dict[str, set[str]] = {}
    planned_chapters: dict[str, set[str]] = {}
    for date in dates:
        facts = study_domain.plan_facts(date, db_path=db_path)
        active = facts.get("active_items") or []
        plan_items_by_date[date] = active
        planned_work_ids[date] = _plan_work_ids(active)
        planned_chapters[date] = _planned_chapter_keys(active, db_path=db_path)

    homework_rows = _rows(
        "work_items",
        "archived=0 AND kind='Coaching Homework' AND status NOT IN ('Completed','Dismissed')",
        db_path=db_path,
    )
    backlog_rows = _rows(
        "work_items",
        "archived=0 AND status IN ('Backlog','Inbox') AND kind<>'Coaching Homework'",
        db_path=db_path,
    )
    revision_rows = _rows(
        "revision",
        "archived=0 AND next_execution_date IS NOT NULL "
        "AND LOWER(COALESCE(status,''))<>'completed' "
        "AND substr(COALESCE(next_execution_date,''),1,10)<=?",
        (end_day.isoformat(),), db_path=db_path,
    )
    doubts = study_domain.doubt_queue(db_path=db_path)
    unresolved_doubts = [d for d in doubts if not d.get("teacher_ready")]
    teacher_ready_doubts = [d for d in doubts if d.get("teacher_ready")]

    by_date: dict[str, dict[str, list[dict[str, Any]]]] = {
        date: {"fixed": [], "committed": [], "anchored": [], "generic": []}
        for date in dates
    }

    for date in dates:
        for index, row in enumerate(classes_by_date[date]):
            class_block = _class_block(row, index)
            by_date[date]["fixed"].append(class_block)
            by_date[date]["anchored"].extend(
                _pre_post_blocks(class_block, cfg, day_start_min, day_end_min)
            )
        for window in windows_by_date.get(date, []):
            window_block = _window_block(window, date)
            if window_block is None:
                continue
            by_date[date]["fixed"].append(window_block)
            if teacher_ready_doubts:
                prep = _doubt_prep_block(
                    window, teacher_ready_doubts, date, cfg, day_start_min,
                )
                if prep is not None:
                    by_date[date]["anchored"].append(prep)
        for row in plan_items_by_date[date]:
            by_date[date]["committed"].append(_plan_item_block(row))

    for row in homework_rows:
        due = _due_day(row, start_day, end_day)
        if due is None:
            continue
        date = due.isoformat()
        if row.get("notion_page_id") in planned_work_ids[date]:
            continue
        by_date[date]["generic"].append(_homework_block(row, date, cfg))

    for index, row in enumerate(backlog_rows):
        if row.get("notion_page_id") in planned_work_ids[dates[index % days]]:
            continue
        date = dates[index % days]
        by_date[date]["generic"].append(_backlog_block(row, date, cfg))

    overdue_revision = [
        row for row in revision_rows
        if (_parse_date(row.get("next_execution_date")) or end_day) < start_day
    ]
    for index, row in enumerate(overdue_revision):
        date = dates[index % days]
        chapter = re.sub(r"\s+", " ", str(row.get("chapter_module") or "").strip().lower())
        if chapter and chapter in planned_chapters[date]:
            continue
        by_date[date]["generic"].append(_revision_block(row, date, cfg))
    for row in revision_rows:
        due = _parse_date(row.get("next_execution_date"))
        if due is None or due < start_day or due > end_day:
            continue
        date = due.isoformat()
        chapter = re.sub(r"\s+", " ", str(row.get("chapter_module") or "").strip().lower())
        if chapter and chapter in planned_chapters[date]:
            continue
        by_date[date]["generic"].append(_revision_block(row, date, cfg))

    nearest_test = _nearest_test(start_day, db_path=db_path)
    if nearest_test is not None:
        for date in dates:
            days_to = (nearest_test["test_date_day"] - dt.date.fromisoformat(date)).days
            if 1 <= days_to <= 2:
                # T-2 and T-1: a dedicated Mock Prep block replaces the generic
                # Test Prep on the two days before the nearest test.
                by_date[date]["generic"].append(_mock_prep_block(nearest_test, date, cfg))
            elif 0 <= days_to <= int(cfg["test_prep_days"]):
                by_date[date]["generic"].append(_test_prep_block(nearest_test, date, cfg))

    for date in dates:
        has_doubt_work = any(
            c.get("kind") == "Doubt Work" for c in by_date[date]["anchored"]
        )
        if unresolved_doubts and not has_doubt_work:
            by_date[date]["generic"].append(_doubt_reattempt_block(unresolved_doubts, date, cfg))

    blocks: list[dict[str, Any]] = []
    unplaced: list[dict[str, Any]] = []
    warnings: list[str] = []
    capacity_map: dict[str, dict[str, Any]] = {}
    for date in dates:
        cfg_cap = cfg.get("cy_capacity")
        cy_capacity = (
            int(cfg_cap)
            if cfg_cap is not None
            else int(study_domain.adaptive_target(today=date, db_path=db_path)["ceiling"])
        )
        day_placed, day_unplaced, capacity_row = _place_day(
            date=date,
            fixed=by_date[date]["fixed"],
            committed=by_date[date]["committed"],
            anchored=by_date[date]["anchored"],
            generic=by_date[date]["generic"],
            cfg=cfg,
            day_start_min=day_start_min,
            day_end_min=day_end_min,
            cy_capacity=cy_capacity,
            warnings=warnings,
        )
        blocks.extend(day_placed)
        capacity_map[date] = capacity_row
        for candidate in day_unplaced:
            candidate = _validate_block(candidate)
            unplaced.append(candidate)
            warnings.append(
                f"could not place '{candidate['title']}' on {candidate['date']}: "
                f"{candidate['skip_reason']}"
            )

    for block in blocks:
        _validate_block(block)

    _check_overlaps(blocks, warnings)

    total_classes = sum(len(rows) for rows in classes_by_date.values())
    if total_classes == 0:
        warnings.append(
            f"no coaching classes cached for {dates[0]}..{dates[-1]}; "
            "run the portal sync first"
        )

    sources = {
        "classes": total_classes,
        "teacher_windows": sum(len(rows) for rows in windows_by_date.values()),
        "plan_items": sum(len(items) for items in plan_items_by_date.values()),
        "revision": len(revision_rows),
        "homework": len(homework_rows),
        "backlog": len(backlog_rows),
        "test_prep_blocks": sum(
            1 for group in by_date.values()
            for b in group["generic"] if b.get("kind") == "Test Prep"
        ),
        "mock_prep_blocks": sum(
            1 for group in by_date.values()
            for b in group["generic"] if b.get("kind") == "Mock Prep"
        ),
        "doubt_blocks": sum(
            1 for group in by_date.values()
            for b in group["anchored"] + group["generic"]
            if b.get("kind") in ("Doubt Work", "Doubt Reattempt")
        ),
    }

    return {
        "plan_type": "weekly" if days > 1 else "daily",
        "start_date": dates[0],
        "end_date": dates[-1],
        "dates": dates,
        "blocks": blocks,
        "unplaced": unplaced,
        "warnings": warnings,
        "capacity": capacity_map,
        "totals": {
            "planned_minutes": sum(row["planned_minutes"] for row in capacity_map.values()),
            "planned_cy": round(sum(row["planned_cy"] for row in capacity_map.values()), 1),
            "unplaced_count": len(unplaced),
            "days": len(dates),
        },
        "sources": sources,
        "generated_with": "deterministic",
        "llm_involved": False,
    }


def _check_overlaps(blocks: list[dict[str, Any]], warnings: list[str]) -> None:
    timed = [
        b for b in blocks
        if b.get("placed") and b.get("start") is not None and b.get("end") is not None
    ]
    by_date: dict[str, list[dict[str, Any]]] = {}
    for block in timed:
        by_date.setdefault(block["date"], []).append(block)
    for date, day_blocks in by_date.items():
        ordered = sorted(day_blocks, key=lambda b: (_to_min(b["start"]), str(b["id"])))
        for first, second in zip(ordered, ordered[1:]):
            if _to_min(second["start"]) < _to_min(first["end"]):
                warnings.append(
                    f"overlap on {date}: '{first['title']}' and '{second['title']}'"
                )


def plan_tomorrow(
    *, chat_id: int | str | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
    capacity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Deterministic coaching plan suggestion for tomorrow (local)."""
    tomorrow = (dt.date.fromisoformat(session_context.local_today_iso()) + dt.timedelta(days=1)).isoformat()
    return build_plan(target_date=tomorrow, days=1, chat_id=chat_id, db_path=db_path, capacity=capacity)


def plan_week(
    *, start_date: str | None = None, days: int = 7,
    chat_id: int | str | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
    capacity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Deterministic coaching plan suggestions for a week starting at ``start_date``."""
    return build_plan(
        target_date=start_date, days=days, chat_id=chat_id,
        db_path=db_path, capacity=capacity,
    )
