"""Time-travel week: the entire time engine proven over 7 simulated days.

Runs the real nightly-check / morning-backfill / nudge / job-firing logic
over an explicit simulated week (Mon 2026-08-03 … Sun 2026-08-09) on a temp
DB — no clock mocking needed because every engine function takes explicit
dates. Covers: streak build → break → rebuild, a bot-offline night healed by
backfill, and daily/weekly/once jobs firing exactly once each.
"""

from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

import advisor
import commitments
import reminders
import study_domain as sd
import sync
import user_jobs

MON = dt.date(2026, 8, 3)  # Monday
DAYS = [MON + dt.timedelta(days=i) for i in range(7)]  # Mon..Sun
CHAT = 77


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "week.db"
    with sync.connect(path) as conn:
        sync.init_db(conn)
    return path


def log_pyqs(db, day: dt.date, i: int) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO ledger (notion_page_id, archived, last_synced_at, raw_json, "
            "subject, exercise_type, date, questions_attempted, questions_correct) "
            'VALUES (?, 0, ?, "{}", "Physics", "PYQs", ?, 10, 8)',
            (f"wk-{i}", day.isoformat(), day.isoformat()),
        )
        conn.commit()


def test_full_week(db):
    goal = sd.create_goal({
        "title": "Daily PYQs", "goal_type": "Coverage", "metric": "sessions",
        "target": 1, "period": "Daily",
    }, db_path=db)
    goal_id = goal["notion_page_id"]

    daily = user_jobs.create_job(CHAT, {
        "title": "daily report", "schedule_kind": "daily", "run_time": "08:00",
        "weekday": None, "run_date": None, "action_kind": "message",
        "action_text": "gm",
    }, db_path=db)
    weekly = user_jobs.create_job(CHAT, {
        "title": "sunday plan", "schedule_kind": "weekly", "run_time": "20:00",
        "weekday": 6, "run_date": None, "action_kind": "message",
        "action_text": "plan",
    }, db_path=db)
    once = user_jobs.create_job(CHAT, {
        "title": "one-off", "schedule_kind": "once", "run_time": "09:00",
        "weekday": None, "run_date": DAYS[2].isoformat(), "action_kind": "message",
        "action_text": "once!",
    }, db_path=db)

    study_days = {DAYS[0], DAYS[1], DAYS[2], DAYS[4], DAYS[5]}   # skips Thu, Sun
    offline_nights = {DAYS[3]}                                    # bot down Thu night
    job_fires: dict[str, int] = {}

    for i, day in enumerate(DAYS):
        if day in study_days:
            log_pyqs(db, day, i)

        # --- 08:30 & 20:30 job scans (the 60-s scan, sampled twice a day) ---
        for hh in (8, 20):
            now = dt.datetime.combine(day, dt.time(hh, 30))
            for job in user_jobs.due_jobs(now, db_path=db):
                key = f"user-job:{job['id']}:{day.isoformat()}"
                if reminders.claim(key, db_path=db):
                    job_fires[job["title"]] = job_fires.get(job["title"], 0) + 1
                    user_jobs.mark_ran(job["id"], db_path=db)

        # --- 23:50 nightly check (skipped when "offline") ---
        if day not in offline_nights:
            commitments.run_checks_for_date(day.isoformat(), db_path=db)

        # --- next morning 07:30: backfill + nudge ---
        morning = day + dt.timedelta(days=1)
        commitments.backfill_checks(days=3, end_date=morning.isoformat(), db_path=db)
        nudge = advisor.morning_nudge(day.isoformat(), db_path=db)
        assert nudge is not None

        if day == DAYS[2]:      # Wed morning-after: 3-day streak
            assert "kept (3-day streak)" in nudge
        if day == DAYS[3]:      # Thu was skipped AND offline — backfill still records the miss honestly
            assert "missed" in nudge and "3-day streak broken" in nudge
        if day == DAYS[5]:      # Sat morning-after: rebuilt to 2
            assert "kept (2-day streak)" in nudge

    # --- end-of-week truths ---
    assert commitments.streak(goal_id, as_of=DAYS[5].isoformat(), db_path=db) == 2
    assert commitments.streak(goal_id, as_of=DAYS[2].isoformat(), db_path=db) == 3
    stats = commitments.adherence(goal_id, as_of=DAYS[6].isoformat(), days=7, db_path=db)
    assert stats["met"] == 5 and stats["total"] == 7

    assert job_fires["daily report"] == 7          # every day, exactly once
    assert job_fires["sunday plan"] == 1           # Sunday only
    assert job_fires["one-off"] == 1               # fired on its date...
    assert user_jobs.get_job(once["id"], db_path=db)["enabled"] == 0   # ...then disarmed

    weekly_goal_check = commitments.verify_weekly_goal(
        {"title": "Weekly PYQs", "goal_type": "Coverage", "metric": "PYQ sessions",
         "target": 5, "period": "Weekly"},
        DAYS[6].isoformat(), db_path=db,
    )
    assert weekly_goal_check["met"] is True and weekly_goal_check["value"] == 5
