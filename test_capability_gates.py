"""Tests for capability_gates.py — progressive capability unlock gates.

All tests use temp-db fixtures — the real sqlite_mirror.db is never touched.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import capability_gates


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path: Path) -> Path:
    """Empty temp db — all tables absent, all gates locked (except agent_chat)."""
    db_path = tmp_path / "test_gates.db"
    conn = sqlite3.connect(str(db_path))
    conn.close()
    return db_path


@pytest.fixture
def seeded_db(tmp_path: Path) -> Path:
    """Db with enough data to unlock most gates."""
    db_path = tmp_path / "seeded_gates.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE execution_blocks (block_key TEXT, seq INTEGER);
        INSERT INTO execution_blocks VALUES ('exec_a', 1), ('exec_b', 2);

        CREATE TABLE ledger (subject TEXT, task TEXT, archived INTEGER DEFAULT 0);
        INSERT INTO ledger VALUES ('Physics', 'test', 0), ('Chemistry', 'test', 0),
                                   ('Maths', 'test', 0), ('Physics', 'test2', 0),
                                   ('Chemistry', 'test2', 0), ('Maths', 'test2', 0),
                                   ('Physics', 'test3', 0), ('Chemistry', 'test3', 0);

        CREATE TABLE op_exams (title TEXT, exam_date TEXT, archived INTEGER DEFAULT 0);
        INSERT INTO op_exams VALUES ('JEE Main Mock', '2026-08-15', 0);

        CREATE TABLE op_work_items (kind TEXT, status TEXT, subject TEXT, archived INTEGER DEFAULT 0);
        INSERT INTO op_work_items VALUES ('Current Syllabus', 'Completed', 'Physics', 0);

        CREATE TABLE op_jee_metadata (id INTEGER PRIMARY KEY CHECK(id=1), total_papers INTEGER);
        INSERT INTO op_jee_metadata VALUES (1, 414);

        CREATE TABLE learn_formulas (formula_id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT);
        INSERT INTO learn_formulas VALUES (1, 'Physics'), (2, 'Chemistry');

        CREATE TABLE op_doubt_attempts (id INTEGER PRIMARY KEY AUTOINCREMENT, doubt_title TEXT);
        INSERT INTO op_doubt_attempts VALUES (1, 'doubt 1'), (2, 'doubt 2');
    """)
    conn.commit()
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# check() — individual gate tests
# ---------------------------------------------------------------------------

def test_check_agent_chat_always_unlocked(db: Path):
    """Given empty db, agent_chat should still be unlocked (primary interface)."""
    result = capability_gates.check("agent_chat", db_path=db)
    assert result["unlocked"] is True
    assert result["capability"] == "agent_chat"


def test_check_discipline_locked_when_no_blocks(db: Path):
    """Given empty db (no execution_blocks), discipline should be locked."""
    result = capability_gates.check("discipline", db_path=db)
    assert result["unlocked"] is False
    assert "timetable" in result["reason"].lower()


def test_check_discipline_unlocked_when_blocks_seeded(seeded_db: Path):
    """Given execution_blocks with 2 rows, discipline should be unlocked."""
    result = capability_gates.check("discipline", db_path=seeded_db)
    assert result["unlocked"] is True
    assert "2" in result["reason"]


def test_check_mock_prep_locked_when_insufficient_ledger(db: Path):
    """Given 0 ledger rows, mock_prep should be locked."""
    result = capability_gates.check("mock_prep", db_path=db)
    assert result["unlocked"] is False
    assert "3" in result["reason"]


def test_check_mock_prep_unlocked_with_ledger_and_exams(seeded_db: Path):
    """Given 8 ledger rows + 1 exam, mock_prep should be unlocked."""
    result = capability_gates.check("mock_prep", db_path=seeded_db)
    assert result["unlocked"] is True
    assert "8" in result["reason"]


