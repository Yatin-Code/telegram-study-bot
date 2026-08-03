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
import json
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

import coaching_doubts
import coaching_lifecycle
import coaching_planner
import coaching_policy
import coaching_prediction
import coaching_syllabus
import commitments
import ntsc_coaching
import session_context
import study_domain
import llm.router as llm_router
from config import settings
from llm.router import LLMRequest

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

    A cached execution_day_types row for the date is returned while it is
    fresher than coaching_lifecycle.FRESHNESS_MAX_AGE_MINUTES; a stale cached
    row is re-resolved against the current coaching cache. On a
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
            f"SELECT day_type, resolved_at FROM {DAY_TYPES_TABLE} WHERE local_date = ?",
            (date_iso[:10],),
        ).fetchone()
        if row is not None:
            cached_day_type = row["day_type"]
            try:
                resolved_at = dt.datetime.fromisoformat(row["resolved_at"])
                if resolved_at.tzinfo is None:
                    resolved_at = resolved_at.replace(tzinfo=dt.timezone.utc)
                age_minutes = (session_context.local_now() - resolved_at).total_seconds() / 60
                if age_minutes <= coaching_lifecycle.FRESHNESS_MAX_AGE_MINUTES:
                    return cached_day_type
                # stale cache — fall through to re-resolve
            except (ValueError, TypeError):
                return cached_day_type  # unparseable timestamp — trust the cache
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


# ---------------------------------------------------------------------------
# Escalation timing + pending-only auto-skip (pure, no sends)
# ---------------------------------------------------------------------------

ESCALATION_MINUTES = {"start": 0, "push": 10, "shame": 20, "auto_skip": 25}


def _combine_hhmm(date_text: str, hhmm: str) -> dt.datetime:
    """Aware local datetime for an HH:MM wall-clock on a given local date."""
    day = dt.date.fromisoformat(date_text)
    hour, minute = (int(part) for part in hhmm.split(":"))
    return dt.datetime.combine(day, dt.time(hour, minute), tzinfo=_tz())


def _elapsed_minutes(now: dt.datetime, block: dict) -> float:
    """Minutes since the block's actual start (its local_date + start_hhmm).

    A crossing block's local_date IS its start date, so 22:15-01:00 started at
    22:15 on local_date and the tail is handled correctly.
    """
    start = _combine_hhmm(block["local_date"], block["start_hhmm"])
    return (now - start).total_seconds() / 60.0


def _block_end(block: dict) -> dt.datetime:
    """The block's end as an aware local datetime (next day when crossing)."""
    date_text = block["local_date"]
    if block["crosses_midnight"]:
        date_text = (dt.date.fromisoformat(date_text) + dt.timedelta(days=1)).isoformat()
    return _combine_hhmm(date_text, block["end_hhmm"])


def due_escalation_candidates(now: dt.datetime, db_path: str | Path = DEFAULT_DB_PATH) -> list[dict]:
    """Due escalation tiers for the current study block, pending blocks only.

    One candidate per tier whose threshold (ESCALATION_MINUTES) is reached —
    several may be due at once (start+11 → start AND push) because the scan
    claims each event_key independently, so only new tiers actually send.
    Nothing fires for sleep/break/class blocks, for started blocks, or when
    there is no current study block; a missing confirmation row counts as
    pending. Pure candidate generation — nothing is sent here.
    """
    now = _as_local(now)
    block = current_block(now, db_path=db_path)
    if block is None or block["kind"] != "study":
        return []
    state = get_state(block["local_date"], block["block_key"], db_path=db_path)
    if state is not None and state["status"] != "pending":
        return []
    elapsed = _elapsed_minutes(now, block)
    candidates: list[dict] = []
    for tier, threshold in ESCALATION_MINUTES.items():
        if tier == "auto_skip":
            continue
        if elapsed >= threshold:
            candidates.append({
                "tier": tier,
                "kind": f"discipline_{tier}",
                "event_key": (
                    f"discipline:{block['local_date']}:{block['block_key']}:{tier}"
                ),
                "date": block["local_date"],
                "block_key": block["block_key"],
                "title": block["title"],
                "window": f"{block['start_hhmm']}-{block['end_hhmm']}",
                "block": block,
            })
    return candidates


def run_auto_skip(now: dt.datetime, db_path: str | Path = DEFAULT_DB_PATH) -> dict | None:
    """Auto-skip a never-started study block at start+25 or block end (first wins).

    Pending-only (C1 finding): a started block is never auto-skipped and never
    produces push/shame — it is handled by the later check-in instead. Returns
    a small record of the skip, or None when nothing was auto-skipped. This is
    not a message candidate.
    """
    now = _as_local(now)
    block = current_block(now, db_path=db_path)
    if block is None or block["kind"] != "study":
        return None
    state = get_state(block["local_date"], block["block_key"], db_path=db_path)
    if state is not None and state["status"] != "pending":
        return None
    elapsed = _elapsed_minutes(now, block)
    if elapsed < ESCALATION_MINUTES["auto_skip"] and now < _block_end(block):
        return None
    skipped = confirm_skip(block["local_date"], block["block_key"], db_path=db_path)
    if skipped is None or skipped["status"] != "skipped":
        return None
    return {
        "date": block["local_date"],
        "block_key": block["block_key"],
        "title": block["title"],
        "skipped": True,
        "auto": True,
    }


