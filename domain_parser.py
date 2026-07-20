"""Strict LLM-assisted parsers for goals and exam records.

The model may interpret language, but these validators decide what can become
state. Missing facts stay missing and trigger clarification; invented enum
values or impossible numbers are rejected.
"""

from __future__ import annotations

from typing import Any

from config import notion_schema
from intent_parser import _call_model, _extract_json
from config import settings


class DomainParseError(ValueError):
    pass


def _call(kind: str, text: str, contract: str) -> dict[str, Any]:
    prompt = f"""You extract one {kind} for a personal JEE study system.
Return one JSON object only. Never infer a date, number, subject, exam result,
or target that the user did not state. Use null for missing facts.

Contract:
{contract}

If a required fact is missing, set needs_clarification=true and ask one short
question in clarification_question. Do not add advice or markdown."""
    errors: list[str] = []
    for model in [settings.llm_model(), *settings.llm_fallback_models()]:
        try:
            raw = _call_model(model, prompt, text)
            data = _extract_json(raw)
            if not isinstance(data, dict):
                raise DomainParseError("model output is not an object")
            return data
        except Exception as exc:
            errors.append(f"{model}: {exc}")
    raise DomainParseError("; ".join(errors))


def _number(value: Any, name: str, *, minimum: float = 0) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise DomainParseError(f"{name} cannot be boolean")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise DomainParseError(f"{name} must be numeric") from exc
    if result < minimum:
        raise DomainParseError(f"{name} must be at least {minimum:g}")
    return int(result) if result.is_integer() else result


def _signed_number(value: Any, name: str) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise DomainParseError(f"{name} cannot be boolean")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise DomainParseError(f"{name} must be numeric") from exc
    return int(result) if result.is_integer() else result


def _enum(value: Any, name: str, allowed: list[str], *, nullable: bool = True) -> str | None:
    if value is None and nullable:
        return None
    for option in allowed:
        if str(value).strip().lower() == option.lower():
            return option
    raise DomainParseError(f"invalid {name}: {value!r}")


def parse_goal(text: str) -> dict[str, Any]:
    data = _call("goal", text, """
{
  "title": string|null,
  "goal_type": "CY|Duration|Coverage|Marks|Accuracy|Rank|Custom"|null,
  "metric": string|null,
  "target": number|null,
  "period": "Daily|Weekly|Exam|Deadline|One-time"|null,
  "subject": "Chem|Maths|Physics"|null,
  "deadline": ISO-date|null,
  "minimum": number|null,
  "priority": integer 1..100|null,
  "hard_constraint": boolean,
  "needs_clarification": boolean,
  "clarification_question": string|null
}
For "300 CY" use goal_type CY and metric cognitive_yield. For hours convert
the target to minutes and use goal_type Duration. Rank goals may use target 1.
""")
    if data.get("needs_clarification"):
        return data
    if not data.get("title") or not data.get("goal_type") or data.get("target") is None:
        return {
            **data,
            "needs_clarification": True,
            "clarification_question": "What measurable target and frequency should this goal use?",
        }
    data["goal_type"] = _enum(data["goal_type"], "goal_type", notion_schema.GOAL_TYPE_OPTIONS, nullable=False)
    data["period"] = _enum(data.get("period") or "Deadline", "period", notion_schema.GOAL_PERIOD_OPTIONS, nullable=False)
    data["subject"] = _enum(data.get("subject"), "subject", notion_schema.SUBJECT_OPTIONS)
    data["target"] = _number(data["target"], "target")
    data["minimum"] = _number(data.get("minimum"), "minimum")
    priority = _number(data.get("priority") or 50, "priority", minimum=1)
    if priority is not None and priority > 100:
        raise DomainParseError("priority must be 1..100")
    data["priority"] = priority
    data["hard_constraint"] = bool(data.get("hard_constraint", False))
    data["needs_clarification"] = False
    data["source_text"] = text
    return data


