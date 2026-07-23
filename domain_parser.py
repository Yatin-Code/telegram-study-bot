"""Strict LLM-assisted parsers for goals and exam records.

The model may interpret language, but these validators decide what can become
state. Missing facts stay missing and trigger clarification; invented enum
values or impossible numbers are rejected.
"""

from __future__ import annotations

import math
from typing import Any

import bot_identity
from config import notion_schema
from intent_parser import _extract_json, _legacy_call_model
from config import settings

try:
    from llm.errors import AllRoutesExhausted, RouterUnavailable
except Exception:  # pragma: no cover - llm package absent/broken
    class RouterUnavailable(Exception):  # type: ignore
        """Fallback so router delegation always has an exception to catch."""

    class AllRoutesExhausted(Exception):  # type: ignore
        """Fallback mirror of llm.errors.AllRoutesExhausted."""


class DomainParseError(ValueError):
    pass


def _require_object(data: Any) -> dict[str, Any]:
    """Return ``data`` if it is a dict, else raise DomainParseError.

    Used as the router validator so a non-object response counts as a model
    failure and the router advances to the next provider.
    """
    if not isinstance(data, dict):
        raise DomainParseError("model output is not an object")
    return data


def _build_domain_prompt(kind: str, contract: str) -> str:
    import session_context
    now = session_context.local_now()
    identity = bot_identity.identity_prompt(role=f"{kind} parser")
    return f"""{identity}

You extract one {kind} for this JEE study system.
Current local date/time: {now:%Y-%m-%d %H:%M} ({now:%A}). Use it to resolve
relative dates the user states ("tomorrow", "next month", "10th august" ->
the next 10 August from today). Return one JSON object only. Never invent a
date, number, subject, exam result, or target that the user did not state or
that does not follow from the current date. Use null for missing facts.

Contract:
{contract}

If a required fact is missing, set needs_clarification=true and ask one short
question in clarification_question. Do not add advice or markdown."""


def _call(kind: str, text: str, contract: str) -> dict[str, Any]:
    prompt = _build_domain_prompt(kind, contract)
    try:
        from llm import router
        resp = router.complete(router.LLMRequest(
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": text},
            ],
            purpose="domain",
            max_output_tokens=1024,
            validator=lambda t: _require_object(_extract_json(t)),
        ))
        return resp.value
    except RouterUnavailable:
        return _legacy_call(kind, text, prompt)
    except AllRoutesExhausted as exc:
        raise DomainParseError(str(exc))


def _legacy_call(kind: str, text: str, prompt: str) -> dict[str, Any]:
    errors: list[str] = []
    for model in [settings.llm_model(), *settings.llm_fallback_models()]:
        try:
            raw = _legacy_call_model(model, prompt, text)
            return _require_object(_extract_json(raw))
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
    if not math.isfinite(result):
        raise DomainParseError(f"{name} must be finite")
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
    if not math.isfinite(result):
        raise DomainParseError(f"{name} must be finite")
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


