"""/jobs engine (user_jobs.py) — offline tests, no Telegram/LLM."""

from __future__ import annotations

import datetime as dt

import pytest

import user_jobs
from config import settings

CHAT = 31


@pytest.fixture(autouse=True)
def _isolated_overrides(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "_OVERRIDES_PATH", str(tmp_path / "settings.json"))


@pytest.fixture()
def db(tmp_path):
    return tmp_path / "jobs.db"


def make(db, **overrides):
    data = {
        "title": "Weekday CY report", "schedule_kind": "weekdays",
        "run_time": "21:00", "weekday": None, "run_date": None,
        "action_kind": "ask",
        "action_text": "What is my overall cognitive yield for the last 7 days?",
    }
    data.update(overrides)
    return user_jobs.create_job(CHAT, data, db_path=db)


def test_validate_parsed_shapes():
    data, err = user_jobs.validate_parsed({
        "title": "CY report", "schedule_kind": "weekdays", "time": "9:05",
        "action_kind": "ask", "action_text": "week CY?",
    })
    assert err is None and data["run_time"] == "09:05" and data["weekday"] is None
    data, err = user_jobs.validate_parsed({
        "schedule_kind": "weekly", "time": "20:00", "weekday": 6,
        "action_kind": "message", "action_text": "plan the week",
    })
    assert err is None and data["weekday"] == 6 and data["title"] == "plan the week"
    for bad in (
        {"schedule_kind": "hourly", "time": "20:00", "action_kind": "ask", "action_text": "x"},
        {"schedule_kind": "daily", "time": "25:99", "action_kind": "ask", "action_text": "x"},
        {"schedule_kind": "weekly", "time": "20:00", "action_kind": "ask", "action_text": "x"},
        {"schedule_kind": "once", "time": "20:00", "action_kind": "ask", "action_text": "x"},
        {"schedule_kind": "daily", "time": "20:00", "action_kind": "launch", "action_text": "x"},
        {"schedule_kind": "daily", "time": "20:00", "action_kind": "ask", "action_text": " "},
    ):
        data, err = user_jobs.validate_parsed(bad)
        assert data is None and err


def test_due_logic_all_kinds(db):
    daily = make(db, title="daily", schedule_kind="daily", run_time="08:00")
    wkdays = make(db, title="weekdays", schedule_kind="weekdays", run_time="08:00")
    weekly = make(db, title="weekly-sun", schedule_kind="weekly", weekday=6, run_time="08:00")
    once = make(db, title="once", schedule_kind="once", run_date="2026-07-25", run_time="08:00")
    late = make(db, title="later", schedule_kind="daily", run_time="23:59")

    tue = dt.datetime(2026, 7, 21, 9, 0)   # Tuesday
    due = {j["title"] for j in user_jobs.due_jobs(tue, db_path=db)}
    assert due == {"daily", "weekdays"}
    sun = dt.datetime(2026, 7, 26, 9, 0)   # Sunday
    due = {j["title"] for j in user_jobs.due_jobs(sun, db_path=db)}
    assert due == {"daily", "weekly-sun"}
    sat_run_date = dt.datetime(2026, 7, 25, 9, 0)  # Saturday = run_date
    due = {j["title"] for j in user_jobs.due_jobs(sat_run_date, db_path=db)}
    assert due == {"daily", "once"}
    early = dt.datetime(2026, 7, 21, 7, 0)  # before 08:00
    assert user_jobs.due_jobs(early, db_path=db) == []


def test_once_disables_after_run(db):
    once = make(db, schedule_kind="once", run_date="2026-07-25", run_time="08:00")
    user_jobs.mark_ran(once["id"], db_path=db)
    job = user_jobs.get_job(once["id"], db_path=db)
    assert job["enabled"] == 0 and job["last_run"]
    daily = make(db, title="d", schedule_kind="daily", run_time="08:00")
    user_jobs.mark_ran(daily["id"], db_path=db)
    assert user_jobs.get_job(daily["id"], db_path=db)["enabled"] == 1


def test_run_now_keeps_once_armed(db):
    once = make(db, schedule_kind="once", run_date="2026-07-25", run_time="08:00")
    user_jobs.mark_ran(once["id"], consume_once=False, db_path=db)  # manual ▶ Run now
    job = user_jobs.get_job(once["id"], db_path=db)
    assert job["enabled"] == 1 and job["last_run"]
    user_jobs.mark_ran(once["id"], db_path=db)  # scheduler path consumes
    assert user_jobs.get_job(once["id"], db_path=db)["enabled"] == 0


