"""Execution discipline — block-level enforcement of the daily JEE timetable.

Foundation module of the execution-discipline system. The bot enforces the
user's daily timetable (JEE 2028 MASTER EXECUTION SYSTEM) at the block level:
each local date resolves to one of two fixed daily templates (Coaching Day /
Non-Coaching Day) and every study block is nudged, escalated and verified.

This module owns the schema, the two seeded templates, and the day/block
lookups the discipline loop runs on:

- execution_templates  — the two fixed day templates
- execution_blocks     — the 20 blocks (10 per template), verbatim from the PDF
- block_confirmations  — per (date, block) state machine (pending/started/
                         skipped/completed); schema only here — writes arrive
                         in later todos
- execution_day_types  — per-date cached day-type resolution, written by
                         day_type_for (coaching iff the portal cache has a
                         class for the date and had a recent successful sync)

Everything in this module is deterministic SQLite — no LLM, no messaging, no
writes to block_confirmations and no touch of op_* / Notion-owned data.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

import coaching_lifecycle
import ntsc_coaching
import session_context
from config import settings

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


def _tz() -> ZoneInfo:
    """User's timezone; falls back to UTC if the configured name is unknown."""
    name = settings.user_timezone()
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("UTC")


def _as_local(value: dt.datetime) -> dt.datetime:
    """Normalise a datetime to an aware datetime in the user's timezone."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=_tz())
    return value.astimezone(_tz())


def _hhmm_to_min(hhmm: str) -> int:
    """HH:MM to minutes since midnight."""
    hour, minute = (int(part) for part in hhmm.split(":"))
    return hour * 60 + minute


def _block_minutes(start_hhmm: str, end_hhmm: str, kind: str) -> int | None:
    """Minutes for study/sleep blocks; None for break/class blocks.

    The rule is (end - start) mod 1440, so a midnight-crossing window wraps
    (22:15-01:00 → 165, 01:00-08:00 → 420). Break/class blocks carry no
    minutes.
    """
    if kind in ("break", "class"):
        return None
    return (_hhmm_to_min(end_hhmm) - _hhmm_to_min(start_hhmm)) % 1440


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


# ---------------------------------------------------------------------------
# Per-date day type + block windows
# ---------------------------------------------------------------------------

def day_type_for(date_iso: str, db_path: str | Path = DEFAULT_DB_PATH) -> str:
    """Resolve a local date to 'coaching' or 'non_coaching', cached once per date.

    A cached execution_day_types row for the date is returned unchanged. On a
    miss the date is 'coaching' iff the coaching cache holds at least one class
    for it AND had a recent successful sync — freshness is judged against the
    ACTUAL current local time (a fixed noon would wrongly mark after-noon syncs
    as stale). The result is persisted (INSERT OR REPLACE) so later ticks reuse
    it. An empty / never-synced coaching cache falls back to 'non_coaching'
    without raising.
    """
    conn = _connect(db_path)
    try:
        row = conn.execute(
            f"SELECT day_type FROM {DAY_TYPES_TABLE} WHERE local_date = ?",
            (date_iso[:10],),
        ).fetchone()
        if row is not None:
            return row["day_type"]
    finally:
        conn.close()

    classes = ntsc_coaching.classes_for_date(date_iso, db_path=db_path)
    day_type = (
        "coaching"
        if classes and coaching_lifecycle.fresh(
            now=session_context.local_now(), db_path=db_path,
        )
        else "non_coaching"
    )

    conn = _connect(db_path)
    try:
        conn.execute(
            f"INSERT OR REPLACE INTO {DAY_TYPES_TABLE} "
            "(local_date, day_type, resolved_at) VALUES (?, ?, ?)",
            (date_iso[:10], day_type, session_context.local_now().isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
    return day_type


def blocks_for_date(date_iso: str, db_path: str | Path = DEFAULT_DB_PATH) -> list[dict]:
    """The day's blocks (from its resolved template) with minute windows, by seq.

    Each block gains ``window_start_min`` / ``window_end_min`` (minutes since
    midnight) and ``crosses_midnight`` (end on the next day). Returns [] when
    the resolved template is unknown.
    """
    day_type = day_type_for(date_iso[:10], db_path=db_path)
    template = get_template(day_type, db_path=db_path)
    if template is None:
        return []
    blocks = blocks_for_template(template["template_key"], db_path=db_path)
    for block in blocks:
        start = _hhmm_to_min(block["start_hhmm"])
        end = _hhmm_to_min(block["end_hhmm"])
        block["window_start_min"] = start
        block["window_end_min"] = end
        block["crosses_midnight"] = end <= start
    return blocks


def current_block(now_aware_local: dt.datetime, db_path: str | Path = DEFAULT_DB_PATH) -> dict | None:
    """The block active at ``now_aware_local`` (aware local time), or None in a gap.

    Considers today's blocks and yesterday's midnight-crossing blocks (a block
    starting 22:15 yesterday covers 00:00–01:00 today). Windows are
    end-inclusive with start-precedence: the block whose start equals another
    block's end wins (22:15 → Execution Block C; 01:00 → Sleep; 08:00 → Sleep).
    Candidates are scanned in seq order and the first containing window wins;
    between-window gaps (08:00–08:30) return None.
    """
    local_now = _as_local(now_aware_local)
    now_min = local_now.hour * 60 + local_now.minute
    today_iso = local_now.date().isoformat()
    yesterday_iso = (local_now.date() - dt.timedelta(days=1)).isoformat()

    def candidates(date_iso: str) -> list[dict]:
        day_type = day_type_for(date_iso, db_path=db_path)
        return [
            {**block, "local_date": date_iso, "day_type": day_type}
            for block in blocks_for_date(date_iso, db_path=db_path)
        ]

    todays = candidates(today_iso)
    yesterdays = candidates(yesterday_iso)

    def contains(block: dict) -> bool:
        start = block["window_start_min"]
        end = block["window_end_min"]
        if block["local_date"] == today_iso:
            if block["crosses_midnight"]:
                return now_min >= start
            return start <= now_min <= end
        if block["crosses_midnight"]:
            return now_min < end
        return False

    for block in todays:
        if block["window_start_min"] == now_min:
            return block
    for block in todays + yesterdays:
        if contains(block):
            return block
    return None


# ---------------------------------------------------------------------------
# Block confirmation state machine
# ---------------------------------------------------------------------------

def _block_template_key(conn: sqlite3.Connection, block_key: str) -> str | None:
    """The block's template_key, or None when block_key is unknown."""
    row = conn.execute(
        f"SELECT template_key FROM {BLOCKS_TABLE} WHERE block_key = ?",
        (block_key,),
    ).fetchone()
    return row["template_key"] if row is not None else None


