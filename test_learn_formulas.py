"""Formula memory system — tests for learn_formulas.py.

Covers the 35-day active-recall unlock module:
  * add + get round-trip (all fields match)
  * unlock_at = created_at + UNLOCK_DAYS (and custom days_to_unlock)
  * list filters by subject/chapter/topic, newest-first, limit cap
  * browse grouped category counts
  * due_for_recall (locked vs unlocked, mastered excluded)
  * mark_recalled (count bump, recalled_at, mastery learning/mastered)
  * stats aggregates
  * delete_formula True/False
  * idempotent init_db
  * subject stored/matched as given (no case folding)
  * empty subject/formula_text raise ValueError

All tests use a ``tmp_path`` SQLite db — the real ``sqlite_mirror.db`` is never
touched.

Usage:
    .venv-test/bin/python -m pytest -q -p no:cacheprovider -m "not live" test_learn_formulas.py
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import time

import pytest

import learn_formulas as lf


def _db(tmp_path):
    return tmp_path / "formulas.db"


# ---------------------------------------------------------------------------
# add + get round-trip
# ---------------------------------------------------------------------------

def test_add_then_get_round_trip_when_formula_stored(tmp_path):
    """Given a stored formula, when fetched by id, all fields match."""
    db = _db(tmp_path)
    formula = lf.add(
        "Physics", "Kinematics", "SUVAT", "v = u + at",
        source="teacher", notes="check units", db_path=db,
    )
    fetched = lf.get(formula["formula_id"], db_path=db)
    assert fetched is not None
    assert fetched["formula_id"] == formula["formula_id"]
    assert fetched["subject"] == "Physics"
    assert fetched["chapter"] == "Kinematics"
    assert fetched["topic"] == "SUVAT"
    assert fetched["formula_text"] == "v = u + at"
    assert fetched["source"] == "teacher"
    assert fetched["notes"] == "check units"
    assert fetched["recall_count"] == 0
    assert fetched["mastery"] == "new"
    assert fetched["recalled_at"] is None
    assert fetched["created_at"] == formula["created_at"]
    assert fetched["unlock_at"] == formula["unlock_at"]


def test_get_returns_none_when_unknown_id(tmp_path):
    """Given an empty store, when fetching an unknown id, None is returned."""
    db = _db(tmp_path)
    assert lf.get(9999, db_path=db) is None


# ---------------------------------------------------------------------------
# unlock_at
# ---------------------------------------------------------------------------

def test_add_sets_unlock_at_35_days_after_created_when_default_unlock(tmp_path):
    """Given a default add, when checking timestamps, unlock is created + 35d."""
    db = _db(tmp_path)
    formula = lf.add("Maths", "Limits", None, "lim = 1", db_path=db)
    created = dt.datetime.fromisoformat(formula["created_at"])
    unlock = dt.datetime.fromisoformat(formula["unlock_at"])
    assert unlock - created == dt.timedelta(days=lf.UNLOCK_DAYS)


def test_add_honors_custom_days_to_unlock_when_provided(tmp_path):
    """Given a custom days_to_unlock, when added, unlock is created + that many days."""
    db = _db(tmp_path)
    formula = lf.add("Maths", "Limits", None, "lim = 1", days_to_unlock=2, db_path=db)
    created = dt.datetime.fromisoformat(formula["created_at"])
    unlock = dt.datetime.fromisoformat(formula["unlock_at"])
    assert unlock - created == dt.timedelta(days=2)


# ---------------------------------------------------------------------------
# list filters
# ---------------------------------------------------------------------------

def test_list_filters_by_subject_when_subject_given(tmp_path):
    """Given mixed subjects, when listing by subject, only that subject returns."""
    db = _db(tmp_path)
    lf.add("Physics", "A", "t1", "f1", db_path=db)
    lf.add("Physics", "B", "t2", "f2", db_path=db)
    lf.add("Chemistry", "C", "t3", "f3", db_path=db)
    rows = lf.list(subject="Physics", db_path=db)
    assert {r["formula_text"] for r in rows} == {"f1", "f2"}


def test_list_filters_by_chapter_and_topic_when_given(tmp_path):
    """Given formulas, when filtering by chapter+topic, only matches return."""
    db = _db(tmp_path)
    lf.add("Physics", "Kinematics", "SUVAT", "v = u + at", db_path=db)
    lf.add("Physics", "Kinematics", "Projectile", "R = v^2/g", db_path=db)
    lf.add("Physics", "Dynamics", "SUVAT", "F = ma", db_path=db)
    rows = lf.list(chapter="Kinematics", topic="SUVAT", db_path=db)
    assert [r["formula_text"] for r in rows] == ["v = u + at"]


def test_list_orders_newest_first_when_multiple(tmp_path):
    """Given formulas added in sequence, when listed, newest is first."""
    db = _db(tmp_path)
    lf.add("Physics", "K", "t", "first", db_path=db)
    time.sleep(0.01)
    lf.add("Physics", "K", "t", "second", db_path=db)
    rows = lf.list(subject="Physics", db_path=db)
    assert [r["formula_text"] for r in rows] == ["second", "first"]


def test_list_respects_limit_when_requested(tmp_path):
    """Given three formulas, when listing with limit=2, only two return."""
    db = _db(tmp_path)
    for i in range(3):
        lf.add("Physics", "K", "t", f"f{i}", db_path=db)
    rows = lf.list(subject="Physics", limit=2, db_path=db)
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# browse
# ---------------------------------------------------------------------------

def test_browse_returns_grouped_counts_when_formulas_exist(tmp_path):
    """Given formulas across categories, when browsing, counts are grouped."""
    db = _db(tmp_path)
    lf.add("Physics", "Kinematics", "SUVAT", "v = u + at", db_path=db)
    lf.add("Physics", "Kinematics", "SUVAT", "s = ut + 0.5at^2", db_path=db)
    lf.add("Physics", "Kinematics", "Projectile", "R = v^2/g", db_path=db)
    lf.add("Chemistry", "Mole Concept", None, "n = m/M", db_path=db)
    cats = lf.browse(db_path=db)
    keyed = {(c["subject"], c["chapter"], c["topic"]): c["count"] for c in cats}
    assert keyed == {
        ("Physics", "Kinematics", "SUVAT"): 2,
        ("Physics", "Kinematics", "Projectile"): 1,
        ("Chemistry", "Mole Concept", None): 1,
    }


def test_browse_filters_by_subject_when_subject_given(tmp_path):
    """Given a subject filter, when browse, only that subject's categories return."""
    db = _db(tmp_path)
    lf.add("Physics", "Kinematics", "SUVAT", "v = u + at", db_path=db)
    lf.add("Chemistry", "Mole Concept", None, "n = m/M", db_path=db)
    cats = lf.browse(subject="Physics", db_path=db)
    assert {c["subject"] for c in cats} == {"Physics"}
    assert len(cats) == 1


