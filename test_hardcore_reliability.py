"""Adversarial reliability contract for long-lived, evidence-honest operation.

These tests target corruption, false confidence, restart/migration loss, WAL
backup loss, archive leakage, and two-year calendar/storage behavior. They are
offline and deterministic; no Telegram, Notion, or LLM network call is made.
"""

from __future__ import annotations

import datetime as dt
import math
import random
import sqlite3

import pytest

import bot
import commitments
import domain_parser
import formulas
import logging_flow
import operational_store
import sql_query_flow
import study_domain as sd
import sync
from intent_parser import _validate_intent


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "hardcore.db"
    with sync.connect(path) as conn:
        sync.init_db(conn)
    return path


def _execution_intent(fields):
    return _validate_intent({
        "action": "log_execution", "database": "ledger", "fields": fields,
        "filters": {}, "needs_clarification": False,
        "clarification_question": None,
    })


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, "NaN", "Infinity"])
def test_nonfinite_numbers_are_rejected_at_every_trust_boundary(bad):
    with pytest.raises(domain_parser.DomainParseError, match="finite"):
        domain_parser._number(bad, "target")
    with pytest.raises(domain_parser.DomainParseError, match="finite"):
        domain_parser._signed_number(bad, "marks")
    with pytest.raises(sd.DomainError, match="finite"):
        sd._require_nonnegative("target", bad)
    with pytest.raises(sd.DomainError, match="finite"):
        sd._require_number("marks", bad)
    with pytest.raises(ValueError, match="finite"):
        formulas.accuracy_ratio(10, bad)
    with pytest.raises(ValueError, match="finite"):
        formulas.cognitive_yield("Physics", "PYQs", 45, 10, bad)


def test_logging_rejects_fractional_counts_and_nonfinite_time(db):
    fractional = logging_flow.build_write_plan(_execution_intent({
        "exercise_type": "PYQs", "questions_attempted": 10.5,
        "questions_correct": 8, "actual_time_min": 45,
    }), 1, db_path=db, first_round=False)
    assert fractional.needs_clarification
    assert "whole number" in fractional.clarification_question

    nonfinite = logging_flow.build_write_plan(_execution_intent({
        "exercise_type": "PYQs", "questions_attempted": 10,
        "questions_correct": 8, "actual_time_min": math.nan,
    }), 1, db_path=db, first_round=False)
    assert nonfinite.needs_clarification
    assert "finite number" in nonfinite.clarification_question


def test_incomplete_or_nonfinite_source_never_becomes_a_fake_zero_score():
    for bad in (None, "NaN", "Infinity", "not-a-number"):
        row = {
            "subject": "Physics", "exercise_type": "PYQs",
            "actual_time_min": 45, "questions_attempted": 10,
            "questions_correct": bad,
        }
        sync._materialise_computed_columns("ledger", row)
        assert row["cognitive_yield"] is None
        assert row["accuracy_ratio"] is None


def test_formula_invariants_over_thousands_of_sessions():
    rng = random.Random(20280721)
    subjects = ("Chem", "Physics", "Maths")
    exercises = tuple(formulas._TQ_TABLE)
    for _ in range(3000):
        attempted = rng.randint(1, 300)
        correct = rng.randint(0, attempted)
        minutes = rng.uniform(0.25, 720)
        subject = rng.choice(subjects)
        exercise = rng.choice(exercises)
        accuracy = formulas.accuracy_ratio(attempted, correct)
        cy = formulas.cognitive_yield(subject, exercise, minutes, attempted, correct)
        theory = formulas.theory_yield(subject, exercise, minutes, attempted, correct)
        assert 0 <= accuracy <= 1
        assert cy >= 0
        assert theory >= cy


def test_dates_reject_valid_prefix_with_trailing_garbage():
    assert sd._iso_date("2028-02-29") == "2028-02-29"
    assert sd._iso_date("2028-02-29T09:30:00+05:30").startswith("2028-02-29")
    for bad in ("2028-02-30", "2028-02-29garbage", "2026-01-01 DROP TABLE"):
        with pytest.raises(sd.DomainError, match="invalid ISO date"):
            sd._iso_date(bad)


@pytest.mark.parametrize("priority", [0, -1, 101, 10.5, math.inf])
def test_priority_gates_cannot_be_bypassed(priority, db):
    with pytest.raises(sd.DomainError):
        sd.create_work_item({"title": "bad", "priority": priority}, db_path=db)
    with pytest.raises(sd.DomainError):
        sd.create_goal({"title": "bad", "target": 1, "priority": priority}, db_path=db)


def test_exam_summary_rejects_partial_impossible_counts(db):
    sd.create_exam({
        "title": "Hard mock", "exam_date": "2027-01-10",
        "max_marks": 300, "target_marks": 220,
    }, db_path=db)
    with pytest.raises(sd.DomainError, match="correct cannot exceed attempted"):
        sd.record_exam_summary(
            "Hard mock", {"actual_marks": 100, "attempted": 10, "correct": 11},
            db_path=db,
        )
    with pytest.raises(sd.DomainError, match="incorrect cannot exceed attempted"):
        sd.record_exam_summary(
            "Hard mock", {"actual_marks": 100, "attempted": 10, "incorrect": 11},
            db_path=db,
        )


