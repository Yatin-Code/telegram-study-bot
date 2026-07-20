"""
Notion schema for the three study-tracking databases.

Source of truth: spec Section 1 "Known Notion schema".
This module hardcodes the *known* structure (property names, types, option
lists) so the rest of the bot never has to re-discover it at runtime.

The only thing that CANNOT be hardcoded here is the live `database_id` /
`data_source_id` for each DB — those must be resolved in Phase 2 by calling
Notion's /v1/search with the integration token. Until then they are None.

Field name mapping convention
-----------------------------
Each property is recorded as a dict with:
    notion_name : exact Notion property name (do NOT rename — writes depend on this)
    type        : one of title | rich_text | number | select | status | date |
                  checkbox | relation | rollup | formula | people
    human_name  : short stable key used in code / LLM JSON / SQLite columns
    options     : list of allowed values for select/status (None otherwise)
    required    : True if Notion requires this property on page creation
    read_only   : True for formula/rollup fields — never write these
    relates_to  : for relation fields, the target DB key in this module
    notes       : free-form clarification from the spec

DB keys: "ledger", "doubts", "revision"  (used everywhere in code, never the
human title, which has spaces/punctuation).
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Database registry
# ---------------------------------------------------------------------------
# database_id and data_source_id are resolved in Phase 2 via /v1/search.
# They are None until then — code that needs them must check and raise a
# clear "run Phase 2 ID resolver first" error.

DATABASES: dict[str, dict[str, Any]] = {
    "ledger": {
        "title": "1. Daily Execution Ledger (The 80% Output)",
        "database_id": None,        # TODO Phase 2: resolve live
        "data_source_id": None,     # TODO Phase 2: resolve live
        "description": "Per-session study execution log. The main write target.",
    },
    "doubts": {
        "title": "Doubts",
        "database_id": None,
        "data_source_id": None,
        "description": "Doubts/bugs surfaced during execution. Cross-linked from Ledger.",
    },
    "revision": {
        "title": "Revision",
        "database_id": None,
        "data_source_id": None,
        "description": "Revision schedule and mastery tracking per chapter/module.",
    },
    "work_items": {
        "title": "Study Bot - Work Items & Backlog",
        "database_id": None,
        "data_source_id": None,
        "description": "All actionable homework, backlog, revision and recurring work.",
    },
    "goals": {
        "title": "Study Bot - Goals",
        "database_id": None,
        "data_source_id": None,
        "description": "Measurable long-term and recurring goals.",
    },
    "exams": {
        "title": "Study Bot - Exams",
        "database_id": None,
        "data_source_id": None,
        "description": "Mock and official exam calendar, targets and outcomes.",
    },
    "exam_questions": {
        "title": "Study Bot - Exam Questions & Mistakes",
        "database_id": None,
        "data_source_id": None,
        "description": "Question-level exam analysis and reattempt state.",
    },
    "doubt_attempts": {
        "title": "Study Bot - Doubt Attempts",
        "database_id": None,
        "data_source_id": None,
        "description": "Auditable attempts made before a doubt is escalated.",
    },
    "timetable": {
        "title": "Study Bot - Class & Teacher Timetable",
        "database_id": None,
        "data_source_id": None,
        "description": "Weekly classes and teacher doubt windows.",
    },
    "daily_plan": {
        "title": "Study Bot - Daily Plan",
        "database_id": None,
        "data_source_id": None,
        "description": "Ordered, timestamp-free daily execution sequence.",
    },
}


# ---------------------------------------------------------------------------
# Option lists (select / status) — frozen here so validation is offline
# ---------------------------------------------------------------------------

EXERCISE_TYPE_OPTIONS = [
    "MLE",
    "Ex 1A",
    "Ex 1B",
    "Ex 2A",
    "Ex 2B",
    "Ex 3A",
    "Ex 3B",
    "Ex 4A",
    "Ex 4B",
    "JMYL",
    "JAYL",
    "PYQs",
    "Revision",
    "Theory",
]

BLOCK_OPTIONS = [
    "EB-1",
    "EB-2",
    "EB-3",
    "EB - A",
    "EB - B",
    "EB-C",
    "RB",
    "TA",
    "ADV.",
    "AB",
]

DOUBT_STATUS_OPTIONS = ["Unresolved", "Resolved"]

FAILURE_TYPE_OPTIONS = [
    "Omission",
    "Time Management",
    "Concept",
    "Calculation",
    "Misread",
    "Guessing",
    "Strategy",
]

REVISION_STATUS_OPTIONS = ["Pending", "Completed"]

MASTERY_STATUS_OPTIONS = ["Not started", "In progress", "Done"]

# Subject options — live on Ledger and Revision as select properties
# (Phase 2: originally relation properties to external Subject/Exercise DBs,
# converted to selects by the user to keep the workspace self-contained.)
SUBJECT_OPTIONS = ["Chem", "Maths", "Physics"]

WORK_KIND_OPTIONS = [
    "Coaching Homework", "Current Syllabus", "Revision", "PYQ",
    "Short Notes", "Backlog", "Doubt Reattempt", "Exam Review", "Other",
]
WORK_STATUS_OPTIONS = [
    "Inbox", "Planned", "Active", "Blocked", "Backlog", "Completed", "Dismissed",
]
GOAL_TYPE_OPTIONS = ["CY", "Duration", "Coverage", "Marks", "Accuracy", "Rank", "Custom"]
GOAL_STATUS_OPTIONS = ["Draft", "Active", "Paused", "Completed", "Cancelled"]
GOAL_PERIOD_OPTIONS = ["Daily", "Weekly", "Exam", "Deadline", "One-time"]
EXAM_KIND_OPTIONS = [
    "JEE Main Mock", "JEE Advanced Mock", "Coaching Test",
    "JEE Main", "JEE Advanced", "Other",
]
EXAM_STATUS_OPTIONS = ["Planned", "Completed", "Analysing", "Analysed", "Cancelled"]
DATE_CONFIDENCE_OPTIONS = ["Tentative", "Official"]
REATTEMPT_STATUS_OPTIONS = ["Pending", "Passed", "Failed", "Dismissed"]
DOUBT_WORKFLOW_OPTIONS = [
    "New", "Attempting", "Eligible for Teacher", "Scheduled to Ask",
    "Solved Independently", "Resolved", "Dismissed",
]
ATTEMPT_OUTCOME_OPTIONS = [
    "Unsolved", "Solved Independently", "Hint Used", "Solution Viewed", "Dismissed",
]
WEEKDAY_OPTIONS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
TIMETABLE_KIND_OPTIONS = ["Class", "Doubt Window", "Test", "Other"]
PLAN_STATUS_OPTIONS = ["Planned", "Active", "Completed", "Skipped", "Moved"]


# ---------------------------------------------------------------------------
# Property schemas per database
# ---------------------------------------------------------------------------

LEDGER_PROPERTIES: dict[str, dict[str, Any]] = {
    "task": {
        "notion_name": "Task (Actionable Verb + Exact Scope)",
        "human_name": "task",
        "type": "title",
        "required": True,
        "read_only": False,
        "options": None,
        "notes": "Title of the ledger entry.",
    },
    "date": {
        "notion_name": "Date",
        "human_name": "date",
        "type": "date",
        "required": False,
        "read_only": False,
        "options": None,
        "notes": "Used for 'today' filtering.",
    },
    "subject": {
        "notion_name": "Subject",
        "human_name": "subject",
        "type": "select",
        "required": False,
        "read_only": False,
        "options": SUBJECT_OPTIONS,
        "notes": (
            "Phase 2: originally a relation to an external Subjects DB. "
            "User converted to select (CHEMISTRY/MATHS/PHYSICS) so the "
            "workspace is self-contained within the 3 scoped DBs."
        ),
    },
    "chapter": {
        "notion_name": "Chapter",
        "human_name": "chapter",
        "type": "relation",
        "required": False,
        "read_only": False,
        "options": None,
        "relates_to": "revision",   # Chapter relation targets the Revision DB
        "notes": (
            "Relation to Revision DB. Phase 2 confirmed: this is the only "
            "live relation on Ledger (Exercise relation was deleted by user)."
        ),
    },
    "exercise_type": {
        "notion_name": "Exercise Type",
        "human_name": "exercise_type",
        "type": "select",
        "required": False,
        "read_only": False,
        "options": EXERCISE_TYPE_OPTIONS,
        "notes": None,
    },
    "block": {
        "notion_name": "BLOCK",
        "human_name": "block",
        "type": "select",
        "required": False,
        "read_only": False,
        "options": BLOCK_OPTIONS,
        "notes": None,
    },
    "max_time_min": {
        "notion_name": "Max Time (min)",
        "human_name": "max_time_min",
        "type": "rich_text",
        "required": False,
        "read_only": False,
        "options": None,
        "notes": "Spec lists as 'text' — stored as rich_text in Notion.",
    },
    "actual_time_min": {
        "notion_name": "Actual Time Spent (mins)",
        "human_name": "actual_time_min",
        "type": "number",
        "required": False,
        "read_only": False,
        "options": None,
        "notes": None,
    },
    "questions_attempted": {
        "notion_name": "Questions Attempted",
        "human_name": "questions_attempted",
        "type": "number",
        "required": False,
        "read_only": False,
        "options": None,
        "notes": None,
    },
    "questions_correct": {
        "notion_name": "Questions Correct",
        "human_name": "questions_correct",
        "type": "number",
        "required": False,
        "read_only": False,
        "options": None,
        "notes": None,
    },
    "tickbox": {
        "notion_name": "Tickbox",
        "human_name": "tickbox",
        "type": "checkbox",
        "required": False,
        "read_only": False,
        "options": None,
        "notes": None,
    },
    "doubts": {
        "notion_name": "Doubts",
        "human_name": "doubts",
        "type": "rich_text",
        "required": False,
        "read_only": False,
        "options": None,
        "notes": "Free text.",
    },
    "key_points_notes": {
        "notion_name": "Key Points / Notes",
        "human_name": "key_points_notes",
        "type": "rich_text",
        "required": False,
        "read_only": False,
        "options": None,
        "notes": "Free text.",
    },
    "alternative": {
        "notion_name": "Alternative",
        "human_name": "alternative",
        "type": "rich_text",
        "required": False,
        "read_only": False,
        "options": None,
        "notes": None,
    },
    "logged_errors": {
        "notion_name": "Logged Errors",
        "human_name": "logged_errors",
        "type": "relation",
        "required": False,
        "read_only": False,
        "options": None,
        "relates_to": "doubts",
        "notes": "Relation to Doubts DB. Used when a doubt is cross-logged from a ledger entry.",
    },
    "work_item": {
        "notion_name": "Work Item",
        "human_name": "work_item",
        "type": "relation",
        "required": False,
        "read_only": False,
        "options": None,
        "relates_to": "work_items",
        "notes": "Operational task completed by this execution block.",
    },
    # ---- read-only formula columns (never write) ----
    "cognitive_yield": {
        "notion_name": "Cognitive Yield ",
        "human_name": "cognitive_yield",
        "type": "formula",
        "required": False,
        "read_only": True,
        "options": None,
        "notes": "Read-only formula. Replicate logic locally for SQLite queries (Phase 8).",
    },
    "theory_yield": {
        "notion_name": "Theory Yield ",
        "human_name": "theory_yield",
        "type": "formula",
        "required": False,
        "read_only": True,
        "options": None,
        "notes": "Read-only formula.",
    },
    "cognitive_yield_task": {
        "notion_name": "Cognitive Yield (Task)",
        "human_name": "cognitive_yield_task",
        "type": "formula",
        "required": False,
        "read_only": True,
        "options": None,
        "notes": "Read-only formula. Discovered in Phase 2; not in spec Section 1.",
    },
    "id": {
        "notion_name": "Id",
        "human_name": "id",
        "type": "unique_id",
        "required": False,
        "read_only": True,
        "options": None,
        "notes": "Notion auto-incremented ID. Read-only.",
    },
    "accuracy_ratio": {
        "notion_name": "Accuracy Ratio",
        "human_name": "accuracy_ratio",
        "type": "formula",
        "required": False,
        "read_only": True,
        "options": None,
        "notes": "Read-only formula. questions_correct / questions_attempted.",
    },
    "mins_per_question": {
        "notion_name": "Mins per Question",
        "human_name": "mins_per_question",
        "type": "formula",
        "required": False,
        "read_only": True,
        "options": None,
        "notes": "Read-only formula. actual_time_min / questions_attempted.",
    },
    "calculated_circled_qs": {
        "notion_name": "Calculated Circled Qs",
        "human_name": "calculated_circled_qs",
        "type": "formula",
        "required": False,
        "read_only": True,
        "options": None,
        "notes": "Read-only formula.",
    },
    "chapter_text": {
        "notion_name": "Chapter Text",
        "human_name": "chapter_text",
        "type": "formula",
        "required": False,
        "read_only": True,
        "options": None,
        "notes": "Read-only formula (pulls from Chapter relation).",
    },
}


DOUBTS_PROPERTIES: dict[str, dict[str, Any]] = {
    "core_concept": {
        "notion_name": "Core Concept / Root Bug",
        "human_name": "core_concept",
        "type": "title",
        "required": True,
        "read_only": False,
        "options": None,
        "notes": None,
    },
    "status": {
        "notion_name": "Status",
        "human_name": "status",
        "type": "select",
        "required": False,
        "read_only": False,
        "options": DOUBT_STATUS_OPTIONS,
        "notes": None,
    },
    "failure_type": {
        "notion_name": "Failure Type",
        "human_name": "failure_type",
        "type": "select",
        "required": False,
        "read_only": False,
        "options": FAILURE_TYPE_OPTIONS,
        "notes": None,
    },
    "concept_deficit_failure_reason": {
        "notion_name": "Concept Deficit / Failure Reason",
        "human_name": "concept_deficit_failure_reason",
        "type": "rich_text",
        "required": False,
        "read_only": False,
        "options": None,
        "notes": None,
    },
    "ledger_entry": {
        "notion_name": "Ledger Entry",
        "human_name": "ledger_entry",
        "type": "relation",
        "required": False,
        "read_only": False,
        "options": None,
        "relates_to": "ledger",
        "notes": "Relation back to Ledger DB.",
    },
    "workflow_state": {
        "notion_name": "Workflow State",
        "human_name": "workflow_state",
        "type": "select",
        "required": False,
        "read_only": False,
        "options": DOUBT_WORKFLOW_OPTIONS,
        "notes": "Two-attempt workflow state before teacher escalation.",
    },
    "valid_attempts": {
        "notion_name": "Valid Attempts",
        "human_name": "valid_attempts",
        "type": "number",
        "required": False,
        "read_only": False,
        "options": None,
        "notes": "Materialised valid-attempt count; maintained by the bot.",
    },
    "teacher_ready": {
        "notion_name": "Teacher Ready",
        "human_name": "teacher_ready",
        "type": "checkbox",
        "required": False,
        "read_only": False,
        "options": None,
        "notes": "True only after at least two valid attempts.",
    },
    "next_teacher_window": {
        "notion_name": "Next Teacher Window",
        "human_name": "next_teacher_window",
        "type": "date",
        "required": False,
        "read_only": False,
        "options": None,
        "notes": None,
    },
    "dismissed_reason": {
        "notion_name": "Dismissed Reason",
        "human_name": "dismissed_reason",
        "type": "rich_text",
        "required": False,
        "read_only": False,
        "options": None,
        "notes": None,
    },
    "resolution": {
        "notion_name": "Resolution",
        "human_name": "resolution",
        "type": "rich_text",
        "required": False,
        "read_only": False,
        "options": None,
        "notes": "The corrected concept or method, not merely a status change.",
    },
    "resolved_at": {
        "notion_name": "Resolved At",
        "human_name": "resolved_at",
        "type": "date",
        "required": False,
        "read_only": False,
        "options": None,
        "notes": None,
    },
    "teacher_asked": {
        "notion_name": "Teacher Asked",
        "human_name": "teacher_asked",
        "type": "checkbox",
        "required": False,
        "read_only": False,
        "options": None,
        "notes": "Cannot become true before two valid attempts.",
    },
    # ---- read-only rollups (inherited via Ledger Entry) ----
    "chapter": {
        "notion_name": "Chapter",
        "human_name": "chapter",
        "type": "rollup",
        "required": False,
        "read_only": True,
        "options": None,
        "notes": "Read-only rollup, inherited via Ledger Entry.",
    },
    "exercise_type": {
        "notion_name": "Exercise Type",
        "human_name": "exercise_type",
        "type": "rollup",
        "required": False,
        "read_only": True,
        "options": EXERCISE_TYPE_OPTIONS,
        "notes": "Read-only rollup, inherited via Ledger Entry.",
    },
    "subject": {
        "notion_name": "Subject",
        "human_name": "subject",
        "type": "rollup",
        "required": False,
        "read_only": True,
        "options": None,
        "notes": "Read-only rollup, inherited via Ledger Entry.",
    },
}


REVISION_PROPERTIES: dict[str, dict[str, Any]] = {
    "chapter_module": {
        "notion_name": "Chapter / Module",
        "human_name": "chapter_module",
        "type": "title",
        "required": True,
        "read_only": False,
        "options": None,
        "notes": None,
    },
    "status": {
        "notion_name": "Status",
        "human_name": "status",
        "type": "select",
        "required": False,
        "read_only": False,
        "options": REVISION_STATUS_OPTIONS,
        "notes": None,
    },
    "mastery": {
        "notion_name": "Mastery",
        "human_name": "mastery",
        "type": "status",
        "required": False,
        "read_only": False,
        "options": MASTERY_STATUS_OPTIONS,
        "notes": None,
    },
    "next_execution_date": {
        "notion_name": "Next Execution Date",
        "human_name": "next_execution_date",
        "type": "date",
        "required": False,
        "read_only": False,
        "options": None,
        "notes": "Used for overdue queries (next_execution_date < today).",
    },
    "is_short_notes_completed": {
        "notion_name": "Is Short notes Completed?",
        "human_name": "is_short_notes_completed",
        "type": "checkbox",
        "required": False,
        "read_only": False,
        "options": None,
        "notes": None,
    },
    "double_circled_faculty_intervention_req": {
        "notion_name": "Double-Circled (Faculty Intervention Req.)",
        "human_name": "double_circled_faculty_intervention_req",
        "type": "number",
        "required": False,
        "read_only": False,
        "options": None,
        "notes": None,
    },
    "total_circled_questions_manual": {
        "notion_name": "Total circled questions (manual)",
        "human_name": "total_circled_questions_manual",
        "type": "number",
        "required": False,
        "read_only": False,
        "options": None,
        "notes": None,
    },
    "subject": {
        "notion_name": "Subject",
        "human_name": "subject",
        "type": "select",
        "required": False,
        "read_only": False,
        "options": SUBJECT_OPTIONS,
        "notes": (
            "Phase 2: originally a relation to an external Subjects DB. "
            "User converted to select (CHEMISTRY/MATHS/PHYSICS)."
        ),
    },
    "exercises": {
        "notion_name": "Exercises",
        "human_name": "exercises",
        "type": "select",
        "required": False,
        "read_only": False,
        "options": EXERCISE_TYPE_OPTIONS,
        "notes": (
            "Phase 2: originally a relation to an external Exercises DB. "
            "User converted to select, sharing the same option list as "
            "Ledger's Exercise Type."
        ),
    },
    "ledger_entries": {
        "notion_name": "Ledger Entries",
        "human_name": "ledger_entries",
        "type": "relation",
        "required": False,
        "read_only": False,
        "options": None,
        "relates_to": "ledger",
        "notes": "Relation to Ledger DB.",
    },
    # ---- read-only formula/rollup columns (never write) ----
    "total_circled_questions": {
        "notion_name": "TOTAL CIRCLED QUESTIONS ",
        "human_name": "total_circled_questions",
        "type": "formula",
        "required": False,
        "read_only": True,
        "options": None,
        "notes": "Read-only formula/rollup.",
    },
    "total_circled_qs": {
        "notion_name": "Total Circled Qs",
        "human_name": "total_circled_qs",
        "type": "rollup",
        "required": False,
        "read_only": True,
        "options": None,
        "notes": "Read-only rollup (spec Section 1 listed it as formula; live type is rollup).",
    },
    "total_questions_attempted": {
        "notion_name": "Total Questions Attempted",
        "human_name": "total_questions_attempted",
        "type": "rollup",
        "required": False,
        "read_only": True,
        "options": None,
        "notes": "Read-only rollup.",
    },
    "total_questions_correct": {
        "notion_name": "Total Questions Correct",
        "human_name": "total_questions_correct",
        "type": "rollup",
        "required": False,
        "read_only": True,
        "options": None,
        "notes": "Read-only rollup.",
    },
    "total_time_spent_mins": {
        "notion_name": "Total Time Spent (mins)",
        "human_name": "total_time_spent_mins",
        "type": "rollup",
        "required": False,
        "read_only": True,
        "options": None,
        "notes": "Read-only rollup.",
    },
    "chapter_accuracy_pct": {
        "notion_name": "Chapter Accuracy %",
        "human_name": "chapter_accuracy_pct",
        "type": "formula",
        "required": False,
        "read_only": True,
        "options": None,
        "notes": "Read-only formula/rollup.",
    },
    "all_doubts": {
        "notion_name": "All Doubts",
        "human_name": "all_doubts",
        "type": "rollup",
        "required": False,
        "read_only": True,
        "options": None,
        "notes": "Read-only rollup.",
    },
}


def _p(
    notion_name: str,
    prop_type: str,
    *,
    options: list[str] | None = None,
    required: bool = False,
    relates_to: str | None = None,
) -> dict[str, Any]:
    """Build a property definition for the Study Bot operational databases."""
    prop = {
        "notion_name": notion_name,
        "human_name": "",
        "type": prop_type,
        "required": required,
        "read_only": False,
        "options": options,
        "notes": None,
    }
    if relates_to:
        prop["relates_to"] = relates_to
    return prop


def _with_human_names(props: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    for human_name, prop in props.items():
        prop["human_name"] = human_name
    return props


WORK_ITEM_PROPERTIES = _with_human_names({
    "title": _p("Work Item", "title", required=True),
    "kind": _p("Kind", "select", options=WORK_KIND_OPTIONS),
    "status": _p("Status", "select", options=WORK_STATUS_OPTIONS),
    "subject": _p("Subject", "select", options=SUBJECT_OPTIONS),
    "chapter": _p("Chapter", "rich_text"),
    "source": _p("Source", "rich_text"),
    "due_date": _p("Due Date", "date"),
    "planned_date": _p("Planned Date", "date"),
    "sequence": _p("Sequence", "number"),
    "priority": _p("Priority", "number"),
    "estimated_min": _p("Estimated Minutes", "number"),
    "expected_cy": _p("Expected CY", "number"),
    "remaining_units": _p("Remaining Units", "number"),
    "total_units": _p("Total Units", "number"),
    "goal": _p("Goal", "relation", relates_to="goals"),
    "exam": _p("Exam", "relation", relates_to="exams"),
    "interruptible": _p("Interruptible", "checkbox"),
    "exit_condition": _p("Exit Condition", "rich_text"),
    "notes": _p("Notes", "rich_text"),
})

GOAL_PROPERTIES = _with_human_names({
    "title": _p("Goal", "title", required=True),
    "goal_type": _p("Goal Type", "select", options=GOAL_TYPE_OPTIONS),
    "status": _p("Status", "select", options=GOAL_STATUS_OPTIONS),
    "metric": _p("Metric", "rich_text"),
    "target": _p("Target", "number"),
    "period": _p("Period", "select", options=GOAL_PERIOD_OPTIONS),
    "subject": _p("Subject", "select", options=SUBJECT_OPTIONS),
    "deadline": _p("Deadline", "date"),
    "priority": _p("Priority", "number"),
    "minimum": _p("Minimum", "number"),
    "current_value": _p("Current Value", "number"),
    "hard_constraint": _p("Hard Constraint", "checkbox"),
    "source_text": _p("Original Request", "rich_text"),
    "notes": _p("Notes", "rich_text"),
})

EXAM_PROPERTIES = _with_human_names({
    "title": _p("Exam", "title", required=True),
    "kind": _p("Kind", "select", options=EXAM_KIND_OPTIONS),
    "status": _p("Status", "select", options=EXAM_STATUS_OPTIONS),
    "exam_date": _p("Exam Date", "date"),
    "date_confidence": _p("Date Confidence", "select", options=DATE_CONFIDENCE_OPTIONS),
    "syllabus": _p("Syllabus", "rich_text"),
    "max_marks": _p("Maximum Marks", "number"),
    "target_marks": _p("Target Marks", "number"),
    "actual_marks": _p("Actual Marks", "number"),
    "attempted": _p("Attempted", "number"),
    "correct": _p("Correct", "number"),
    "incorrect": _p("Incorrect", "number"),
    "unattempted": _p("Unattempted", "number"),
    "analysis_complete": _p("Analysis Complete", "checkbox"),
    "source_url": _p("Official Source", "rich_text"),
    "notes": _p("Notes", "rich_text"),
})

EXAM_QUESTION_PROPERTIES = _with_human_names({
    "title": _p("Question Review", "title", required=True),
    "exam": _p("Exam", "relation", relates_to="exams"),
    "question_no": _p("Question Number", "rich_text"),
    "subject": _p("Subject", "select", options=SUBJECT_OPTIONS),
    "chapter": _p("Chapter", "rich_text"),
    "attempted": _p("Attempted", "checkbox"),
    "correct": _p("Correct", "checkbox"),
    "marks_awarded": _p("Marks Awarded", "number"),
    "marks_lost": _p("Marks Lost", "number"),
    "time_min": _p("Time Minutes", "number"),
    "failure_type": _p("Failure Type", "select", options=FAILURE_TYPE_OPTIONS),
    "root_cause": _p("Root Cause", "rich_text"),
    "correct_approach": _p("Correct Approach", "rich_text"),
    "reattempt_status": _p("Reattempt Status", "select", options=REATTEMPT_STATUS_OPTIONS),
    "reattempt_date": _p("Reattempt Date", "date"),
})

DOUBT_ATTEMPT_PROPERTIES = _with_human_names({
    "title": _p("Attempt", "title", required=True),
    "doubt": _p("Doubt", "relation", relates_to="doubts"),
    "attempt_no": _p("Attempt Number", "number"),
    "attempted_at": _p("Attempted At", "date"),
    "duration_min": _p("Duration Minutes", "number"),
    "approach": _p("Approach Tried", "rich_text"),
    "stuck_point": _p("Stuck Point", "rich_text"),
    "outcome": _p("Outcome", "select", options=ATTEMPT_OUTCOME_OPTIONS),
    "valid": _p("Valid Attempt", "checkbox"),
    "ledger_entry": _p("Ledger Entry", "relation", relates_to="ledger"),
})

TIMETABLE_PROPERTIES = _with_human_names({
    "title": _p("Timetable Entry", "title", required=True),
    "weekday": _p("Weekday", "select", options=WEEKDAY_OPTIONS),
    "start_time": _p("Start Time", "rich_text"),
    "end_time": _p("End Time", "rich_text"),
    "subject": _p("Subject", "select", options=SUBJECT_OPTIONS),
    "teacher": _p("Teacher", "rich_text"),
    "kind": _p("Kind", "select", options=TIMETABLE_KIND_OPTIONS),
    "location": _p("Location", "rich_text"),
    "effective_from": _p("Effective From", "date"),
    "effective_to": _p("Effective To", "date"),
    "active": _p("Active", "checkbox"),
})

DAILY_PLAN_PROPERTIES = _with_human_names({
    "title": _p("Plan Item", "title", required=True),
    "plan_date": _p("Plan Date", "date"),
    "sequence": _p("Sequence", "number"),
    "work_item": _p("Work Item", "relation", relates_to="work_items"),
    "subject": _p("Subject", "select", options=SUBJECT_OPTIONS),
    "kind": _p("Kind", "select", options=WORK_KIND_OPTIONS),
    "expected_cy": _p("Expected CY", "number"),
    "estimated_min": _p("Estimated Minutes", "number"),
    "status": _p("Status", "select", options=PLAN_STATUS_OPTIONS),
    "priority": _p("Priority", "number"),
    "interruptible": _p("Interruptible", "checkbox"),
    "exit_condition": _p("Exit Condition", "rich_text"),
    "planner_note": _p("Planner Note", "rich_text"),
})

# Stable client-generated key used to make create/retry idempotent after an
# ambiguous network timeout. It is intentionally writable but never model-owned.
for _props in (
    LEDGER_PROPERTIES, DOUBTS_PROPERTIES, REVISION_PROPERTIES,
    WORK_ITEM_PROPERTIES, GOAL_PROPERTIES, EXAM_PROPERTIES,
    EXAM_QUESTION_PROPERTIES, DOUBT_ATTEMPT_PROPERTIES,
    TIMETABLE_PROPERTIES, DAILY_PLAN_PROPERTIES,
):
    _props["operation_id"] = {
        "notion_name": "Operation ID",
        "human_name": "operation_id",
        "type": "rich_text",
        "required": False,
        "read_only": False,
        "options": None,
        "notes": "Internal idempotency key; never supplied by the language model.",
    }


# ---------------------------------------------------------------------------
# Convenience access
# ---------------------------------------------------------------------------

PROPERTIES_BY_DB: dict[str, dict[str, dict[str, Any]]] = {
    "ledger": LEDGER_PROPERTIES,
    "doubts": DOUBTS_PROPERTIES,
    "revision": REVISION_PROPERTIES,
    "work_items": WORK_ITEM_PROPERTIES,
    "goals": GOAL_PROPERTIES,
    "exams": EXAM_PROPERTIES,
    "exam_questions": EXAM_QUESTION_PROPERTIES,
    "doubt_attempts": DOUBT_ATTEMPT_PROPERTIES,
    "timetable": TIMETABLE_PROPERTIES,
    "daily_plan": DAILY_PLAN_PROPERTIES,
}


def writable_properties(db_key: str) -> dict[str, dict[str, Any]]:
    """Return only the writable (non-read-only) properties for a DB."""
    return {
        k: v for k, v in PROPERTIES_BY_DB[db_key].items() if not v["read_only"]
    }


def required_properties(db_key: str) -> dict[str, dict[str, Any]]:
    """Return only the Notion-required-on-create properties for a DB."""
    return {
        k: v
        for k, v in PROPERTIES_BY_DB[db_key].items()
        if v["required"] and not v["read_only"]
    }


def select_option_lists(db_key: str) -> dict[str, list[str]]:
    """Return {human_name: options} for every select/status property in a DB."""
    return {
        k: v["options"]
        for k, v in PROPERTIES_BY_DB[db_key].items()
        if v["type"] in ("select", "status") and v["options"]
    }


def is_db_id_resolved(db_key: str) -> bool:
    """True once Phase 2 has filled in the real database_id for this DB."""
    return DATABASES[db_key]["database_id"] is not None


def assert_all_db_ids_resolved() -> None:
    """Raise if any DB ID is still unresolved (i.e. Phase 2 not done yet)."""
    missing = [k for k in DATABASES if not is_db_id_resolved(k)]
    if missing:
        raise RuntimeError(
            f"Database IDs not yet resolved for: {missing}. "
            f"Run the Phase 2 ID resolver (config/resolve_db_ids.py) with a "
            f"valid Notion integration token first."
        )
