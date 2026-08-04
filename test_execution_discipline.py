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

import datetime as dt
import sqlite3
from pathlib import Path

import pytest

import execution_discipline as ed
import ntsc_coaching
import session_context
import sync
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
        "stopped_at", "duration_min", "completion_source",
    }
    assert _column_names(db, ed.DAY_TYPES_TABLE) == {
        "local_date", "day_type", "resolved_at",
    }


def test_init_db_migrates_old_confirmations_table_additively(tmp_path):
    """A pre-stop-metadata table gains the three columns without a rebuild,
    and its existing rows survive untouched."""
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            f"CREATE TABLE {ed.CONFIRMATIONS_TABLE} ("
            "local_date TEXT NOT NULL, block_key TEXT NOT NULL, template_key TEXT, "
            "status TEXT NOT NULL DEFAULT 'pending' "
            "CHECK(status IN ('pending','started','skipped','completed')), "
            "started_at TEXT, skipped_at TEXT, completed_at TEXT, "
            "PRIMARY KEY (local_date, block_key))"
        )
        conn.execute(
            f"INSERT INTO {ed.CONFIRMATIONS_TABLE} "
            "(local_date, block_key, template_key, status) VALUES (?, ?, ?, ?)",
            ("2026-08-05", "coach_b02_exec_a", ed.COACHING_TEMPLATE_KEY, "pending"),
        )
        conn.commit()
    finally:
        conn.close()
    ed._connect(db)
    cols = _column_names(db, ed.CONFIRMATIONS_TABLE)
    assert {"stopped_at", "duration_min", "completion_source"} <= cols
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            f"SELECT status, stopped_at, duration_min, completion_source "
            f"FROM {ed.CONFIRMATIONS_TABLE} "
            "WHERE local_date='2026-08-05' AND block_key='coach_b02_exec_a'"
        ).fetchone()
    assert row[0] == "pending"
    assert row[1] is None and row[2] is None and row[3] is None


def test_templates_and_blocks_exist_with_correct_columns(db):
    ed._connect(db)
    assert _column_names(db, ed.TEMPLATES_TABLE) == {
        "template_key", "name", "day_type",
    }
    assert _column_names(db, ed.BLOCKS_TABLE) == {
        "block_key", "template_key", "seq", "start_hhmm", "end_hhmm",
        "kind", "title", "minutes",
    }


# ---------------------------------------------------------------------------
# day_type_for — per-date cached resolution
# ---------------------------------------------------------------------------

def _seed_coaching_day(db, date_iso, *, with_classes=True, synced_at=None):
    conn = ntsc_coaching._connect(db)
    try:
        if with_classes:
            conn.execute(
                "INSERT OR REPLACE INTO coaching_classes "
                "(source_id, class_date, start_time, duration_min, class_type) "
                "VALUES (?, ?, ?, ?, ?)",
                (f"c-{date_iso}", date_iso, "15:00", 60, "class"),
            )
        if synced_at is not None:
            conn.execute(
                "INSERT INTO coaching_sync_runs (started_at, finished_at, status, datasets) "
                "VALUES (?, ?, 'success', '[\"classes\"]')",
                (synced_at.isoformat(), synced_at.isoformat()),
            )
        conn.commit()
    finally:
        conn.close()


def _set_day_type(db, date_iso, day_type):
    with sqlite3.connect(db) as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO {ed.DAY_TYPES_TABLE} "
            "(local_date, day_type, resolved_at) VALUES (?, ?, ?)",
            (date_iso, day_type, session_context.local_now().isoformat()),
        )
        conn.commit()


def test_day_type_coaching_when_classes_and_fresh_after_noon(db):
    ed.seed_templates(db)
    _seed_coaching_day(
        db, "2026-08-05", synced_at=dt.datetime.now(dt.timezone.utc),
    )
    assert ed.day_type_for("2026-08-05", db) == "coaching"
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            f"SELECT day_type, resolved_at FROM {ed.DAY_TYPES_TABLE} WHERE local_date = '2026-08-05'"
        ).fetchone()
    assert row is not None
    assert row[0] == "coaching"
    assert row[1]


def test_day_type_non_coaching_when_no_classes(db):
    ed.seed_templates(db)
    _seed_coaching_day(
        db, "2026-08-05", with_classes=False,
        synced_at=dt.datetime.now(dt.timezone.utc),
    )
    assert ed.day_type_for("2026-08-05", db) == "non_coaching"


