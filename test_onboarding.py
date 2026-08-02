"""/setup wizard backend (onboarding.py). Temp mirror, no Telegram/LLM."""

from __future__ import annotations

import datetime as dt

import pytest

import coaching_syllabus
import commitments
import ntsc_coaching
import onboarding
import session_context
import study_domain as sd
import sync
from config import settings

CHAT = 11


@pytest.fixture(autouse=True)
def _isolated_overrides(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "_OVERRIDES_PATH", str(tmp_path / "settings.json"))


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "onb.db"
    with sync.connect(path) as conn:
        sync.init_db(conn)
    return path


def test_status_gaps_then_filled(db, monkeypatch):
    monkeypatch.setenv("USER_TIMEZONE", "UTC")
    stats = onboarding.status(db_path=db)
    assert stats["timezone"]["ok"] is False
    assert stats["target_exam"]["ok"] is False
    assert stats["timetable"]["ok"] is False
    assert stats["chapters"]["ok"] is False
    assert stats["commitments"]["ok"] is False
    assert stats["backlog"]["ok"] is True  # informational

    monkeypatch.setenv("USER_TIMEZONE", "Asia/Kolkata")
    sd.create_exam({"title": "JEE Main 2028", "kind": "JEE Main",
                    "exam_date": "2028-01-24"}, db_path=db)
    sd.create_timetable_entry({"subject": "Physics", "weekday": "Monday",
                               "start_time": "17:00", "end_time": "19:00"}, db_path=db)
    for subject in onboarding.SUBJECTS:
        sd.create_work_item({"title": f"{subject}: ch", "kind": "Current Syllabus",
                             "status": "Active", "subject": subject}, db_path=db)
    sd.create_goal({"title": "Daily PYQs", "goal_type": "Coverage",
                    "metric": "sessions", "target": 1, "period": "Daily"}, db_path=db)
    stats = onboarding.status(db_path=db)
    for key in ("timezone", "target_exam", "timetable", "chapters", "commitments"):
        assert stats[key]["ok"] is True, key


def test_state_machine_run_all_walks_unfilled(db, monkeypatch):
    monkeypatch.setenv("USER_TIMEZONE", "Asia/Kolkata")  # timezone already ok
    assert onboarding.is_complete(CHAT, db_path=db) is False
    onboarding.start(CHAT, "target_exam", "run_all", db_path=db)
    assert onboarding.active_section(CHAT, db_path=db) == ("target_exam", "run_all")
    sd.create_exam({"title": "JEE Main 2028", "kind": "JEE Main",
                    "exam_date": "2028-01-24"}, db_path=db)
    nxt = onboarding.advance(CHAT, db_path=db)
    assert nxt == "next_mock"  # target_exam now ok, skipped over
    while nxt is not None:
        nxt = onboarding.advance(CHAT, db_path=db)
    assert onboarding.active_section(CHAT, db_path=db) is None


def test_state_machine_single_returns_to_hub(db):
    onboarding.start(CHAT, "backlog", "single", db_path=db)
    assert onboarding.advance(CHAT, db_path=db) is None
    assert onboarding.active_section(CHAT, db_path=db) is None


def test_complete_flag_persists(db):
    onboarding.mark_complete(CHAT, db_path=db)
    assert onboarding.is_complete(CHAT, db_path=db) is True
    assert onboarding.active_section(CHAT, db_path=db) is None
    assert onboarding.is_complete(CHAT + 1, db_path=db) is False


def test_apply_timezone(db):
    ok, reply, adv = onboarding.apply_answer(CHAT, "timezone", "Asia/Kolkata", db_path=db)
    assert ok and adv and settings.get_override("USER_TIMEZONE") == "Asia/Kolkata"
    ok, reply, adv = onboarding.apply_answer(CHAT, "timezone", "Nope/Nope", db_path=db)
    assert ok is False and adv is False