def test_check_chapter_classify_locked_when_no_completed_chapters(db: Path):
    """Given no completed work items, chapter_classify should be locked."""
    result = capability_gates.check("chapter_classify", db_path=db)
    assert result["unlocked"] is False


def test_check_chapter_classify_unlocked_when_completed_chapter_exists(seeded_db: Path):
    """Given a completed Current Syllabus work item, chapter_classify should unlock."""
    result = capability_gates.check("chapter_classify", db_path=seeded_db)
    assert result["unlocked"] is True
    assert "1" in result["reason"]


def test_check_weekly_report_locked_when_insufficient_sessions(db: Path):
    """Given 0 ledger rows, weekly_report should be locked."""
    result = capability_gates.check("weekly_report", db_path=db)
    assert result["unlocked"] is False
    assert "7" in result["reason"]


def test_check_weekly_report_unlocked_with_seven_sessions(seeded_db: Path):
    """Given 8 ledger rows, weekly_report should be unlocked."""
    result = capability_gates.check("weekly_report", db_path=seeded_db)
    assert result["unlocked"] is True


def test_check_jee_analytics_locked_when_no_metadata(db: Path):
    """Given no op_jee_metadata, jee_analytics should be locked."""
    result = capability_gates.check("jee_analytics", db_path=db)
    assert result["unlocked"] is False
    assert "not loaded" in result["reason"]


def test_check_jee_analytics_unlocked_when_metadata_exists(seeded_db: Path):
    """Given op_jee_metadata row, jee_analytics should be unlocked."""
    result = capability_gates.check("jee_analytics", db_path=seeded_db)
    assert result["unlocked"] is True


def test_check_active_recall_locked_when_no_formulas(db: Path):
    """Given no learn_formulas, active_recall should be locked."""
    result = capability_gates.check("active_recall", db_path=db)
    assert result["unlocked"] is False
    assert "/learn" in result["reason"]


def test_check_active_recall_unlocked_when_formulas_exist(seeded_db: Path):
    """Given learn_formulas with 2 rows, active_recall should be unlocked."""
    result = capability_gates.check("active_recall", db_path=seeded_db)
    assert result["unlocked"] is True
    assert "2" in result["reason"]


def test_check_teacher_window_locked_when_insufficient_attempts(db: Path):
    """Given 0 doubt attempts, teacher_window should be locked."""
    result = capability_gates.check("teacher_window", db_path=db)
    assert result["unlocked"] is False
    assert "2" in result["reason"]


def test_check_teacher_window_unlocked_with_two_attempts(seeded_db: Path):
    """Given 2 doubt attempts, teacher_window should be unlocked."""
    result = capability_gates.check("teacher_window", db_path=seeded_db)
    assert result["unlocked"] is True


def test_check_unknown_capability_returns_locked(db: Path):
    """Given an unknown capability name, check should return unlocked=False."""
    result = capability_gates.check("nonexistent_feature", db_path=db)
    assert result["unlocked"] is False
    assert "unknown" in result["reason"].lower()


# ---------------------------------------------------------------------------
# check_all() — bulk check
# ---------------------------------------------------------------------------

def test_check_all_returns_every_capability(db: Path):
    """Given check_all, it should return every capability in CAPABILITIES."""
    results = capability_gates.check_all(db_path=db)
    for cap in capability_gates.CAPABILITIES:
        assert cap in results
        assert "unlocked" in results[cap]
        assert "reason" in results[cap]


def test_check_all_on_empty_db_only_agent_chat_unlocked(db: Path):
    """Given empty db, only agent_chat should be unlocked."""
    results = capability_gates.check_all(db_path=db)
    unlocked = [c for c, v in results.items() if v["unlocked"]]
    assert unlocked == ["agent_chat"]


def test_check_all_on_seeded_db_most_unlocked(seeded_db: Path):
    """Given seeded db with ledger/exams/work_items/formulas, most gates should pass."""
    results = capability_gates.check_all(db_path=seeded_db)
    locked = [c for c, v in results.items() if not v["unlocked"]]
    # On the seeded fixture, everything should be unlocked
    assert locked == [], f"Expected all unlocked, but locked: {locked}"