def parse_exam(text: str) -> dict[str, Any]:
    data = _call("exam", text, """
{
  "title": string|null,
  "kind": "JEE Main Mock|JEE Advanced Mock|Coaching Test|JEE Main|JEE Advanced|Other"|null,
  "exam_date": ISO-8601 date-or-datetime|null,
  "date_confidence": "Tentative|Official"|null,
  "syllabus": string|null,
  "max_marks": number|null,
  "target_marks": number|null,
  "source_url": string|null,
  "needs_clarification": boolean,
  "clarification_question": string|null
}
An actual JEE date is Official only when the user explicitly says official or
provides an official source. Otherwise use Tentative.
""")
    if data.get("needs_clarification"):
        return data
    if not data.get("exam_date"):
        return {
            **data,
            "needs_clarification": True,
            "clarification_question": "What is the exam name and its date/time?",
        }
    data["kind"] = _enum(data.get("kind") or "Other", "kind", notion_schema.EXAM_KIND_OPTIONS, nullable=False)
    if not data.get("title"):
        data["title"] = f"{data['kind']} {str(data['exam_date'])[:10]}"
    data["date_confidence"] = _enum(
        data.get("date_confidence") or "Tentative", "date_confidence",
        notion_schema.DATE_CONFIDENCE_OPTIONS, nullable=False,
    )
    data["max_marks"] = _number(data.get("max_marks"), "max_marks", minimum=1)
    data["target_marks"] = _number(data.get("target_marks"), "target_marks")
    if data["target_marks"] is None:
        return {
            **data,
            "needs_clarification": True,
            "clarification_question": "What marks target should I use for this exam?",
        }
    if data["max_marks"] is not None and data["target_marks"] is not None:
        if data["target_marks"] > data["max_marks"]:
            raise DomainParseError("target marks cannot exceed maximum marks")
    data["needs_clarification"] = False
    return data


def parse_exam_summary(text: str) -> dict[str, Any]:
    data = _call("exam result summary", text, """
{
  "exam": string|null,
  "actual_marks": number|null,
  "attempted": integer|null,
  "correct": integer|null,
  "incorrect": integer|null,
  "unattempted": integer|null,
  "needs_clarification": boolean,
  "clarification_question": string|null
}
""")
    data["actual_marks"] = _signed_number(data.get("actual_marks"), "actual_marks")
    for key in ("attempted", "correct", "incorrect", "unattempted"):
        data[key] = _number(data.get(key), key)
    if data.get("attempted") is not None and data.get("correct") is not None and data.get("incorrect") is not None:
        if data["correct"] + data["incorrect"] > data["attempted"]:
            raise DomainParseError("correct + incorrect cannot exceed attempted")
    if not data.get("exam") or data.get("actual_marks") is None:
        data["needs_clarification"] = True
        data["clarification_question"] = "Which exam was this, and what score did you receive?"
    return data


def parse_question_review(text: str) -> dict[str, Any]:
    data = _call("exam question review", text, """
{
  "exam": string|null,
  "question_no": string|null,
  "subject": "Chem|Maths|Physics"|null,
  "chapter": string|null,
  "attempted": boolean|null,
  "correct": boolean|null,
  "marks_awarded": number|null,
  "marks_lost": number|null,
  "time_min": number|null,
  "failure_type": "Omission|Time Management|Concept|Calculation|Misread|Guessing|Strategy"|null,
  "root_cause": string|null,
  "correct_approach": string|null,
  "needs_clarification": boolean,
  "clarification_question": string|null
}
Never infer the chapter, cause, correctness or marks.
""")
    data["subject"] = _enum(data.get("subject"), "subject", notion_schema.SUBJECT_OPTIONS)
    data["failure_type"] = _enum(data.get("failure_type"), "failure_type", notion_schema.FAILURE_TYPE_OPTIONS)
    data["marks_awarded"] = _signed_number(data.get("marks_awarded"), "marks_awarded")
    for key in ("marks_lost", "time_min"):
        data[key] = _number(data.get(key), key)
    if not data.get("exam") or not data.get("question_no"):
        data["needs_clarification"] = True
        data["clarification_question"] = "Which exam and question number is this review for?"
    return data
