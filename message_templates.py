"""Deterministic, reusable Telegram message cards.

Automated coaching messages must present facts, priority and one next action;
they never use an LLM to invent narrative around operational data.
"""

from __future__ import annotations

from typing import Any, Iterable


def action_card(
    icon: str,
    title: str,
    *,
    context: str | None = None,
    conclusion: str | None = None,
    sections: Iterable[tuple[str, Iterable[str]]] = (),
    action: str | None = None,
    footer: str | None = None,
) -> str:
    lines = [f"{icon} {title}" + (f" · {context}" if context else "")]
    if conclusion:
        lines.extend(("", conclusion))
    for heading, items in sections:
        clean = [str(item).strip() for item in items if str(item).strip()]
        if not clean:
            continue
        lines.extend(("", heading))
        lines.extend(f"• {item}" for item in clean)
    if action:
        lines.extend(("", "Next", f"→ {action}"))
    if footer:
        lines.extend(("", footer))
    return "\n".join(lines)


def insert_section(card: str, heading: str, items: Iterable[str]) -> str:
    """Insert a section before Next/footer without rebuilding an existing card."""
    clean = [str(item).strip() for item in items if str(item).strip()]
    if not clean:
        return card
    block = f"\n\n{heading}\n" + "\n".join(f"• {item}" for item in clean)
    for marker in ("\n\nNext\n", "\n\nEvidence date:"):
        index = card.find(marker)
        if index >= 0:
            return card[:index] + block + card[index:]
    return card + block


def doubt_dashboard(rows: list[dict[str, Any]]) -> str:
    cleanup = [row for row in rows if row.get("metadata_incomplete")]
    classified = [row for row in rows if not row.get("metadata_incomplete")]
    ready = [row for row in classified if row.get("readiness") == "ready"]
    attempting = [row for row in classified if row.get("readiness") in ("attempting", "expedited")]
    new = [row for row in classified if row.get("readiness") == "new"]

    def item(row: dict[str, Any]) -> str:
        title = str(row.get("core_concept") or "Untitled doubt")
        attempts = int(row.get("valid_attempts") or 0)
        subject = str(row.get("subject") or "").strip()
        suffix = f" · {subject}" if subject else ""
        if row.get("readiness") == "expedited":
            return f"{title}{suffix} — {attempts}/2 attempts; imminent-window exception available"
        return f"{title}{suffix} — {attempts}/2 valid attempts"

    sections = []
    if ready:
        sections.append(("Teacher-ready", [item(row) for row in ready[:10]]))
    if attempting:
        sections.append(("Attempting", [item(row) for row in attempting[:10]]))
    if new:
        sections.append(("New", [item(row) for row in new[:10]]))
    if cleanup:
        sections.append(("Data cleanup", [
            f"{row.get('core_concept') or 'Untitled doubt'} — "
            f"{int(row.get('valid_attempts') or 0)}/2 attempts; missing status/workflow metadata"
            for row in cleanup[:5]
        ]))
    if not rows:
        return action_card(
            "✅", "Doubt dashboard", conclusion="No open doubts.",
            action="Log the exact concept and stuck point when the next doubt appears.",
        )
    return action_card(
        "❓", "Doubt dashboard",
        conclusion=f"{len(rows)} open · {len(ready)} teacher-ready",
        sections=sections,
        action=(
            "Take a genuine attempt with /attempt doubt | minutes | approach | stuck point."
            if not ready else "Use the next matching teacher window for the teacher-ready list."
        ),
    )


def attempt_result(result: dict[str, Any], doubt: str) -> str:
    count = int(result.get("valid_attempts") or 0)
    ready = bool(result.get("teacher_ready"))
    if ready:
        conclusion = f"{doubt} is now teacher-ready."
        action = "Take the exact stuck point to the next matching teacher window."
    elif count:
        conclusion = f"Attempt recorded for {doubt}; one independent attempt is proven."
        action = "Revisit after at least 30 minutes, then record the next distinct approach."
    else:
        conclusion = f"Attempt recorded for {doubt}, but it did not qualify as independent evidence."
        action = "Try a distinct approach before recording another attempt."
    return action_card(
        "✅" if ready else "🟡", "Doubt attempt",
        conclusion=conclusion,
        sections=(("Evidence", (
            f"Valid attempts: {count}/2",
            f"Workflow: {result.get('workflow_state')}",
        )),),
        action=action,
    )


