"""Bounded, evidence-backed plan critic with structured action ranking.

The deterministic layer owns every candidate action.  An optional LLM pass may
only reorder those candidates; it cannot create actions, edit Notion, or change
the evidence.  This keeps planning useful offline and safe under bad model
output while removing the old coupling to human warning strings.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import bot_identity
import study_domain
from intent_parser import _extract_json


logger = logging.getLogger(__name__)

_SIGNAL_PRIORITY = {
    "capacity_exceeded": 100,
    "missing_sequence": 95,
    "duplicate_sequence": 95,
    "homework_unplanned": 85,
    "overdue_revision_unplanned": 80,
    "active_goal_gap": 75,
    "backlog_unplanned": 65,
    "below_adaptive_target": 60,
    "goal_unverifiable": 55,
}


def _suggestion(
    signal: dict[str, Any], action: str, reason: str, **fields: Any
) -> dict[str, Any]:
    code = str(signal["code"])
    suffix = str(fields.get("goal") or fields.get("item") or action)
    suggestion_id = f"{code}:{suffix}".lower().replace(" ", "_")
    return {
        "id": suggestion_id,
        "source_signal": code,
        "severity": signal["severity"],
        "priority": _SIGNAL_PRIORITY.get(code, 50),
        "action": action,
        "reason": reason,
        "evidence": dict(signal.get("evidence") or {}),
        **fields,
    }


def _candidate_suggestions(facts: dict[str, Any]) -> list[dict[str, Any]]:
    suggestions: list[dict[str, Any]] = []
    for signal in facts.get("signals", []):
        code = signal.get("code")
        if code == "capacity_exceeded":
            candidates = [
                row for row in facts.get("active_items", facts["items"])
                if row.get("interruptible", 1)
                and int(row.get("priority") or 0) < 80
            ]
            candidates.sort(key=lambda row: int(row.get("priority") or 0))
            if candidates:
                suggestions.append(_suggestion(
                    signal, "move_to_backlog",
                    "planned CY exceeds the hard ceiling",
                    kind="capacity", item=candidates[0].get("title"),
                ))
            else:
                suggestions.append(_suggestion(
                    signal, "ask_user",
                    "no interruptible optional item can be moved safely",
                    kind="capacity",
                ))
        elif code in {"missing_sequence", "duplicate_sequence"}:
            suggestions.append(_suggestion(
                signal, "fix_sequence_numbers",
                "each active block needs one deterministic order",
                kind="sequence",
            ))
        elif code == "active_goal_gap":
            for gap in signal.get("evidence", {}).get("gaps", []):
                suggestions.append(_suggestion(
                    signal, "add_or_expand_plan_item",
                    "active daily goal is not covered by the plan",
                    kind="goal", goal=gap.get("goal"),
                    missing=float(gap.get("target") or 0) - float(gap.get("planned") or 0),
                ))
        elif code == "homework_unplanned":
            suggestions.append(_suggestion(
                signal, "link_current_homework",
                "current coaching homework is missing from the sequence",
                kind="homework",
            ))
        elif code == "backlog_unplanned":
            suggestions.append(_suggestion(
                signal, "reserve_backlog_slot",
                "tracked backlog exists but is not represented in the sequence",
                kind="backlog",
            ))
        elif code == "overdue_revision_unplanned":
            suggestions.append(_suggestion(
                signal, "reserve_next_available_slot",
                "an overdue revision is absent from the sequence",
                kind="revision",
            ))
        elif code == "below_adaptive_target":
            suggestions.append(_suggestion(
                signal, "add_or_expand_plan_item",
                "active plan capacity is below the evidence-backed daily target",
                kind="capacity_gap",
                missing=max(
                    0,
                    float(signal.get("evidence", {}).get("target") or 0)
                    - float(signal.get("evidence", {}).get("expected_cy") or 0),
                ),
            ))
        elif code == "goal_unverifiable":
            suggestions.append(_suggestion(
                signal, "clarify_goal_metric",
                "the goal cannot be checked against plan evidence yet",
                kind="goal_definition",
            ))

    # Several malformed rows can emit the same repair action. Keep one stable
    # candidate per id before ranking.
    unique: dict[str, dict[str, Any]] = {}
    for item in suggestions:
        unique.setdefault(item["id"], item)
    return sorted(
        unique.values(),
        key=lambda item: (-int(item["priority"]), item["id"]),
    )


def _ai_rank_suggestion_ids(
    suggestions: list[dict[str, Any]], *, plan_date: str
) -> list[str]:
    """Ask the model to reorder only validated suggestion IDs."""
    from llm import router

    allowed = {item["id"] for item in suggestions}

    def validate(text: str) -> list[str]:
        data = _extract_json(text)
        ordered = data.get("ordered_ids")
        if not isinstance(ordered, list):
            raise ValueError("ordered_ids must be a list")
        clean: list[str] = []
        for value in ordered:
            value = str(value)
            if value not in allowed:
                raise ValueError(f"unknown suggestion id {value!r}")
            if value not in clean:
                clean.append(value)
        if not clean:
            raise ValueError("ranking cannot be empty")
        return clean

    import actions
    prompt = f"""{actions.identity_with_actions(role="plan action ranker", context="planning_time")}