def parse_commitment(text: str) -> dict[str, Any]:
    """Parse a remembered statement into a measurable commitment or a preference.

    Commitments become op_goals rows (period Daily/Weekly) so the planner and
    the nightly ledger verification both watch them; preferences are stored
    as free text and only injected into prompts.
    """
    data = _call("commitment or preference", text, """
{
  "kind": "commitment|preference|bot_instruction",
  "title": string|null,
  "goal_type": "CY|Duration|Coverage"|null,
  "metric": string|null,
  "target": number|null,
  "period": "Daily|Weekly"|null,
  "subject": "Chem|Maths|Physics"|null,
  "study_related": boolean,
  "relevance_note": string|null,
  "needs_clarification": boolean,
  "clarification_question": string|null
}
study_related: does this genuinely relate to JEE prep / studying? Things like
testing the bot, chores, entertainment, unrelated personal matters -> false,
with a one-line relevance_note. Study habits, subjects, revision, exams,
focus preferences -> true.
A commitment is a measurable recurring promise. "PYQs every day" -> kind
commitment, title "Daily PYQs", goal_type Coverage, metric "sessions",
target 1, period Daily. "2 hours of maths daily" -> goal_type Duration,
target 120 (hours become minutes), subject Maths. "300 CY per day" ->
goal_type CY, metric cognitive_yield, target 300. Keep the activity name
(PYQ, revision, ...) inside the title. A count of sessions uses goal_type
Coverage with metric "sessions". A preference or fact with nothing to count
("I prefer maths in the morning") is kind preference — leave goal_type,
target and period null and put the statement in title.
An instruction for the BOT to do/send/tell something on a schedule ("every
weekday tell me my week's cognitive yield", "remind me each sunday to plan")
is kind bot_instruction — set title to the statement, everything else null.
""")
    if data.get("needs_clarification"):
        return data
    kind = str(data.get("kind") or "").strip().lower()
    if kind not in ("commitment", "preference", "bot_instruction"):
        kind = "commitment" if data.get("target") is not None else "preference"
    data["kind"] = kind
    data["source_text"] = text
    data["study_related"] = bool(data.get("study_related", True))
    if kind == "bot_instruction":
        data["needs_clarification"] = False
        if not data.get("title"):
            data["title"] = text.strip()
        return data
    if kind == "preference":
        data["needs_clarification"] = False
        if not data.get("title"):
            data["title"] = text.strip()
        return data
    if not data.get("title") or data.get("target") is None:
        return {
            **data,
            "needs_clarification": True,
            "clarification_question": "What exactly should I track, and how much per day or week?",
        }
    data["goal_type"] = _enum(data.get("goal_type") or "Coverage", "goal_type", notion_schema.GOAL_TYPE_OPTIONS, nullable=False)
    data["period"] = _enum(data.get("period") or "Daily", "period", notion_schema.GOAL_PERIOD_OPTIONS, nullable=False)
    data["subject"] = _enum(data.get("subject"), "subject", notion_schema.SUBJECT_OPTIONS)
    data["target"] = _number(data["target"], "target")
    if not data.get("metric"):
        data["metric"] = "sessions" if data["goal_type"] == "Coverage" else data["goal_type"]
    data["needs_clarification"] = False
    return data


SETUP_AI_ACTION_TYPES = (
    "remember_preference",
    "create_commitment",
    "create_work_item",
    "create_exam",
    "create_timetable_entry",
    "set_setting",
    "skip_section",
)


