"""Compact, safe coaching context shared by all LLM request paths.

Always-on, privacy-redacted summary only.  It deliberately surfaces small,
deterministic signals — progress gaps, next-doubt priority, score-projection
range + confidence, backlog escalation level, and per-dataset freshness — and
never embeds full raw payloads (no profile blobs, raw JSON, or page bodies).
Every value is passed through ``coaching_policy.redact_text`` before it reaches
a model prompt.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import ntsc_coaching
import session_context
import study_domain

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "sqlite_mirror.db"


def _class_line(row: dict[str, Any]) -> str:
    duration = row.get("duration_min")
    end = ""
    if duration and str(row.get("start_time") or "").count(":") == 1:
        hour, minute = (int(x) for x in str(row["start_time"]).split(":", 1))
        end_dt = dt.datetime(2000, 1, 1, hour, minute) + dt.timedelta(minutes=int(duration))
        end = end_dt.strftime("%H:%M")
    times = f"{row.get('start_time')}" + (f"-{end}" if end else "")
    return f"{times} {row.get('class_type') or 'Class'}: {row.get('subjects') or 'n/a'}"


def snapshot(*, chat_id: int | None = None, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    data = ntsc_coaching.context_snapshot(db_path=db_path)
    today = session_context.local_today_iso()
    try:
        data["execution"] = study_domain.plan_facts(today, db_path=db_path)
    except Exception:
        data["execution"] = None
    return data


def _redact(text: str) -> str:
    try:
        from coaching_policy import redact_text
        return redact_text(text)
    except Exception:
        return text


def _progress_gaps_line(*, db_path: str | Path) -> list[str]:
    """Compact progress-gap signals from coaching_progress (bounded)."""
    lines: list[str] = []
    try:
        import coaching_progress
        cov = coaching_progress.coverage_summary(limit=3, db_path=db_path)
        subjects = cov.get("subjects") or []
        missing_subjects = [s for s in subjects if (s.get("missing") or 0) > 0]
        if missing_subjects:
            lines.append("- Progress gaps: " + "; ".join(
                f"{s['subject']} {s['missing']} uncovered topic(s)"
                for s in missing_subjects[:3]
            ))
        elif subjects:
            lines.append("- Progress gaps: none in the upcoming window")
    except Exception:
        pass
    return lines


def _next_doubt_line(*, db_path: str | Path) -> list[str]:
    lines: list[str] = []
    try:
        import coaching_doubts
        ranked = coaching_doubts.ranked_doubts(db_path=db_path)
        if not ranked:
            lines.append("- Next doubt: none open")
            return lines
        top = ranked[0]
        lines.append(
            f"- Next doubt ({len(ranked)} open, {top.get('confidence')} priority): "
            f"{top.get('concept') or 'untitled'} — {top.get('reason') or 'no signals'}"
        )
    except Exception:
        pass
    return lines


def _prediction_line(*, db_path: str | Path) -> list[str]:
    """Score projection range + confidence. Never stores a snapshot."""
    lines: list[str] = []
    try:
        import coaching_prediction
        snap = coaching_prediction.project_coaching_score(db_path=db_path, store=False)
        if snap.get("status") == "ok":
            total = snap.get("total") or {}
            pct = total.get("pct") or {}
            lines.append(
                f"- Score projection ({snap.get('confidence')}): {pct.get('likely_low')}-"
                f"{pct.get('likely_high')}% for {snap.get('test_title') or 'next test'} "
                f"({snap.get('days_to_test')} days out)"
            )
        elif snap.get("missing"):
            lines.append(f"- Score projection: unavailable ({', '.join(snap['missing'])})")
    except Exception:
        pass
    return lines


def _escalation_line(*, db_path: str | Path) -> list[str]:
    lines: list[str] = []
    try:
        import coaching_policy
        esc = coaching_policy.backlog_escalation(db_path=db_path)
        level = esc.get("level") or "normal"
        metrics = esc.get("metrics") or {}
        line = f"- Backlog: {level} ({metrics.get('count', 0)} item(s), ~{metrics.get('estimated_hours', 0)}h)"
        if level != "normal":
            line += f" — {esc.get('recommendation') or ''}"
        lines.append(line)
    except Exception:
        pass
    return lines


def _freshness_line(*, db_path: str | Path) -> list[str]:
    lines: list[str] = []
    try:
        import coaching_policy
        data = coaching_policy.classify_freshness(db_path=db_path)
        parts = ", ".join(
            f"{k}={v['status']}" for k, v in data.items() if k in coaching_policy.ALL_DATASETS
        )
        if parts:
            lines.append(f"- Data freshness: {parts}")
    except Exception:
        pass
    return lines


def _discipline_lines(*, db_path: str | Path) -> list[str]:
    """Two compact execution-discipline lines (current block + today's progress).

    Omitted gracefully (no lines, no exception) when the discipline tables are
    empty or any lookup fails.
    """
    lines: list[str] = []
    try:
        import execution_discipline
        import session_context
        now = session_context.local_now()
        today_iso = session_context.local_today_iso()
        block = execution_discipline.current_block(now, db_path=db_path)
        if block is not None:
            state = execution_discipline.get_state(
                block["local_date"], block["block_key"], db_path=db_path,
            )
            status = (state or {}).get("status") or "pending"
            lines.append(
                f"- Current block: {block['title']} "
                f"({block['start_hhmm']}-{block['end_hhmm']}) · {status}"
            )
        blocks = execution_discipline.blocks_for_date(today_iso, db_path=db_path)
        study = [b for b in blocks if b["kind"] == "study"]
        if study:
            started = 0
            skipped = 0
            for b in study:
                state = execution_discipline.get_state(today_iso, b["block_key"], db_path=db_path)
                status = (state or {}).get("status") or "pending"
                if status == "started":
                    started += 1
                elif status == "skipped":
                    skipped += 1
            lines.append(
                f"- Today: {started}/{len(study)} blocks started, {skipped} skipped"
            )
    except Exception:
        pass
    return lines


def render_compact(
    chat_id: int | None = None, *, user_text: str = "",
    db_path: str | Path = DEFAULT_DB_PATH,
) -> str:
    data = snapshot(chat_id=chat_id, db_path=db_path)
    lines = [f"- Local date: {session_context.local_today_iso()}"]
    profile = data.get("profile") or {}
    if profile:
        lines.append(
            f"- Coaching: {profile.get('course_name') or 'course unknown'} / "
            f"batch {profile.get('batch') or 'unknown'}"
        )
    for label, key in (("Today", "today"), ("Tomorrow", "tomorrow")):
        classes = (data.get(key) or {}).get("classes") or []
        lines.append(f"- {label} classes: " + ("; ".join(_class_line(row) for row in classes) if classes else "none recorded"))
    tests = data.get("next_tests") or []
    if tests:
        lines.append("- Next tests: " + "; ".join(
            f"{row.get('title')} on {str(row.get('test_date') or '')[:16]}" for row in tests
        ))
    latest = data.get("latest_result")
    if latest:
        lines.append(
            f"- Latest portal result: {latest.get('title')} {latest.get('total_marks')}/"
            f"{latest.get('maximum_marks')} · rank {latest.get('rank')} · "
            f"batch rank {latest.get('batch_rank')} · percentile {latest.get('percentile')}"
        )
    execution = data.get("execution") or {}
    if execution:
        lines.append(
            f"- Plan load: {len(execution.get('active_items') or [])} active items, "
            f"{execution.get('planned_minutes', 0):g} planned minutes, "
            f"{execution.get('unplanned_backlog_count', 0)} unplanned backlog, "
            f"{execution.get('due_revision_count', 0)} due revisions"
        )
    try:
        import coaching_syllabus
        progress = coaching_syllabus.progress_snapshot(limit=3, db_path=db_path)
        test_rows = progress.get("tests") or []
        subject_rows = progress.get("subjects") or []
        if test_rows:
            lines.append(
                "- Upcoming syllabus: " + "; ".join(
                    f"{row.get('title')} on {str(row.get('test_date') or '')[:10]} "
                    f"({row.get('covered_count')}/{row.get('topic_count')} topics covered)"
                    for row in test_rows
                )
            )
        if subject_rows:
            lines.append(
                "- Syllabus coverage: " + "; ".join(
                    f"{row['subject']} {row['covered_count']}/{row['topic_count']} "
                    f"({row.get('doubt_count', 0)} doubt(s))"
                    for row in subject_rows[:4]
                )
            )
    except Exception:
        pass
    try:
        import coaching_planner
        plan = coaching_planner.plan_tomorrow(db_path=db_path)
        placed = [b for b in plan.get("blocks") or [] if b.get("placed")]
        unplaced = plan.get("unplaced") or []
        capacity = (plan.get("capacity") or {}).get(plan.get("start_date") or "") or {}
        lines.append(
            f"- Tomorrow's plan: {len(placed)} placed block(s), {len(unplaced)} unplaced, "
            f"{capacity.get('planned_minutes', 0):g} planned minutes of "
            f"{capacity.get('budget_minutes', '?')} budget"
        )
        if plan.get("warnings"):
            lines.append("- Plan notes: " + "; ".join(plan["warnings"][:2]))
    except Exception:
        pass
    workload = {}
    try:
        import learner_profile
        profile_data = learner_profile.latest(chat_id) if chat_id is not None else None
        workload = (profile_data or {}).get("workload") or {}
    except Exception:
        pass
    if workload:
        lines.append(
            f"- Study load: {workload.get('backlog_count', 0)} backlog, "
            f"{workload.get('overdue_revision_count', 0)} overdue revisions, "
            f"{workload.get('unresolved_doubt_count', 0)} unresolved doubts"
        )
    # Phase 9-14 compact signals (bounded, privacy-redacted).
    lines.extend(_progress_gaps_line(db_path=db_path))
    lines.extend(_next_doubt_line(db_path=db_path))
    lines.extend(_prediction_line(db_path=db_path))
    lines.extend(_escalation_line(db_path=db_path))
    lines.extend(_freshness_line(db_path=db_path))
    lines.extend(_discipline_lines(db_path=db_path))
    return _redact("\n".join(lines))
