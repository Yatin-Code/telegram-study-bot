"""Central action registry for every Telegram command and inline button.

The LLM receives this registry so it can choose which actions to show per
context instead of every screen showing the same static buttons.  Each action
has a stable key, a human label, a callback_data prefix, and context tags that
describe when the action is most useful.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import bot_identity


@dataclass(frozen=True)
class Action:
    key: str
    label: str
    description: str
    kind: str  # "command" | "callback" | "message"
    callback_prefix: str | None = None
    command: str | None = None
    tags: tuple[str, ...] = ()
    contexts: tuple[str, ...] = ()  # when this action is most useful


ACTIONS: tuple[Action, ...] = (
    # ── Core navigation ──
    Action("help", "Help", "Show the help menu with all commands", "command",
           command="help", tags=("navigation", "always"),
           contexts=("any",)),
    Action("start", "Start", "Start the bot", "command",
           command="start", tags=("navigation",),
           contexts=("first_contact",)),
    Action("setup", "Setup", "First-run setup or fill data gaps", "command",
           command="setup", tags=("navigation", "onboarding"),
           contexts=("fresh_install", "incomplete_setup")),
    Action("newsession", "New Session", "Clear current study context", "command",
           command="newsession", tags=("context",),
           contexts=("session_end",)),

    # ── Data inspection ──
    Action("settings", "Settings", "View & edit bot settings", "command",
           command="settings", tags=("config",),
           contexts=("any",)),
    Action("memory", "Memory", "See & edit remembered context", "command",
           command="memory", tags=("config", "context"),
           contexts=("any",)),
    Action("inspect", "Inspect", "Inspect SQLite/Notion/context state", "command",
           command="inspect", tags=("debug",),
           contexts=("debugging",)),
    Action("health", "Health", "Show bot & mirror health status", "command",
           command="health", tags=("debug",),
           contexts=("debugging",)),
    Action("sync", "Sync", "Force-refresh data from Notion", "command",
           command="sync", tags=("data",),
           contexts=("stale_data",)),

    # ── Planning & daily ops ──
    Action("today", "Today", "Analyze today's study plan", "command",
           command="today", tags=("planning", "daily"),
           contexts=("morning", "planning_time", "before_study")),
    Action("next", "Next", "Show the next sequence item", "command",
           command="next", tags=("planning", "daily"),
           contexts=("during_study", "block_start")),
    Action("backlog", "Backlog", "Show or add prioritized backlog", "command",
           command="backlog", tags=("planning",),
           contexts=("planning_time", "end_of_day")),
    Action("weekly", "Weekly", "Show the weekly growth report", "command",
           command="weekly", tags=("reporting", "weekly"),
           contexts=("weekend", "weekly_review")),

    # ── Goals & commitments ──
    Action("goal", "Goal", "Create or list measurable goals", "command",
           command="goal", tags=("commitments",),
           contexts=("planning_time", "setup")),
    Action("remember", "Remember", "Remember a commitment or preference", "command",
           command="remember", tags=("commitments",),
           contexts=("any",)),
    Action("forget", "Forget", "Forget a remembered item", "command",
           command="forget", tags=("commitments",),
           contexts=("any",)),

    # ── Exams ──
    Action("exam", "Exam", "Create or list exams", "command",
           command="exam", tags=("exams",),
           contexts=("setup", "exam_season")),
    Action("readiness", "Readiness", "Audit evidence for an upcoming exam", "command",
           command="readiness", tags=("exams", "reporting"),
           contexts=("exam_season", "pre_exam")),
    Action("finish_exam", "Finish Exam", "Start post-exam full-paper review", "command",
           command="finish_exam", tags=("exams",),
           contexts=("post_exam",)),
    Action("exam_summary", "Exam Summary", "Record an exam result summary", "command",
           command="exam_summary", tags=("exams",),
           contexts=("post_exam",)),
    Action("question_review", "Question Review", "Record one exam mistake", "command",
           command="question_review", tags=("exams",),
           contexts=("post_exam",)),
    Action("complete_exam_analysis", "Complete Analysis", "Close an exam analysis", "command",
           command="complete_exam_analysis", tags=("exams",),
           contexts=("post_exam",)),

    # ── Doubts ──
    Action("doubts", "Doubts", "Show teacher-ready doubts", "command",
           command="doubts", tags=("doubts",),
           contexts=("teacher_window", "doubt_session")),
    Action("attempt", "Attempt", "Record a doubt attempt", "command",
           command="attempt", tags=("doubts",),
           contexts=("doubt_session",)),
    Action("dismissdoubt", "Dismiss Doubt", "Dismiss a doubt with a reason", "command",
           command="dismissdoubt", tags=("doubts",),
           contexts=("doubt_session",)),
    Action("resolvedoubt", "Resolve Doubt", "Record a doubt resolution", "command",
           command="resolvedoubt", tags=("doubts",),
           contexts=("doubt_session", "teacher_window")),
    Action("reopendoubt", "Reopen Doubt", "Reopen a resolved doubt", "command",
           command="reopendoubt", tags=("doubts",),
           contexts=("doubt_session",)),

    # ── Timetable ──
    Action("timetable", "Timetable", "Show or edit the teacher timetable", "command",
           command="timetable", tags=("config",),
           contexts=("setup", "teacher_window")),

    # ── Analysis ──
    Action("weak", "Weak Points", "Show evidence-backed weak points", "command",
           command="weak", tags=("reporting",),
           contexts=("weekly_review", "exam_season")),

    # ── Jobs ──
    Action("jobs", "Jobs", "Create & manage scheduled jobs", "command",
           command="jobs", tags=("config",),
           contexts=("any",)),

    # ── Reset ──
    Action("reset", "Reset", "Guarded reset of pages, data, or context", "command",
           command="reset", tags=("danger",),
           contexts=("debugging",)),

    # ── Bug reporting ──
    Action("bug", "Bug", "Note something that went wrong", "command",
           command="bug", tags=("debug",),
           contexts=("debugging",)),
    Action("bugs", "Bugs", "List or close open bug notes", "command",
           command="bugs", tags=("debug",),
           contexts=("debugging",)),

    # ── Inline callback actions (not commands, but the AI should know about them) ──
    Action("log_confirm", "Confirm", "Confirm a pending log entry", "callback",
           callback_prefix="log:confirm", tags=("inline",),
           contexts=("pending_draft",)),
    Action("log_edit", "Edit", "Edit a field on a pending log entry", "callback",
           callback_prefix="log:edit", tags=("inline",),
           contexts=("pending_draft",)),
    Action("log_cancel", "Cancel", "Cancel a pending log entry", "callback",
           callback_prefix="log:cancel", tags=("inline",),
           contexts=("pending_draft",)),
    Action("domain_confirm", "Confirm", "Confirm a domain action (goal/exam/commitment)", "callback",
           callback_prefix="domain:confirm", tags=("inline",),
           contexts=("pending_draft",)),
    Action("domain_cancel", "Cancel", "Cancel a domain action", "callback",
           callback_prefix="domain:cancel", tags=("inline",),
           contexts=("pending_draft",)),
    Action("plan_done", "Complete", "Mark the active plan item as done", "callback",
           callback_prefix="plan:done", tags=("inline", "planning"),
           contexts=("active_plan",)),
    Action("plan_carry", "Carry Backlog", "Move remaining work to backlog", "callback",
           callback_prefix="plan:carry", tags=("inline", "planning"),
           contexts=("active_plan",)),
    Action("debrief_skip", "Skip", "Skip the debrief stage", "callback",
           callback_prefix="debrief:done", tags=("inline",),
           contexts=("debrief",)),
    Action("debrief_next", "Next", "Move to the next debrief stage", "callback",
           callback_prefix="debrief:next", tags=("inline",),
           contexts=("debrief",)),
    Action("onb_skip", "Skip", "Skip the current setup section", "callback",
           callback_prefix="onb:skip", tags=("inline", "onboarding"),
           contexts=("onboarding",)),
    Action("onb_done", "Done", "Finish the current loop section", "callback",
           callback_prefix="onb:done", tags=("inline", "onboarding"),
           contexts=("onboarding",)),
    Action("onb_hub", "Hub", "Return to the setup hub", "callback",
           callback_prefix="onb:hub", tags=("inline", "onboarding"),
           contexts=("onboarding",)),
    Action("onb_back", "Back", "Go back one setup step", "callback",
           callback_prefix="onb:back", tags=("inline", "onboarding"),
           contexts=("onboarding",)),
    Action("onb_runall", "Run Full Setup", "Run all setup sections in order", "callback",
           callback_prefix="onb:runall", tags=("inline", "onboarding"),
           contexts=("onboarding",)),
    Action("onb_finish", "Finish Setup", "Mark setup as complete", "callback",
           callback_prefix="onb:finish", tags=("inline", "onboarding"),
           contexts=("onboarding",)),
    Action("ready_open", "Open", "Mark a readiness item as open", "callback",
           callback_prefix="ready:open", tags=("inline", "exams"),
           contexts=("readiness_audit",)),
    Action("ready_solve", "Solve", "Start solving a readiness doubt", "callback",
           callback_prefix="ready:solve", tags=("inline", "exams"),
           contexts=("readiness_audit",)),
    Action("ready_exclude", "Not Here", "Exclude an item from the exam", "callback",
           callback_prefix="ready:exclude", tags=("inline", "exams"),
           contexts=("readiness_audit",)),
    Action("ready_refresh", "Refresh", "Refresh the readiness audit", "callback",
           callback_prefix="ready:refresh", tags=("inline", "exams"),
           contexts=("readiness_audit",)),
)


def by_key(key: str) -> Action | None:
    return next((a for a in ACTIONS if a.key == key), None)


def by_command(command: str) -> Action | None:
    return next((a for a in ACTIONS if a.command == command), None)


def by_tag(tag: str) -> tuple[Action, ...]:
    return tuple(a for a in ACTIONS if tag in a.tags)


def for_context(context: str) -> tuple[Action, ...]:
    return tuple(a for a in ACTIONS if context in a.contexts or "any" in a.contexts)


def commands_only() -> tuple[Action, ...]:
    return tuple(a for a in ACTIONS if a.kind == "command")


def catalog_text(*, context: str | None = None) -> str:
    items = for_context(context) if context else commands_only()
    return "\n".join(
        f"/{a.command} — {a.description}" if a.command
        else f"[{a.callback_prefix}] — {a.description}"
        for a in items
    )


def context_actions_prompt(context: str) -> str:
    """Return a prompt block listing the most useful actions for a context."""
    items = for_context(context)
    if not items:
        items = commands_only()
    lines = [f"Most useful actions for context '{context}':"]
    for a in items:
        if a.command:
            lines.append(f"  /{a.command} — {a.description}")
        else:
            lines.append(f"  [{a.key}] — {a.description}")
    return "\n".join(lines)


def identity_with_actions(*, role: str, context: str | None = None) -> str:
    """Return the identity prompt augmented with the action registry."""
    base = bot_identity.identity_prompt(role=role)
    actions_block = context_actions_prompt(context) if context else ""
    return f"{base}\n\n{actions_block}" if actions_block else base
