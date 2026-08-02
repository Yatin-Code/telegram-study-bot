"""Phase 13 — deterministic proactive coaching notification scan (orchestrator).

A single scheduled scan that gathers *candidate notifications* from the
deterministic coaching modules and passes each through the durable notification
policy (``coaching_policy.decide_notification``) *before* anything is sent:

  * progress-question prompts          (``coaching_progress.missing_data_questions``)
  * due doubt reattempts               (``coaching_doubts.due_reattempts``)
  * prediction / readiness changes     (``coaching_prediction.project_coaching_score``)
  * backlog escalation                 (``coaching_policy.backlog_escalation``)

Class lifecycle (pre/post-class nudges) stays in its existing job
(``coaching_lifecycle.scan_candidates``) and is deliberately not duplicated
here — this orchestrator owns the other Phase 13 kinds.

Design rules:

  * Fully offline and deterministic.  Every source reads the local mirror; no
    portal credentials are required.  When the portal is unconfigured the
    coaching dataset is ``never_synced`` and the gated kinds are suppressed by
    policy, so nothing is sent on missing/stale data.
  * ``scan_candidates`` never sends anything.  It returns candidates with an
    ``allow`` flag and the recorded policy decision; the bot job claims each
    approved ``event_key`` via ``reminders.claim`` before delivery and releases
    the claim when delivery fails (no double-send, retry on failure).
  * Every candidate's decision is persisted via ``coaching_policy.record_decision``
    so the quiet-hours / cooldown / daily-budget / freshness gates are durable
    and audit-able.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import coaching_policy
import session_context

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "sqlite_mirror.db"


def _progress_prompts(*, db_path: str | Path) -> list[dict[str, Any]]:
    """Eligible missing-progress question prompts (cooldown-aware inside)."""
    import coaching_progress
    candidates: list[dict[str, Any]] = []
    for prompt in coaching_progress.missing_data_questions(limit=5, db_path=db_path):
        candidates.append({
            "kind": "coaching_progress",
            "event_key": f"coaching-progress:{prompt['progress_key']}:{prompt['subject'] or '?'}:{prompt['topic'] or '?'}",
            "priority": "normal",
            "message": prompt["question"],
            "metadata": {
                "progress_key": prompt["progress_key"],
                "subject": prompt.get("subject"),
                "topic": prompt.get("topic"),
                "reason": prompt.get("reason"),
            },
        })
    return candidates


def _due_reattempts(
    *, chat_id: int | str | None = None, db_path: str | Path,
) -> list[dict[str, Any]]:
    """Retest-scheduled doubt interactions whose retest date has arrived."""
    import coaching_doubts
    candidates: list[dict[str, Any]] = []
    for session in coaching_doubts.due_reattempts(chat_id, db_path=db_path):
        candidates.append({
            "kind": "doubt_reattempt",
            "event_key": f"doubt-reattempt:{session['id']}:{session.get('doubt_id') or '?'}",
            "priority": "normal",
            "message": session["message"],
            "metadata": {
                "session_id": session["id"],
                "doubt_id": session.get("doubt_id"),
                "doubt_concept": session.get("doubt_concept"),
                "retest_at": session.get("retest_at"),
            },
        })
    return candidates


def _readiness(*, db_path: str | Path) -> list[dict[str, Any]]:
    """Prediction/readiness change: only meaningful projections, never rank claims."""
    import coaching_prediction
    candidates: list[dict[str, Any]] = []
    snapshot = coaching_prediction.project_coaching_score(db_path=db_path, store=False)
    if snapshot.get("status") != "ok":
        return candidates
    total = snapshot.get("total") or {}
    pct = total.get("pct") or {}
    days = snapshot.get("days_to_test")
    confidence = snapshot.get("confidence")
    if confidence not in ("medium", "high"):
        return candidates
    if days is None or days < 0 or days > 7:
        return candidates
    message = (
        f"📊 Projection for {snapshot.get('test_title') or 'the next test'} "
        f"({snapshot.get('test_date')}, {days} day(s) out): "
        f"likely {pct.get('likely_low')}-{pct.get('likely_high')}% "
        f"({confidence} confidence)."
    )
    candidates.append({
        "kind": "readiness",
        "event_key": f"readiness:{snapshot.get('test_id')}:{snapshot.get('test_date')}",
        "priority": "normal",
        "message": message,
        "metadata": {
            "test_id": snapshot.get("test_id"),
            "test_date": snapshot.get("test_date"),
            "days_to_test": days,
            "confidence": confidence,
        },
    })
    return candidates


def _backlog(*, db_path: str | Path) -> list[dict[str, Any]]:
    """Backlog escalation — only when the verdict is not 'normal'."""
    candidates: list[dict[str, Any]] = []
    esc = coaching_policy.backlog_escalation(db_path=db_path)
    level = esc.get("level") or "normal"
    if level == "normal":
        return candidates
    metrics = esc.get("metrics") or {}
    message = (
        f"📈 Backlog {level}: {metrics.get('count', 0)} item(s), "
        f"~{metrics.get('estimated_hours', 0)}h estimated. "
        f"{esc.get('recommendation') or ''}"
    )
    candidates.append({
        "kind": "backlog",
        "event_key": f"backlog:{level}:{esc.get('as_of')}",
        "priority": "high" if level in ("critical", "impossible") else "normal",
        "message": message,
        "metadata": {
            "level": level,
            "count": metrics.get("count"),
            "estimated_hours": metrics.get("estimated_hours"),
        },
    })
    return candidates


def scan_candidates(
    *,
    now: dt.datetime | None = None,
    chat_id: int | str | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    """Gather due candidates and return only policy-approved ones.

    Nothing is sent here.  Every candidate is decided via
    ``coaching_policy.decide_notification`` and the decision is recorded; only
    candidates with ``allow=True`` are returned.  The bot job must claim each
    returned candidate's ``event_key`` with ``reminders.claim`` before sending.
    """
    now = now or session_context.local_now()
    raw = (
        _progress_prompts(db_path=db_path)
        + _due_reattempts(chat_id=chat_id, db_path=db_path)
        + _readiness(db_path=db_path)
        + _backlog(db_path=db_path)
    )
    approved: list[dict[str, Any]] = []
    for candidate in raw:
        decision = coaching_policy.decide_notification(
            kind=candidate["kind"],
            now=now,
            event_key=candidate["event_key"],
            chat_id=chat_id,
            db_path=db_path,
            priority=candidate.get("priority", "normal"),
        )
        coaching_policy.record_decision(decision, db_path=db_path)
        candidate["decision"] = decision
        candidate["allow"] = bool(decision["allow"])
        candidate["blocked_by"] = list(decision.get("blocked_by") or [])
        if candidate["allow"]:
            approved.append(candidate)
    # Deterministic ordering: priority (high first) then kind then event_key.
    approved.sort(key=lambda c: (
        0 if c.get("priority") == "high" else 1,
        c["kind"],
        c["event_key"],
    ))
    return approved


def main() -> int:
    import json
    import sys

    candidates = scan_candidates()
    print(f"{len(candidates)} approved candidate(s):")
    for candidate in candidates:
        print(json.dumps({
            "kind": candidate["kind"],
            "event_key": candidate["event_key"],
            "message": candidate["message"],
            "blocked_by": candidate["blocked_by"],
        }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