# ---------------------------------------------------------------------------
# due_for_recall
# ---------------------------------------------------------------------------

def test_due_for_recall_empty_when_no_formulas_unlocked(tmp_path):
    """Given only locked formulas, when checking due, nothing is due."""
    db = _db(tmp_path)
    lf.add("Physics", "K", "t", "f1", db_path=db)  # 35-day lock
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    assert lf.due_for_recall(now=now, db_path=db) == []


def test_due_for_recall_returns_unlocked_when_unlock_passed(tmp_path):
    """Given a mix of locked and unlocked, when checking due, only unlocked return."""
    db = _db(tmp_path)
    lf.add("Physics", "K", "t", "locked", db_path=db)  # 35-day lock
    due = lf.add("Maths", "L", "u", "unlocked", days_to_unlock=0, db_path=db)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    rows = lf.due_for_recall(now=now, db_path=db)
    assert [r["formula_id"] for r in rows] == [due["formula_id"]]


def test_due_for_recall_excludes_mastered_when_mastered(tmp_path):
    """Given a mastered unlocked formula, when checking due, it is excluded."""
    db = _db(tmp_path)
    formula = lf.add("Maths", "L", "u", "done", days_to_unlock=0, db_path=db)
    lf.mark_recalled(formula["formula_id"], mastered=True, db_path=db)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    assert lf.due_for_recall(now=now, db_path=db) == []


# ---------------------------------------------------------------------------
# mark_recalled
# ---------------------------------------------------------------------------

def test_mark_recalled_increments_count_and_sets_learning_when_not_mastered(tmp_path):
    """Given a formula, when recalled without mastered, count bumps and mastery=learning."""
    db = _db(tmp_path)
    formula = lf.add("Physics", "K", "t", "v = u + at", db_path=db)
    updated = lf.mark_recalled(formula["formula_id"], db_path=db)
    assert updated["recall_count"] == 1
    assert updated["mastery"] == "learning"
    assert updated["recalled_at"] is not None
    fetched = lf.get(formula["formula_id"], db_path=db)
    assert fetched["recall_count"] == 1
    assert fetched["mastery"] == "learning"


