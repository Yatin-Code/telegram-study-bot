#!/usr/bin/env python3
"""Repeatable live smoke test for the Notion-first study system.

This script deliberately stays outside the pytest suite because it performs
real network writes. Every Notion page is tagged with a unique run marker and
archived in a finally block. The Telegram check sends one silent message to the
configured owner and immediately deletes it.

Usage:
    python test_live_features.py
    python test_live_features.py --check-polling
    python test_live_features.py --skip-notion
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sqlite3
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, TypeVar
from unittest import mock

import httpx
from telegram import Bot
from telegram.error import Conflict

import bot as bot_module
import notion_client_wrapper as notion
import operational_store
import session_context
import study_domain
import sync
from config import notion_schema
from config.settings import (
    notion_db_id,
    telegram_allowed_user_id,
    telegram_bot_token,
)


T = TypeVar("T")
TITLE_COLUMNS = {
    "ledger": "task",
    "doubts": "core_concept",
    "revision": "chapter_module",
    "work_items": "title",
    "goals": "title",
    "exams": "title",
    "exam_questions": "title",
    "doubt_attempts": "title",
    "timetable": "title",
    "daily_plan": "title",
}
TOUCHED_DATABASES = (
    "ledger", "doubts", "revision", "daily_plan",
)


class SmokeFailure(AssertionError):
    """Raised when a live invariant is not satisfied."""


class Reporter:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.warnings: list[str] = []

    def check(self, label: str, condition: bool, detail: str = "") -> None:
        if condition:
            self.passed += 1
            print(f"  [OK] {label}")
            return
        self.failed += 1
        suffix = f": {detail}" if detail else ""
        print(f"  [FAIL] {label}{suffix}")
        raise SmokeFailure(f"{label}{suffix}")

    def warn(self, text: str) -> None:
        self.warnings.append(text)
        print(f"  [WARN] {text}")


def _safe_error(exc: BaseException) -> str:
    """Return an error summary without leaking configured credentials."""
    text = str(exc)
    for secret_getter in (telegram_bot_token,):
        try:
            secret = secret_getter()
        except Exception:
            continue
        if secret:
            text = text.replace(secret, "<redacted>")
    return f"{type(exc).__name__}: {text[:300]}"


def _with_retry(action: Callable[[], T], *, attempts: int = 4) -> T:
    """Retry transient Notion/network failures; writes use operation IDs."""
    delay = 1.0
    for number in range(1, attempts + 1):
        try:
            return action()
        except (notion.NotionRateLimitError, httpx.TransportError):
            if number == attempts:
                raise
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("unreachable retry state")


def _page_id(page: dict[str, Any]) -> str:
    page_id = str(page.get("id") or "").strip()
    if not page_id:
        raise SmokeFailure("Notion create response did not include a page ID")
    return page_id


def _archive_page(page_id: str) -> None:
    _with_retry(
        lambda: notion._request(  # noqa: SLF001 - live-test cleanup primitive
            "PATCH",
            f"{notion.NOTION_API}/pages/{page_id}",
            json_body={"archived": True},
        )
    )


def _discover_tagged_pages(tag: str) -> set[str]:
    """Find pages from an interrupted run by their unique title marker."""
    found: set[str] = set()
    for db_key in sync.NOTION_SOURCE_KEYS:
        title_key = TITLE_COLUMNS[db_key]
        title_name = notion_schema.PROPERTIES_BY_DB[db_key][title_key]["notion_name"]
        pages = _with_retry(
            lambda key=db_key, prop=title_name: notion.query_database(
                key,
                filter={"property": prop, "title": {"contains": tag}},
                max_pages=2,
            )
        )
        found.update(str(page["id"]) for page in pages if page.get("id"))
        time.sleep(0.35)
    return found


def _verify_live_schemas(reporter: Reporter) -> None:
    print("\nNotion schema checks")
    schema_failures: list[str] = []
    for db_key in sync.NOTION_SOURCE_KEYS:
        expected_props = notion_schema.PROPERTIES_BY_DB[db_key]
        raw = _with_retry(
            lambda key=db_key: notion._request(  # noqa: SLF001 - schema probe
                "GET", f"{notion.NOTION_API}/databases/{notion_db_id(key)}"
            )
        )
        live_props = raw.get("properties", {})
        problems: list[str] = []
        for prop in expected_props.values():
            live = live_props.get(prop["notion_name"])
            if live is None:
                problems.append(f"missing {prop['notion_name']}")
            elif live.get("type") != prop["type"]:
                problems.append(
                    f"{prop['notion_name']} is {live.get('type')}, expected {prop['type']}"
                )
        if problems:
            schema_failures.append(f"{db_key}: {', '.join(problems)}")
            print(f"  [FAIL] {db_key}: {len(problems)} schema mismatch(es)")
        else:
            reporter.passed += 1
            print(f"  [OK] {db_key}: {len(expected_props)} modeled properties")
        time.sleep(0.35)
    if schema_failures:
        reporter.failed += len(schema_failures)
        raise SmokeFailure("; ".join(schema_failures))


def _mirror_row(db_path: Path, table: str, page_id: str) -> sqlite3.Row | None:
    with sync.connect(db_path) as conn:
        sync.init_db(conn)
        return conn.execute(
            f'SELECT * FROM "{table}" WHERE notion_page_id=?', (page_id,)
        ).fetchone()


def _register_generated_exam_pages(exam_id: str, created: set[str]) -> None:
    operation_ids = {
        "work_items": f"exam-review-work:{exam_id}",
        "daily_plan": f"exam-review-plan:{exam_id}",
    }
    for db_key, operation_id in operation_ids.items():
        pages = _with_retry(
            lambda key=db_key, op=operation_id: notion.query_database(
                key,
                filter={
                    "property": "Operation ID",
                    "rich_text": {"equals": op},
                },
                max_pages=1,
            )
        )
        created.update(str(page["id"]) for page in pages if page.get("id"))
        time.sleep(0.35)


def _assert_no_active_tagged_rows(tag: str) -> bool:
    with sync.connect(sync.DEFAULT_DB_PATH) as conn:
        sync.init_db(conn)
        for table in sync.NOTION_SOURCE_KEYS:
            title_col = TITLE_COLUMNS[table]
            row = conn.execute(
                f'SELECT COUNT(*) AS n FROM "{table}" '
                f'WHERE archived=0 AND "{title_col}" LIKE ?',
                (f"%{tag}%",),
            ).fetchone()
            if row["n"]:
                return False
    return True


def run_notion_smoke(reporter: Reporter, tag: str) -> None:
    _verify_live_schemas(reporter)
    print("\nNotion workflow checks")

    created: set[str] = set()
    cleanup_errors: list[str] = []
    today = dt.date.fromisoformat(session_context.local_today_iso())
    isolated_day = today + dt.timedelta(days=730)
    now = session_context.local_now()
    operation_prefix = f"live-smoke:{tag}"

    with tempfile.TemporaryDirectory(prefix="study-bot-live-") as tmp:
        db_path = Path(tmp) / "mirror.db"
        try:
            goal_data = {
                "title": f"{tag} daily CY goal",
                "goal_type": "CY",
                "status": "Draft",
                "metric": "Cognitive Yield",
                "target": 300,
                "minimum": 240,
                "period": "Daily",
                "priority": 90,
                "hard_constraint": False,
                "source_text": "Live smoke test",
                "operation_id": f"{operation_prefix}:goal",
            }
            goal = _with_retry(lambda: study_domain.create_goal(goal_data, db_path=db_path))
            goal_id = _page_id(goal)
            created.add(goal_id)
            duplicate = _with_retry(lambda: study_domain.create_goal(goal_data, db_path=db_path))
            reporter.check("operation-ID retry returns the same goal", _page_id(duplicate) == goal_id)
            time.sleep(0.4)

            exam_title = f"{tag} JEE mock"
            exam = _with_retry(
                lambda: study_domain.create_exam(
                    {
                        "title": exam_title,
                        "kind": "JEE Main Mock",
                        "exam_date": isolated_day.isoformat(),
                        "date_confidence": "Tentative",
                        "max_marks": 300,
                        "target_marks": 220,
                        "operation_id": f"{operation_prefix}:exam",
                    },
                    db_path=db_path,
                )
            )
            exam_id = _page_id(exam)
            created.add(exam_id)
            reporter.check("exam is created in the live Exams database", bool(exam_id))
            time.sleep(0.4)

            work_title = f"{tag} Physics PYQ backlog"
            work = _with_retry(
                lambda: study_domain.create_work_item(
                    {
                        "title": work_title,
                        "kind": "PYQ",
                        "status": "Backlog",
                        "subject": "Physics",
                        "chapter": "Rotation",
                        "planned_date": isolated_day.isoformat(),
                        "priority": 80,
                        "estimated_min": 120,
                        "expected_cy": 120,
                        "remaining_units": 20,
                        "total_units": 20,
                        "goal": goal_id,
                        "exam": exam_id,
                        "interruptible": True,
                        "exit_condition": "Attempt 20 PYQs and log the block",
                        "operation_id": f"{operation_prefix}:work",
                    },
                    db_path=db_path,
                )
            )
            work_id = _page_id(work)
            created.add(work_id)

            plan = _with_retry(
                lambda: study_domain.create_plan_item(
                    {
                        "title": f"{tag} sequential block",
                        "plan_date": isolated_day.isoformat(),
                        "sequence": 1,
                        "work_item": work_id,
                        "subject": "Physics",
                        "kind": "PYQ",
                        "expected_cy": 120,
                        "estimated_min": 120,
                        "priority": 80,
                        "interruptible": True,
                        "exit_condition": "Finish the selected PYQ set",
                        "operation_id": f"{operation_prefix}:plan",
                    },
                    db_path=db_path,
                )
            )
            plan_id = _page_id(plan)
            created.add(plan_id)
            sync.sync_once(db_path=db_path, db_keys=("goals", "exams", "work_items", "daily_plan"))
            work_row = _mirror_row(db_path, "work_items", work_id)
            plan_row = _mirror_row(db_path, "daily_plan", plan_id)
            reporter.check(
                "goal/exam/work/plan relations survive mirror sync",
                bool(
                    work_row
                    and goal_id in str(work_row["goal"])
                    and exam_id in str(work_row["exam"])
                    and plan_row
                    and work_id in str(plan_row["work_item"])
                ),
            )

            chat_state_id = f"live-{uuid.uuid4().hex}"
            activated = _with_retry(
                lambda: study_domain.activate_next_plan(
                    chat_state_id, isolated_day.isoformat(), db_path=db_path
                )
            )
            reporter.check(
                "sequential /next activation selects the linked plan item",
                bool(activated and activated.get("notion_page_id") == plan_id),
            )
            carried = _with_retry(
                lambda: study_domain.complete_active_plan(
                    chat_state_id, carry_to_backlog=True, db_path=db_path
                )
            )
            reporter.check(
                "carry action moves the plan and restores work to backlog",
                carried.get("status") == "Moved",
            )

            window_date = isolated_day + dt.timedelta(days=1)
            timetable = _with_retry(
                lambda: study_domain._create(  # noqa: SLF001 - live domain probe
                    "timetable",
                    {
                        "title": f"{tag} Physics teacher window",
                        "weekday": notion_schema.WEEKDAY_OPTIONS[window_date.weekday()],
                        "start_time": "09:00",
                        "end_time": "10:00",
                        "subject": "Physics",
                        "teacher": "Live Test Teacher",
                        "kind": "Doubt Window",
                        "active": True,
                        "operation_id": f"{operation_prefix}:timetable",
                    },
                    db_path=db_path,
                )
            )
            timetable_id = _page_id(timetable)
            created.add(timetable_id)
            probe_now = dt.datetime.combine(
                window_date,
                dt.time(8, 45),
                tzinfo=now.tzinfo,
            )
            windows = study_domain.upcoming_teacher_windows(
                now=probe_now, days=1, db_path=db_path
            )
            reporter.check(
                "weekly timetable produces a teacher opportunity",
                any(row.get("notion_page_id") == timetable_id for row in windows),
            )

            doubt_title = f"{tag} torque sign doubt"
            doubt = _with_retry(
                lambda: study_domain._create(  # noqa: SLF001 - live domain probe
                    "doubts",
                    {
                        "core_concept": doubt_title,
                        "status": "Unresolved",
                        "workflow_state": "New",
                        "valid_attempts": 0,
                        "teacher_ready": False,
                        "teacher_asked": False,
                        "operation_id": f"{operation_prefix}:doubt",
                    },
                    db_path=db_path,
                )
            )
            doubt_id = _page_id(doubt)
            created.add(doubt_id)
            first = _with_retry(
                lambda: study_domain.record_doubt_attempt(
                    doubt_title,
                    duration_min=8,
                    approach="Resolved torque into components",
                    stuck_point="Sign after changing the reference axis",
                    attempted_at=now - dt.timedelta(minutes=65),
                    db_path=db_path,
                )
            )
            if first.get("page_id"):
                created.add(str(first["page_id"]))
            reporter.check(
                "first independent doubt attempt is valid but not teacher-ready",
                first.get("valid_attempts") == 1 and not first.get("teacher_ready"),
            )
            second = _with_retry(
                lambda: study_domain.record_doubt_attempt(
                    doubt_title,
                    duration_min=9,
                    approach="Re-derived with a free-body diagram",
                    stuck_point="Clockwise convention still disagrees",
                    attempted_at=now,
                    db_path=db_path,
                )
            )
            if second.get("page_id"):
                created.add(str(second["page_id"]))
            reporter.check(
                "two separated valid attempts unlock teacher escalation",
                second.get("valid_attempts") == 2 and second.get("teacher_ready") is True,
            )
            eligible = study_domain.eligible_doubts(db_path=db_path)
            reporter.check(
                "teacher-ready doubt appears in the eligible queue",
                any(row.get("notion_page_id") == doubt_id for row in eligible),
            )
            resolved = _with_retry(
                lambda: study_domain.resolve_doubt(
                    doubt_title,
                    "Teacher confirmed the clockwise sign convention.",
                    teacher_asked=True,
                    db_path=db_path,
                )
            )
            reporter.check(
                "teacher resolution is accepted after two valid attempts",
                resolved.get("workflow_state") == "Resolved",
            )

            with mock.patch.object(
                study_domain.session_context,
                "local_today_iso",
                return_value=isolated_day.isoformat(),
            ):
                finished = _with_retry(
                    lambda: study_domain.finish_exam(exam_title, db_path=db_path)
                )
            if finished.get("work_item_id"):
                created.add(str(finished["work_item_id"]))
            _register_generated_exam_pages(exam_id, created)
            reporter.check(
                "finishing an exam creates the mandatory paper-review work",
                finished.get("status") == "Analysing" and bool(finished.get("work_item_id")),
            )

            summary = _with_retry(
                lambda: study_domain.record_exam_summary(
                    exam_title,
                    {
                        "actual_marks": 18,
                        "attempted": 6,
                        "correct": 4,
                        "incorrect": 2,
                        "unattempted": 4,
                    },
                    db_path=db_path,
                )
            )
            reporter.check("exam summary persists signed marks and counts", summary.get("actual_marks") == 18)
            chapter = f"{tag} Rotation"
            question = _with_retry(
                lambda: study_domain.record_question_review(
                    exam_title,
                    {
                        "question_no": "7",
                        "subject": "Physics",
                        "chapter": chapter,
                        "attempted": True,
                        "correct": False,
                        "marks_awarded": -1,
                        "marks_lost": 5,
                        "time_min": 7,
                        "failure_type": "Concept",
                        "root_cause": "Used the wrong torque sign convention",
                        "correct_approach": "Fix the axis first, then apply r cross F",
                    },
                    db_path=db_path,
                )
            )
            question_id = _page_id(question)
            created.add(question_id)
            reporter.check(
                "question-level mistake feeds evidence-backed weak points",
                any(row.get("chapter") == chapter for row in study_domain.weak_points(db_path=db_path)),
            )
            analysis = _with_retry(
                lambda: study_domain.complete_exam_analysis(exam_title, db_path=db_path)
            )
            reporter.check(
                "exam analysis closes only after a question review",
                analysis.get("status") == "Analysed" and analysis.get("questions_reviewed", 0) >= 1,
            )

            ledger = _with_retry(
                lambda: study_domain._create(  # noqa: SLF001 - relation probe
                    "ledger",
                    {
                        "task": f"{tag} end-of-block PYQ log",
                        "date": today.isoformat(),
                        "subject": "Physics",
                        "exercise_type": "PYQs",
                        "block": "EB-1",
                        "actual_time_min": 10,
                        "questions_attempted": 2,
                        "questions_correct": 1,
                        "tickbox": True,
                        "work_item": work_id,
                        "operation_id": f"{operation_prefix}:ledger",
                    },
                    db_path=db_path,
                )
            )
            ledger_id = _page_id(ledger)
            created.add(ledger_id)
            sync.sync_once(db_path=db_path, db_keys=("ledger", "work_items"))
            ledger_row = _mirror_row(db_path, "ledger", ledger_id)
            reporter.check(
                "end-of-block Ledger entry remains linked to its Work Item",
                bool(ledger_row and work_id in str(ledger_row["work_item"])),
            )
        finally:
            try:
                created.update(_discover_tagged_pages(tag))
            except Exception as exc:
                cleanup_errors.append(f"discovery: {_safe_error(exc)}")
            for page_id in reversed(tuple(created)):
                try:
                    _archive_page(page_id)
                    time.sleep(0.35)
                except Exception as exc:
                    cleanup_errors.append(_safe_error(exc))

    if cleanup_errors:
        reporter.failed += 1
        raise SmokeFailure(
            f"Notion cleanup failed for {len(cleanup_errors)} page(s): "
            f"{cleanup_errors[0]}"
        )

    counts = sync.sync_once(db_keys=TOUCHED_DATABASES)
    reporter.check(
        "production mirror resynced all touched databases after cleanup",
        set(counts) == set(TOUCHED_DATABASES),
    )
    reporter.check(
        "no live smoke records remain active in the production mirror",
        _assert_no_active_tagged_rows(tag),
    )


def run_hybrid_notion_smoke(reporter: Reporter, tag: str) -> None:
    """Verify the hybrid ownership boundary using real Notion and local SQLite."""
    _verify_live_schemas(reporter)
    print("\nHybrid workflow checks")
    created: set[str] = set()
    cleanup_errors: list[str] = []
    today = dt.date.fromisoformat(session_context.local_today_iso())
    isolated_day = today + dt.timedelta(days=730)
    now = session_context.local_now()

    with tempfile.TemporaryDirectory(prefix="study-bot-hybrid-live-") as tmp:
        db_path = Path(tmp) / "hybrid.db"
        try:
            counts = sync.sync_once(db_path=db_path)
            reporter.check(
                "only four Notion-owned databases are synchronized",
                set(counts) == set(sync.NOTION_SOURCE_KEYS),
            )

            system_goal = study_domain.ensure_system_goals(db_path=db_path)
            repeated_goal = study_domain.ensure_system_goals(db_path=db_path)
            reporter.check(
                "AIR-1 operating target is local and idempotent",
                system_goal["id"] == repeated_goal["id"],
            )
            pyq_goal = study_domain.create_goal({
                "title": f"{tag} Physics PYQ duration",
                "goal_type": "Duration", "metric": "PYQ minutes",
                "target": 120, "period": "Daily", "subject": "Physics",
                "operation_id": f"hybrid:{tag}:goal",
            }, db_path=db_path)
            exam_title = f"{tag} JEE mock"
            exam = study_domain.create_exam({
                "title": exam_title, "kind": "JEE Main Mock",
                "exam_date": isolated_day.isoformat(),
                "date_confidence": "Tentative", "max_marks": 300,
                "target_marks": 220,
                "operation_id": f"hybrid:{tag}:exam",
            }, db_path=db_path)
            work = study_domain.create_work_item({
                "title": f"{tag} Physics PYQ backlog", "kind": "PYQ",
                "status": "Backlog", "subject": "Physics", "priority": 85,
                "estimated_min": 120, "expected_cy": 120,
                "goal": pyq_goal["id"], "exam": exam["id"],
                "operation_id": f"hybrid:{tag}:work",
            }, db_path=db_path)
            timetable = study_domain.create_timetable_entry({
                "title": f"{tag} teacher window", "weekday": "Monday",
                "start_time": "09:00", "end_time": "10:00",
                "kind": "Doubt Window", "subject": "Physics",
                "teacher": "Live Test Teacher",
                "operation_id": f"hybrid:{tag}:timetable",
            }, db_path=db_path)
            health = operational_store.health(db_path)
            reporter.check(
                "operational records are authoritative in healthy SQLite",
                health["integrity"] == "ok"
                and health["counts"]["goals"] == 2
                and health["counts"]["work_items"] == 1
                and health["counts"]["exams"] == 1
                and health["counts"]["timetable"] == 1,
            )

            plan = study_domain.create_plan_item({
                "title": f"{tag} sequential PYQ block",
                "plan_date": isolated_day.isoformat(), "sequence": 1,
                "work_item": work["id"], "subject": "Physics", "kind": "PYQ",
                "expected_cy": 120, "estimated_min": 120, "priority": 85,
                "exit_condition": "Attempt the selected PYQ set",
                "operation_id": f"hybrid:{tag}:plan",
            }, db_path=db_path)
            plan_id = _page_id(plan)
            created.add(plan_id)
            parsed_plan = notion.parse_page(notion.get_page(plan_id))
            reporter.check(
                "Notion plan stores a local Work Item marker, not an invalid relation",
                work["id"] in str(parsed_plan.get("planner_note") or "")
                and not parsed_plan.get("work_item"),
            )
            sync.sync_once(db_path=db_path, db_keys=("daily_plan",))
            chat_id = f"hybrid-{uuid.uuid4().hex}"
            active = study_domain.activate_next_plan(
                chat_id, isolated_day.isoformat(), db_path=db_path
            )
            reporter.check(
                "plan activation restores the SQLite Work Item link",
                bool(active and active.get("work_item_id") == work["id"]),
            )
            moved = study_domain.complete_active_plan(
                chat_id, carry_to_backlog=True, db_path=db_path
            )
            work_after = study_domain._row(
                "work_items", "notion_page_id=?", (work["id"],), db_path=db_path
            )
            reporter.check(
                "carry-to-backlog updates local work and remote plan",
                moved["status"] == "Moved" and work_after.get("status") == "Backlog",
            )

            doubt_title = f"{tag} torque sign doubt"
            doubt = notion.create_page("doubts", {
                "core_concept": doubt_title, "status": "Unresolved",
                "workflow_state": "New", "valid_attempts": 0,
                "teacher_ready": False, "teacher_asked": False,
                "operation_id": f"hybrid:{tag}:doubt",
            })
            doubt_id = _page_id(doubt)
            created.add(doubt_id)
            sync.sync_once(db_path=db_path, db_keys=("doubts",))
            first = study_domain.record_doubt_attempt(
                doubt_title, duration_min=8, approach="Resolve components",
                stuck_point="Torque sign after changing axis",
                attempted_at=now - dt.timedelta(minutes=65), db_path=db_path,
            )
            second = study_domain.record_doubt_attempt(
                doubt_title, duration_min=9, approach="Use a free-body diagram",
                stuck_point="Clockwise convention remains unclear",
                attempted_at=now, db_path=db_path,
            )
            reporter.check(
                "two local attempts deterministically unlock teacher escalation",
                first["valid_attempts"] == 1
                and second["valid_attempts"] == 2
                and second["teacher_ready"] is True,
            )
            eligible = study_domain.eligible_doubts(db_path=db_path)
            reporter.check(
                "eligibility is computed from SQLite attempts",
                any(row["notion_page_id"] == doubt_id for row in eligible),
            )
            study_domain.resolve_doubt(
                doubt_title, "Teacher confirmed the sign convention.",
                teacher_asked=True, db_path=db_path,
            )

            with mock.patch.object(
                study_domain.session_context, "local_today_iso",
                return_value=isolated_day.isoformat(),
            ):
                finished = study_domain.finish_exam(exam_title, db_path=db_path)
            reporter.check(
                "finishing an exam creates local review work and a Notion plan row",
                finished["status"] == "Analysing" and bool(finished["work_item_id"]),
            )
            study_domain.record_exam_summary(
                exam_title,
                {"actual_marks": 18, "attempted": 6, "correct": 4,
                 "incorrect": 2, "unattempted": 4},
                db_path=db_path,
            )
            study_domain.record_question_review(exam_title, {
                "question_no": "7", "subject": "Physics",
                "chapter": f"{tag} Rotation", "attempted": True,
                "correct": False, "marks_awarded": -1, "marks_lost": 5,
                "failure_type": "Concept",
                "root_cause": "Wrong torque sign convention",
            }, db_path=db_path)
            closed = study_domain.complete_exam_analysis(exam_title, db_path=db_path)
            reporter.check(
                "exam summary and question analysis remain local and evidence-backed",
                closed["status"] == "Analysed" and closed["questions_reviewed"] == 1,
            )

            ledger = notion.create_page("ledger", {
                "task": f"{tag} end-of-block log", "date": today.isoformat(),
                "subject": "Physics", "exercise_type": "PYQs", "block": "EB-1",
                "actual_time_min": 10, "questions_attempted": 2,
                "questions_correct": 1, "operation_id": f"hybrid:{tag}:ledger",
            })
            ledger_id = _page_id(ledger)
            created.add(ledger_id)
            operational_store.link_execution(work["id"], ledger_id, db_path=db_path)
            reporter.check(
                "end-of-block Ledger page links to local Work Item without a Notion relation",
                operational_store.execution_links(work["id"], db_path=db_path)
                == [ledger_id],
            )
        finally:
            try:
                created.update(_discover_tagged_pages(tag))
            except Exception as exc:
                cleanup_errors.append(f"discovery: {_safe_error(exc)}")
            for page_id in reversed(tuple(created)):
                try:
                    _archive_page(page_id)
                    time.sleep(0.35)
                except Exception as exc:
                    cleanup_errors.append(_safe_error(exc))

    if cleanup_errors:
        reporter.failed += 1
        raise SmokeFailure(
            f"Notion cleanup failed for {len(cleanup_errors)} page(s): {cleanup_errors[0]}"
        )
    counts = sync.sync_once()
    reporter.check(
        "production mirror resynced the four Notion sources after cleanup",
        set(counts) == set(sync.NOTION_SOURCE_KEYS),
    )
    reporter.check(
        "no hybrid smoke records remain active in the production mirror",
        _assert_no_active_tagged_rows(tag),
    )


async def run_telegram_smoke(
    reporter: Reporter,
    tag: str,
    *,
    check_polling: bool,
) -> None:
    print("\nTelegram API checks")
    token = telegram_bot_token()
    chat_id = telegram_allowed_user_id()
    sent_message_id: int | None = None

    async with Bot(token=token) as telegram_bot:
        identity = await telegram_bot.get_me()
        reporter.check("getMe returns the configured bot identity", bool(identity.id and identity.is_bot))
        chat = await telegram_bot.get_chat(chat_id)
        reporter.check("getChat reaches the configured owner", chat.id == chat_id)
        await telegram_bot.set_my_commands(bot_module.BOT_COMMANDS)
        commands = await telegram_bot.get_my_commands()
        reporter.check(
            "all production commands are registered",
            {item.command for item in commands}
            == {item.command for item in bot_module.BOT_COMMANDS},
        )
        sent = await telegram_bot.send_message(
            chat_id=chat_id,
            text=f"{tag}: live Telegram send/delete verification",
            disable_notification=True,
        )
        sent_message_id = sent.message_id
        deleted = await telegram_bot.delete_message(chat_id=chat_id, message_id=sent.message_id)
        reporter.check("real Telegram send/delete round trip succeeds", bool(deleted))

        webhook = await telegram_bot.get_webhook_info()
        reporter.check("Telegram webhook state is readable", webhook.url is not None)

        if check_polling:
            try:
                await telegram_bot.get_updates(timeout=0, limit=1)
            except Conflict:
                reporter.warn(
                    "long polling is owned by another bot process; a second local poller would receive 409 Conflict"
                )
            else:
                print("  [OK] a short getUpdates request completed without a polling conflict")
                reporter.passed += 1

    application = bot_module.build_application()
    try:
        await application.initialize()
        await bot_module.post_init(application)
        jobs = application.job_queue.jobs() if application.job_queue is not None else ()
        expected_jobs = {
            "periodic_sync", "expire_drafts", "planning_reminder",
            "timetable_reminder", "weekly_report", "commitment_verify",
            "commitment_nudge", "exam_reminders", "teacher_windows",
            "schedule_watcher", "user_jobs",
        }
        reporter.check(
            "real bot initialization registers every production scheduled job",
            {job.name for job in jobs} == expected_jobs,
            f"registered {len(jobs)}: {sorted(job.name for job in jobs)}",
        )
    finally:
        await application.shutdown()

    if sent_message_id is None:
        raise SmokeFailure("Telegram send/delete verification did not run")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-notion", action="store_true")
    parser.add_argument("--skip-telegram", action="store_true")
    parser.add_argument(
        "--check-polling",
        action="store_true",
        help="make one non-blocking getUpdates call to detect a competing poller",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    reporter = Reporter()
    tag = f"LIVE-SMOKE-{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
    print("Live study-system smoke test")
    print("Temporary Notion data will be archived automatically.")

    if not args.skip_notion:
        try:
            run_hybrid_notion_smoke(reporter, tag)
        except Exception as exc:
            if not isinstance(exc, SmokeFailure):
                reporter.failed += 1
            print(f"  [FAIL] Notion phase stopped: {_safe_error(exc)}")

    if not args.skip_telegram:
        try:
            asyncio.run(
                run_telegram_smoke(
                    reporter,
                    tag,
                    check_polling=args.check_polling,
                )
            )
        except Exception as exc:
            if not isinstance(exc, SmokeFailure):
                reporter.failed += 1
            print(f"  [FAIL] Telegram phase stopped: {_safe_error(exc)}")

    print("\nLive smoke summary")
    print(f"  Passed: {reporter.passed}")
    print(f"  Failed: {reporter.failed}")
    print(f"  Warnings: {len(reporter.warnings)}")
    return 0 if reporter.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
