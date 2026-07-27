"""
Broadened warn-and-confirm advisor.

Thin aggregation over engines that already exist — commitments (deterministic
ledger verification), study_domain.adaptive_target / plan_facts — rendering
warnings for the four triggers the user chose:

(a) log_warnings      — a ledger log late in the day ignores a still-unmet
                        daily commitment (surfaced inside the existing
                        Confirm/Edit/Cancel log preview).
(b) plan-under-serves — already live via plan_facts/planner; today_command
                        adds streak context from here.
(c) conflicting new memory — commitments.capture_conflicts, at capture time.
(d) trajectory_warnings — week-over-week accuracy drop, low adherence,
                        overdue revision / unplanned backlog.

morning_nudge renders the daily adherence report for the nudge job. Every
number here comes from deterministic SQL; nothing is an LLM judgment, and no
warning ever blocks an action — the user always confirms.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import commitments
import session_context
import study_domain
import message_templates
from config import settings

DEFAULT_DB_PATH = commitments.DEFAULT_DB_PATH

# Trajectory thresholds. Drop/adherence cutoffs are tunable via /settings
# (settings.accuracy_drop_pts / low_adherence_pct); evidence minimums stay
# fixed so thin data can never fake a trend.
MIN_ATTEMPTED_PER_WINDOW = 20   # per 7-day window, else no accuracy claim
MIN_ADHERENCE_CHECKS = 4        # verified days needed before adherence is judged


def _parse_hhmm(raw: str, fallback: tuple[int, int]) -> dt.time:
    try:
        hour, minute = (int(part) for part in raw.split(":", 1))
        return dt.time(hour, minute)
    except Exception:
        return dt.time(*fallback)


# ---------------------------------------------------------------------------
# Trigger (a): log entry ignores a still-unmet daily commitment
# ---------------------------------------------------------------------------

def _entry_serves_goal(props: dict[str, Any], goal: dict[str, Any]) -> bool:
    kind = study_domain._goal_scope_kind(goal)
    if kind is not None:
        types = commitments._KIND_TO_EXERCISE_TYPES.get(kind)
        if types is None:
            return False
        if str(props.get("exercise_type") or "").strip().lower() not in types:
            return False
    subject = str(goal.get("subject") or "").strip().lower()
    if subject and str(props.get("subject") or "").strip().lower() != subject:
        return False
    return True


def log_warnings(
    props: dict[str, Any], *, now: dt.datetime | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> list[str]:
    """Warnings for a ledger entry about to be logged (props = Notion field dict)."""
    now = now or session_context.local_now()
    threshold = _parse_hhmm(settings.commitment_warn_after(), (19, 0))
    if now.time() < threshold:
        return []
    today = now.date().isoformat()
    warnings: list[str] = []
    for goal in commitments.active_daily_goals(db_path=db_path):
        check = commitments.verify_goal_for_date(goal, today, db_path=db_path)
        if check["met"] is not False:
            continue
        if _entry_serves_goal(props, goal):
            continue
        warnings.append(
            f"daily commitment '{goal.get('title')}' still unmet today "
            f"({check['value']:g}/{check['target']:g})"
        )
    return warnings


# ---------------------------------------------------------------------------
# Morning nudge (adherence report for one date, normally yesterday)
# ---------------------------------------------------------------------------

def morning_nudge(
    date: str | None = None, *, chat_id: int | str | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> str | None:
    """Render the adherence report for `date`. None when nothing is trackable."""
    if date is None:
        date = (
            dt.date.fromisoformat(session_context.local_today_iso())
            - dt.timedelta(days=1)
        ).isoformat()
    date = date[:10]
    checks = commitments.run_checks_for_date(date, db_path=db_path)
    verifiable = [c for c in checks if c["met"] is not None]
    if not verifiable:
        return None
    prev_day = (dt.date.fromisoformat(date) - dt.timedelta(days=1)).isoformat()
    commitment_lines: list[str] = []
    evidence_lines: list[str] = []
    missed_titles: list[str] = []
    for check in verifiable:
        if check["met"]:
            days = commitments.streak(check["goal_id"], as_of=date, db_path=db_path)
            commitment_lines.append(
                f"✅ {check['title']} — kept · {check['value']:g}/{check['target']:g} · "
                f"{days}-day streak"
            )
        else:
            missed_titles.append(str(check["title"]))
            prior = commitments.streak(check["goal_id"], as_of=prev_day, db_path=db_path)
            line = f"❌ {check['title']} — missed · {check['value']:g}/{check['target']:g} logged"
            if prior:
                line += f" · {prior}-day streak ended"
            commitment_lines.append(line)
    for check in verifiable:
        if check["met"]:
            continue
        stats = commitments.adherence(check["goal_id"], as_of=date, db_path=db_path)
        if stats["total"] >= 2:
            evidence_lines.append(
                f"{check['title']} — {stats['met']}/{stats['total']} verified days kept "
                f"({stats['pct']}%)"
            )
    missed = len(missed_titles)
    conclusion = (
        f"{missed} commitment{'s were' if missed != 1 else ' was'} missed yesterday."
        if missed else "Every verifiable commitment was kept yesterday."
    )
    action = (
        f"Recover {missed_titles[0]} today and log the evidence before day end."
        if missed_titles else "Repeat the same targets today."
    )
    card = message_templates.action_card(
        "🔴" if missed else "🟢", "Morning accountability", context=date,
        conclusion=conclusion,
        sections=(("Commitments", commitment_lines), ("Recent evidence", evidence_lines)),
        action=action,
        footer=f"Evidence date: {date} · only synced ledger records count.",
    )
    if chat_id is not None:
        try:
            import learner_profile
            profile = learner_profile.latest(chat_id, db_path=db_path) or learner_profile.refresh(
                chat_id, as_of=date, db_path=db_path
            )
            hints = [
                str(item.get("reason") or "").strip()
                for item in (profile.get("coaching_focus") or [])[:2]
                if str(item.get("reason") or "").strip()
            ]
            card = message_templates.insert_section(card, "Learner profile", hints)
        except Exception:
            pass
    return card


# ---------------------------------------------------------------------------
# Trigger (d): trajectory drift
# ---------------------------------------------------------------------------

def _accuracy_between(
    start: str, end: str, *, db_path: str | Path
) -> tuple[float | None, int]:
    with commitments._connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(questions_attempted),0) attempted,
                   COALESCE(SUM(questions_correct),0) correct
            FROM ledger
            WHERE archived=0 AND substr(COALESCE(date,''),1,10) BETWEEN ? AND ?
              AND questions_attempted IS NOT NULL
            """,
            (start, end),
        ).fetchone()
    attempted = int(row["attempted"])
    if not attempted:
        return None, 0
    return int(row["correct"]) / attempted, attempted