Rank the supplied already-validated plan actions for {plan_date}. You may only
reorder IDs from the list. Do not invent, delete, or modify actions. Prefer hard
constraints, time-sensitive homework/revision, active commitments, then optional
backlog. Return JSON only: {{"ordered_ids": ["id", ...]}}.

VALIDATED ACTIONS:
{json.dumps(suggestions, ensure_ascii=False, sort_keys=True)}"""
    response = router.complete(router.LLMRequest(
        messages=[{"role": "system", "content": prompt}],
        purpose="domain",
        max_output_tokens=500,
        validator=validate,
    ))
    return response.value


def _rank_suggestions(
    suggestions: list[dict[str, Any]], *, plan_date: str, use_ai: bool
) -> tuple[list[dict[str, Any]], str]:
    if not suggestions or not use_ai:
        return suggestions, "deterministic"
    try:
        ordered_ids = _ai_rank_suggestion_ids(suggestions, plan_date=plan_date)
    except Exception:
        logger.info("AI plan ranking unavailable; using deterministic priority", exc_info=True)
        return suggestions, "deterministic_fallback"
    by_id = {item["id"]: item for item in suggestions}
    ranked = [by_id[item_id] for item_id in ordered_ids]
    ranked.extend(item for item in suggestions if item["id"] not in ordered_ids)
    return ranked, "ai"


def _personalize_suggestions(
    suggestions: list[dict[str, Any]], profile: dict[str, Any] | None
) -> list[dict[str, Any]]:
    if not profile:
        return suggestions
    weakest = str((profile.get("weakest_subject") or {}).get("subject") or "").lower()
    workload = profile.get("workload") or {}
    personalized: list[dict[str, Any]] = []
    for item in suggestions:
        copy = dict(item)
        boost = 0
        reasons: list[str] = []
        searchable = json.dumps({
            "goal": copy.get("goal"), "item": copy.get("item"),
            "evidence": copy.get("evidence"),
        }, ensure_ascii=False).lower()
        if weakest and weakest in searchable:
            boost += 10
            reasons.append(f"matches the weakest evidenced subject ({weakest.title()})")
        if (
            copy.get("source_signal") == "overdue_revision_unplanned"
            and int(workload.get("overdue_revision_count") or 0) > 0
        ):
            boost += 5
            reasons.append("matches current revision pressure")
        if (
            copy.get("source_signal") == "backlog_unplanned"
            and int(workload.get("backlog_count") or 0) >= 3
        ):
            boost += 5
            reasons.append("matches current backlog pressure")
        if boost:
            copy["priority"] = int(copy["priority"]) + boost
            copy["personalization"] = reasons
        personalized.append(copy)
    return sorted(
        personalized,
        key=lambda item: (-int(item["priority"]), item["id"]),
    )


def analyze(
    plan_date: str | None = None,
    *,
    max_iterations: int = 3,
    ai_rank: bool = False,
    chat_id: int | str | None = None,
    db_path: str | Path = study_domain.DEFAULT_DB_PATH,
) -> dict[str, Any]:
    if max_iterations < 1:
        raise ValueError("max_iterations must be positive")
    facts = study_domain.plan_facts(plan_date, db_path=db_path)
    candidates = _candidate_suggestions(facts)
    profile = None
    if chat_id is not None:
        try:
            import learner_profile
            profile = learner_profile.latest(chat_id, db_path=db_path) or learner_profile.refresh(
                chat_id, as_of=facts["plan_date"], db_path=db_path
            )
        except Exception:
            profile = None
    candidates = _personalize_suggestions(candidates, profile)
    suggestions, ranking_source = _rank_suggestions(
        candidates, plan_date=facts["plan_date"], use_ai=ai_rank,
    )
    trace = [{
        "iteration": 1,
        "signals": list(facts.get("signals", [])),
        "candidate_ids": [item["id"] for item in candidates],
        "ranking_source": ranking_source,
        "personalized": profile is not None,
    }]
    return {
        **facts,
        "suggestions": suggestions,
        "trace": trace,
        "ranking_source": ranking_source,
        "bounded": True,
        "max_iterations": min(max_iterations, 3),
    }