def test_apply_target_exam_and_no_duplicates(db):
    ok, reply, adv = onboarding.apply_answer(CHAT, "target_exam", "2028", db_path=db)
    assert ok and adv
    rows = sd._rows("exams", "archived=0", db_path=db)
    titles = sorted(r["title"] for r in rows)
    assert titles == ["JEE Advanced 2028", "JEE Main 2028"]
    ok, reply, adv = onboarding.apply_answer(CHAT, "target_exam", "2029", db_path=db)
    assert ok and "existing" in reply.lower()
    assert len(sd._rows("exams", "archived=0", db_path=db)) == 2
    ok, _, adv = onboarding.apply_answer(CHAT, "target_exam", "someday", db_path=db)
    assert ok is False and adv is False


def test_apply_timetable_line(db):
    ok, reply, adv = onboarding.apply_answer(
        CHAT, "timetable", "Physics | Mon | 17:00-19:00 | Ramesh sir", db_path=db
    )
    assert ok and adv is False  # loop stays
    row = sd._rows("timetable", "archived=0", db_path=db)[0]
    assert row["subject"] == "Physics" and row["weekday"] == "Monday"
    assert row["start_time"] == "17:00" and row["end_time"] == "19:00"
    assert row["teacher"] == "Ramesh sir"
    ok, reply, adv = onboarding.apply_answer(CHAT, "timetable", "Physics Monday", db_path=db)
    assert ok is False and "format" in reply


def test_apply_chapters_walks_subjects(db):
    ok, reply, adv = onboarding.apply_answer(CHAT, "chapters", "Rotational Motion", db_path=db)
    assert ok and adv is False and "Chem" in reply
    ok, reply, adv = onboarding.apply_answer(CHAT, "chapters", "Mole Concept", db_path=db)
    assert ok and adv is False and "Maths" in reply
    ok, reply, adv = onboarding.apply_answer(CHAT, "chapters", "Limits", db_path=db)
    assert ok and adv is True
    rows = sd._rows("work_items", "archived=0 AND kind='Current Syllabus'", db_path=db)
    assert {r["subject"] for r in rows} == set(onboarding.SUBJECTS)


def test_apply_backlog_and_capacity_and_prefs(db):
    ok, _, adv = onboarding.apply_answer(CHAT, "backlog", "Ex 2B rotation | Physics", db_path=db)
    assert ok and adv is False
    ok, _, adv = onboarding.apply_answer(CHAT, "backlog", "Organic notes", db_path=db)
    assert ok
    rows = sd._rows("work_items", "archived=0 AND kind='Backlog'", db_path=db)
    assert len(rows) == 2 and any(r["subject"] == "Physics" for r in rows)

    ok, _, adv = onboarding.apply_answer(CHAT, "capacity", "260 320", db_path=db)
    assert ok and adv and settings.daily_cy_baseline() == 260 and settings.daily_cy_ceiling() == 320
    ok, reply, _ = onboarding.apply_answer(CHAT, "capacity", "abc", db_path=db)
    assert ok is False
    ok, _, adv = onboarding.apply_answer(CHAT, "capacity", "keep", db_path=db)
    assert ok and adv

    ok, _, adv = onboarding.apply_answer(CHAT, "prefs", "maths in the morning", db_path=db)
    assert ok and adv is False
    assert commitments.active_prefs(CHAT, db_path=db)[0]["text"] == "maths in the morning"


def test_next_mock_line(db):
    ok, reply, adv = onboarding.apply_answer(
        CHAT, "next_mock", "Coaching major test | 2026-08-10", db_path=db
    )
    assert ok and adv
    row = sd._rows("exams", "archived=0", db_path=db)[0]
    assert row["kind"] == "Coaching Test" and str(row["exam_date"])[:10] == "2026-08-10"
    ok, _, _ = onboarding.apply_answer(CHAT, "next_mock", "just a title", db_path=db)
    assert ok is False