def teacher_opportunity(item: dict[str, Any]) -> str:
    window = item["window"]
    decision = item["decision"]
    candidates = item.get("doubts") or []
    phase = decision.get("phase") or "open"
    subject = str(window.get("subject") or "All subjects")
    teacher = str(window.get("teacher") or "Teacher")
    if phase == "prepare":
        minutes = decision.get("minutes_to_start")
        sections: list[tuple[str, list[str]]] = [("Window", [
            f"{teacher} · {subject}",
            f"Starts in {minutes:g} minutes; ends {window['ends_at']:%H:%M}",
        ])]
        doubt_lines = []
        for row in candidates[:5]:
            state = row.get("readiness")
            label = {
                "ready": "teacher-ready",
                "expedited": "expedited after one serious attempt",
                "attempting": "needs a longer/distinct attempt",
                "new": "new; attempt not started",
            }.get(state, str(state))
            doubt_lines.append(f"{row.get('core_concept') or 'Untitled doubt'} — {label}")
        sections.append(("Doubts", doubt_lines))
        needs_attempt = any(row.get("readiness") in ("new", "attempting") for row in candidates)
        action = (
            "Use the next 10–15 minutes for one genuine attempt, then record the exact stuck point."
            if needs_attempt else "Write each exact stuck point now and ask during the window."
        )
        return action_card(
            "🟡", "Teacher window approaching", context=subject,
            conclusion="Prepare evidence before asking; do not waste teacher access.",
            sections=sections, action=action,
        )

    minutes_left = float(decision.get("minutes_left") or 0)
    doubt_lines = [
        f"{row.get('core_concept') or 'Untitled doubt'} — "
        + ("2-attempt verified" if row.get("readiness") == "ready" else "expedited: 1 serious attempt")
        for row in candidates[:5]
    ]
    action = (
        "Pause at the current boundary and ask now."
        if decision.get("interrupt") else "Keep the questions ready and ask at the next safe boundary."
    )
    return action_card(
        "⚡", "Teacher available", context=f"{minutes_left:g} min left",
        conclusion=f"{teacher} can take {subject} doubts now.",
        sections=(("Prepared questions", doubt_lines), ("Plan", (decision.get("reason"),))),
        action=action,
    )


