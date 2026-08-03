"""Tests for portal_bridge: portal -> op_* upsert bridge (offline)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import ntsc_coaching
import operational_store
import portal_bridge


def _seed_tests(db: Path) -> None:
    ntsc_coaching.replace_tests([
        {"id": "t1", "testName": "Weekly Test 1", "testDateTime": "2026-08-15T09:00:00",
         "courseId": "7", "batch": "B1", "goal": "Test",
         "syllabus": "<p>Physics: Kinematics</p>"},
    ], db_path=db)


def _seed_classes(db: Path) -> None:
    ntsc_coaching.replace_classes([
        {"classDate": "2026-08-03", "startTime": "07:00", "duration": 60,
         "classType": "Lecture", "subjects": "Physics"},
        {"classDate": "2026-08-04", "startTime": "17:30", "duration": 90,
         "classType": "", "subjects": "Chemistry"},
    ], db_path=db)


def _class_weekday(date_iso: str) -> str:
    return dt.date.fromisoformat(date_iso).strftime("%A")


def test_promote_tests_inserts_new(tmp_path):
    db = tmp_path / "test.db"
    _seed_tests(db)
    result = portal_bridge.promote_tests_to_exams(db_path=db)
    assert result["inserted"] == 1
    assert result["linked"] == 0
    rows = operational_store.rows("exams", "operation_id=?", ("portal:t1",), db_path=db)
    assert len(rows) == 1
    assert rows[0]["title"] == "Weekly Test 1"
    assert rows[0]["kind"] == "Coaching Test"
    assert rows[0]["status"] == "Planned"
    assert rows[0]["date_confidence"] == "Confirmed"
    assert rows[0]["exam_date"] == "2026-08-15"


def test_promote_tests_skips_existing(tmp_path):
    db = tmp_path / "test.db"
    _seed_tests(db)
    first = portal_bridge.promote_tests_to_exams(db_path=db)
    second = portal_bridge.promote_tests_to_exams(db_path=db)
    assert first["inserted"] == 1
    assert second["inserted"] == 0
    assert second["skipped"] == 1
    rows = operational_store.rows("exams", "operation_id=?", ("portal:t1",), db_path=db)
    assert len(rows) == 1


def test_promote_tests_links_manual_duplicate(tmp_path):
    db = tmp_path / "test.db"
    _seed_tests(db)
    manual = operational_store.create("exams", {
        "title": "Weekly Test 1",
        "kind": "Mock",
        "status": "Planned",
        "exam_date": "2026-08-15",
    }, db_path=db)
    result = portal_bridge.promote_tests_to_exams(db_path=db)
    assert result["inserted"] == 0
    assert result["linked"] == 1
    rows = operational_store.rows("exams", "operation_id=?", ("portal:t1",), db_path=db)
    assert len(rows) == 1
    assert rows[0]["id"] == manual["id"]
    assert rows[0]["kind"] == "Mock"
    assert rows[0]["status"] == "Planned"


def test_promote_timetable_inserts_new(tmp_path):
    db = tmp_path / "test.db"
    _seed_classes(db)
    result = portal_bridge.promote_classes_to_timetable(db_path=db)
    assert result["inserted"] == 2
    assert result["linked"] == 0
    monday = _class_weekday("2026-08-03")
    tuesday = _class_weekday("2026-08-04")
    rows = operational_store.rows(
        "timetable", "operation_id=?", (f"portal:timetable:{monday}:07:00:Physics",),
        db_path=db,
    )
    assert len(rows) == 1
    assert rows[0]["weekday"] == monday
    assert rows[0]["start_time"] == "07:00"
    assert rows[0]["end_time"] == "08:00"
    assert rows[0]["subject"] == "Physics"
    assert rows[0]["kind"] == "Lecture"
    assert rows[0]["active"] == 1
    rows2 = operational_store.rows(
        "timetable", "operation_id=?", (f"portal:timetable:{tuesday}:17:30:Chemistry",),
        db_path=db,
    )
    assert len(rows2) == 1
    assert rows2[0]["end_time"] == "19:00"
    assert rows2[0]["kind"] == "Class"
    assert rows2[0]["title"] == "Chemistry Class"


def test_promote_timetable_skips_existing(tmp_path):
    db = tmp_path / "test.db"
    _seed_classes(db)
    first = portal_bridge.promote_classes_to_timetable(db_path=db)
    second = portal_bridge.promote_classes_to_timetable(db_path=db)
    assert first["inserted"] == 2
    assert second["inserted"] == 0
    assert second["skipped"] == 2
    rows = operational_store.rows(
        "timetable", "archived=0", (), db_path=db
    )
    assert len(rows) == 2


def test_promote_timetable_links_manual(tmp_path):
    db = tmp_path / "test.db"
    _seed_classes(db)
    weekday = _class_weekday("2026-08-03")
    manual = operational_store.create("timetable", {
        "title": "Physics Lecture",
        "weekday": weekday,
        "start_time": "07:00",
        "end_time": "08:00",
        "subject": "Physics",
        "kind": "Lecture",
        "active": 1,
        "questions_allowed": 0,
    }, db_path=db)
    result = portal_bridge.promote_classes_to_timetable(db_path=db)
    assert result["inserted"] == 1
    assert result["linked"] == 1
    rows = operational_store.rows(
        "timetable", "operation_id=?", (f"portal:timetable:{weekday}:07:00:Physics",),
        db_path=db,
    )
    assert len(rows) == 1
    assert rows[0]["id"] == manual["id"]
    assert rows[0]["questions_allowed"] == 0