def _utc_iso(value: dt.datetime) -> str:
    """Notion-style UTC ISO string for a datetime (matches ledger created_time)."""
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+00:00")


def has_ledger_evidence(date_iso: str, block: dict, db_path: str | Path = DEFAULT_DB_PATH) -> bool:
    """True iff a non-archived ledger row's session falls inside the block window.

    The window is the block's start..end on its local_date with ±10 min slack.
    ``created_time`` is the authoritative column (a crossing-midnight block's
    session logged at 00:30 carries the next day's timestamp, so a
    ``substr(date,1,10)=?`` filter alone would miss it); the ``date`` column is
    only used as a fallback when ``created_time`` is missing. A missing ledger
    table means no evidence (False).
    """
    local_date = date_iso[:10]
    start = _combine_hhmm(local_date, block["start_hhmm"]) - dt.timedelta(minutes=10)
    end = _block_end(block) + dt.timedelta(minutes=10)
    dates = [local_date]
    if block["crosses_midnight"]:
        dates.append((dt.date.fromisoformat(local_date) + dt.timedelta(days=1)).isoformat())
    placeholders = ",".join("?" for _ in dates)
    conn = _connect(db_path)
    try:
        row = conn.execute(
            f"SELECT 1 FROM ledger WHERE archived = 0 AND ("
            f"(created_time >= ? AND created_time <= ?) OR "
            f"(COALESCE(created_time, '') = '' AND "
            f"substr(COALESCE(date, ''), 1, 10) IN ({placeholders}))) LIMIT 1",
            (_utc_iso(start), _utc_iso(end), *dates),
        ).fetchone()
        return row is not None
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


def evaluate_completion(now: dt.datetime, db_path: str | Path = DEFAULT_DB_PATH) -> list[dict]:
    """Post-block check-in candidates for started blocks (today + yesterday).

    A started block whose window has ended is auto-completed silently when it
    has ledger evidence; otherwise, once it has been over for at least 15
    minutes and the CURRENT block is a study block, a ``discipline_checkin``
    candidate is emitted. The candidate regenerates every scan tick until
    claimed (reminders.claim dedups), so a block that ends into Sleep is
    checked in at the next study block. Started blocks are never auto-skipped —
    they end here (completed via evidence, or check-in).
    """
    now = _as_local(now)
    today_iso = now.date().isoformat()
    yesterday_iso = (now.date() - dt.timedelta(days=1)).isoformat()
    candidates: list[dict] = []
    for date_iso in (today_iso, yesterday_iso):
        day_type = day_type_for(date_iso, db_path=db_path)
        for block in blocks_for_date(date_iso, db_path=db_path):
            block = {**block, "local_date": date_iso, "day_type": day_type}
            state = get_state(date_iso, block["block_key"], db_path=db_path)
            if state is None or state["status"] != "started":
                continue
            end = _block_end(block)
            if now < end:
                continue
            if has_ledger_evidence(date_iso, block, db_path=db_path):
                mark_completed(date_iso, block["block_key"], db_path=db_path)
                continue
            elapsed_after_end = (now - end).total_seconds() / 60.0
            if elapsed_after_end < 15:
                continue
            current = current_block(now, db_path=db_path)
            if current is not None and current["kind"] == "study":
                candidates.append({
                    "kind": "discipline_checkin",
                    "event_key": f"discipline:{date_iso}:{block['block_key']}:checkin",
                    "tier": "checkin",
                    "date": date_iso,
                    "block_key": block["block_key"],
                    "title": block["title"],
                    "window": f"{block['start_hhmm']}-{block['end_hhmm']}",
                    "block": block,
                })
    return candidates


# ---------------------------------------------------------------------------
# LLM coach message (code decides when; LLM writes what) + deterministic fallback
# ---------------------------------------------------------------------------

_FALLBACKS = {
    "start": "⏰ {title} ({start}-{end}) — time to start.",
    "push": "You haven't started {title} yet. Still time — start now.",
    "shame": "You skipped {title}. This is your dream — log it or lose the streak.",
    "checkin": "Did you finish {title}? Log it or tell me what you did.",
}


def _llm_complete(messages: list[dict[str, str]]) -> str:
    """Thin wrapper over the LLM router. Raises on failure (caller catches)."""
    response = llm_router.complete(LLMRequest(
        messages=messages,
        purpose="domain",
        max_output_tokens=256,
        temperature=0.6,
    ))
    return response.text


def _first_active_daily_goal(db_path: str | Path) -> str | None:
    """The first active Daily goal's id, or None when none exists."""
    conn = study_domain._connect(db_path)
    try:
        row = conn.execute(
            "SELECT id FROM op_goals WHERE archived=0 AND status='Active' "
            "AND period='Daily' ORDER BY created_time LIMIT 1"
        ).fetchone()
        return row["id"] if row is not None else None
    finally:
        conn.close()