def test_day_type_non_coaching_when_only_doubt_classes(db):
    ed.seed_templates(db)
    conn = ntsc_coaching._connect(db)
    try:
        conn.execute(
            "INSERT INTO coaching_classes "
            "(source_id, class_date, start_time, duration_min, class_type) "
            "VALUES (?, ?, ?, ?, ?)",
            ("doubt-only", "2026-08-05", "15:00", 60, "Doubt Class"),
        )
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO coaching_sync_runs (started_at, finished_at, status, datasets) "
            "VALUES (?, ?, 'success', '[\"classes\"]')",
            (now, now),
        )
        conn.commit()
    finally:
        conn.close()
    assert ed.day_type_for("2026-08-05", db) == "non_coaching"


def test_day_type_coaching_when_regular_class_and_doubt_class(db):
    ed.seed_templates(db)
    conn = ntsc_coaching._connect(db)
    try:
        conn.executemany(
            "INSERT INTO coaching_classes "
            "(source_id, class_date, start_time, duration_min, class_type) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                ("regular", "2026-08-05", "09:00", 60, "Regular Class"),
                ("doubt", "2026-08-05", "15:00", 60, "Doubt Class"),
            ],
        )
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO coaching_sync_runs (started_at, finished_at, status, datasets) "
            "VALUES (?, ?, 'success', '[\"classes\"]')",
            (now, now),
        )
        conn.commit()
    finally:
        conn.close()
    assert ed.day_type_for("2026-08-05", db) == "coaching"