def test_mark_recalled_sets_mastered_when_mastered_flag_true(tmp_path):
    """Given a formula, when recalled with mastered=True, mastery becomes mastered."""
    db = _db(tmp_path)
    formula = lf.add("Physics", "K", "t", "v = u + at", db_path=db)
    updated = lf.mark_recalled(formula["formula_id"], mastered=True, db_path=db)
    assert updated["mastery"] == "mastered"
    assert updated["recall_count"] == 1


def test_mark_recalled_returns_none_when_unknown_id(tmp_path):
    """Given an unknown id, when recalled, None is returned."""
    db = _db(tmp_path)
    assert lf.mark_recalled(9999, db_path=db) is None


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------

def test_stats_reports_counts_when_formulas_exist(tmp_path):
    """Given formulas, when stats, totals and breakdowns are correct."""
    db = _db(tmp_path)
    lf.add("Physics", "K", "t", "f1", db_path=db)
    lf.add("Physics", "K", "t", "f2", db_path=db)
    lf.add("Chemistry", "M", "u", "f3", db_path=db)
    stats = lf.stats(db_path=db)
    assert stats["total"] == 3
    assert stats["by_subject"] == {"Physics": 2, "Chemistry": 1}
    assert stats["by_mastery"] == {"new": 3}
    assert stats["due_for_recall"] == 0
    assert stats["locked"] == 3


def test_stats_counts_due_and_locked_when_mixed(tmp_path):
    """Given locked, due and mastered formulas, when stats, due/locked are right."""
    db = _db(tmp_path)
    lf.add("Physics", "K", "t", "f1", db_path=db)  # locked 35d
    due = lf.add("Maths", "L", "u", "f2", days_to_unlock=0, db_path=db)
    lf.mark_recalled(due["formula_id"], mastered=True, db_path=db)  # mastered
    stats = lf.stats(db_path=db)
    assert stats["total"] == 2
    assert stats["due_for_recall"] == 0  # f2 mastered, f1 locked
    assert stats["locked"] == 1
    assert stats["by_mastery"] == {"new": 1, "mastered": 1}


# ---------------------------------------------------------------------------
# delete_formula
# ---------------------------------------------------------------------------

def test_delete_formula_returns_true_when_row_exists(tmp_path):
    """Given an existing formula, when deleted, True and the row is gone."""
    db = _db(tmp_path)
    formula = lf.add("Physics", "K", "t", "v = u + at", db_path=db)
    assert lf.delete_formula(formula["formula_id"], db_path=db) is True
    assert lf.get(formula["formula_id"], db_path=db) is None


def test_delete_formula_returns_false_when_missing(tmp_path):
    """Given an unknown id, when deleted, then False is returned."""
    db = _db(tmp_path)
    assert lf.delete_formula(9999, db_path=db) is False


# ---------------------------------------------------------------------------
# init_db idempotency
# ---------------------------------------------------------------------------

def test_init_db_idempotent_when_called_twice(tmp_path):
    """Given a fresh db, when init_db runs twice, no error and table exists."""
    db = _db(tmp_path)
    conn = sqlite3.connect(str(db))
    lf.init_db(conn)
    lf.init_db(conn)
    conn.close()
    with sqlite3.connect(str(db)) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    assert "learn_formulas" in tables


def test_connect_creates_table_when_called(tmp_path):
    """Given a fresh db, when _connect runs, the table is created."""
    db = _db(tmp_path)
    lf._connect(db)
    with sqlite3.connect(str(db)) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    assert "learn_formulas" in tables


# ---------------------------------------------------------------------------
# subject normalization + validation
# ---------------------------------------------------------------------------

def test_list_subject_matches_as_given_when_case_differs(tmp_path):
    """Given a stored subject, when listing with different case, no match."""
    db = _db(tmp_path)
    lf.add("Physics", "K", "t", "v = u + at", db_path=db)
    assert lf.list(subject="physics", db_path=db) == []  # no case folding
    assert len(lf.list(subject="Physics", db_path=db)) == 1  # exact match


def test_add_raises_valueerror_when_formula_text_empty(tmp_path):
    """Given an empty formula_text, when added, then ValueError is raised."""
    db = _db(tmp_path)
    with pytest.raises(ValueError):
        lf.add("Physics", "K", "t", "", db_path=db)
    with pytest.raises(ValueError):
        lf.add("Physics", "K", "t", "   ", db_path=db)


def test_add_raises_valueerror_when_subject_empty(tmp_path):
    """Given an empty subject, when added, then ValueError is raised."""
    db = _db(tmp_path)
    with pytest.raises(ValueError):
        lf.add("", "K", "t", "v = u + at", db_path=db)