def build_llm_context(now: dt.datetime, block: dict, db_path: str | Path = DEFAULT_DB_PATH) -> dict:
    """Compact, redacted-friendly context for the coach message.

    Each source is guarded individually so one phase's failure never drops the
    whole context; only keys whose source succeeded are included (no values are
    fabricated). The caller redacts the result before it reaches the LLM.
    """
    now = _as_local(now)
    local_date = block.get("local_date") or block.get("date") or now.date().isoformat()
    context: dict = {
        "block": {
            "title": block["title"],
            "window": f"{block['start_hhmm']}-{block['end_hhmm']}",
            "kind": block["kind"],
            "day_type": block.get("day_type"),
        },
    }

    try:
        classes = ntsc_coaching.classes_for_date(local_date, db_path=db_path)
        if classes:
            context["today_classes"] = [
                {"time": c.get("start_time"), "subjects": c.get("subjects")}
                for c in classes[:5]
            ]
    except Exception:
        pass

    try:
        facts = study_domain.plan_facts(local_date, db_path=db_path)
        items = facts.get("active_items") or []
        if items:
            context["plan_items"] = [
                {"title": it.get("title"), "minutes": it.get("estimated_min")}
                for it in items[:5]
            ]
    except Exception:
        pass

    try:
        level = coaching_policy.backlog_escalation(today=local_date, db_path=db_path).get("level")
        if level:
            context["backlog_level"] = level
    except Exception:
        pass

    try:
        snap = ntsc_coaching.context_snapshot(db_path=db_path)
        tests = snap.get("next_tests") or []
        if tests:
            first = tests[0]
            context["next_test"] = first.get("title")
            test_date = str(first.get("test_date") or "")[:10]
            if test_date:
                context["days_to_test"] = (
                    dt.date.fromisoformat(test_date) - dt.date.fromisoformat(local_date)
                ).days
        latest = snap.get("latest_result")
        if latest:
            context["latest_result"] = {
                "title": latest.get("title"),
                "marks": latest.get("total_marks"),
                "max": latest.get("maximum_marks"),
            }
    except Exception:
        pass

    try:
        tests = coaching_syllabus.coverage_snapshot(today=local_date, limit=3, db_path=db_path)
        uncovered = [
            {"test": test.get("title"), "uncovered": (test.get("coverage") or {}).get("uncovered_count")}
            for test in tests
            if (test.get("coverage") or {}).get("uncovered_count")
        ]
        if uncovered:
            context["uncovered_topics"] = uncovered[:3]
    except Exception:
        pass

    try:
        doubts = coaching_doubts.ranked_doubts(now=now, db_path=db_path)
        if doubts:
            context["next_doubt"] = (
                doubts[0].get("core_concept") or doubts[0].get("title")
            )
    except Exception:
        pass

    try:
        proj = coaching_prediction.project_coaching_score(db_path=db_path)
        if proj.get("total"):
            context["score_projection"] = {
                "range": proj.get("total"),
                "confidence": proj.get("confidence"),
            }
    except Exception:
        pass

    try:
        plan = coaching_planner.plan_tomorrow(db_path=db_path)
        warnings = plan.get("warnings") or []
        unplaced = plan.get("unplaced_count") or 0
        if warnings or unplaced:
            context["planner"] = {"warnings": warnings[:5], "unplaced": unplaced}
    except Exception:
        pass

    try:
        goal = _first_active_daily_goal(db_path=db_path)
        if goal:
            context["streak"] = commitments.streak(goal, db_path=db_path)
    except Exception:
        pass

    return context


def _fallback(tier: str, block: dict) -> str:
    template = _FALLBACKS.get(tier, _FALLBACKS["start"])
    return template.format(
        title=block["title"], start=block["start_hhmm"], end=block["end_hhmm"],
    )


def discipline_message(
    tier: str, block: dict, *, db_path: str | Path = DEFAULT_DB_PATH, now: dt.datetime | None = None,
) -> str:
    """LLM-written message for a tier, or the deterministic fallback on any failure.

    The LLM writes the text from a bounded, redacted, fact-only context; on ANY
    exception (no keys, router error, quota, empty reply) the fixed fallback is
    returned so the scan never blocks. No AIR/rank references anywhere.
    """
    try:
        context = build_llm_context(now or session_context.local_now(), block, db_path=db_path)
        redacted = coaching_policy.redact_payload(context)
        system = (
            "You are the strict-but-caring study coach of a JEE aspirant. "
            f"Write a SHORT (<220 chars) Telegram message for tier {tier} about this block. "
            "Use ONLY these facts; never invent marks, dates, ranks, AIR, or promises."
        )
        if tier == "checkin":
            system += (
                " This block just ended. Ask whether they finished it (log it) or tell you "
                "what they did instead. Never imply the block is upcoming or starting now."
            )
        system += "\n" + json.dumps(redacted, ensure_ascii=False, sort_keys=True)
        messages = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": (
                    f"Block: {block['title']} ({block['start_hhmm']}-{block['end_hhmm']}). "
                    f"Tier: {tier}."
                ),
            },
        ]
        text = _llm_complete(messages).strip()
        if text:
            return text
    except Exception:
        pass
    return _fallback(tier, block)
