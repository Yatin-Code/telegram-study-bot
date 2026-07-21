"""Commitment storage + deterministic ledger verification (commitments.py).

Everything runs on a temp mirror — no Notion, no LLM, no Telegram.
"""

from __future__ import annotations

import sqlite3

import pytest

import commitments
import study_domain as sd
import sync


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "commit.db"
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


def seed_check(db, goal_id, date, met, value=None, target=1):
    with commitments._connect(db) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO commitment_checks "
            "(goal_id, check_date, met, value, target, checked_at) VALUES (?,?,?,?,?,?)",
            (goal_id, date, int(met), value, target, "2026-07-20T00:00:00+00:00"),
        )
        conn.commit()


def test_fresh_db_no_goals_no_crash(db):
    assert commitments.run_checks_for_date("2026-07-15", db_path=db) == []


def test_coverage_pyq_met_and_missed(db):
    goal = make_goal(db)
    check = commitments.verify_goal_for_date(goal, "2026-07-15", db_path=db)
    assert check["met"] is False and check["value"] == 0
    insert(db, "ledger", notion_page_id="l1", date="2026-07-15",
           subject="Physics", exercise_type="PYQs")
    check = commitments.verify_goal_for_date(goal, "2026-07-15", db_path=db)
    assert check["met"] is True and check["value"] == 1


def test_subject_scope_excludes_other_subject(db):
    goal = make_goal(db, title="Daily Physics PYQs", subject="Physics")
    insert(db, "ledger", notion_page_id="l1", date="2026-07-15",
           subject="Chem", exercise_type="PYQs")
    check = commitments.verify_goal_for_date(goal, "2026-07-15", db_path=db)
    assert check["met"] is False
    insert(db, "ledger", notion_page_id="l2", date="2026-07-15",
           subject="Physics", exercise_type="PYQs")
    assert commitments.verify_goal_for_date(goal, "2026-07-15", db_path=db)["met"] is True


def test_duration_and_cy_goal_types(db):
    duration = make_goal(db, title="Maths time", goal_type="Duration",
                         metric="minutes", target=120, subject="Maths")
    cy = make_goal(db, title="Daily grind", goal_type="CY",
                   metric="cognitive_yield", target=100)
    insert(db, "ledger", notion_page_id="l1", date="2026-07-15", subject="Maths",
           actual_time_min=90, cognitive_yield=60)
    assert commitments.verify_goal_for_date(duration, "2026-07-15", db_path=db)["met"] is False
    assert commitments.verify_goal_for_date(cy, "2026-07-15", db_path=db)["met"] is False
    insert(db, "ledger", notion_page_id="l2", date="2026-07-15", subject="Maths",
           actual_time_min=40, cognitive_yield=50)
    assert commitments.verify_goal_for_date(duration, "2026-07-15", db_path=db)["met"] is True
    assert commitments.verify_goal_for_date(cy, "2026-07-15", db_path=db)["met"] is True


def test_run_checks_idempotent_and_reverifiable(db):
    goal = make_goal(db)
    goal_id = goal["notion_page_id"]
    commitments.run_checks_for_date("2026-07-15", db_path=db)
    commitments.run_checks_for_date("2026-07-15", db_path=db)
    with commitments._connect(db) as conn:
        rows = conn.execute(
            "SELECT met FROM commitment_checks WHERE goal_id=? AND check_date='2026-07-15'",
            (goal_id,),
        ).fetchall()
    assert len(rows) == 1 and rows[0]["met"] == 0
    # Late logging lands, morning re-check flips the same row to met.
    insert(db, "ledger", notion_page_id="l1", date="2026-07-15", exercise_type="PYQs")
    commitments.run_checks_for_date("2026-07-15", db_path=db)
    with commitments._connect(db) as conn:
        rows = conn.execute(
            "SELECT met FROM commitment_checks WHERE goal_id=? AND check_date='2026-07-15'",
            (goal_id,),
        ).fetchall()
    assert len(rows) == 1 and rows[0]["met"] == 1


def test_streak_and_adherence(db):
    goal = make_goal(db)
    goal_id = goal["notion_page_id"]
    for date, met in (("2026-07-11", 1), ("2026-07-12", 1), ("2026-07-13", 0),
                      ("2026-07-14", 1), ("2026-07-15", 1)):
        seed_check(db, goal_id, date, met)
    assert commitments.streak(goal_id, as_of="2026-07-15", db_path=db) == 2
    assert commitments.streak(goal_id, as_of="2026-07-12", db_path=db) == 2
    assert commitments.streak(goal_id, as_of="2026-07-13", db_path=db) == 0
    stats = commitments.adherence(goal_id, as_of="2026-07-15", days=7, db_path=db)
    assert stats == {"met": 4, "total": 5, "pct": 80}