def trajectory_warnings(
    *, today: str | None = None, db_path: str | Path = DEFAULT_DB_PATH
) -> list[str]:
    """Deterministic drift signals. Never claims a trend from thin evidence."""
    today = (today or session_context.local_today_iso())[:10]
    day = dt.date.fromisoformat(today)
    warnings: list[str] = []
    try:
        drop_pts = settings.accuracy_drop_pts()
    except Exception:
        drop_pts = 10
    try:
        low_pct = settings.low_adherence_pct()
    except Exception:
        low_pct = 60
    recent_acc, recent_n = _accuracy_between(
        (day - dt.timedelta(days=6)).isoformat(), today, db_path=db_path
    )
    prior_acc, prior_n = _accuracy_between(
        (day - dt.timedelta(days=13)).isoformat(),
        (day - dt.timedelta(days=7)).isoformat(), db_path=db_path,
    )
    if (
        recent_acc is not None and prior_acc is not None
        and recent_n >= MIN_ATTEMPTED_PER_WINDOW and prior_n >= MIN_ATTEMPTED_PER_WINDOW
        and (prior_acc - recent_acc) * 100 >= drop_pts
    ):
        warnings.append(
            f"accuracy fell {round((prior_acc - recent_acc) * 100)} pts week-over-week "
            f"({round(prior_acc * 100)}% → {round(recent_acc * 100)}%)"
        )
    for goal in commitments.active_daily_goals(db_path=db_path):
        goal_id = goal.get("notion_page_id")
        if not goal_id:
            continue
        stats = commitments.adherence(goal_id, as_of=today, db_path=db_path)
        if (
            stats["total"] >= MIN_ADHERENCE_CHECKS
            and stats["pct"] is not None and stats["pct"] < low_pct
        ):
            warnings.append(
                f"'{goal.get('title')}' kept only {stats['met']}/{stats['total']} "
                f"day(s) ({stats['pct']}%) in the last week"
            )
    try:
        facts = study_domain.plan_facts(today, db_path=db_path)
        if facts["due_revision_count"]:
            warnings.append(f"{facts['due_revision_count']} revision item(s) overdue")
        if facts["unplanned_backlog_count"]:
            warnings.append(
                f"{facts['unplanned_backlog_count']} backlog item(s) not planned anywhere"
            )
    except Exception:
        pass
    return warnings


