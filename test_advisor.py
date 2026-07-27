"""Advisor warnings + morning nudge (advisor.py). Temp mirror, no LLM."""

from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

import advisor
import commitments
import reminders
import study_domain as sd
import sync


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "advisor.db"
    with sync.connect(path) as conn:
        sync.init_db(conn)
    return path


def insert(path, table, **values):
    base = {
        "notion_page_id": values.pop("notion_page_id", f"{table}-id"),
        "archived": 0,
        "last_synced_at": "2026-07-20T00:00:00+00:00",
        "raw_json": "{}",
    }
    base.update(values)
    with sqlite3.connect(path) as conn:
        cols = ",".join(f'"{key}"' for key in base)
        marks = ",".join("?" for _ in base)
        conn.execute(f'INSERT INTO "{table}" ({cols}) VALUES ({marks})', tuple(base.values()))
        conn.commit()


def make_goal(db, **overrides):
    data = {
        "title": "Daily PYQs", "goal_type": "Coverage", "metric": "sessions",
        "target": 1, "period": "Daily",
    }
    data.update(overrides)
    return sd.create_goal(data, db_path=db)


def test_morning_nudge_kept_and_missed(db):
    kept = make_goal(db)
    missed = make_goal(db, title="Daily Physics revision", metric="revision sessions",
                       subject="Physics")
    insert(db, "ledger", notion_page_id="l1", date="2026-07-15", exercise_type="PYQs")
    # A met check the day before gives the broken-streak wording something real.
    with commitments._connect(db) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO commitment_checks "
            "(goal_id, check_date, met, value, target, checked_at) VALUES (?,?,?,?,?,?)",
            (missed["notion_page_id"], "2026-07-14", 1, 1, 1, "x"),
        )
        conn.commit()
    nudge = advisor.morning_nudge("2026-07-15", db_path=db)
    assert nudge is not None
    assert "🟢" not in nudge and "🔴 Morning accountability" in nudge
    assert "✅ Daily PYQs — kept · 1/1 · 1-day streak" in nudge
    assert "❌ Daily Physics revision — missed · 0/1 logged · 1-day streak ended" in nudge
    assert "Daily Physics revision — 1/2 verified days kept (50%)" in nudge
    assert "Next\n→ Recover Daily Physics revision today" in nudge


def test_morning_nudge_none_without_daily_goals(db):
    assert advisor.morning_nudge("2026-07-15", db_path=db) is None
    make_goal(db, title="Coaching homework daily")  # unverifiable scope
    assert advisor.morning_nudge("2026-07-15", db_path=db) is None


def test_log_warnings_only_late_and_only_nonserving(db):
    make_goal(db)  # unmet Daily PYQs
    evening = dt.datetime(2026, 7, 15, 21, 0)
    morning = dt.datetime(2026, 7, 15, 10, 0)
    theory = {"subject": "Chem", "exercise_type": "Theory"}
    pyqs = {"subject": "Physics", "exercise_type": "PYQs"}
    assert any("Daily PYQs" in w for w in advisor.log_warnings(theory, now=evening, db_path=db))
    assert advisor.log_warnings(theory, now=morning, db_path=db) == []
    assert advisor.log_warnings(pyqs, now=evening, db_path=db) == []
    # Met commitments never warn.
    insert(db, "ledger", notion_page_id="l1", date="2026-07-15", exercise_type="PYQs")
    assert advisor.log_warnings(theory, now=evening, db_path=db) == []


def test_memory_prompt_block_includes_op_goals_and_prefs(db):
    # Local operational goal should appear alongside prefs
    sd.create_goal({
        "title": "Local CY Goal",
        "goal_type": "Coverage",
        "metric": "cognitive_yield",
        "target": 300,
        "period": "Daily",
    }, db_path=db)
    commitments.add_pref(1, "I prefer morning study blocks", db_path=db)

    block = advisor.memory_prompt_block(1, db_path=db)
    assert "Local CY Goal" in block
    assert "morning study blocks" in block


def test_memory_prompt_block_includes_op_work_items(db):
    sd.create_work_item({
        "title": "Physics Rotation notes",
        "kind": "PYQ",
        "status": "Planned",
    }, db_path=db)

    block = advisor.memory_prompt_block(1, db_path=db)
    assert "Physics Rotation notes" in block


def test_trajectory_accuracy_drop(db):
    for i in range(3):  # prior week: 80% on plenty of questions
        insert(db, "ledger", notion_page_id=f"p{i}", date=f"2026-07-{7 + i:02d}",
               questions_attempted=20, questions_correct=16)
    for i in range(3):  # recent week: 60%
        insert(db, "ledger", notion_page_id=f"r{i}", date=f"2026-07-{15 + i:02d}",
               questions_attempted=20, questions_correct=12)
    warnings = advisor.trajectory_warnings(today="2026-07-19", db_path=db)
    assert any("accuracy fell 20 pts" in w for w in warnings)


def test_trajectory_silent_on_healthy_or_thin_data(db):
    assert advisor.trajectory_warnings(today="2026-07-19", db_path=db) == []
    # Only one window has data -> no week-over-week claim.
    insert(db, "ledger", notion_page_id="r1", date="2026-07-18",
           questions_attempted=40, questions_correct=20)
    assert not any("accuracy" in w for w in advisor.trajectory_warnings(today="2026-07-19", db_path=db))


def test_nudge_dedup_via_claim(db):
    assert reminders.claim("commitment-nudge:2026-07-15", db_path=db) is True
    assert reminders.claim("commitment-nudge:2026-07-15", db_path=db) is False


def test_memory_prompt_block(db):
    assert advisor.memory_prompt_block(7, db_path=db) == ""
    make_goal(db)
    commitments.add_pref(7, "prefers maths in the morning", db_path=db)
    block = advisor.memory_prompt_block(7, db_path=db)
    assert "USER COMMITMENTS" in block and "Daily PYQs" in block
    assert "USER PREFERENCES" in block and "maths in the morning" in block
    # Other chats see the shared commitments but not this chat's prefs.
    other = advisor.memory_prompt_block(8, db_path=db)
    assert "Daily PYQs" in other and "maths in the morning" not in other
