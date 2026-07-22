"""
Commitment memory + deterministic adherence verification.

A "commitment" ("from now on I'll do PYQs every day") is stored as a normal
op_goals row with period="Daily"/"Weekly" — study_domain.create_goal owns
that, so /goal, plan_facts gap warnings and pause/resume all apply for free.
This module adds what the goal system lacks:

- commitment_checks: one row per (goal, local date) recording whether the
  ledger PROVES the commitment was met that day. Written by the nightly
  verification job and re-written by the morning re-check (INSERT OR
  REPLACE, so post-midnight logging and the 4-min sync lag are absorbed).
- streak / adherence: derived from commitment_checks on demand, never
  stored, so the numbers can't drift from the evidence.
- user_prefs: free-form, non-measurable preferences ("I prefer maths in the
  morning") that are injected into LLM prompts but never "verified".
- capture_conflicts: deterministic warnings raised BEFORE a new commitment
  is saved (duplicate scope, infeasible total daily load).

Verification is deterministic SQL over the ledger mirror — never an LLM
judgment. A goal whose scope can't be mapped onto ledger columns is
"unverifiable": verify_goal_for_date returns met=None, the nightly job skips
it, and the user is told at capture time (plan_facts still watches it at
planning time).
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path
from typing import Any

import session_context
import study_domain
from config import settings

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = PROJECT_ROOT / "sqlite_mirror.db"

CHECKS_TABLE = "commitment_checks"
PREFS_TABLE = "user_prefs"

# Ledger exercise_type values (lowercased) that satisfy each goal scope kind
# from study_domain._goal_scope_kind. Kinds absent here (Coaching Homework,
# Short Notes, ...) have no ledger exercise_type, so such goals are
# unverifiable from the ledger.
_KIND_TO_EXERCISE_TYPES: dict[str, tuple[str, ...]] = {
    "PYQ": ("pyqs", "pyq"),
    "Revision": ("revision",),
}

# Verifiable goal types and the ledger aggregate that measures each.
_GOAL_TYPE_EXPR = {
    "Coverage": "COUNT(*)",
    "Duration": "COALESCE(SUM(actual_time_min), 0)",
    "CY": "COALESCE(SUM(cognitive_yield), 0)",
}

# Feasibility guard for capture_conflicts: total committed Duration minutes
# per day above settings.max_daily_committed_minutes() (default 600) is
# flagged as unrealistic. Kept as a fallback for odd settings failures.
MAX_DAILY_COMMITTED_MINUTES = 600


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    # study_domain._connect ensures the ledger mirror + op_* tables exist,
    # so verification works on a fresh (test) database too.
    conn = study_domain._connect(db_path)
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {CHECKS_TABLE} (
            goal_id TEXT NOT NULL,
            check_date TEXT NOT NULL,
            met INTEGER NOT NULL,
            value REAL,
            target REAL NOT NULL,
            checked_at TEXT NOT NULL,
            PRIMARY KEY (goal_id, check_date)
        )
    """)
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {PREFS_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            category TEXT,
            created_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        )
    """)
    conn.commit()
    return conn


def format_target(target: Any, metric: Any, period: Any) -> str:
    """Human wording for a commitment target: '1 session/day', '3 sessions/week'."""
    value = float(target or 0)
    unit = str(metric or "").strip() or "times"
    if value == 1 and unit.endswith("s") and not unit.endswith("ss"):
        unit = unit[:-1]
    period_text = {"Daily": "/day", "Weekly": "/week"}.get(
        str(period or ""), f" per {str(period or '?').lower()}"
    )
    return f"{value:g} {unit}{period_text}"


# ---------------------------------------------------------------------------
# Deterministic ledger verification
# ---------------------------------------------------------------------------

def ledger_filter_for_goal(goal: dict[str, Any]) -> tuple[str, list[Any]] | None:
    """WHERE fragment (no date clause) + params for ledger rows counting toward a goal.

    Returns None when the goal's scope has no ledger representation — the
    goal is then unverifiable from execution data.
    """
    clauses = ["archived = 0"]
    params: list[Any] = []
    kind = study_domain._goal_scope_kind(goal)
    if kind is not None:
        types = _KIND_TO_EXERCISE_TYPES.get(kind)
        if types is None:
            return None
        placeholders = ", ".join("?" for _ in types)
        clauses.append(f"LOWER(COALESCE(exercise_type,'')) IN ({placeholders})")
        params.extend(types)
    subject = str(goal.get("subject") or "").strip()
    if subject:
        clauses.append("LOWER(COALESCE(subject,'')) = ?")
        params.append(subject.lower())
    return " AND ".join(clauses), params


def verify_goal_for_date(
    goal: dict[str, Any], date: str, *, db_path: str | Path = DEFAULT_DB_PATH
) -> dict[str, Any]:
    """Measure one goal against the ledger for one local date. Pure SQL.

    met is True/False when measurable, None when the goal can't be verified
    from the ledger (unsupported goal_type or unmappable scope).
    """
    target = float(goal.get("target") or 0)
    result: dict[str, Any] = {
        "goal_id": goal.get("notion_page_id"),
        "title": goal.get("title"),
        "date": date[:10],
        "value": None,
        "target": target,
        "met": None,
    }
    expr = _GOAL_TYPE_EXPR.get(str(goal.get("goal_type") or ""))
    if expr is None:
        return result
    filt = ledger_filter_for_goal(goal)
    if filt is None:
        return result
    where, params = filt
    with _connect(db_path) as conn:
        row = conn.execute(
            f"SELECT {expr} AS value FROM ledger "
            f"WHERE {where} AND substr(COALESCE(date,''),1,10) = ?",
            (*params, date[:10]),
        ).fetchone()
    value = float(row["value"] or 0)
    result["value"] = value
    result["met"] = value >= target
    return result


def active_daily_goals(*, db_path: str | Path = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    return study_domain._rows(
        "goals", "archived=0 AND status='Active' AND period='Daily'", db_path=db_path
    )


def run_checks_for_date(
    date: str | None = None, *, db_path: str | Path = DEFAULT_DB_PATH
) -> list[dict[str, Any]]:
    """Verify every active Daily goal against the ledger and record the result.

    INSERT OR REPLACE keyed on (goal_id, check_date) makes this idempotent
    and re-runnable: the morning re-check overwrites the nightly row once
    late logging has synced. Unverifiable goals are returned (met=None) but
    never recorded.
    """
    date = (date or session_context.local_today_iso())[:10]
    now = _utc_now().isoformat()
    results: list[dict[str, Any]] = []
    checks_to_store: list[dict[str, Any]] = []
    for goal in active_daily_goals(db_path=db_path):
        check = verify_goal_for_date(goal, date, db_path=db_path)
        results.append(check)
        if check["met"] is not None and check["goal_id"]:
            checks_to_store.append(check)
    if checks_to_store:
        with _connect(db_path) as conn:
            for check in checks_to_store:
                conn.execute(
                    f"INSERT OR REPLACE INTO {CHECKS_TABLE} "
                    "(goal_id, check_date, met, value, target, checked_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (check["goal_id"], date, int(check["met"]),
                     check["value"], check["target"], now),
                )
            conn.commit()
    return results


def backfill_checks(
    *, days: int = 3, end_date: str | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> None:
    """Re-verify the last N days (idempotent). Heals streaks after downtime:
    without this, a bot offline over a nightly check leaves gap days that
    break streaks the user actually kept."""
    end = dt.date.fromisoformat((end_date or session_context.local_today_iso())[:10])
    for offset in range(days, 0, -1):
        run_checks_for_date((end - dt.timedelta(days=offset)).isoformat(), db_path=db_path)


def verify_weekly_goal(
    goal: dict[str, Any], end_date: str, *, db_path: str | Path = DEFAULT_DB_PATH
) -> dict[str, Any]:
    """Measure a Weekly-period goal over the 7 days ending end_date.

    Same deterministic ledger machinery as daily checks, summed across the
    week. met is None when the goal can't be verified from the ledger.
    """
    end = dt.date.fromisoformat(end_date[:10])
    target = float(goal.get("target") or 0)
    total = 0.0
    for offset in range(7):
        check = verify_goal_for_date(
            goal, (end - dt.timedelta(days=offset)).isoformat(), db_path=db_path
        )
        if check["met"] is None:
            return {"title": goal.get("title"), "value": None, "target": target, "met": None}
        total += check["value"] or 0
    return {"title": goal.get("title"), "value": total, "target": target,
            "met": total >= target}


def streak(
    goal_id: str, *, as_of: str | None = None, db_path: str | Path = DEFAULT_DB_PATH
) -> int:
    """Consecutive met days ending at as_of (default: newest check on file).

    A gap in check dates breaks the streak — continuity is never claimed
    across days that were not verified.
    """
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT check_date, met FROM {CHECKS_TABLE} "
            "WHERE goal_id = ? ORDER BY check_date DESC",
            (goal_id,),
        ).fetchall()
    count = 0
    expected: dt.date | None = None
    for row in rows:
        if as_of and row["check_date"] > as_of[:10]:
            continue
        if not row["met"]:
            break
        try:
            day = dt.date.fromisoformat(row["check_date"])
        except ValueError:
            break
        if expected is not None and day != expected:
            break
        count += 1
        expected = day - dt.timedelta(days=1)
    return count


def adherence(
    goal_id: str, *, days: int = 7, as_of: str | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Met/total over the trailing window (counting only verified days)."""
    as_of = (as_of or session_context.local_today_iso())[:10]
    start = (dt.date.fromisoformat(as_of) - dt.timedelta(days=days - 1)).isoformat()
    with _connect(db_path) as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS total, COALESCE(SUM(met),0) AS met FROM {CHECKS_TABLE} "
            "WHERE goal_id = ? AND check_date BETWEEN ? AND ?",
            (goal_id, start, as_of),
        ).fetchone()
    total, met = int(row["total"]), int(row["met"])
    return {"met": met, "total": total,
            "pct": round(100 * met / total) if total else None}


