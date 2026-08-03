"""Bridge portal coaching data into the op_* operational tables.

Portal data (coaching_tests / coaching_classes) is auto-promoted into the
existing SQLite-owned read surface (op_exams / op_timetable) so every feature
that reads those tables — readiness reminders, exam pacing, timetable — sees
the portal's tests and classes without a separate manual entry.

The bridge is strictly additive:
  * never overwrites user edits to an already-promoted row (insert-only);
  * links a matching manual row (same title+date / weekday+start+subject) by
    tagging its operation_id, so future syncs recognize it and never duplicate;
  * on any failure logs and returns the partial counts — never breaks the sync.
"""

from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from pathlib import Path
from typing import Any

import ntsc_coaching
import operational_store
import session_context

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "sqlite_mirror.db"

_OP_ID_TESTS = "portal:{source_id}"
_OP_ID_TIMETABLE = "portal:timetable:{weekday}:{start}:{subject}"


def _compute_end_time(start: str, duration_min: Any) -> str | None:
    """end_time = start + duration_min as HH:MM, or None when unknown."""
    try:
        minutes = int(duration_min or 0)
    except (TypeError, ValueError):
        return None
    if minutes <= 0:
        return None
    try:
        hour_text, minute_text = start.split(":", 1)
        total = int(hour_text) * 60 + int(minute_text) + minutes
    except (TypeError, ValueError):
        return None
    return f"{total // 60 % 24:02d}:{total % 60:02d}"


def promote_tests_to_exams(*, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, int]:
    """Upsert portal tests into op_exams so readiness/reminders/pacing see them.

    For each coaching_test: if a row with operation_id='portal:{source_id}'
    already exists, skip (already promoted). Else if a manual exam with the
    same lower(title)+exam_date exists, link it by setting its operation_id
    (so future syncs recognize it and we don't duplicate). Else insert a new
    row with kind='Coaching Test', status='Planned', date_confidence='Confirmed'.
    Never overwrite user edits to an already-promoted row (insert-only, no
    update of mutable fields on re-sync — the portal date lives in
    coaching_tests for freshness; op_exams is the read surface).
    """
    counts = {"inserted": 0, "linked": 0, "skipped": 0}
    try:
        # Only promote UPCOMING tests (test_date >= today). Past tests are
        # already in coaching_results; promoting them as status='Planned'
        # would trigger spurious "has it finished?" reminders.
        today = session_context.local_today_iso()
        tests = ntsc_coaching.next_tests(today=today, limit=50, db_path=db_path)
        for row in tests:
            source_id = row.get("source_id")
            title = row.get("title")
            test_date = row.get("test_date")
            if not source_id or not test_date or not title:
                counts["skipped"] += 1
                continue
            op_id = _OP_ID_TESTS.format(source_id=source_id)
            if operational_store.rows(
                "exams", "operation_id=?", (op_id,), db_path=db_path
            ):
                counts["skipped"] += 1
                continue
            manual = operational_store.rows(
                "exams",
                "archived=0 AND LOWER(title)=? AND COALESCE(exam_date,'')=?",
                (str(title).lower(), str(test_date or "")),
                db_path=db_path,
            )
            if manual:
                operational_store.update(
                    "exams", manual[0]["id"], {"operation_id": op_id},
                    db_path=db_path,
                )
                counts["linked"] += 1
                continue
            operational_store.create("exams", {
                "title": title,
                "kind": "Coaching Test",
                "status": "Planned",
                "exam_date": test_date,
                "date_confidence": "Confirmed",
                "syllabus": row.get("syllabus"),
                "operation_id": op_id,
            }, db_path=db_path)
            counts["inserted"] += 1
    except Exception:
        logger.exception("promote_tests_to_exams failed")
    return counts


def promote_classes_to_timetable(*, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, int]:
    """Upsert portal classes into op_timetable as recurring weekly entries.

    Each coaching_class becomes a weekly-recurring op_timetable entry keyed by
    (weekday, start_time, subject). Same dedup/link/insert pattern as exams.
    end_time is computed from start_time + duration_min. weekday is the full
    weekday NAME (Monday/Tuesday/...). teacher is unknown from the portal
    (left None). kind = class_type or 'Class'.
    """
    counts = {"inserted": 0, "linked": 0, "skipped": 0}
    try:
        with ntsc_coaching._connect(db_path) as conn:
            raw = conn.execute(
                "SELECT class_date, start_time, duration_min, class_type, subjects "
                "FROM coaching_classes"
            ).fetchall()
        for row in raw:
            class_date = str(row["class_date"] or "")
            try:
                weekday_name = dt.date.fromisoformat(class_date[:10]).strftime("%A")
            except ValueError:
                counts["skipped"] += 1
                continue
            start = str(row["start_time"] or "").strip()
            if not start:
                counts["skipped"] += 1
                continue
            subject = str(row["subjects"] or "").strip()
            if not subject:
                counts["skipped"] += 1
                continue
            end_time = _compute_end_time(start, row["duration_min"])
            class_type = str(row["class_type"] or "")
            op_id = _OP_ID_TIMETABLE.format(
                weekday=weekday_name, start=start, subject=subject
            )
            if operational_store.rows(
                "timetable", "operation_id=?", (op_id,), db_path=db_path
            ):
                counts["skipped"] += 1
                continue
            manual = operational_store.rows(
                "timetable",
                "archived=0 AND active=1 AND LOWER(subject)=? AND weekday=? "
                "AND start_time=?",
                (subject.lower(), weekday_name, start),
                db_path=db_path,
            )
            if manual:
                operational_store.update(
                    "timetable", manual[0]["id"], {"operation_id": op_id},
                    db_path=db_path,
                )
                counts["linked"] += 1
                continue
            operational_store.create("timetable", {
                "title": f"{subject} {class_type or 'Class'}",
                "weekday": weekday_name,
                "start_time": start,
                "end_time": end_time,
                "subject": subject,
                "kind": class_type or "Class",
                "active": 1,
                "questions_allowed": 1,
                "operation_id": op_id,
            }, db_path=db_path)
            counts["inserted"] += 1
    except Exception:
        logger.exception("promote_classes_to_timetable failed")
    return counts