def test_streak_never_spans_unverified_gap(db):
    goal = make_goal(db)
    goal_id = goal["notion_page_id"]
    for date in ("2026-07-10", "2026-07-11", "2026-07-13", "2026-07-14"):
        seed_check(db, goal_id, date, 1)  # 07-12 was never checked
    assert commitments.streak(goal_id, as_of="2026-07-14", db_path=db) == 2


def test_ledger_filter_mapping(db):
    assert commitments.ledger_filter_for_goal({"title": "Daily PYQs"}) is not None
    assert commitments.ledger_filter_for_goal({"title": "Weekly revision"}) is not None
    # No ledger exercise_type represents homework -> unverifiable.
    assert commitments.ledger_filter_for_goal({"title": "Coaching homework daily"}) is None
    unverifiable = {"title": "Coaching homework daily", "goal_type": "Coverage", "target": 1}
    assert commitments.verify_goal_for_date(unverifiable, "2026-07-15", db_path=db)["met"] is None


def test_capture_conflicts(db):
    make_goal(db)  # Daily PYQs, Coverage 1
    dup = {"title": "PYQ grind", "metric": "PYQ sessions", "goal_type": "Coverage",
           "target": 2, "period": "Daily", "subject": None}
    conflicts = commitments.capture_conflicts(dup, db_path=db)
    assert any(c["goal_id"] for c in conflicts)
    unrelated = {"title": "Weekly revision", "metric": "revision sessions",
                 "goal_type": "Coverage", "target": 3, "period": "Weekly", "subject": None}
    assert commitments.capture_conflicts(unrelated, db_path=db) == []


def test_capture_conflicts_cy_ceiling(db):
    make_goal(db, title="Grind A", goal_type="CY", metric="cognitive_yield", target=200)
    over = {"title": "Grind B", "metric": "cognitive_yield", "goal_type": "CY",
            "target": 200, "period": "Daily", "subject": None}
    conflicts = commitments.capture_conflicts(over, db_path=db)
    assert any("ceiling" in c["message"] for c in conflicts)


def test_backfill_heals_offline_gap(db):
    """Bot offline for 2 nightly checks -> backfill fills them, streak survives."""
    goal = make_goal(db)
    goal_id = goal["notion_page_id"]
    # Ledger shows the user actually did PYQs on all 4 days.
    for i, date in enumerate(("2026-07-12", "2026-07-13", "2026-07-14", "2026-07-15")):
        insert(db, "ledger", notion_page_id=f"l{i}", date=date, exercise_type="PYQs")
    # Only the first day was verified before the bot went offline.
    commitments.run_checks_for_date("2026-07-12", db_path=db)
    assert commitments.streak(goal_id, as_of="2026-07-15", db_path=db) <= 1
    commitments.backfill_checks(days=3, end_date="2026-07-16", db_path=db)
    assert commitments.streak(goal_id, as_of="2026-07-15", db_path=db) == 4


def test_verify_weekly_goal(db):
    weekly = make_goal(db, title="Weekly revision", goal_type="Coverage",
                       metric="revision sessions", target=3, period="Weekly")
    for i, date in enumerate(("2026-07-10", "2026-07-12", "2026-07-14")):
        insert(db, "ledger", notion_page_id=f"r{i}", date=date, exercise_type="Revision")
    check = commitments.verify_weekly_goal(weekly, "2026-07-15", db_path=db)
    assert check["met"] is True and check["value"] == 3
    check = commitments.verify_weekly_goal(weekly, "2026-07-11", db_path=db)
    assert check["met"] is False and check["value"] == 1
    unverifiable = {"title": "Coaching homework weekly", "goal_type": "Coverage",
                    "target": 2, "period": "Weekly"}
    assert commitments.verify_weekly_goal(unverifiable, "2026-07-15", db_path=db)["met"] is None


def test_cancel_recreate_cancel_not_ambiguous(db):
    """Cancelled goals must not poison title matching forever (E2E run-3 bug)."""
    make_goal(db)
    sd.update_goal_status("Daily PYQs", "Cancelled", db_path=db)
    make_goal(db)  # recreate same title
    result = sd.update_goal_status("Daily PYQs", "Cancelled", db_path=db)
    assert result["status"] == "Cancelled"
    rows = sd._rows("goals", "archived=0 AND title='Daily PYQs'", db_path=db)
    assert all(r["status"] == "Cancelled" for r in rows) and len(rows) == 2


def test_prefs_roundtrip(db):
    pref_id = commitments.add_pref(7, "prefers maths in the morning", db_path=db)
    prefs = commitments.active_prefs(7, db_path=db)
    assert [p["id"] for p in prefs] == [pref_id]
    assert commitments.active_prefs(8, db_path=db) == []  # per-chat isolation
    removed = commitments.deactivate_pref(7, "maths in the morning", db_path=db)
    assert removed and removed["id"] == pref_id
    assert commitments.active_prefs(7, db_path=db) == []