# ---------------------------------------------------------------------------
# Free-form preferences (non-measurable memory)
# ---------------------------------------------------------------------------

def add_pref(
    chat_id: int, text: str, *, category: str = "preference",
    db_path: str | Path = DEFAULT_DB_PATH,
) -> int:
    text = (text or "").strip()
    if not text:
        raise ValueError("preference text is required")
    with _connect(db_path) as conn:
        cur = conn.execute(
            f"INSERT INTO {PREFS_TABLE} (chat_id, text, category, created_at, active) "
            "VALUES (?, ?, ?, ?, 1)",
            (chat_id, text, category, _utc_now().isoformat()),
        )
        conn.commit()
        return int(cur.lastrowid)


def active_prefs(
    chat_id: int, *, db_path: str | Path = DEFAULT_DB_PATH
) -> list[dict[str, Any]]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT id, text, category, created_at FROM {PREFS_TABLE} "
            "WHERE chat_id = ? AND active = 1 ORDER BY id",
            (chat_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def deactivate_pref(
    chat_id: int, ident: int | str, *, db_path: str | Path = DEFAULT_DB_PATH
) -> dict[str, Any] | None:
    """Deactivate a pref by numeric id or unique text substring. Returns it, or None."""
    with _connect(db_path) as conn:
        if isinstance(ident, int) or str(ident).isdigit():
            rows = conn.execute(
                f"SELECT * FROM {PREFS_TABLE} WHERE chat_id = ? AND active = 1 AND id = ?",
                (chat_id, int(ident)),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT * FROM {PREFS_TABLE} WHERE chat_id = ? AND active = 1 "
                "AND LOWER(text) LIKE ?",
                (chat_id, f"%{str(ident).strip().lower()}%"),
            ).fetchall()
        if len(rows) != 1:
            return None
        conn.execute(
            f"UPDATE {PREFS_TABLE} SET active = 0 WHERE id = ?", (rows[0]["id"],)
        )
        conn.commit()
        return dict(rows[0])


def reactivate_pref(
    chat_id: int, pref_id: int, *, db_path: str | Path = DEFAULT_DB_PATH
) -> bool:
    """Undo a removal: re-enable a pref by id. False when nothing matched."""
    with _connect(db_path) as conn:
        cur = conn.execute(
            f"UPDATE {PREFS_TABLE} SET active = 1 WHERE id = ? AND chat_id = ?",
            (int(pref_id), chat_id),
        )
        conn.commit()
        return cur.rowcount == 1


# ---------------------------------------------------------------------------
# Capture-time conflict detection (warn-and-confirm trigger c)
# ---------------------------------------------------------------------------

def capture_conflicts(
    parsed: dict[str, Any], *, db_path: str | Path = DEFAULT_DB_PATH
) -> list[dict[str, Any]]:
    """Deterministic warnings to show BEFORE saving a new commitment.

    Each conflict is {"message": str, "goal_id": str | None}; goal_id is set
    for duplicate-scope conflicts so the capture UI can offer Replace.
    """
    conflicts: list[dict[str, Any]] = []
    goals = study_domain._rows("goals", "archived=0 AND status='Active'", db_path=db_path)
    new_kind = study_domain._goal_scope_kind(parsed)
    new_subject = str(parsed.get("subject") or "").strip().lower()
    for goal in goals:
        if study_domain._goal_scope_kind(goal) != new_kind:
            continue
        if str(goal.get("subject") or "").strip().lower() != new_subject:
            continue
        if new_kind is None and goal.get("goal_type") != parsed.get("goal_type"):
            continue
        target = goal.get("target")
        target_text = f"{float(target):g}" if target is not None else "?"
        conflicts.append({
            "message": (
                f"you already committed to '{goal.get('title')}' — "
                f"{target_text} {goal.get('metric') or ''} ({goal.get('period')})"
            ).strip(),
            "goal_id": goal.get("notion_page_id"),
        })
    if parsed.get("period") == "Daily":
        daily = [g for g in goals if g.get("period") == "Daily"]
        goal_type = parsed.get("goal_type")
        if goal_type == "CY":
            total = float(parsed.get("target") or 0) + sum(
                float(g.get("target") or 0) for g in daily if g.get("goal_type") == "CY"
            )
            ceiling = study_domain.adaptive_target(db_path=db_path)["ceiling"]
            if total > ceiling:
                conflicts.append({
                    "message": (
                        f"daily CY commitments would total {total:g}, above your "
                        f"proven ceiling {ceiling} — this risks burnout, not progress"
                    ),
                    "goal_id": None,
                })
        elif goal_type == "Duration":
            total = float(parsed.get("target") or 0) + sum(
                float(g.get("target") or 0) for g in daily if g.get("goal_type") == "Duration"
            )
            try:
                limit = settings.max_daily_committed_minutes()
            except Exception:
                limit = MAX_DAILY_COMMITTED_MINUTES
            if total > limit:
                conflicts.append({
                    "message": (
                        f"daily time commitments would total {total:g} min "
                        f"(over {limit / 60:g} h/day) — unrealistic to sustain"
                    ),
                    "goal_id": None,
                })
    return conflicts