def exam_readiness(snapshot: dict[str, Any]) -> str:
    """Render a concise evidence audit; never imply that a plan was created."""
    exam = snapshot["exam"]
    phase = str(snapshot.get("phase") or "manual")
    days = snapshot.get("days_until")
    timing = "today" if days == 0 else (f"in {days} day(s)" if days is not None else "date unknown")
    phase_label = {
        "t7": "T−7 window · full audit", "t3": "T−3 window · unresolved check",
        "t1": "T−1 final check", "day": "exam-day check",
        "added": "new-exam audit", "manual": "manual audit",
    }.get(phase, phase)
    doubts = list(snapshot.get("doubts") or [])
    revision = list(snapshot.get("revision") or [])
    points = list(snapshot.get("key_points") or [])

    def clip(value: Any, limit: int) -> str:
        text = str(value or "").strip()
        return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"

    if phase == "day":
        shown_doubts = [row for row in doubts if row.get("readiness") == "ready"][:5]
        shown_revision = revision[:3]
    elif phase == "t1":
        shown_doubts = doubts[:6]
        shown_revision = revision[:4]
    else:
        shown_doubts = doubts[:10]
        shown_revision = revision[:7]

    doubt_lines = []
    for index, row in enumerate(shown_doubts, 1):
        state = {
            "ready": "teacher-ready",
            "attempting": f"{int(row.get('valid_attempts') or 0)}/2 attempts",
            "new": "0/2 attempts",
        }.get(str(row.get("readiness")), str(row.get("readiness") or "open"))
        classified = " · confirmed open" if row.get("exam_decision") == "open" else ""
        if row.get("scope_uncertain"):
            classified += " · confirm whether it is in this exam"
        subject = f" · {clip(row.get('subject'), 30)}" if row.get("subject") else ""
        doubt_lines.append(
            f"#{index} {clip(row.get('core_concept') or 'Untitled doubt', 90)}"
            f"{subject} — {state}{classified}"
        )

    revision_lines = []
    for row in shown_revision:
        risk = str(row.get("exam_schedule_risk") or "")
        risk_label = {
            "overdue": "overdue",
            "unscheduled": "no date",
            "due_before_exam": "due before exam",
            "scheduled_after_exam": "after exam",
        }.get(risk, "schedule unknown")
        revision_lines.append(
            f"{clip(row.get('chapter_module') or 'Untitled revision', 90)}"
            f" — {risk_label} · {str(row.get('next_execution_date') or 'not scheduled')[:14]}"
        )
    point_lines = [
        f"{str(row.get('date') or '')[:10]} · {clip(row.get('task') or 'Study block', 60)}: "
        f"{clip(row.get('key_points_notes'), 130)}"
        for row in points[:6 if phase not in ("t1", "day") else 3]
    ]

    scope = (
        f"Syllabus filter: {clip(snapshot['syllabus'], 300)}"
        if snapshot.get("syllabus_known") else
        "Syllabus is not recorded, so this audit includes all open evidence."
    )
    excluded = list(snapshot.get("excluded_doubts") or [])
    if excluded:
        scope += f" · {len(excluded)} doubt(s) marked not in this exam"
    conclusion = (
        f"{len(doubts)} relevant open doubt(s): "
        f"{snapshot.get('zero_attempt_count', 0)} unattempted, "
        f"{snapshot.get('teacher_ready_count', 0)} teacher-ready. "
        f"{len(revision)} incomplete revision item(s); "
        f"{len(points)} matching takeaway(s) recorded in the last 7 days."
    )
    if snapshot.get("scope_uncertain_count"):
        conclusion += (
            f" {snapshot['scope_uncertain_count']} lack enough chapter metadata "
            "for a safe automatic syllabus decision."
        )
    sections: list[tuple[str, list[str]]] = [("Scope", [scope])]
    if doubt_lines:
        sections.append(("Open doubts", doubt_lines))
    elif phase == "day" and doubts:
        sections.append(("Open doubts", ["No teacher-ready doubt remains for an exam-day escalation."]))
    else:
        sections.append(("Open doubts", ["No matching open doubt is recorded."]))
    high_weightage = list(snapshot.get("high_weightage_doubts") or [])
    if high_weightage:
        weightage_lines = []
        for row in high_weightage[:5]:
            weight = row.get("weightage") or {}
            chapter = weight.get("chapter") or clip(row.get("core_concept"), 60)
            weightage_lines.append(
                f"{clip(chapter, 60)} — #{weight.get('weightage_rank')} by "
                f"weightage · {weight.get('total_questions')} Q, still open"
            )
        sections.append(("High-weightage open doubts", weightage_lines))
    if excluded:
        sections.append(("Excluded for this exam", [
            clip(row.get("core_concept") or "Untitled doubt", 90)
            for row in excluded[:5]
        ]))
    if revision_lines:
        sections.append(("Revision readiness risks", revision_lines))
    if point_lines and phase != "day":
        sections.append(("Key takeaways · last 7 days", point_lines))
    elif not point_lines and phase not in ("t1", "day"):
        sections.append(("Key takeaways · last 7 days", ["None recorded in matching blocks."]))

    if phase == "day":
        action = "Handle only the unresolved high-priority items and exam logistics; do not add new scope."
    elif not snapshot.get("syllabus_known"):
        action = (
            "Set it with /readiness exam name | syllabus, then classify each doubt "
            "as open, solved, or not in this mock."
        )
    else:
        action = "Classify each doubt below; solved requires a typed resolution, not a status-only tap."
    card = action_card(
        "🧭", "Exam readiness",
        context=f"{clip(exam.get('title') or 'Exam', 100)} · {timing} · {phase_label}",
        conclusion=conclusion, sections=sections, action=action,
        footer="Evidence audit only — no Daily Plan rows were created.",
    )
    if len(card) <= 4000:
        return card
    footer = "\n\nEvidence audit only — no Daily Plan rows were created."
    return card[:4000 - len(footer) - 2].rstrip() + "…" + footer
