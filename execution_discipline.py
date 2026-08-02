"""Execution discipline — block-level enforcement of the daily JEE timetable.

Foundation module of the execution-discipline system. The bot enforces the
user's daily timetable (JEE 2028 MASTER EXECUTION SYSTEM) at the block level:
each local date resolves to one of two fixed daily templates (Coaching Day /
Non-Coaching Day) and every study block is nudged, escalated and verified.

This module owns the schema and the two seeded templates only:

- execution_templates  — the two fixed day templates
- execution_blocks     — the 20 blocks (10 per template), verbatim from the PDF
- block_confirmations  — per (date, block) state machine (pending/started/
                         skipped/completed); schema only here — writes arrive
                         in later todos
- execution_day_types  — per-date cached day-type resolution; schema only here

These four tables are LOCAL-only (owned by SQLite, never synced to Notion).
Everything in this module is deterministic SQLite — no LLM, no messaging, no
writes to the block_confirmations/execution_day_types tables and no touch of
op_* / Notion-owned data.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = PROJECT_ROOT / "sqlite_mirror.db"

TEMPLATES_TABLE = "execution_templates"
BLOCKS_TABLE = "execution_blocks"
CONFIRMATIONS_TABLE = "block_confirmations"
DAY_TYPES_TABLE = "execution_day_types"

# The four local tables registered in config.ownership.LOCAL_SQL_TABLES.
LOCAL_TABLES = (TEMPLATES_TABLE, BLOCKS_TABLE, CONFIRMATIONS_TABLE, DAY_TYPES_TABLE)

COACHING_TEMPLATE_KEY = "tpl_coaching"
NON_COACHING_TEMPLATE_KEY = "tpl_non_coaching"

KINDS = ("sleep", "break", "class", "study")
DAY_TYPES = ("coaching", "non_coaching")


def init_db(conn: sqlite3.Connection) -> None:
    """Create the four local execution-discipline tables if they don't exist."""
    conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS {TEMPLATES_TABLE} (
            template_key TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            day_type TEXT NOT NULL CHECK(day_type IN ('coaching','non_coaching'))
        );
        CREATE TABLE IF NOT EXISTS {BLOCKS_TABLE} (
            block_key TEXT PRIMARY KEY,
            template_key TEXT NOT NULL REFERENCES {TEMPLATES_TABLE}(template_key),
            seq INTEGER NOT NULL,
            start_hhmm TEXT NOT NULL,
            end_hhmm TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('sleep','break','class','study')),
            title TEXT NOT NULL,
            minutes INTEGER
        );
        CREATE TABLE IF NOT EXISTS {CONFIRMATIONS_TABLE} (
            local_date TEXT NOT NULL,
            block_key TEXT NOT NULL,
            template_key TEXT,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','started','skipped','completed')),
            started_at TEXT,
            skipped_at TEXT,
            completed_at TEXT,
            PRIMARY KEY (local_date, block_key)
        );
        CREATE TABLE IF NOT EXISTS {DAY_TYPES_TABLE} (
            local_date TEXT PRIMARY KEY,
            day_type TEXT NOT NULL CHECK(day_type IN ('coaching','non_coaching')),
            resolved_at TEXT NOT NULL
        );
    """)
    conn.commit()


def _connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    init_db(conn)
    return conn


def _block_minutes(start_hhmm: str, end_hhmm: str, kind: str) -> int | None:
    """Minutes for study/sleep blocks; None for break/class blocks.

    The rule is (end - start) mod 1440, so a midnight-crossing window wraps
    (22:15-01:00 → 165, 01:00-08:00 → 420). Break/class blocks carry no
    minutes.
    """
    if kind in ("break", "class"):
        return None
    sh, sm = (int(part) for part in start_hhmm.split(":"))
    eh, em = (int(part) for part in end_hhmm.split(":"))
    return (eh * 60 + em - sh * 60 - sm) % 1440


# (template_key, name, day_type)
_TEMPLATE_ROWS = (
    (COACHING_TEMPLATE_KEY, "Coaching Day", "coaching"),
    (NON_COACHING_TEMPLATE_KEY, "Non-Coaching Day", "non_coaching"),
)

# (block_key, template_key, seq, start_hhmm, end_hhmm, kind, title) — verbatim
# from the JEE 2028 MASTER EXECUTION SYSTEM PDF. minutes is derived at seed
# time by _block_minutes so the rule can't drift from the data.
_BLOCK_ROWS = (
    # Coaching Day.
    ("coach_b01_sleep", COACHING_TEMPLATE_KEY, 1, "01:00", "08:00", "sleep", "Sleep"),
    ("coach_b02_exec_a", COACHING_TEMPLATE_KEY, 2, "08:30", "10:00", "study", "Execution Block A"),
    ("coach_b03_break", COACHING_TEMPLATE_KEY, 3, "10:00", "10:30", "break", "Break"),
    ("coach_b04_exec_b", COACHING_TEMPLATE_KEY, 4, "10:30", "12:00", "study", "Execution Block B"),
    ("coach_b05_acquisition", COACHING_TEMPLATE_KEY, 5, "12:00", "14:00", "study", "Acquisition Block"),
    ("coach_b06_lunch", COACHING_TEMPLATE_KEY, 6, "14:00", "15:00", "break", "Lunch & Commute Prep"),
    ("coach_b07_classes", COACHING_TEMPLATE_KEY, 7, "15:00", "20:45", "class", "Narayana Classes & Transit"),
    ("coach_b08_dinner", COACHING_TEMPLATE_KEY, 8, "21:00", "21:30", "break", "Dinner"),
    ("coach_b09_review", COACHING_TEMPLATE_KEY, 9, "21:30", "22:15", "study", "Active Review (Extract Formulas)"),
    ("coach_b10_exec_c", COACHING_TEMPLATE_KEY, 10, "22:15", "01:00", "study", "Execution Block C (Level 1 HW)"),
    # Non-Coaching Day.
    ("noncoach_b01_sleep", NON_COACHING_TEMPLATE_KEY, 1, "01:00", "08:00", "sleep", "Sleep"),
    ("noncoach_b02_revision", NON_COACHING_TEMPLATE_KEY, 2, "08:30", "10:30", "study", "Revision Block (Notion Backlog)"),
    ("noncoach_b03_theory", NON_COACHING_TEMPLATE_KEY, 3, "10:30", "11:30", "study", "Theory Acquisition (Revision Failures)"),
    ("noncoach_b04_lunch", NON_COACHING_TEMPLATE_KEY, 4, "11:30", "12:30", "break", "Break & Lunch"),
    ("noncoach_b05_exec1", NON_COACHING_TEMPLATE_KEY, 5, "12:30", "15:30", "study", "Execution Block 1 (Level 2/3)"),
    ("noncoach_b06_break", NON_COACHING_TEMPLATE_KEY, 6, "15:30", "16:30", "break", "Break"),
    ("noncoach_b07_exec2", NON_COACHING_TEMPLATE_KEY, 7, "16:30", "18:30", "study", "Execution Block 2 (Chemistry/Weakest)"),
    ("noncoach_b08_dinner", NON_COACHING_TEMPLATE_KEY, 8, "18:30", "19:30", "break", "Dinner & Break"),
    ("noncoach_b09_exec3", NON_COACHING_TEMPLATE_KEY, 9, "19:30", "21:30", "study", "Execution Block 3 (Overflow HW)"),
    ("noncoach_b10_spillover", NON_COACHING_TEMPLATE_KEY, 10, "21:30", "01:00", "study", "Spillover/Advanced"),
)


def seed_templates(db_path: str | Path = DEFAULT_DB_PATH) -> int:
    """Seed the two daily templates and their 20 blocks.

    Idempotent: INSERT OR IGNORE on the primary keys, so re-running never
    duplicates rows. Returns the number of block rows newly inserted (20 on a
    fresh database, 0 on a re-run).
    """
    conn = _connect(db_path)
    try:
        conn.executemany(
            f"INSERT OR IGNORE INTO {TEMPLATES_TABLE} "
            "(template_key, name, day_type) VALUES (?, ?, ?)",
            _TEMPLATE_ROWS,
        )
        cursor = conn.executemany(
            f"INSERT OR IGNORE INTO {BLOCKS_TABLE} "
            "(block_key, template_key, seq, start_hhmm, end_hhmm, kind, title, minutes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (key, tpl, seq, start, end, kind, title, _block_minutes(start, end, kind))
                for key, tpl, seq, start, end, kind, title in _BLOCK_ROWS
            ],
        )
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def get_template(day_type: str, db_path: str | Path = DEFAULT_DB_PATH) -> dict | None:
    """Return the template dict for a day_type, or None when unknown."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            f"SELECT template_key, name, day_type FROM {TEMPLATES_TABLE} WHERE day_type = ?",
            (day_type,),
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def blocks_for_template(template_key: str, db_path: str | Path = DEFAULT_DB_PATH) -> list[dict]:
    """Return the template's blocks as dicts ordered by seq (empty if unknown)."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT block_key, template_key, seq, start_hhmm, end_hhmm, kind, title, minutes "
            f"FROM {BLOCKS_TABLE} WHERE template_key = ? ORDER BY seq",
            (template_key,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()
