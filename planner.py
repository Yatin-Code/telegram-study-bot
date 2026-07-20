"""Bounded, evidence-backed plan critic.

The planner never edits Notion silently. It runs a small deterministic loop,
returns concrete changes, and leaves the user in control of the final sequence.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import study_domain


def analyze(
    plan_date: str | None = None,
    *, max_iterations: int = 3,
    db_path: str | Path = study_domain.DEFAULT_DB_PATH,
) -> dict[str, Any]:
    if max_iterations < 1:
        raise ValueError("max_iterations must be positive")
    facts = study_domain.plan_facts(plan_date, db_path=db_path)
    suggestions: list[dict[str, Any]] = []
    trace: list[dict[str, Any]] = []
    for iteration in range(1, min(max_iterations, 3) + 1):
        errors = list(facts["errors"])
        warnings = list(facts["warnings"])
        trace.append({"iteration": iteration, "errors": errors, "warnings": warnings})
        if errors:
            if any("exceeds ceiling" in error for error in errors):
                candidates = [
                    row for row in facts["items"]
                    if row.get("interruptible", 1) and int(row.get("priority") or 0) < 80
                ]
                candidates.sort(key=lambda row: int(row.get("priority") or 0))
                if candidates:
                    suggestions.append({
                        "kind": "capacity",
                        "action": "move_to_backlog",
                        "item": candidates[0].get("title"),
                        "reason": "planned CY exceeds the hard ceiling",
                    })
                else:
                    suggestions.append({
                        "kind": "capacity",
                        "action": "ask_user",
                        "reason": "no interruptible optional item can be moved safely",
                    })
            if any("duplicate sequence" in error for error in errors):
                suggestions.append({
                    "kind": "sequence", "action": "fix_sequence_numbers",
                    "reason": "each block needs one deterministic order",
                })
            break
        if not warnings:
            break
        for gap in facts["active_goal_gaps"]:
            suggestions.append({
                "kind": "goal", "action": "add_or_expand_plan_item",
                "goal": gap["goal"], "missing": gap["target"] - gap["planned"],
                "reason": "active daily goal is not covered by the Notion plan",
            })
        if any("coaching homework" in warning for warning in warnings):
            suggestions.append({
                "kind": "homework", "action": "link_current_homework",
                "reason": "current coaching homework is missing from the sequence",
            })
        if any("backlog item" in warning for warning in warnings):
            suggestions.append({
                "kind": "backlog", "action": "reserve_backlog_slot",
                "reason": "tracked backlog exists but is not represented in the sequence",
            })
        if any("overdue revision" in warning for warning in warnings):
            suggestions.append({
                "kind": "revision", "action": "reserve_next_available_slot",
                "reason": "an overdue revision is absent from the sequence",
            })
        break
    return {
        **facts,
        "suggestions": suggestions,
        "trace": trace,
        "bounded": True,
        "max_iterations": min(max_iterations, 3),
    }