def test_old_operational_schema_migrates_without_losing_rows(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE op_goals (
                id TEXT PRIMARY KEY, notion_page_id TEXT NOT NULL UNIQUE,
                notion_url TEXT, created_time TEXT NOT NULL,
                last_edited_time TEXT NOT NULL, last_synced_at TEXT,
                archived INTEGER NOT NULL DEFAULT 0,
                raw_json TEXT NOT NULL DEFAULT '{}', page_content TEXT,
                title TEXT
            )
        """)
        conn.execute(
            "INSERT INTO op_goals (id, notion_page_id, created_time, "
            "last_edited_time, title) VALUES ('g1','g1','t','t','keep me')"
        )
        operational_store.init_db(conn)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(op_goals)")}
        title = conn.execute("SELECT title FROM op_goals WHERE id='g1'").fetchone()[0]
    assert "operation_id" in columns
    assert "goal_type" in columns
    assert title == "keep me"


def test_archived_operational_record_is_immutable(db):
    goal = sd.create_goal({"title": "Archive me", "target": 1}, db_path=db)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE op_goals SET archived=1 WHERE id=?", (goal["id"],))
        conn.commit()
    with pytest.raises(operational_store.OperationalStoreError, match="no active"):
        operational_store.update(
            "goals", goal["id"], {"title": "resurrected"}, db_path=db,
        )


def test_backup_includes_committed_wal_and_restores_cleanly(tmp_path):
    source = tmp_path / "state.db"
    settings = tmp_path / "settings.json"
    settings.write_text('{"safe": true}', encoding="utf-8")
    backup_root = tmp_path / "backups"
    with sqlite3.connect(source) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE critical (value TEXT)")
        writer.execute("INSERT INTO critical VALUES ('committed-in-wal')")
        writer.commit()
        day_dir = bot._backup_state(
            source, settings, backup_root, "2028-02-29",
        )
    restored = day_dir / "state.db"
    with sqlite3.connect(restored) as conn:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT value FROM critical").fetchone()[0] == "committed-in-wal"
    assert (day_dir / "settings.json").read_text(encoding="utf-8") == '{"safe": true}'


def test_backup_rotation_keeps_exactly_seven_daily_snapshots(tmp_path):
    source = tmp_path / "state.db"
    settings = tmp_path / "settings.json"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE x (n INTEGER)")
    root = tmp_path / "backups"
    for offset in range(8):
        day = (dt.date(2028, 1, 1) + dt.timedelta(days=offset)).isoformat()
        bot._backup_state(source, settings, root, day)
    kept = sorted(path.name for path in root.iterdir() if path.is_dir())
    assert kept == [f"2028-01-{day:02d}" for day in range(2, 9)]


def test_archive_policy_covers_every_alias_in_self_joins():
    unsafe = (
        "SELECT a.task, b.task FROM ledger a JOIN ledger b ON a.subject=b.subject "
        "WHERE a.archived=0"
    )
    safe = unsafe + " AND b.archived=0"
    assert sql_query_flow._active_filter_error(unsafe, "compare sessions") is not None
    assert sql_query_flow._active_filter_error(safe, "compare sessions") is None


def test_answer_prompt_uses_the_same_isolated_database_as_queries(db, monkeypatch):
    seen = []
    monkeypatch.setattr(
        sql_query_flow.sql_tool, "schema_digest",
        lambda db_path: seen.append(db_path) or "ISOLATED SCHEMA",
    )
    prompt = sql_query_flow._build_system_prompt(chat_id=None, db_path=db)
    assert seen == [db]
    assert "ISOLATED SCHEMA" in prompt


def test_two_year_store_survives_leap_day_restart_and_archive_filter(db):
    start = dt.date(2026, 3, 1)
    end = dt.date(2028, 2, 29)
    days = (end - start).days + 1
    assert days == 731
    rows = []
    for offset in range(days):
        day = start + dt.timedelta(days=offset)
        rows.append((
            f"day-{offset}", 0, day.isoformat(), "{}", day.isoformat(),
            "Physics", "PYQs", 45, 10, 8, 40, 0.8,
        ))
    with sqlite3.connect(db) as conn:
        conn.executemany(
            "INSERT INTO ledger (notion_page_id, archived, last_synced_at, raw_json, "
            "date, subject, exercise_type, actual_time_min, questions_attempted, "
            "questions_correct, cognitive_yield, accuracy_ratio) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
    report = sd.weekly_report(end_date=end.isoformat(), db_path=db)
    assert report["ledger"]["blocks"] == 7
    assert report["ledger"]["attempted"] == 70

    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE ledger SET archived=1 WHERE date='2028-02-29'")
        conn.commit()
    report = sd.weekly_report(end_date=end.isoformat(), db_path=db)
    assert report["ledger"]["blocks"] == 6
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def test_streak_is_not_silently_capped_before_two_year_goal(db):
    goal = sd.create_goal({
        "title": "Daily PYQs", "goal_type": "Coverage", "metric": "sessions",
        "target": 1, "period": "Daily",
    }, db_path=db)
    start = dt.date(2026, 3, 1)
    checks = [
        (goal["id"], (start + dt.timedelta(days=i)).isoformat(), 1, 1, 1, "now")
        for i in range(731)
    ]
    with commitments._connect(db) as conn:
        conn.executemany(
            f"INSERT INTO {commitments.CHECKS_TABLE} "
            "(goal_id, check_date, met, value, target, checked_at) VALUES (?,?,?,?,?,?)",
            checks,
        )
        conn.commit()
    assert commitments.streak(
        goal["id"], as_of="2028-02-29", db_path=db,
    ) == 731


def test_air_one_target_is_idempotent_and_explicitly_not_a_prediction(db):
    first = sd.ensure_system_goals(db_path=db)
    second = sd.ensure_system_goals(db_path=db)
    assert first["id"] == second["id"]
    rows = operational_store.rows(
        "goals", "operation_id=?", ("system-goal:jee-2028-air-1",), db_path=db,
    )
    assert len(rows) == 1
    assert "not a rank prediction" in rows[0]["notes"].lower()
