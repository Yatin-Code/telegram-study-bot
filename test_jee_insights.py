"""Tests for jee_insights — personalized JEE coaching insight engine (offline).

Covers all 6 read-only insight functions against a tiny seeded SQLite mirror:
  * graceful status codes when JEE analytics or user data is missing
  * missed_opportunities (time vs ROI allocation)
  * doubt_prioritization (open doubts ranked by JEE weightage)
  * skip_or_study (chapter prioritization)
  * trending_chapters (year-over-year question trends)
  * formula_priority (formulas ranked by JEE pattern frequency)
  * strengths_vs_reality (accuracy vs JEE difficulty)

All tests use a tmp_path db seeded via the real init_db loaders — the real
sqlite_mirror.db is never read or written.

Usage:
    .venv-test/bin/python -m pytest -q test_jee_insights.py
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import jee_insights as ji
import jee_data_loader
import learn_formulas


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "insights.db"
    with sqlite3.connect(str(path)) as conn:
        jee_data_loader.init_db(conn)
        learn_formulas.init_db(conn)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ledger ("
            " notion_page_id TEXT, archived INTEGER DEFAULT 0, subject TEXT,"
            " chapter_text TEXT, actual_time_min REAL, questions_attempted INTEGER,"
            " questions_correct INTEGER)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS doubts ("
            " notion_page_id TEXT, archived INTEGER DEFAULT 0, subject TEXT,"
            " core_concept TEXT, status TEXT, created_time TEXT)"
        )
        conn.commit()
    return path


def _seed_stats(db, rows):
    with sqlite3.connect(str(db)) as conn:
        for subject, chapter, total, roi, rep, easy, med, hard in rows:
            conn.execute(
                "INSERT OR REPLACE INTO op_jee_chapter_stats "
                "(subject, chapter, exam_type, total_questions, repeating_questions,"
                " unique_questions, repeat_ratio, easy_ratio, medium_ratio, hard_ratio,"
                " importance_score, by_year_json, by_difficulty_json,"
                " by_question_type_json, sub_topics_json, needs_figure) "
                "VALUES (?,?,'mains',?,?,0,?,?,?,?,?,'{}','{}','{}','{}',0)",
                (subject, chapter, total, total, rep, easy, med, hard, roi),
            )
        conn.commit()


def _seed_ledger(db, rows):
    with sqlite3.connect(str(db)) as conn:
        for i, (subject, chapter, minutes, attempted, correct) in enumerate(rows):
            conn.execute(
                "INSERT INTO ledger (notion_page_id, archived, subject, chapter_text,"
                " actual_time_min, questions_attempted, questions_correct) "
                "VALUES (?,0,?,?,?,?,?)",
                (f"l{i}", subject, chapter, minutes, attempted, correct),
            )
        conn.commit()


def _seed_doubts(db, rows):
    with sqlite3.connect(str(db)) as conn:
        for i, (subject, concept, status) in enumerate(rows):
            conn.execute(
                "INSERT INTO doubts (notion_page_id, archived, subject, core_concept,"
                " status, created_time) VALUES (?,0,?,?,?,?)",
                (f"d{i}", subject, concept, status, f"2026-07-{10+i:02d}"),
            )
        conn.commit()


@pytest.fixture()
def bare_db(tmp_path):
    """A db file with NO tables at all — triggers the no_jee_data path."""
    path = tmp_path / "bare.db"
    sqlite3.connect(str(path)).close()
    return path


# ---------------------------------------------------------------------------
# Status codes when data is missing
# ---------------------------------------------------------------------------

def test_missed_opportunities_no_jee_data(bare_db):
    assert ji.missed_opportunities(db_path=bare_db)["status"] == "no_jee_data"


def test_doubt_prioritization_no_jee_data(bare_db):
    assert ji.doubt_prioritization(db_path=bare_db)["status"] == "no_jee_data"


def test_skip_or_study_no_jee_data(bare_db):
    assert ji.skip_or_study(db_path=bare_db)["status"] == "no_jee_data"


def test_trending_chapters_no_jee_data(bare_db):
    assert ji.trending_chapters(db_path=bare_db)["status"] == "no_jee_data"


def test_formula_priority_no_formulas(bare_db):
    assert ji.formula_priority(db_path=bare_db)["status"] == "no_formulas"


def test_strengths_vs_reality_no_jee_data(bare_db):
    assert ji.strengths_vs_reality(db_path=bare_db)["status"] == "no_jee_data"


def test_missed_opportunities_insufficient_sessions(db):
    _seed_stats(db, [("Physics", "Kinematics", 100, 120.0, 0.9, 0.5, 0.3, 0.2)])
    _seed_ledger(db, [("Physics", "Kinematics", 60, 10, 8)])
    result = ji.missed_opportunities(db_path=db)
    assert result["status"] == "insufficient_sessions"
    assert result["allocations"] == []


def test_doubt_prioritization_no_open_doubts(db):
    _seed_stats(db, [("Physics", "Kinematics", 100, 120.0, 0.9, 0.5, 0.3, 0.2)])
    result = ji.doubt_prioritization(db_path=db)
    assert result["status"] == "no_doubts"


# ---------------------------------------------------------------------------
# missed_opportunities — time allocation vs ROI
# ---------------------------------------------------------------------------

def test_missed_opportunities_flags_over_and_under_allocation(db):
    # 5+ chapters with time; one high-time/low-ROI, one low-time/high-ROI
    _seed_stats(db, [
        ("Physics", "LowRoiChapter", 10, 20.0, 0.5, 0.6, 0.3, 0.1),
        ("Physics", "HighRoiChapter", 300, 200.0, 0.95, 0.4, 0.3, 0.3),
        ("Chemistry", "MidA", 100, 80.0, 0.8, 0.5, 0.3, 0.2),
        ("Chemistry", "MidB", 100, 80.0, 0.8, 0.5, 0.3, 0.2),
        ("Mathematics", "MidC", 100, 80.0, 0.8, 0.5, 0.3, 0.2),
    ])
    _seed_ledger(db, [
        ("Physics", "LowRoiChapter", 500, 20, 15),   # lots of time, low ROI
        ("Physics", "HighRoiChapter", 5, 5, 4),      # tiny time, high ROI
        ("Chemistry", "MidA", 100, 10, 7),
        ("Chemistry", "MidB", 100, 10, 7),
        ("Mathematics", "MidC", 100, 10, 7),
    ])
    result = ji.missed_opportunities(db_path=db)
    assert result["status"] == "ok"
    recs = {a["chapter"]: a["recommendation"] for a in result["allocations"]}
    assert recs["LowRoiChapter"] == "consider_reducing"
    assert recs["HighRoiChapter"] == "consider_increasing"
    assert any(a["chapter"] == "HighRoiChapter" for a in result["under_allocated"])
    assert any(a["chapter"] == "LowRoiChapter" for a in result["over_allocated"])
    assert result["top_recommendation"] is not None


# ---------------------------------------------------------------------------
# doubt_prioritization — open doubts ranked by JEE weightage
# ---------------------------------------------------------------------------

def test_doubt_prioritization_ranks_by_weightage(db):
    _seed_stats(db, [
        ("Physics", "Rotational Mechanics", 400, 200.0, 0.9, 0.3, 0.3, 0.4),
        ("Chemistry", "Some Tiny Topic", 5, 10.0, 0.2, 0.8, 0.1, 0.1),
    ])
    _seed_doubts(db, [
        ("Physics", "Rotational Mechanics torque confusion", "open"),
        ("Chemistry", "Some Tiny Topic detail", "open"),
    ])
    result = ji.doubt_prioritization(db_path=db)
    assert result["status"] == "ok"
    doubts = result["doubts"]
    assert len(doubts) == 2
    # Rotational (rank 1 by total_questions) should be high urgency, first
    assert doubts[0]["urgency"] == "high"
    assert "Rotational" in doubts[0]["core_concept"]
    assert doubts[0]["estimated_marks"] is not None
    assert result["highest_priority"] is not None


def test_doubt_prioritization_ignores_resolved_doubts(db):
    _seed_stats(db, [("Physics", "Rotational Mechanics", 400, 200.0, 0.9, 0.3, 0.3, 0.4)])
    _seed_doubts(db, [("Physics", "Rotational Mechanics resolved", "Resolved")])
    assert ji.doubt_prioritization(db_path=db)["status"] == "no_doubts"


# ---------------------------------------------------------------------------
# skip_or_study — chapter prioritization
# ---------------------------------------------------------------------------

def test_skip_or_study_prioritizes_high_roi_low_accuracy(db):
    _seed_stats(db, [
        ("Physics", "HardHighRoi", 300, 150.0, 0.9, 0.2, 0.3, 0.5),
        ("Chemistry", "EasyLowRoi", 10, 15.0, 0.3, 0.9, 0.05, 0.05),
    ])
    # 2+ sessions each so accuracy counts
    _seed_ledger(db, [
        ("Physics", "HardHighRoi", 60, 10, 3),
        ("Physics", "HardHighRoi", 60, 10, 4),   # ~35% accuracy
        ("Chemistry", "EasyLowRoi", 60, 10, 9),
        ("Chemistry", "EasyLowRoi", 60, 10, 10),  # ~95% accuracy
    ])
    result = ji.skip_or_study(db_path=db)
    assert result["status"] == "ok"
    prioritized = {c["chapter"] for c in result["prioritize"]}
    deprioritized = {c["chapter"] for c in result["deprioritize"]}
    assert "HardHighRoi" in prioritized
    assert "EasyLowRoi" in deprioritized


# ---------------------------------------------------------------------------
# trending_chapters — year-over-year question trends
# ---------------------------------------------------------------------------

def test_trending_chapters_detects_up_and_down(db):
    with sqlite3.connect(str(db)) as conn:
        for year, count in (("2020", 2), ("2021", 2), ("2023", 8), ("2024", 9)):
            conn.execute(
                "INSERT INTO op_jee_trends (subject, chapter, year, question_count)"
                " VALUES ('Physics','RisingChapter',?,?)", (year, count),
            )
        for year, count in (("2020", 9), ("2021", 8), ("2023", 1), ("2024", 1)):
            conn.execute(
                "INSERT INTO op_jee_trends (subject, chapter, year, question_count)"
                " VALUES ('Physics','FallingChapter',?,?)", (year, count),
            )
        conn.commit()
    result = ji.trending_chapters(db_path=db)
    assert result["status"] == "ok"
    up = {c["chapter"] for c in result["trending_up"]}
    down = {c["chapter"] for c in result["trending_down"]}
    assert "RisingChapter" in up
    assert "FallingChapter" in down


# ---------------------------------------------------------------------------
# formula_priority — formulas ranked by JEE pattern frequency
# ---------------------------------------------------------------------------

def test_formula_priority_ranks_by_pattern_count(db):
    with sqlite3.connect(str(db)) as conn:
        # chapter with many patterns
        for i in range(12):
            conn.execute(
                "INSERT INTO op_jee_patterns (subject, chapter, sub_topic, frequency,"
                " years_json, exams_json, core_concept, key_formula, common_trap,"
                " difficulty, question_type) VALUES ('Physics','BigChapter','t',5,"
                " '[]','[]','c','f','tr','Hard','MCQ')",
            )
        conn.execute(
            "INSERT INTO learn_formulas (subject, chapter, topic, formula_text, source,"
            " created_at, unlock_at, mastery) VALUES ('Physics','BigChapter','t',"
            " 'F = ma','self','2026-07-01','2026-08-05','new')",
        )
        conn.execute(
            "INSERT INTO learn_formulas (subject, chapter, topic, formula_text, source,"
            " created_at, unlock_at, mastery) VALUES ('Physics','ObscureChapter','t',"
            " 'x = 1','self','2026-07-01','2026-08-05','new')",
        )
        conn.commit()
    result = ji.formula_priority(db_path=db)
    assert result["status"] == "ok"
    formulas = result["formulas"]
    assert formulas[0]["chapter"] == "BigChapter"
    assert formulas[0]["priority"] == "high"
    assert formulas[0]["jee_patterns"] == 12
    assert result["highest_priority"] is not None


# ---------------------------------------------------------------------------
# strengths_vs_reality — accuracy vs JEE difficulty
# ---------------------------------------------------------------------------

def test_strengths_vs_reality_detects_gap(db):
    _seed_stats(db, [
        ("Physics", "VeryHardChapter", 300, 150.0, 0.9, 0.1, 0.2, 0.7),  # 70% hard
        ("Physics", "EasyChapter", 100, 60.0, 0.8, 0.8, 0.1, 0.1),       # 10% hard
    ])
    _seed_ledger(db, [
        ("Physics", "VeryHardChapter", 60, 10, 8),
        ("Physics", "VeryHardChapter", 60, 10, 9),  # 85% accuracy despite hard
        ("Physics", "EasyChapter", 60, 10, 8),
        ("Physics", "EasyChapter", 60, 10, 9),       # 85% accuracy, easy
    ])
    result = ji.strengths_vs_reality(db_path=db)
    assert result["status"] == "ok"
    # EasyChapter: high acc + low hard → strength
    strength_chapters = {s["chapter"] for s in result["strengths"]}
    assert "EasyChapter" in strength_chapters
