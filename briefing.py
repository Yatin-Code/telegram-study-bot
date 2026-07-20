"""
Start-of-block briefing: when the user sets a study context ("starting EB-1
physics mle"), pull live numbers from the SQLite mirror and brief them —
target pace, last comparable session, unresolved doubts, revision status.

Read-only and best-effort: any failure degrades to whatever lines were built
so a briefing problem can never block setting context.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path
from typing import Any, Optional

import formulas
import session_context

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = PROJECT_ROOT / "sqlite_mirror.db"


def _connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _header(ctx: dict[str, Any]) -> str:
    bits = [b for b in (ctx.get("block"), ctx.get("subject"), ctx.get("exercise")) if b]
    label = " ".join(str(b) for b in bits) or "Session"
    if ctx.get("chapter"):
        label += f" ({ctx['chapter']})"
    started = ""
    raw = ctx.get("session_started_at")
    if raw:
        try:
            started = " — started " + dt.datetime.fromisoformat(raw).strftime("%H:%M")
        except ValueError:
            pass
    return f"⏱ *{label}*{started}"


def _pace_line(ctx: dict[str, Any]) -> Optional[str]:
    exercise = ctx.get("exercise")
    if not exercise:
        return None
    t_q = formulas.t_q_for(ctx.get("subject"), exercise)
    return f"🎯 Target pace: {t_q:g} min/q"


def _last_session_line(conn: sqlite3.Connection, ctx: dict[str, Any]) -> Optional[str]:
    subject = ctx.get("subject")
    if not subject:
        return None
    exercise = ctx.get("exercise")
    where = "archived = 0 AND LOWER(subject) = LOWER(?) AND date IS NOT NULL"
    params: list[Any] = [subject]
    if exercise:
        where += " AND exercise_type = ?"
        params.append(exercise)
    row = conn.execute(
        f"SELECT date, cognitive_yield, accuracy_ratio, actual_time_min "
        f"FROM ledger WHERE {where} ORDER BY date DESC LIMIT 1",
        params,
    ).fetchone()
    if row is None:
        return None
    label = f"Last {exercise}" if exercise else f"Last {subject}"
    bits = []
    if row["cognitive_yield"] is not None:
        bits.append(f"yield {row['cognitive_yield']}")
    else:
        bits.append("incomplete data")
    if row["accuracy_ratio"] is not None:
        bits.append(f"accuracy {round(row['accuracy_ratio'] * 100)}%")
    if row["actual_time_min"] is not None:
        bits.append(f"{row['actual_time_min']:g} min")
    return f"📈 {label}: " + " · ".join(bits) + f" ({str(row['date'])[:10]})"


def _doubts_line(conn: sqlite3.Connection, ctx: dict[str, Any]) -> Optional[str]:
    chapter = ctx.get("chapter")
    subject = ctx.get("subject")
    base = "archived = 0 AND LOWER(COALESCE(status,'')) = 'unresolved'"
    if chapter:
        # chapter is a rollup mirrored as text (sometimes a JSON list) — LIKE
        # matches both '["Kinematics 2D"]' and plain 'Kinematics 2D'.
        where, param, scope = (
            base + " AND LOWER(COALESCE(chapter,'')) LIKE ?",
            f"%{chapter.lower()}%",
            "in this chapter",
        )
    elif subject:
        where, param, scope = (
            base + " AND LOWER(COALESCE(subject,'')) LIKE ?",
            f"%{subject.lower()}%",
            f"in {subject}",
        )
    else:
        return None
    n = conn.execute(f"SELECT COUNT(*) AS n FROM doubts WHERE {where}", (param,)).fetchone()["n"]
    if not n:
        return None
    return f"❓ {n} unresolved doubt{'s' if n != 1 else ''} {scope}"


def _revision_line(conn: sqlite3.Connection, ctx: dict[str, Any]) -> Optional[str]:
    chapter = ctx.get("chapter")
    if not chapter:
        return None
    row = conn.execute(
        "SELECT chapter_module, status, mastery, next_execution_date FROM revision "
        "WHERE archived = 0 AND LOWER(chapter_module) LIKE ? LIMIT 1",
        (f"%{chapter.lower()}%",),
    ).fetchone()
    if row is None:
        return None
    bits = []
    if row["mastery"]:
        bits.append(f"mastery {row['mastery']}")
    elif row["status"]:
        bits.append(str(row["status"]))
    due = row["next_execution_date"]
    if due:
        due = str(due)[:10]
        overdue = " (overdue)" if due < session_context.local_today_iso() else ""
        bits.append(f"next due {due}{overdue}")
    if not bits:
        return None
    return "🔁 Revision: " + " · ".join(bits)


def build_briefing(ctx: dict[str, Any], db_path: str | Path = DEFAULT_DB_PATH) -> str:
    """Render the start-of-block briefing for an active context."""
    lines = [_header(ctx)]
    try:
        pace = _pace_line(ctx)
        if pace:
            lines.append(pace)
        with _connect(db_path) as conn:
            for fn in (_last_session_line, _doubts_line, _revision_line):
                try:
                    line = fn(conn, ctx)
                except sqlite3.Error:
                    continue
                if line:
                    lines.append(line)
    except Exception:
        pass
    lines.append("_Timer running — when you finish, just send your results._")
    return "\n".join(lines)
