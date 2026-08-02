"""Execution discipline (todo 1) — schema, seed and read helpers.

Covers the foundation of the execution-discipline system:
  * fresh tmp db has 0 rows before seed
  * seed_templates creates exactly 2 templates and 20 blocks (10 per template)
  * seed idempotency (INSERT OR IGNORE on PK)
  * kind / minutes correctness (midnight-crossing windows)
  * day_type filter and PK/CHECK constraint enforcement
  * all 4 table names registered in config.ownership.LOCAL_SQL_TABLES
  * block_confirmations / execution_day_types exist with the right columns

Usage:
    .venv-test/bin/python -m pytest -q -p no:cacheprovider -m "not live" test_execution_discipline.py
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import execution_discipline as ed
from config import ownership


@pytest.fixture()
def db(tmp_path):
    """Fresh tmp SQLite file per test (tables created by _connect)."""
    return tmp_path / "discipline.db"


def _count(db_path: Path, table: str) -> int:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _column_names(db_path: Path, table: str) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


# ---------------------------------------------------------------------------
# Fresh database / seeding
# ---------------------------------------------------------------------------

def test_fresh_db_has_zero_rows_before_seed(db):
    ed._connect(db)
    assert _count(db, ed.TEMPLATES_TABLE) == 0
    assert _count(db, ed.BLOCKS_TABLE) == 0
    assert _count(db, ed.CONFIRMATIONS_TABLE) == 0
    assert _count(db, ed.DAY_TYPES_TABLE) == 0


def test_seed_creates_two_templates_and_20_blocks(db):
    ed.seed_templates(db)
    assert _count(db, ed.TEMPLATES_TABLE) == 2
    assert _count(db, ed.BLOCKS_TABLE) == 20
    assert len(ed.blocks_for_template(ed.COACHING_TEMPLATE_KEY, db)) == 10
    assert len(ed.blocks_for_template(ed.NON_COACHING_TEMPLATE_KEY, db)) == 10


def test_seed_is_idempotent(db):
    inserted = ed.seed_templates(db)
    assert inserted == 20
    templates = _count(db, ed.TEMPLATES_TABLE)
    blocks = _count(db, ed.BLOCKS_TABLE)
    re_inserted = ed.seed_templates(db)
    assert re_inserted == 0
    assert _count(db, ed.TEMPLATES_TABLE) == templates == 2
    assert _count(db, ed.BLOCKS_TABLE) == blocks == 20


def test_blocks_ordered_by_seq(db):
    ed.seed_templates(db)
    for template_key in (ed.COACHING_TEMPLATE_KEY, ed.NON_COACHING_TEMPLATE_KEY):
        seqs = [b["seq"] for b in ed.blocks_for_template(template_key, db)]
        assert seqs == list(range(1, 11))


# ---------------------------------------------------------------------------
# Kinds and minutes
# ---------------------------------------------------------------------------

def test_kinds_are_correct(db):
    ed.seed_templates(db)
    all_kinds = set()
    counts = {}
    for template_key in (ed.COACHING_TEMPLATE_KEY, ed.NON_COACHING_TEMPLATE_KEY):
        template_kinds = [b["kind"] for b in ed.blocks_for_template(template_key, db)]
        counts[template_key] = template_kinds.count("study")
        all_kinds.update(template_kinds)
    assert all_kinds == {"sleep", "break", "class", "study"}
    # Verbatim seed: coaching day has 5 study blocks, non-coaching day has 6
    # (the extra Theory Acquisition block in the morning, seq 3).
    assert counts[ed.COACHING_TEMPLATE_KEY] == 5
    assert counts[ed.NON_COACHING_TEMPLATE_KEY] == 6
    coaching_kinds = {
        b["kind"] for b in ed.blocks_for_template(ed.COACHING_TEMPLATE_KEY, db)
    }
    non_kinds = {
        b["kind"] for b in ed.blocks_for_template(ed.NON_COACHING_TEMPLATE_KEY, db)
    }
    assert "class" in coaching_kinds
    assert "class" not in non_kinds


def test_minutes_are_computed_per_rule(db):
    ed.seed_templates(db)
    by_key = {
        b["block_key"]: b
        for b in ed.blocks_for_template(ed.COACHING_TEMPLATE_KEY, db)
        + ed.blocks_for_template(ed.NON_COACHING_TEMPLATE_KEY, db)
    }
    # Midnight-crossing study block: 22:15-01:00 -> 165.
    assert by_key["coach_b10_exec_c"]["minutes"] == 165
    assert by_key["noncoach_b10_spillover"]["minutes"] == 210
    # Sleep: 01:00-08:00 -> 420.
    assert by_key["coach_b01_sleep"]["minutes"] == 420
    assert by_key["noncoach_b01_sleep"]["minutes"] == 420
    # Study blocks that do not cross midnight keep plain (end - start).
    assert by_key["coach_b02_exec_a"]["minutes"] == 90
    # Break/class blocks carry no minutes.
    assert by_key["coach_b06_lunch"]["minutes"] is None
    assert by_key["coach_b07_classes"]["minutes"] is None


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------

def test_day_type_filter_returns_right_template(db):
    ed.seed_templates(db)
    coaching = ed.get_template("coaching", db)
    assert coaching is not None
    assert coaching["template_key"] == ed.COACHING_TEMPLATE_KEY
    assert coaching["day_type"] == "coaching"
    non = ed.get_template("non_coaching", db)
    assert non is not None
    assert non["template_key"] == ed.NON_COACHING_TEMPLATE_KEY
    assert non["day_type"] == "non_coaching"
    assert ed.get_template("holiday", db) is None


def test_blocks_for_template_unknown_returns_empty(db):
    ed.seed_templates(db)
    assert ed.blocks_for_template("tpl_nope", db) == []


# ---------------------------------------------------------------------------
# Constraint enforcement
# ---------------------------------------------------------------------------

def test_duplicate_block_key_raises_integrity_error(db):
    ed.seed_templates(db)
    conn = ed._connect(db)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"INSERT INTO {ed.BLOCKS_TABLE} "
                "(block_key, template_key, seq, start_hhmm, end_hhmm, kind, title) "
                "VALUES ('coach_b01_sleep', ?, 99, '00:00', '00:30', 'study', 'dup')",
                (ed.COACHING_TEMPLATE_KEY,),
            )
    finally:
        conn.close()


def test_wrong_kind_violates_check_constraint(db):
    ed.seed_templates(db)
    conn = ed._connect(db)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"INSERT INTO {ed.BLOCKS_TABLE} "
                "(block_key, template_key, seq, start_hhmm, end_hhmm, kind, title) "
                "VALUES ('bad_kind_block', ?, 99, '00:00', '00:30', 'fun', 'nope')",
                (ed.COACHING_TEMPLATE_KEY,),
            )
    finally:
        conn.close()


def test_wrong_day_type_violates_check_constraint(db):
    conn = ed._connect(db)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"INSERT INTO {ed.TEMPLATES_TABLE} "
                "(template_key, name, day_type) VALUES ('tpl_bad', 'Bad', 'holiday')"
            )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Ownership registration + full schema
# ---------------------------------------------------------------------------

def test_all_four_table_names_in_local_sql_tables():
    for name in ed.LOCAL_TABLES:
        assert name in ownership.LOCAL_SQL_TABLES
    assert len(ed.LOCAL_TABLES) == 4


def test_confirmations_and_day_types_exist_with_correct_columns(db):
    ed._connect(db)
    assert _column_names(db, ed.CONFIRMATIONS_TABLE) == {
        "local_date", "block_key", "template_key", "status",
        "started_at", "skipped_at", "completed_at",
    }
    assert _column_names(db, ed.DAY_TYPES_TABLE) == {
        "local_date", "day_type", "resolved_at",
    }


def test_templates_and_blocks_exist_with_correct_columns(db):
    ed._connect(db)
    assert _column_names(db, ed.TEMPLATES_TABLE) == {
        "template_key", "name", "day_type",
    }
    assert _column_names(db, ed.BLOCKS_TABLE) == {
        "block_key", "template_key", "seq", "start_hhmm", "end_hhmm",
        "kind", "title", "minutes",
    }