# ---------------------------------------------------------------------------
# "Right now" situational block (used by sql_query_flow's system prompt)
# ---------------------------------------------------------------------------

def now_block(chat_id: int | None, *, db_path: str | Path = DEFAULT_DB_PATH) -> str:
    """Deterministic snapshot of what the user is doing right now.

    Lets the model reason about time ("it's 14:30, you're 27 min into an
    EB-1 physics block") instead of being time-blind. Lines are omitted when
    unknown; never guessed.
    """
    now = session_context.local_now()
    lines = [f"RIGHT NOW: {now:%Y-%m-%d %H:%M} ({now:%A})"]
    if chat_id is not None:
        try:
            ctx = session_context.get_context(chat_id)
            if ctx:
                bits = ", ".join(
                    str(ctx[k]) for k in ("subject", "chapter", "block", "exercise")
                    if ctx.get(k)
                )
                elapsed = session_context.elapsed_minutes(chat_id)
                line = f"- Active study session: {bits or 'set'}"
                if elapsed is not None:
                    line += f" — running for {round(elapsed)} min"
                lines.append(line)
        except Exception:
            pass
        try:
            plan = study_domain.active_plan(chat_id, db_path=db_path)
            if plan:
                lines.append(
                    f"- Active plan item: {plan.get('title')} "
                    f"[{plan.get('status') or 'Active'}]"
                )
        except Exception:
            pass
    try:
        weekday_name = f"{now:%A}"
        hhmm = f"{now:%H:%M}"
        for slot in study_domain._rows(
            "timetable", "archived=0 AND active=1 AND weekday=?", (weekday_name,),
            db_path=db_path,
        ):
            start = str(slot.get("start_time") or "")
            end = str(slot.get("end_time") or "")
            if start <= hhmm < end:
                teacher = slot.get("teacher")
                lines.append(
                    f"- Per the coaching timetable the user is IN "
                    f"{slot.get('subject')} {str(slot.get('kind') or 'Class').lower()} "
                    f"{start}-{end}" + (f" with {teacher}" if teacher else "")
                )
                break
    except Exception:
        pass
    if len(lines) == 1:
        lines.append("- No active session, plan item, or class right now.")
    lines.append(
        "Use this to judge whether the user is mid-block, in class, or free — "
        "do not assume anything beyond these lines."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Prompt injection block (used by sql_query_flow)
# ---------------------------------------------------------------------------

def memory_prompt_block(
    chat_id: int | None, *, db_path: str | Path = DEFAULT_DB_PATH
) -> str:
    """Commitments + preferences as a system-prompt section. '' when empty."""
    lines: list[str] = []
    today = session_context.local_today_iso()
    goals = [
        g for g in study_domain._rows(
            "goals", "archived=0 AND status='Active' AND period IN ('Daily','Weekly')",
            db_path=db_path,
        )
    ]
    goals.extend(
        g for g in study_domain._rows(
            "op_goals", "archived=0 AND status='Active' AND period IN ('Daily','Weekly')",
            db_path=db_path,
        )
    )
    if goals:
        lines.append(
            "USER COMMITMENTS (adherence below comes from deterministic checks; "
            "for any figure in your answer, still run SQL):"
        )
        for goal in goals:
            entry = (
                f"- {goal.get('title')} — target "
                f"{commitments.format_target(goal.get('target'), goal.get('metric'), goal.get('period'))}"
            )
            goal_id = goal.get("notion_page_id") or goal.get("id")
            if goal_id and goal.get("period") == "Daily":
                days = commitments.streak(goal_id, as_of=today, db_path=db_path)
                stats = commitments.adherence(goal_id, as_of=today, db_path=db_path)
                if stats["total"]:
                    entry += f" (streak {days}, last 7 days {stats['met']}/{stats['total']})"
            lines.append(entry)
    if chat_id is not None:
        prefs = commitments.active_prefs(chat_id, db_path=db_path)
        if prefs:
            lines.append("USER PREFERENCES (frame advice around these; they are not data):")
            lines.extend(f"- {p['text']}" for p in prefs)
        # Work items / active plan from operational store too
        try:
            work_items = study_domain._rows(
                "op_work_items",
                "archived=0 AND status IN ('Planned','In Progress')",
                db_path=db_path,
            )
            if work_items:
                lines.append("ACTIVE WORK ITEMS:")
                for wi in work_items[:10]:
                    lines.append(f"- {wi.get('title')} ({wi.get('status')})")
        except Exception:
            pass
    return "\n".join(lines)