def parse_setup_ai(section_title: str, section_prompt: str, text: str) -> dict[str, Any]:
    """Interpret a free-form /setup answer into a bounded list of actions.

    The model may only choose from SETUP_AI_ACTION_TYPES; every action is
    re-validated deterministically (onboarding.validate_ai_actions) before
    anything is shown or executed, and nothing runs without the user tapping
    Confirm.
    """
    setting_keys = ", ".join(e["key"] for e in settings.SETTINGS_REGISTRY)
    contract = f"""
{{
  "reply": string,          // one short, friendly sentence answering the user
  "actions": [              // zero or more, ONLY these types:
    {{"type": "remember_preference", "text": string}},
    {{"type": "create_commitment", "statement": string}},
    {{"type": "create_work_item", "title": string,
      "kind": "Coaching Homework|Current Syllabus|Revision|PYQ|Short Notes|Backlog|Other",
      "subject": "Chem|Maths|Physics"|null, "due_date": "YYYY-MM-DD"|null}},
    {{"type": "create_exam", "title": string,
      "kind": "JEE Main Mock|JEE Advanced Mock|Coaching Test|JEE Main|JEE Advanced|Other",
      "exam_date": "YYYY-MM-DD"}},
    {{"type": "create_timetable_entry", "subject": "Chem|Maths|Physics",
      "weekday": "Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday",
      "start": "HH:MM", "end": "HH:MM", "teacher": string|null,
      "questions_allowed": boolean}},
    {{"type": "set_setting", "key": one of [{setting_keys}], "value": string}},
        // weekday-type settings (TIMETABLE_REMINDER_WEEKDAY) use "0"=Monday … "6"=Sunday
    {{"type": "skip_section"}}
  ],
  "needs_clarification": boolean,
  "clarification_question": string|null,
  "study_related": boolean,       // does the request relate to JEE prep/studying?
  "relevance_note": string|null   // one line when study_related is false
}}

Context: the user is inside the "{section_title}" step of a study-bot setup
wizard, which asked: {section_prompt!r}
They answered in free form instead of the requested format. Work out what
should actually happen using ONLY the action types above. Prefer the
smallest set of actions that honours the intent. Examples:
- "my schedule changes constantly, I'll tell you at the end of every week"
  (timetable step) -> remember_preference("coaching schedule changes weekly;
  user updates it at the end of each week") + set_setting
  TIMETABLE_REMINDER_WEEKDAY "6" (Sunday check-in) + skip_section.
- "make my baseline 260" -> set_setting DAILY_CY_BASELINE "260".
- "remind me to add my mock dates next month" -> create_work_item
  (title "Add mock exam dates", kind Other, due_date next month).
- "I'll do PYQs daily" -> create_commitment("PYQs every day").
- "skip this" -> skip_section only.
Never invent dates, numbers or names the user did not state. If the intent
is genuinely unclear, set needs_clarification=true with one short question
and an empty actions list."""
    data = _call("setup assistant plan", text, contract)
    if not isinstance(data.get("actions"), list):
        data["actions"] = []
    data.setdefault("reply", "")
    data.setdefault("needs_clarification", False)
    data["study_related"] = bool(data.get("study_related", True))
    return data


def parse_job(text: str) -> dict[str, Any]:
    """Parse a natural-language scheduled-job request for /jobs.

    A job makes the bot ACT at a time: action_kind "ask" answers a saved
    question from the study database and sends the result; "message" sends a
    fixed reminder text. Validated deterministically by
    user_jobs.validate_parsed before anything is created.
    """
    weekday_name = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][
        settings.timetable_reminder_weekday()
    ]
    builtins = (
        f"weekly growth report every {weekday_name} at {settings.weekly_report_time()}; "
        f"morning commitment nudge daily at {settings.commitment_nudge_time()}; "
        f"nightly commitment check at {settings.commitment_check_time()}; "
        f"planning check daily at {settings.planning_reminder_time()}"
    )
    data = _call("scheduled job", text, f"""
{{
  "title": string|null,               // short label, e.g. "Weekday CY report"
  "schedule_kind": "daily|weekdays|weekly|once",
  "time": "HH:MM",                    // 24h local time — REQUIRED
  "weekday": integer 0..6|null,       // required for weekly (0=Monday ... 6=Sunday)
  "date": "YYYY-MM-DD"|null,          // required for once
  "action_kind": "ask|message",
  "action_text": string,
  "note": string|null,
  "study_related": boolean,       // does this job relate to JEE prep/studying?
  "relevance_note": string|null,  // one line when study_related is false
  "needs_clarification": boolean,
  "clarification_question": string|null
}}
action_kind "ask": at that time the bot ANSWERS action_text as a question
against the user's study database (ledger/goals/doubts/...) and sends the
answer. Use it for any report/analysis request, e.g. "tell me my overall
week cognitive yield" -> action_text "What is my overall cognitive yield for
the last 7 days?". action_kind "message": send action_text verbatim as a
reminder. "every weekday" -> schedule_kind weekdays. If the user states NO
time, set needs_clarification=true asking what time.
These bot schedules already exist: {builtins}. If the request overlaps or
duplicates one of them, still fill the job but say so plainly in "note".""")
    data["study_related"] = bool(data.get("study_related", True))
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
