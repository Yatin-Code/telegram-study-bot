"""Progressive capability unlock gates.

The bot gradually enables features as the user demonstrates engagement and
provides data.  Each capability has a deterministic gate that checks whether
the user has met the prerequisites (e.g. first ledger entry, 7-day streak,
onboarding complete).  Gates are read-only queries against existing tables —
they never write and never call the LLM.

Design rules (from AGENTS.md):
  - Code decides WHEN; the LLM decides WHAT to say.
  - No auto-write without Confirm.
  - No data wiping/deleting/archiving.
  - All gates thread ``db_path`` from callers.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import session_context
import study_domain
from config import settings

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "sqlite_mirror.db"

# ---------------------------------------------------------------------------
# Capability registry
# ---------------------------------------------------------------------------

CAPABILITIES: tuple[str, ...] = (
    "agent_chat",        # natural-language agent conversation
    "mock_prep",         # 2-day pre-mock plan proposal
    "chapter_classify",  # chapter mastery/revision/hard tagging
    "weekly_report",     # weekly progress report
    "jee_analytics",     # JEE pattern/ROI commands
    "active_recall",     # /learn active recall unlock
    "discipline",        # execution-block discipline nudges
    "teacher_window",    # teacher escalation window
    "jee_insights",      # personalized missed-opportunity / strength insights
    "doubt_prioritizer", # rank open doubts by JEE weightage
    "trend_awareness",   # year-over-year chapter trend insights
)


# ---------------------------------------------------------------------------
# Connection (same pattern as coaching_progress.py / ntsc_coaching.py)
# ---------------------------------------------------------------------------

def _connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _count(conn: sqlite3.Connection, table: str, where: str = "1=1", params: tuple = ()) -> int:
    if not _table_exists(conn, table):
        return 0
    row = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()
    return int(row[0]) if row else 0


# ---------------------------------------------------------------------------
# Individual gate checks — each returns (unlocked: bool, reason: str)
# ---------------------------------------------------------------------------

def _gate_agent_chat(conn: sqlite3.Connection) -> tuple[bool, str]:
    """Always unlocked — the agent chat is the primary interface."""
    return True, "available"


def _gate_discipline(conn: sqlite3.Connection) -> tuple[bool, str]:
    """Unlocked when execution templates are seeded (init_db has run)."""
    count = _count(conn, "execution_blocks")
    if count == 0:
        return False, "timetable not seeded yet"
    return True, f"{count} blocks configured"


def _gate_mock_prep(conn: sqlite3.Connection) -> tuple[bool, str]:
    """Unlocked when the user has ≥3 ledger entries (enough for a plan)."""
    ledger = _count(conn, "ledger", "archived=0")
    if ledger < 3:
        return False, f"need 3+ study sessions (have {ledger})"
    exams = _count(conn, "op_exams", "archived=0 AND exam_date IS NOT NULL")
    if exams == 0:
        return False, "no upcoming exams recorded"
    return True, f"{ledger} sessions logged"


def _gate_chapter_classify(conn: sqlite3.Connection) -> tuple[bool, str]:
    """Unlocked when a 'Current Syllabus' work item is completed."""
    completed = _count(
        conn, "op_work_items",
        "archived=0 AND kind='Current Syllabus' AND status='Completed'",
    )
    if completed == 0:
        return False, "no completed chapter tracked yet"
    return True, f"{completed} completed chapter(s)"


def _gate_weekly_report(conn: sqlite3.Connection) -> tuple[bool, str]:
    """Unlocked when the user has ≥7 days of ledger history."""
    ledger = _count(conn, "ledger", "archived=0")
    if ledger < 7:
        return False, f"need 7+ sessions for a weekly report (have {ledger})"
    return True, f"{ledger} sessions logged"


def _gate_jee_analytics(conn: sqlite3.Connection) -> tuple[bool, str]:
    """Unlocked when JEE analytics data is loaded."""
    meta = _count(conn, "op_jee_metadata")
    if meta == 0:
        return False, "JEE analytics not loaded yet"
    return True, "JEE analytics available"


def _gate_active_recall(conn: sqlite3.Connection) -> tuple[bool, str]:
    """Unlocked when the user has stored ≥1 formula."""
    formulas = _count(conn, "learn_formulas")
    if formulas == 0:
        return False, "no formulas saved yet — use /learn to add one"
    return True, f"{formulas} formula(s) stored"


def _gate_teacher_window(conn: sqlite3.Connection) -> tuple[bool, str]:
    """Unlocked when the user has ≥2 doubt attempts (escalation threshold)."""
    attempts = _count(conn, "op_doubt_attempts")
    if attempts < 2:
        return False, f"need 2+ doubt attempts for escalation (have {attempts})"
    return True, f"{attempts} doubt attempt(s) recorded"


def _gate_jee_insights(conn: sqlite3.Connection) -> tuple[bool, str]:
    """Unlocked when JEE analytics + ≥5 study sessions exist (enough to compare)."""
    if _count(conn, "op_jee_metadata") == 0:
        return False, "JEE analytics not loaded yet"
    ledger = _count(conn, "ledger", "archived=0")
    if ledger < 5:
        return False, f"need 5+ study sessions for insights (have {ledger})"
    return True, f"insights ready ({ledger} sessions)"


def _gate_doubt_prioritizer(conn: sqlite3.Connection) -> tuple[bool, str]:
    """Unlocked when JEE analytics + ≥1 open doubt exist."""
    if _count(conn, "op_jee_metadata") == 0:
        return False, "JEE analytics not loaded yet"
    open_doubts = _count(conn, "doubts", "archived=0 AND status='open'")
    if open_doubts == 0:
        return False, "no open doubts to prioritize"
    return True, f"{open_doubts} open doubt(s) to rank"


def _gate_trend_awareness(conn: sqlite3.Connection) -> tuple[bool, str]:
    """Unlocked when JEE year-trend data is loaded."""
    if _count(conn, "op_jee_trends") == 0:
        return False, "no JEE trend data loaded yet"
    return True, "trend data available"


_GATE_FUNCS: dict[str, Any] = {
    "agent_chat": _gate_agent_chat,
    "discipline": _gate_discipline,
    "mock_prep": _gate_mock_prep,
    "chapter_classify": _gate_chapter_classify,
    "weekly_report": _gate_weekly_report,
    "jee_analytics": _gate_jee_analytics,
    "active_recall": _gate_active_recall,
    "teacher_window": _gate_teacher_window,
    "jee_insights": _gate_jee_insights,
    "doubt_prioritizer": _gate_doubt_prioritizer,
    "trend_awareness": _gate_trend_awareness,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check(capability: str, *, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    """Check whether a capability is unlocked for the user.

    Returns ``{"capability": str, "unlocked": bool, "reason": str}``.
    Unknown capabilities return ``unlocked=False`` with a descriptive reason.
    """
    func = _GATE_FUNCS.get(capability)
    if func is None:
        return {"capability": capability, "unlocked": False, "reason": "unknown capability"}
    with _connect(db_path) as conn:
        unlocked, reason = func(conn)
    return {"capability": capability, "unlocked": unlocked, "reason": reason}


def check_all(*, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, dict[str, Any]]:
    """Check every capability at once.

    Returns ``{capability: {"unlocked": bool, "reason": str}}``.
    """
    results: dict[str, dict[str, Any]] = {}
    with _connect(db_path) as conn:
        for cap, func in _GATE_FUNCS.items():
            unlocked, reason = func(conn)
            results[cap] = {"unlocked": unlocked, "reason": reason}
    return results


def unlocked(capability: str, *, db_path: str | Path = DEFAULT_DB_PATH) -> bool:
    """Convenience boolean — is this capability unlocked?"""
    return check(capability, db_path=db_path)["unlocked"]


def locked_reason(capability: str, *, db_path: str | Path = DEFAULT_DB_PATH) -> str | None:
    """Return the human-readable reason a capability is locked, or None if unlocked."""
    result = check(capability, db_path=db_path)
    if result["unlocked"]:
        return None
    return result["reason"]


def progress_summary(*, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    """A compact summary for the onboarding hub or /inspect.

    Returns ``{unlocked: [cap, ...], locked: {cap: reason}, total: int, unlocked_count: int}``.
    """
    all_caps = check_all(db_path=db_path)
    unlocked_list = [c for c, v in all_caps.items() if v["unlocked"]]
    locked_map = {c: v["reason"] for c, v in all_caps.items() if not v["unlocked"]}
    return {
        "unlocked": sorted(unlocked_list),
        "locked": dict(sorted(locked_map.items())),
        "total": len(all_caps),
        "unlocked_count": len(unlocked_list),
    }