def test_status_baseline_bare_db(db, monkeypatch):
    """Bare mirror: no portal ok, and next_mock/timetable fall back to op_* rows.

    Baseline pin of the CURRENT status() behavior before portal awareness: a
    mirror with zero coaching rows must not raise, portal must not be ok, and
    the wizard detectors keep their old op_* sources. The portal assertion is
    written so it holds both before the key exists and after it is added.
    """
    monkeypatch.setenv("USER_TIMEZONE", "UTC")
    stats = onboarding.status(db_path=db)
    assert stats.get("portal", {}).get("ok") is not True
    assert stats["next_mock"]["ok"] is False
    assert stats["timetable"]["ok"] is False
    assert stats["chapters"]["ok"] is False
    assert stats["chapters"]["missing_subjects"] == onboarding.SUBJECTS
    assert not stats["chapters"].get("suggested_topics")
    sd.create_exam({"title": "Coaching major test", "kind": "Coaching Test",
                    "exam_date": "2026-08-10"}, db_path=db)
    sd.create_timetable_entry({"subject": "Physics", "weekday": "Monday",
                               "start_time": "17:00", "end_time": "19:00"}, db_path=db)
    stats = onboarding.status(db_path=db)
    assert stats["next_mock"]["ok"] is True
    assert "Coaching major test" in stats["next_mock"]["detail"]
    assert stats["timetable"]["ok"] is True
    assert "entr(ies)" in stats["timetable"]["detail"]
    assert stats.get("portal", {}).get("ok") is not True


def _seed_success_portal(db):
    """Seed the NTSC mirror: success sync run + classes + tests + parsed syllabus."""
    stamp = dt.datetime.now(dt.timezone.utc).isoformat()
    with ntsc_coaching._connect(db) as conn:
        conn.execute(
            "INSERT INTO coaching_sync_runs (started_at, finished_at, status, datasets, error) "
            "VALUES (?,?,?,?,?)",
            (stamp, stamp, "success", '["classes"]', None))
    today = session_context.local_today_iso()
    ntsc_coaching.replace_classes([
        {"classDate": today, "startTime": "08:00", "duration": 60,
         "classType": "Class", "subjects": "Physics"},
        {"classDate": today, "startTime": "10:00", "duration": 60,
         "classType": "Class", "subjects": "Maths"},
    ], db_path=db)
    ntsc_coaching.replace_tests([
        {"id": "t1", "testName": "Major Test 1", "testDateTime": f"{today}T09:00:00"},
    ], db_path=db)
    coaching_syllabus.replace_syllabi([
        {"id": "t1", "syllabus": "Physics: Kinematics, Rotational Motion\n"
         "Chem: Mole Concept\nMaths: Limits"},
    ], db_path=db)
    return today


def test_status_portal_seeded_success(db, monkeypatch):
    """Seeded success mirror: portal ✅, timetable/next_mock portal-derived, pre-filled topics."""
    monkeypatch.setenv("USER_TIMEZONE", "UTC")
    today = _seed_success_portal(db)
    stats = onboarding.status(db_path=db)
    assert stats["portal"]["ok"] is True
    assert "2 classes" in stats["portal"]["detail"]
    assert stats["timetable"]["ok"] is True
    assert stats["timetable"]["detail"] == "from portal: 2 classes cached"
    assert stats["next_mock"]["ok"] is True
    assert "Major Test 1" in stats["next_mock"]["detail"]
    assert today in stats["next_mock"]["detail"]
    assert stats["chapters"]["suggested_topics"]["Physics"] == [
        "Kinematics", "Rotational Motion"]
    assert stats["chapters"]["suggested_topics"]["Chem"] == ["Mole Concept"]
    assert stats["chapters"]["suggested_topics"]["Maths"] == ["Limits"]


def test_status_portal_never_synced(db, monkeypatch):
    """Zero coaching rows: portal ok=False with never-synced detail, no exception."""
    monkeypatch.setenv("USER_TIMEZONE", "UTC")
    stats = onboarding.status(db_path=db)
    assert stats["portal"]["ok"] is False
    assert "never synced" in stats["portal"]["detail"]
    assert stats["timetable"]["ok"] is False
    assert stats["next_mock"]["ok"] is False