# ---------------------------------------------------------------------------
# unlocked() and locked_reason() — convenience helpers
# ---------------------------------------------------------------------------

def test_unlocked_returns_boolean(db: Path):
    """Given unlocked(), it should return a bool, not a dict."""
    assert capability_gates.unlocked("agent_chat", db_path=db) is True
    assert capability_gates.unlocked("discipline", db_path=db) is False


def test_locked_reason_returns_none_when_unlocked(db: Path):
    """Given an unlocked capability, locked_reason should return None."""
    assert capability_gates.locked_reason("agent_chat", db_path=db) is None


def test_locked_reason_returns_string_when_locked(db: Path):
    """Given a locked capability, locked_reason should return a descriptive string."""
    reason = capability_gates.locked_reason("discipline", db_path=db)
    assert reason is not None
    assert isinstance(reason, str)
    assert len(reason) > 0


# ---------------------------------------------------------------------------
# progress_summary() — onboarding hub / inspect view
# ---------------------------------------------------------------------------

def test_progress_summary_empty_db(db: Path):
    """Given empty db, progress_summary should show 1 unlocked, 7 locked."""
    summary = capability_gates.progress_summary(db_path=db)
    assert summary["total"] == 8
    assert summary["unlocked_count"] == 1
    assert "agent_chat" in summary["unlocked"]
    assert len(summary["locked"]) == 7
    assert "discipline" in summary["locked"]


def test_progress_summary_seeded_db(seeded_db: Path):
    """Given seeded db, progress_summary should show all 8 unlocked."""
    summary = capability_gates.progress_summary(db_path=seeded_db)
    assert summary["total"] == 8
    assert summary["unlocked_count"] == 8
    assert len(summary["locked"]) == 0


def test_progress_summary_partial_db(tmp_path: Path):
    """Given partial data (only ledger), some gates should be unlocked."""
    db_path = tmp_path / "partial_gates.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE ledger (subject TEXT, task TEXT, archived INTEGER DEFAULT 0);
        INSERT INTO ledger VALUES ('Physics', 'test', 0), ('Chemistry', 'test', 0),
                                   ('Maths', 'test', 0);
    """)
    conn.commit()
    conn.close()
    summary = capability_gates.progress_summary(db_path=db_path)
    # mock_prep needs 3 ledger + exams (no exams → locked)
    # weekly_report needs 7 (only 3 → locked)
    # agent_chat always unlocked
    unlocked = set(summary["unlocked"])
    assert "agent_chat" in unlocked
    assert "weekly_report" not in unlocked


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_check_does_not_create_tables(db: Path):
    """Given check(), it should NOT create any tables (read-only)."""
    capability_gates.check_all(db_path=db)
    conn = sqlite3.connect(str(db))
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    conn.close()
    # An empty db file has 0 user tables
    assert tables == []


def test_check_handles_archived_ledger_rows(tmp_path: Path):
    """Given archived=1 ledger rows, they should NOT count toward the gate."""
    db_path = tmp_path / "archived_gates.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE ledger (subject TEXT, task TEXT, archived INTEGER DEFAULT 0);
        INSERT INTO ledger VALUES ('Physics', 'test', 1), ('Chemistry', 'test', 1),
                                   ('Maths', 'test', 1);
    """)
    conn.commit()
    conn.close()
    result = capability_gates.check("mock_prep", db_path=db_path)
    assert result["unlocked"] is False
    assert "0" in result["reason"]


def test_capabilities_tuple_is_complete():
    """Given CAPABILITIES, it should match the gate function registry keys."""
    assert set(capability_gates.CAPABILITIES) == set(capability_gates._GATE_FUNCS.keys())
    assert len(capability_gates.CAPABILITIES) == 8