def test_day_type_non_coaching_when_sync_failed(db):
    ed.seed_templates(db)
    conn = ntsc_coaching._connect(db)
    try:
        conn.execute(
            "INSERT INTO coaching_sync_runs (started_at, finished_at, status) "
            "VALUES (?, ?, 'failed')",
            (dt.datetime.now(dt.timezone.utc).isoformat(),
             dt.datetime.now(dt.timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
    assert ed.day_type_for("2026-08-05", db) == "non_coaching"


def test_day_type_non_coaching_when_cache_never_synced(db):
    ed.seed_templates(db)
    _seed_coaching_day(db, "2026-08-05", with_classes=True, synced_at=None)
    assert ed.day_type_for("2026-08-05", db) == "non_coaching"


def test_day_type_non_coaching_when_entirely_empty(db):
    ed.seed_templates(db)
    assert ed.day_type_for("2026-08-05", db) == "non_coaching"


def test_day_type_is_cached(db):
    ed.seed_templates(db)
    _seed_coaching_day(db, "2026-08-05", synced_at=dt.datetime.now(dt.timezone.utc))
    assert ed.day_type_for("2026-08-05", db) == "coaching"
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM coaching_classes")
        conn.execute("DELETE FROM coaching_sync_runs")
        conn.commit()
    assert ed.day_type_for("2026-08-05", db) == "coaching"


# ---------------------------------------------------------------------------
# blocks_for_date — window fields
# ---------------------------------------------------------------------------

def test_blocks_for_date_returns_10_blocks_with_windows(db):
    ed.seed_templates(db)
    _set_day_type(db, "2026-08-05", "coaching")
    blocks = ed.blocks_for_date("2026-08-05", db)
    assert [b["seq"] for b in blocks] == list(range(1, 11))
    first = blocks[0]
    assert first["start_hhmm"] == "01:00"
    assert first["window_start_min"] == 60
    assert first["window_end_min"] == 480
    assert first["crosses_midnight"] is False
    last = blocks[-1]
    assert last["start_hhmm"] == "22:15"
    assert last["window_start_min"] == 1335
    assert last["window_end_min"] == 60
    assert last["crosses_midnight"] is True


def test_blocks_for_date_non_coaching_crosses_midnight(db):
    ed.seed_templates(db)
    _set_day_type(db, "2026-08-06", "non_coaching")
    blocks = ed.blocks_for_date("2026-08-06", db)
    assert len(blocks) == 10
    assert blocks[-1]["title"] == "Spillover/Advanced"
    assert blocks[-1]["crosses_midnight"] is True


# ---------------------------------------------------------------------------
# current_block — boundary semantics
# ---------------------------------------------------------------------------

_TZ = session_context.local_now().tzinfo


def _at(hour, minute):
    return dt.datetime(2026, 8, 2, hour, minute, tzinfo=_TZ)


def _coaching_window(db):
    ed.seed_templates(db)
    _set_day_type(db, "2026-08-02", "coaching")
    _set_day_type(db, "2026-08-01", "coaching")


def test_current_block_00_30_uses_yesterdays_crossing_block(db):
    _coaching_window(db)
    block = ed.current_block(_at(0, 30), db)
    assert block is not None
    assert block["local_date"] == "2026-08-01"
    assert block["day_type"] == "coaching"
    assert block["crosses_midnight"] is True
    assert block["title"] == "Execution Block C (Level 1 HW)"


def test_current_block_01_00_is_sleep_start_precedence(db):
    _coaching_window(db)
    block = ed.current_block(_at(1, 0), db)
    assert block is not None
    assert block["local_date"] == "2026-08-02"
    assert block["title"] == "Sleep"


def test_current_block_08_00_is_sleep_end_inclusive(db):
    _coaching_window(db)
    block = ed.current_block(_at(8, 0), db)
    assert block is not None
    assert block["title"] == "Sleep"
    assert block["window_end_min"] == 480


def test_current_block_08_15_gap_is_none(db):
    _coaching_window(db)
    assert ed.current_block(_at(8, 15), db) is None


def test_current_block_08_45_is_execution_block_a(db):
    _coaching_window(db)
    block = ed.current_block(_at(8, 45), db)
    assert block is not None
    assert block["title"] == "Execution Block A"
    assert block["day_type"] == "coaching"


def test_current_block_22_15_is_execution_block_c_start_precedence(db):
    _coaching_window(db)
    block = ed.current_block(_at(22, 15), db)
    assert block is not None
    assert block["title"] == "Execution Block C (Level 1 HW)"
    assert block["local_date"] == "2026-08-02"
    assert block["seq"] == 10
    assert block["start_hhmm"] == "22:15"
    assert block["end_hhmm"] == "01:00"
    assert block["minutes"] == 165


def test_current_block_non_coaching_day(db):
    ed.seed_templates(db)
    _set_day_type(db, "2026-08-02", "non_coaching")
    _set_day_type(db, "2026-08-01", "non_coaching")
    block = ed.current_block(_at(8, 45), db)
    assert block is not None
    assert block["title"] == "Revision Block (Notion Backlog)"
    assert block["day_type"] == "non_coaching"


# ---------------------------------------------------------------------------
# Block confirmation state machine
# ---------------------------------------------------------------------------

def test_get_state_none_when_no_row(db):
    ed.seed_templates(db)
    assert ed.get_state("2026-08-05", "coach_b02_exec_a", db) is None


def test_confirm_start_pending_to_started_once(db):
    ed.seed_templates(db)
    row = ed.confirm_start("2026-08-05", "coach_b02_exec_a", db)
    assert row["status"] == "started"
    assert row["started_at"]
    assert row["template_key"] == ed.COACHING_TEMPLATE_KEY
    first_started_at = row["started_at"]
    again = ed.confirm_start("2026-08-05", "coach_b02_exec_a", db)
    assert again["status"] == "started"
    assert again["started_at"] == first_started_at


def test_confirm_skip_from_pending_and_from_started(db):
    ed.seed_templates(db)
    pending_skip = ed.confirm_skip("2026-08-05", "coach_b02_exec_a", db)
    assert pending_skip["status"] == "skipped"
    assert pending_skip["skipped_at"]
    started = ed.confirm_start("2026-08-05", "coach_b03_break", db)
    assert started["status"] == "started"
    started_skip = ed.confirm_skip("2026-08-05", "coach_b03_break", db)
    assert started_skip["status"] == "skipped"
    assert started_skip["skipped_at"]


def test_confirm_skip_after_completed_is_noop(db):
    ed.seed_templates(db)
    ed.confirm_start("2026-08-05", "coach_b02_exec_a", db)
    ed.mark_completed("2026-08-05", "coach_b02_exec_a", db)
    after = ed.confirm_skip("2026-08-05", "coach_b02_exec_a", db)
    assert after["status"] == "completed"
    assert after["skipped_at"] is None


def test_mark_completed_only_from_started(db):
    ed.seed_templates(db)
    conn = ed._connect(db)
    try:
        conn.execute(
            f"INSERT INTO {ed.CONFIRMATIONS_TABLE} "
            "(local_date, block_key, template_key, status) "
            "VALUES ('2026-08-05', 'coach_b02_exec_a', ?, 'pending')",
            (ed.COACHING_TEMPLATE_KEY,),
        )
        conn.execute(
            f"INSERT INTO {ed.CONFIRMATIONS_TABLE} "
            "(local_date, block_key, template_key, status) "
            "VALUES ('2026-08-05', 'coach_b03_break', ?, 'skipped')",
            (ed.COACHING_TEMPLATE_KEY,),
        )
        conn.commit()
    finally:
        conn.close()
    pending = ed.mark_completed("2026-08-05", "coach_b02_exec_a", db)
    assert pending["status"] == "pending"
    skipped = ed.mark_completed("2026-08-05", "coach_b03_break", db)
    assert skipped["status"] == "skipped"
    ed.confirm_start("2026-08-05", "coach_b04_exec_b", db)
    done = ed.mark_completed("2026-08-05", "coach_b04_exec_b", db)
    assert done["status"] == "completed"
    assert done["completed_at"]


def test_mark_completed_no_row_returns_none(db):
    ed.seed_templates(db)
    assert ed.mark_completed("2026-08-05", "coach_b02_exec_a", db) is None


def test_confirmations_pk_unique(db):
    ed.seed_templates(db)
    ed.confirm_start("2026-08-05", "coach_b02_exec_a", db)
    conn = ed._connect(db)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"INSERT INTO {ed.CONFIRMATIONS_TABLE} "
                "(local_date, block_key, template_key, status) "
                "VALUES ('2026-08-05', 'coach_b02_exec_a', 'x', 'pending')"
            )
    finally:
        conn.close()


def test_unknown_block_key_is_noop(db):
    ed.seed_templates(db)
    assert ed.get_state("2099-01-01", "nope", db) is None
    assert ed.confirm_start("2099-01-01", "nope", db) is None
    assert ed.confirm_skip("2099-01-01", "nope", db) is None
    assert ed.mark_completed("2099-01-01", "nope", db) is None


def test_full_lifecycle_leaves_one_completed_row(db):
    ed.seed_templates(db)
    ed.confirm_start("2026-08-05", "coach_b02_exec_a", db)
    ed.confirm_start("2026-08-05", "coach_b02_exec_a", db)
    ed.mark_completed("2026-08-05", "coach_b02_exec_a", db)
    ed.confirm_skip("2026-08-05", "coach_b02_exec_a", db)
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT * FROM {ed.CONFIRMATIONS_TABLE} "
            f"WHERE local_date='2026-08-05' AND block_key='coach_b02_exec_a'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "completed"
    assert rows[0]["completed_at"]


# ---------------------------------------------------------------------------
# Stopwatch: elapsed_minutes + confirm_stop + stop metadata
# ---------------------------------------------------------------------------

def _pin_started_at(db, date_iso, block_key, started_at):
    with sqlite3.connect(db) as conn:
        conn.execute(
            f"UPDATE {ed.CONFIRMATIONS_TABLE} SET started_at=? "
            "WHERE local_date=? AND block_key=?",
            (started_at, date_iso, block_key),
        )
        conn.commit()


def test_elapsed_minutes_from_persisted_started_at(db):
    ed.seed_templates(db)
    ed.confirm_start("2026-08-05", "coach_b02_exec_a", db)
    _pin_started_at(db, "2026-08-05", "coach_b02_exec_a", "2026-08-05T08:30:00+00:00")
    now = dt.datetime(2026, 8, 5, 9, 5, tzinfo=dt.timezone.utc)
    assert ed.elapsed_minutes("2026-08-05", "coach_b02_exec_a", now=now, db_path=db) == 35


def test_elapsed_minutes_rounds_and_never_negative(db):
    ed.seed_templates(db)
    ed.confirm_start("2026-08-05", "coach_b02_exec_a", db)
    _pin_started_at(db, "2026-08-05", "coach_b02_exec_a", "2026-08-05T08:30:00+00:00")
    # A clock before started_at must clamp to 0, never go negative.
    early = dt.datetime(2026, 8, 5, 8, 15, tzinfo=dt.timezone.utc)
    assert ed.elapsed_minutes("2026-08-05", "coach_b02_exec_a", now=early, db_path=db) == 0
    # 40s after start rounds up to 1 minute.
    just_after = dt.datetime(2026, 8, 5, 8, 30, 40, tzinfo=dt.timezone.utc)
    assert ed.elapsed_minutes("2026-08-05", "coach_b02_exec_a", now=just_after, db_path=db) == 1
    # 10m20s rounds down to 10.
    ten = dt.datetime(2026, 8, 5, 8, 40, 20, tzinfo=dt.timezone.utc)
    assert ed.elapsed_minutes("2026-08-05", "coach_b02_exec_a", now=ten, db_path=db) == 10


def test_elapsed_minutes_none_when_not_started(db):
    ed.seed_templates(db)
    assert ed.elapsed_minutes("2026-08-05", "coach_b02_exec_a", db_path=db) is None
    ed.confirm_skip("2026-08-05", "coach_b02_exec_a", db)
    assert ed.elapsed_minutes("2026-08-05", "coach_b02_exec_a", db_path=db) is None


def test_confirm_stop_started_to_completed_with_metadata(db):
    ed.seed_templates(db)
    ed.confirm_start("2026-08-05", "coach_b02_exec_a", db)
    _pin_started_at(db, "2026-08-05", "coach_b02_exec_a", "2026-08-05T08:30:00+00:00")
    now = dt.datetime(2026, 8, 5, 9, 5, tzinfo=dt.timezone.utc)
    stopped = ed.confirm_stop("2026-08-05", "coach_b02_exec_a", now=now, db_path=db)
    assert stopped["status"] == "completed"
    assert dt.datetime.fromisoformat(stopped["stopped_at"]) == now
    assert stopped["duration_min"] == 35
    assert stopped["completion_source"] == "stop"


def test_confirm_stop_repeated_is_idempotent(db):
    ed.seed_templates(db)
    ed.confirm_start("2026-08-05", "coach_b02_exec_a", db)
    _pin_started_at(db, "2026-08-05", "coach_b02_exec_a", "2026-08-05T08:30:00+00:00")
    first = ed.confirm_stop(
        "2026-08-05", "coach_b02_exec_a",
        now=dt.datetime(2026, 8, 5, 9, 5, tzinfo=dt.timezone.utc), db_path=db,
    )
    again = ed.confirm_stop(
        "2026-08-05", "coach_b02_exec_a",
        now=dt.datetime(2026, 8, 5, 9, 10, tzinfo=dt.timezone.utc), db_path=db,
    )
    assert again["status"] == "completed"
    assert again["duration_min"] == first["duration_min"] == 35
    assert again["stopped_at"] == first["stopped_at"]


def test_confirm_stop_pending_and_skipped_do_not_transition(db):
    ed.seed_templates(db)
    conn = ed._connect(db)
    try:
        conn.execute(
            f"INSERT INTO {ed.CONFIRMATIONS_TABLE} "
            "(local_date, block_key, template_key, status) VALUES (?, ?, ?, 'pending')",
            ("2026-08-05", "coach_b02_exec_a", ed.COACHING_TEMPLATE_KEY),
        )
        conn.execute(
            f"INSERT INTO {ed.CONFIRMATIONS_TABLE} "
            "(local_date, block_key, template_key, status) VALUES (?, ?, ?, 'skipped')",
            ("2026-08-05", "coach_b03_break", ed.COACHING_TEMPLATE_KEY),
        )
        conn.commit()
    finally:
        conn.close()
    pending = ed.confirm_stop("2026-08-05", "coach_b02_exec_a", db_path=db)
    assert pending["status"] == "pending"
    assert pending["stopped_at"] is None and pending["duration_min"] is None
    skipped = ed.confirm_stop("2026-08-05", "coach_b03_break", db_path=db)
    assert skipped["status"] == "skipped"
    assert skipped["stopped_at"] is None


def test_confirm_stop_no_row_and_unknown_block_none(db):
    ed.seed_templates(db)
    # Known block, no confirmation row: a stop never invents a completion.
    assert ed.confirm_stop("2026-08-05", "coach_b02_exec_a", db_path=db) is None
    assert ed.confirm_stop("2099-01-01", "nope", db_path=db) is None


def test_mark_completed_sets_ledger_source(db):
    ed.seed_templates(db)
    ed.confirm_start("2026-08-05", "coach_b02_exec_a", db)
    done = ed.mark_completed("2026-08-05", "coach_b02_exec_a", db)
    assert done["status"] == "completed"
    assert done["completion_source"] == "ledger"
    assert done["stopped_at"] is None and done["duration_min"] is None


def test_mark_completed_preserves_stop_metadata(db):
    ed.seed_templates(db)
    ed.confirm_start("2026-08-05", "coach_b02_exec_a", db)
    _pin_started_at(db, "2026-08-05", "coach_b02_exec_a", "2026-08-05T08:30:00+00:00")
    stopped = ed.confirm_stop(
        "2026-08-05", "coach_b02_exec_a",
        now=dt.datetime(2026, 8, 5, 9, 5, tzinfo=dt.timezone.utc), db_path=db,
    )
    assert stopped["completion_source"] == "stop"
    after = ed.mark_completed("2026-08-05", "coach_b02_exec_a", db)
    assert after["status"] == "completed"
    assert after["completion_source"] == "stop"
    assert after["duration_min"] == 35


def test_get_block_returns_seeded_block_with_local_date(db):
    ed.seed_templates(db)
    _set_day_type(db, "2026-08-05", "coaching")
    block = ed.get_block("2026-08-05", "coach_b02_exec_a", db)
    assert block is not None
    assert block["title"] == "Execution Block A"
    assert block["local_date"] == "2026-08-05"


def test_get_block_uses_confirmation_template_without_day_type(db):
    ed.seed_templates(db)
    ed.confirm_start("2026-08-05", "coach_b02_exec_a", db)
    block = ed.get_block("2026-08-05", "coach_b02_exec_a", db)
    assert block is not None
    assert block["title"] == "Execution Block A"


def test_get_block_unknown_returns_none(db):
    ed.seed_templates(db)
    assert ed.get_block("2099-01-01", "nope", db) is None


# ---------------------------------------------------------------------------
# Escalation candidates + pending-only auto-skip
# ---------------------------------------------------------------------------

def test_at_start_plus_1_only_start_candidate(db):
    _coaching_window(db)
    candidates = ed.due_escalation_candidates(_at(8, 31), db)
    assert [c["tier"] for c in candidates] == ["start"]
    assert candidates[0]["kind"] == "discipline_start"


def test_at_start_plus_11_start_and_push(db):
    _coaching_window(db)
    candidates = ed.due_escalation_candidates(_at(8, 41), db)
    assert {c["tier"] for c in candidates} == {"start", "push"}


def test_at_start_plus_21_all_tiers(db):
    _coaching_window(db)
    candidates = ed.due_escalation_candidates(_at(8, 51), db)
    assert {c["tier"] for c in candidates} == {"start", "push", "shame"}


def test_no_shame_before_start_plus_20(db):
    _coaching_window(db)
    tiers = {c["tier"] for c in ed.due_escalation_candidates(_at(8, 49), db)}
    assert tiers == {"start", "push"}
    assert "shame" not in tiers


def test_event_key_format(db):
    _coaching_window(db)
    by_tier = {c["tier"]: c for c in ed.due_escalation_candidates(_at(8, 41), db)}
    assert by_tier["start"]["event_key"] == "discipline:2026-08-02:coach_b02_exec_a:start"
    assert by_tier["push"]["event_key"] == "discipline:2026-08-02:coach_b02_exec_a:push"
    assert by_tier["start"]["date"] == "2026-08-02"
    assert by_tier["start"]["window"] == "08:30-10:00"
    assert by_tier["start"]["block"]["seq"] == 2


def test_auto_skip_at_start_plus_26(db):
    _coaching_window(db)
    record = ed.run_auto_skip(_at(8, 56), db)
    assert record == {
        "date": "2026-08-02", "block_key": "coach_b02_exec_a",
        "title": "Execution Block A", "skipped": True, "auto": True,
    }
    assert ed.get_state("2026-08-02", "coach_b02_exec_a", db)["status"] == "skipped"
    assert ed.due_escalation_candidates(_at(8, 56), db) == []


def test_auto_skip_crossing_block(db):
    _coaching_window(db)
    record = ed.run_auto_skip(_at(23, 0), db)
    assert record is not None
    assert record["block_key"] == "coach_b10_exec_c"
    assert ed.get_state("2026-08-02", "coach_b10_exec_c", db)["status"] == "skipped"


def test_auto_skip_never_touches_started(db):
    _coaching_window(db)
    ed.confirm_start("2026-08-02", "coach_b02_exec_a", db)
    assert ed.run_auto_skip(_at(9, 0), db) is None
    assert ed.get_state("2026-08-02", "coach_b02_exec_a", db)["status"] == "started"


def test_started_block_has_no_candidates(db):
    _coaching_window(db)
    ed.confirm_start("2026-08-02", "coach_b02_exec_a", db)
    assert ed.due_escalation_candidates(_at(8, 41), db) == []


def test_no_candidates_for_sleep_break_class(db):
    _coaching_window(db)
    assert ed.due_escalation_candidates(_at(2, 0), db) == []
    assert ed.due_escalation_candidates(_at(10, 15), db) == []
    assert ed.due_escalation_candidates(_at(16, 0), db) == []
    assert ed.run_auto_skip(_at(2, 0), db) is None
    assert ed.run_auto_skip(_at(16, 0), db) is None


# ---------------------------------------------------------------------------
# LLM coach message + deterministic fallback
# ---------------------------------------------------------------------------

def test_discipline_message_returns_mocked_text(db, monkeypatch):
    _coaching_window(db)
    block = ed.current_block(_at(8, 45), db)
    monkeypatch.setattr(ed, "_llm_complete", lambda messages: "custom text")
    assert ed.discipline_message("start", block, db_path=db) == "custom text"


def test_discipline_message_fallback_exact_per_tier(db, monkeypatch):
    _coaching_window(db)
    block = ed.current_block(_at(8, 45), db)

    def boom(messages):
        raise RuntimeError("quota")

    monkeypatch.setattr(ed, "_llm_complete", boom)
    assert ed.discipline_message("start", block, db_path=db) == (
        "⏰ Execution Block A (08:30-10:00) — time to start."
    )
    assert ed.discipline_message("push", block, db_path=db) == (
        "You haven't started Execution Block A yet. Still time — start now."
    )
    assert ed.discipline_message("shame", block, db_path=db) == (
        "You skipped Execution Block A. This is your dream — log it or lose the streak."
    )
    assert ed.discipline_message("checkin", block, db_path=db) == (
        "Did you finish Execution Block A? Log it or tell me what you did."
    )


def test_discipline_message_fallback_has_title_no_air(db, monkeypatch):
    _coaching_window(db)
    block = ed.current_block(_at(8, 45), db)

    def boom(messages):
        raise RuntimeError("x")

    monkeypatch.setattr(ed, "_llm_complete", boom)
    for tier in ("start", "push", "shame", "checkin"):
        text = ed.discipline_message(tier, block, db_path=db)
        assert "Execution Block A" in text
        assert "AIR" not in text
        assert len(text) < 220


def test_discipline_message_redacts_context(db, monkeypatch):
    _coaching_window(db)
    conn = ntsc_coaching._connect(db)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO coaching_classes "
            "(source_id, class_date, start_time, duration_min, class_type, subjects) "
            "VALUES ('c-phone', '2026-08-02', '15:00', 60, 'class', 'Physics — call 9876543210')"
        )
        conn.commit()
    finally:
        conn.close()
    block = ed.current_block(_at(8, 45), db)
    captured = {}

    def fake(messages):
        captured["system"] = messages[0]["content"]
        return "custom text"

    monkeypatch.setattr(ed, "_llm_complete", fake)
    ed.discipline_message("start", block, db_path=db)
    assert "[REDACTED]" in captured["system"]


def test_discipline_message_never_raises(db, monkeypatch):
    _coaching_window(db)
    block = ed.current_block(_at(8, 45), db)

    def boom(messages):
        raise RuntimeError("boom")

    monkeypatch.setattr(ed, "_llm_complete", boom)
    text = ed.discipline_message("shame", block, db_path=db)
    assert isinstance(text, str)
    assert text


def test_discipline_message_checkin_prompt_says_ended_exact(db, monkeypatch):
    """C2: the checkin system prompt explicitly says the block just ended.

    Baseline characterization flipped by the fix: before C2 the prompt was
    generic across tiers (never said \"ended\"), which let the real LLM write
    \"Execution Block B (10:30-12:00) starts now\" for a checkin.
    """
    _coaching_window(db)
    block = ed.current_block(_at(8, 45), db)
    captured = {}

    def fake(messages):
        captured["system"] = messages[0]["content"]
        return "custom text"

    monkeypatch.setattr(ed, "_llm_complete", fake)
    ed.discipline_message("checkin", block, db_path=db)
    assert "strict-but-caring study coach" in captured["system"]
    assert "This block just ended." in captured["system"]
    assert "Ask whether they finished it" in captured["system"]


def test_discipline_message_checkin_prompt_says_ended(db, monkeypatch):
    """C2: checkin prompt says the block just ended and asks if they finished.

    start/push/shame prompts keep the generic wording and never mention the
    block having ended.
    """
    _coaching_window(db)
    block = ed.current_block(_at(8, 45), db)
    prompts = {}

    def fake(messages):
        prompts["current"] = messages[0]["content"]
        return "custom text"

    monkeypatch.setattr(ed, "_llm_complete", fake)
    for tier in ("start", "push", "shame", "checkin"):
        ed.discipline_message(tier, block, db_path=db)
        prompts[tier] = prompts.pop("current")
    checkin = prompts["checkin"]
    assert "ended" in checkin
    assert "finish" in checkin
    assert "starts now" not in checkin
    for tier in ("start", "push", "shame"):
        assert "just ended" not in prompts[tier]


# ---------------------------------------------------------------------------
# Post-block check-in (ledger evidence + regenerating checkin candidate)
# ---------------------------------------------------------------------------

def _seed_ledger(db, created_time_utc, date):
    with sync.connect(db) as conn:
        sync.init_db(conn)
        conn.execute(
            "INSERT INTO ledger (notion_page_id, created_time, date, last_synced_at, raw_json, archived) "
            "VALUES (?, ?, ?, ?, '{}', 0)",
            (f"ledger-{created_time_utc}", created_time_utc, date, "2026-07-20T00:00:00+00:00"),
        )
        conn.commit()


def _exec_block(db, date_iso, block_key):
    for block in ed.blocks_for_date(date_iso, db):
        if block["block_key"] == block_key:
            return {**block, "local_date": date_iso}
    return None


def _utc(hour, minute, day=2, month=8, year=2026):
    return dt.datetime(year, month, day, hour, minute, tzinfo=_TZ).astimezone(
        dt.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%S.000+00:00")


def test_started_block_with_ledger_evidence_auto_completes(db):
    _coaching_window(db)
    ed.confirm_start("2026-08-02", "coach_b02_exec_a", db)
    _seed_ledger(db, _utc(9, 0), "2026-08-02")
    assert ed.evaluate_completion(_at(10, 30), db) == []
    assert ed.get_state("2026-08-02", "coach_b02_exec_a", db)["status"] == "completed"


def test_started_block_no_ledger_emits_checkin(db):
    _coaching_window(db)
    ed.confirm_start("2026-08-02", "coach_b09_review", db)
    candidates = ed.evaluate_completion(_at(22, 35), db)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["tier"] == "checkin"
    assert candidate["kind"] == "discipline_checkin"
    assert candidate["block_key"] == "coach_b09_review"
    assert candidate["event_key"] == "discipline:2026-08-02:coach_b09_review:checkin"
    assert ed.get_state("2026-08-02", "coach_b09_review", db)["status"] == "started"


def test_checkin_not_emitted_before_window_ends(db):
    _coaching_window(db)
    ed.confirm_start("2026-08-02", "coach_b02_exec_a", db)
    assert ed.evaluate_completion(_at(9, 30), db) == []


def test_checkin_not_emitted_during_sleep_then_regenerates(db):
    _coaching_window(db)
    ed.confirm_start("2026-08-02", "coach_b10_exec_c", db)
    sleep_now = dt.datetime(2026, 8, 3, 1, 20, tzinfo=_TZ)
    assert ed.evaluate_completion(sleep_now, db) == []
    study_now = dt.datetime(2026, 8, 3, 8, 45, tzinfo=_TZ)
    candidates = ed.evaluate_completion(study_now, db)
    assert len(candidates) == 1
    assert candidates[0]["block_key"] == "coach_b10_exec_c"


def test_checkin_skipped_block_none(db):
    _coaching_window(db)
    ed.confirm_skip("2026-08-02", "coach_b02_exec_a", db)
    assert ed.evaluate_completion(_at(10, 30), db) == []


def test_has_ledger_evidence_crossing_via_created_time(db):
    _coaching_window(db)
    block = _exec_block(db, "2026-08-02", "coach_b10_exec_c")
    _seed_ledger(db, _utc(0, 30, day=3), "2026-08-03")
    assert ed.has_ledger_evidence("2026-08-02", block, db) is True


def test_has_ledger_evidence_ignores_date_outside_window(db):
    _coaching_window(db)
    block = _exec_block(db, "2026-08-02", "coach_b10_exec_c")
    _seed_ledger(db, _utc(5, 0, day=3), "2026-08-03")
    assert ed.has_ledger_evidence("2026-08-02", block, db) is False


def test_has_ledger_evidence_no_ledger_table_false(db):
    _coaching_window(db)
    block = _exec_block(db, "2026-08-02", "coach_b02_exec_a")
    assert ed.has_ledger_evidence("2026-08-02", block, db) is False


# ---------------------------------------------------------------------------
# JEE chapter-ROI context in build_llm_context (todo 8)
# ---------------------------------------------------------------------------

def _seed_jee_stats(db_path, chapter="Electrostatics", total=107, repeat=0.972):
    import jee_data_loader
    with sqlite3.connect(db_path) as conn:
        jee_data_loader.init_db(conn)
        conn.execute(
            "INSERT OR REPLACE INTO op_jee_chapter_stats "
            "(subject, chapter, exam_type, total_questions, repeating_questions,"
            " unique_questions, repeat_ratio, easy_ratio, medium_ratio, hard_ratio,"
            " importance_score, by_year_json, by_difficulty_json,"
            " by_question_type_json, sub_topics_json, needs_figure) "
            "VALUES ('Physics', ?, 'mains', ?, 0, 0, ?, 0, 0, 0, 0,"
            " '{}','{}','{}','{}',0)",
            (chapter, total, repeat),
        )
        conn.commit()


def _block_for_context(db):
    return {
        "local_date": "2026-08-02",
        "date": "2026-08-02",
        "block_key": "coach_b02_exec_a",
        "title": "Execution Block A",
        "start_hhmm": "10:30",
        "end_hhmm": "12:00",
        "kind": "study",
        "day_type": "coaching",
    }


def test_build_llm_context_includes_jee_evidence_on_chapter_match(db, monkeypatch):
    _seed_jee_stats(db)
    monkeypatch.setattr(
        ed.study_domain, "plan_facts",
        lambda date, db_path=None: {"active_items": [{"title": "Electrostatics", "estimated_min": 90}]},
    )
    ctx = ed.build_llm_context(_at(10, 45), _block_for_context(db), db_path=db)
    assert "jee_evidence" in ctx
    assert "Electrostatics" in ctx["jee_evidence"]
    assert "107" in ctx["jee_evidence"]


def test_build_llm_context_omits_jee_evidence_without_match(db, monkeypatch):
    _seed_jee_stats(db)
    monkeypatch.setattr(
        ed.study_domain, "plan_facts",
        lambda date, db_path=None: {"active_items": [{"title": "Not A Real Chapter", "estimated_min": 30}]},
    )
    ctx = ed.build_llm_context(_at(10, 45), _block_for_context(db), db_path=db)
    assert "jee_evidence" not in ctx


def test_build_llm_context_omits_jee_evidence_when_tables_empty(db, monkeypatch):
    monkeypatch.setattr(
        ed.study_domain, "plan_facts",
        lambda date, db_path=None: {"active_items": [{"title": "Electrostatics", "estimated_min": 90}]},
    )
    ctx = ed.build_llm_context(_at(10, 45), _block_for_context(db), db_path=db)
    assert "jee_evidence" not in ctx