def test_once_rejects_past_date():
    data, err = user_jobs.validate_parsed({
        "schedule_kind": "once", "time": "08:00", "date": "2020-01-01",
        "action_kind": "message", "action_text": "x",
    })
    assert data is None and "past" in err


def test_should_preclaim_today():
    tue_2pm = dt.datetime(2026, 7, 21, 14, 0)  # Tuesday
    daily_morning = {"schedule_kind": "daily", "run_time": "08:00", "weekday": None, "run_date": None}
    daily_evening = {"schedule_kind": "daily", "run_time": "21:00", "weekday": None, "run_date": None}
    weekly_sun = {"schedule_kind": "weekly", "run_time": "08:00", "weekday": 6, "run_date": None}
    weekly_tue = {"schedule_kind": "weekly", "run_time": "08:00", "weekday": 1, "run_date": None}
    once_today = {"schedule_kind": "once", "run_time": "08:00", "weekday": None, "run_date": "2026-07-21"}
    once_future = {"schedule_kind": "once", "run_time": "08:00", "weekday": None, "run_date": "2026-07-25"}
    assert user_jobs.should_preclaim_today(daily_morning, tue_2pm) is True
    assert user_jobs.should_preclaim_today(daily_evening, tue_2pm) is False
    assert user_jobs.should_preclaim_today(weekly_sun, tue_2pm) is False
    assert user_jobs.should_preclaim_today(weekly_tue, tue_2pm) is True
    assert user_jobs.should_preclaim_today(once_today, tue_2pm) is True
    assert user_jobs.should_preclaim_today(once_future, tue_2pm) is False


def test_crud_toggle_edit_delete(db):
    job = make(db)
    assert [j["id"] for j in user_jobs.list_jobs(CHAT, db_path=db)] == [job["id"]]
    user_jobs.set_enabled(job["id"], False, db_path=db)
    assert user_jobs.get_job(job["id"], db_path=db)["enabled"] == 0
    assert user_jobs.due_jobs(dt.datetime(2026, 7, 21, 23, 0), db_path=db) == []
    ok, val = user_jobs.update_field(job["id"], "time", "7:30", db_path=db)
    assert ok and user_jobs.get_job(job["id"], db_path=db)["run_time"] == "07:30"
    ok, err = user_jobs.update_field(job["id"], "time", "25:00", db_path=db)
    assert not ok
    ok, _ = user_jobs.update_field(job["id"], "text", "new question?", db_path=db)
    assert ok and user_jobs.get_job(job["id"], db_path=db)["action_text"] == "new question?"
    user_jobs.delete_job(job["id"], db_path=db)
    assert user_jobs.list_jobs(CHAT, db_path=db) == []


def test_builtin_overlap_detection(monkeypatch):
    monkeypatch.setenv("WEEKLY_REPORT_TIME", "20:00")
    monkeypatch.setenv("TIMETABLE_REMINDER_WEEKDAY", "6")
    clash = {"schedule_kind": "weekly", "run_time": "20:00", "weekday": 6}
    assert any("weekly growth report" in o for o in user_jobs.builtin_overlaps(clash))
    daily_clash = {"schedule_kind": "daily", "run_time": "20:00", "weekday": None}
    assert user_jobs.builtin_overlaps(daily_clash)
    clean = {"schedule_kind": "daily", "run_time": "21:07", "weekday": None}
    assert user_jobs.builtin_overlaps(clean) == []


def test_pending_edit_state(db):
    user_jobs.set_pending_edit(CHAT, 3, "time", db_path=db)
    assert user_jobs.get_pending_edit(CHAT, db_path=db) == (3, "time")
    assert user_jobs.get_pending_edit(CHAT, db_path=db) is None  # fetch clears
    user_jobs.set_pending_edit(CHAT, 3, "text", db_path=db)
    user_jobs.clear_pending_edit(CHAT, db_path=db)
    assert user_jobs.get_pending_edit(CHAT, db_path=db) is None


def test_describe_and_schedule_text(db):
    job = make(db)
    assert "Mon-Fri at 21:00" in user_jobs.schedule_text(job)
    assert "🤖 ask" in user_jobs.describe(job)
    weekly = make(db, title="w", schedule_kind="weekly", weekday=0, run_time="09:00",
                  action_kind="message", action_text="plan!")
    assert "every Mon" in user_jobs.schedule_text(weekly)
    assert "🔔 say" in user_jobs.describe(weekly)