def get_state(date_iso: str, block_key: str, db_path: str | Path = DEFAULT_DB_PATH) -> dict | None:
    """The confirmation row for (date, block) as a dict, or None when absent.

    No row is not an error, and an unknown block_key also yields None here.
    """
    conn = _connect(db_path)
    try:
        row = conn.execute(
            f"SELECT local_date, block_key, template_key, status, started_at, "
            f"skipped_at, completed_at FROM {CONFIRMATIONS_TABLE} "
            f"WHERE local_date = ? AND block_key = ?",
            (date_iso[:10], block_key),
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def confirm_start(date_iso: str, block_key: str, db_path: str | Path = DEFAULT_DB_PATH) -> dict | None:
    """Transition a block to 'started' (pending → started, started_at=now).

    Inserts a new started row when none exists. From started/skipped/completed
    this is a no-op returning the current row unchanged (started_at is never
    rewritten and a finished block is never resurrected). None for an unknown
    block_key (never raises).
    """
    conn = _connect(db_path)
    try:
        template_key = _block_template_key(conn, block_key)
        if template_key is None:
            return None
        local_date = date_iso[:10]
        row = conn.execute(
            f"SELECT * FROM {CONFIRMATIONS_TABLE} WHERE local_date=? AND block_key=?",
            (local_date, block_key),
        ).fetchone()
        now = session_context.local_now().isoformat()
        if row is None:
            conn.execute(
                f"INSERT INTO {CONFIRMATIONS_TABLE} "
                "(local_date, block_key, template_key, status, started_at) "
                "VALUES (?, ?, ?, 'started', ?)",
                (local_date, block_key, template_key, now),
            )
        elif row["status"] == "pending":
            conn.execute(
                f"UPDATE {CONFIRMATIONS_TABLE} SET status='started', started_at=? "
                f"WHERE local_date=? AND block_key=?",
                (now, local_date, block_key),
            )
        conn.commit()
        return get_state(local_date, block_key, db_path=db_path)
    finally:
        conn.close()


def confirm_skip(date_iso: str, block_key: str, db_path: str | Path = DEFAULT_DB_PATH) -> dict | None:
    """Transition a block to 'skipped' (pending OR started → skipped).

    Auto-skip (todo 4) relies on started → skipped being allowed. Inserts a new
    skipped row when none exists; once already skipped/completed this is a
    no-op returning the current row. None for an unknown block_key.
    """
    conn = _connect(db_path)
    try:
        template_key = _block_template_key(conn, block_key)
        if template_key is None:
            return None
        local_date = date_iso[:10]
        row = conn.execute(
            f"SELECT * FROM {CONFIRMATIONS_TABLE} WHERE local_date=? AND block_key=?",
            (local_date, block_key),
        ).fetchone()
        now = session_context.local_now().isoformat()
        if row is None:
            conn.execute(
                f"INSERT INTO {CONFIRMATIONS_TABLE} "
                "(local_date, block_key, template_key, status, skipped_at) "
                "VALUES (?, ?, ?, 'skipped', ?)",
                (local_date, block_key, template_key, now),
            )
        elif row["status"] in ("pending", "started"):
            conn.execute(
                f"UPDATE {CONFIRMATIONS_TABLE} SET status='skipped', skipped_at=? "
                f"WHERE local_date=? AND block_key=?",
                (now, local_date, block_key),
            )
        conn.commit()
        return get_state(local_date, block_key, db_path=db_path)
    finally:
        conn.close()


def mark_completed(date_iso: str, block_key: str, db_path: str | Path = DEFAULT_DB_PATH) -> dict | None:
    """Mark a block 'completed' (started → completed, completed_at=now).

    ONLY from started: from pending/skipped/completed this is a no-op returning
    the current row, and with no existing row it returns None — a completion is
    never invented. None for an unknown block_key.
    """
    conn = _connect(db_path)
    try:
        template_key = _block_template_key(conn, block_key)
        if template_key is None:
            return None
        local_date = date_iso[:10]
        row = conn.execute(
            f"SELECT * FROM {CONFIRMATIONS_TABLE} WHERE local_date=? AND block_key=?",
            (local_date, block_key),
        ).fetchone()
        if row is not None and row["status"] == "started":
            conn.execute(
                f"UPDATE {CONFIRMATIONS_TABLE} SET status='completed', completed_at=? "
                f"WHERE local_date=? AND block_key=?",
                (session_context.local_now().isoformat(), local_date, block_key),
            )
            conn.commit()
        return get_state(local_date, block_key, db_path=db_path)
    finally:
        conn.close()
