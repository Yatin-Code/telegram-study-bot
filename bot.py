"""
Phase 4 Telegram bot skeleton.

Long polling is used for v1 deployment simplicity. This phase intentionally
only acknowledges messages; intent parsing and write/query flows start later.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import logging.handlers
import re
import uuid
from pathlib import Path

from telegram import (
    BotCommand,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import briefing
import draft_store
import logging_flow
import operational_store
import query_flow
import session_context
import sync
import study_domain
import domain_parser
import planner
import reminders
from config import settings as config_settings
from zoneinfo import ZoneInfo
from config import notion_schema
from config.settings import telegram_allowed_user_id, telegram_bot_token
from intent_parser import IntentParseError, parse_message


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_FILE = LOG_DIR / "bot.log"
logger = logging.getLogger(__name__)

BOT_COMMANDS = [
    BotCommand("start", "Start the bot"),
    BotCommand("help", "Get help menu"),
    BotCommand("newsession", "Clear current study context"),
    BotCommand("settings", "Adjust preferences"),
    BotCommand("health", "Show bot & mirror health status"),
    BotCommand("goal", "Create or list measurable goals"),
    BotCommand("exam", "Create or list exams"),
    BotCommand("today", "Analyze today's Notion plan"),
    BotCommand("next", "Show the next sequence item"),
    BotCommand("backlog", "Show prioritized backlog"),
    BotCommand("attempt", "Record a doubt attempt"),
    BotCommand("doubts", "Show teacher-ready doubts"),
    BotCommand("dismissdoubt", "Dismiss a doubt with a reason"),
    BotCommand("resolvedoubt", "Record a doubt resolution"),
    BotCommand("reopendoubt", "Reopen a resolved doubt"),
    BotCommand("timetable", "Show teacher timetable"),
    BotCommand("weak", "Show evidence-backed weak points"),
    BotCommand("weekly", "Show weekly growth report"),
    BotCommand("finish_exam", "Start post-exam full-paper review"),
    BotCommand("exam_summary", "Record an exam result summary"),
    BotCommand("question_review", "Record one exam mistake"),
    BotCommand("complete_exam_analysis", "Close an exam analysis"),
]


def _format_context(ctx: dict | None) -> str:
    if not ctx:
        return "none set"
    bits = [f"{k}={ctx[k]}" for k in session_context.CONTEXT_KEYS if ctx.get(k)]
    return ", ".join(bits) if bits else "none set"


def _is_allowed(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id == telegram_allowed_user_id())


async def _reject_if_unauthorized(update: Update) -> bool:
    if _is_allowed(update):
        return False
    user = update.effective_user
    logger.warning("Ignoring unauthorized user_id=%s", user.id if user else None)
    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    await update.effective_message.reply_text(
        "📚 Study Logger Bot is running.\n\n"
        "Tell me what you're studying or logging:\n"
        "• \"starting EB-1 physics kinematics\" — set context\n"
        "• \"execution block 1: solved ex 2A, 20 qs 15 correct, 25 mins\" — log a session\n"
        "• \"doubt: sign of relative velocity\" — log a doubt (inherits context)\n"
        "• \"list doubts\" / \"revision overdue\" — query\n\n"
        "Commands: /help, /newsession, /health"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    await update.effective_message.reply_text(
        "Commands:\n"
        "/start - Start the bot\n"
        "/help - Get help menu\n"
        "/newsession - Clear current study context\n"
        "/settings - Adjust preferences\n\n"
        "Tell me what you're studying (e.g. \"starting EB-1 physics kinematics\") "
        "and I'll remember it for logging until midnight.\n\n"
        "/goal 300 CY daily\n"
        "/exam JEE Main mock on 2026-08-15\n"
        "/today /next /backlog /weak /weekly\n"
        "/backlog add title | kind | subject | due date | priority | minutes\n"
        "/timetable add title | weekday | start | end | kind | subject | teacher\n"
        "/attempt doubt title | minutes | approach | stuck point | outcome\n"
        "/resolvedoubt doubt title | resolution | teacher(optional)\n"
        "/finish_exam exam name\n"
        "/exam_summary exam | marks | attempted | correct | incorrect | unattempted\n"
        "/question_review exam | qno | subject | chapter | failure type | root cause"
    )


async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    from config import settings as cfg

    chat_id = update.effective_chat.id
    ctx = session_context.get_context(chat_id)
    if ctx:
        ctx_bits = ", ".join(
            f"{k}={v}" for k in ("subject", "chapter", "block", "exercise")
            if (v := ctx.get(k))
        )
        ctx_line = ctx_bits or "set (no fields)"
    else:
        ctx_line = "none (cleared at local midnight)"

    fallbacks = cfg.llm_fallback_models()
    lines = [
        "*Settings* (read-only)",
        "",
        f"LLM provider: `{cfg.llm_provider()}`",
        f"Model: `{cfg.llm_model()}`",
        f"Fallback: `{', '.join(fallbacks) if fallbacks else 'none'}`",
        f"Timezone: `{cfg.user_timezone()}`  (now {session_context.local_now():%H:%M})",
        f"Planning reminder: `{cfg.planning_reminder_time()}`",
        f"Weekly report: `{cfg.weekly_report_time()}`",
        f"Daily CY range: `{cfg.daily_cy_baseline()}–{cfg.daily_cy_ceiling()}`",
        f"Current session context: {ctx_line}",
        "",
        "Preferences live in `.env`. Use /newsession to clear session context.",
    ]
    await update.effective_message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.MARKDOWN
    )


async def newsession(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    chat_id = update.effective_chat.id
    session_context.clear_context(chat_id)
    await update.effective_message.reply_text("Session context cleared.")


async def health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    import sqlite3
    import time as _time
    db = Path(__file__).resolve().parent / "sqlite_mirror.db"
    try:
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        counts = {t: conn.execute(f'SELECT COUNT(*) AS n FROM "{t}" WHERE archived=0').fetchone()["n"]
                  for t in ("ledger", "doubts", "revision", "daily_plan")}
        meta = conn.execute(
            "SELECT db_key, last_completed_at FROM sync_meta ORDER BY last_completed_at DESC LIMIT 1"
        ).fetchone()
        last_sync = meta["last_completed_at"] if meta else "never"
        conn.close()
    except Exception as e:
        await update.effective_message.reply_text(f"⚠️ Health check failed: {e}")
        return
    operations = operational_store.health(db)
    ctx = session_context.get_context(update.effective_chat.id)
    lines = [
        "🤖 *Bot Health*",
        f"• Notion mirror: ledger={counts['ledger']}, doubts={counts['doubts']}, revision={counts['revision']}, plan={counts['daily_plan']}",
        f"• SQLite: {operations['integrity']}, schema v{operations['schema_version']}, goals={operations['counts']['goals']}, backlog={operations['counts']['work_items']}",
        f"• Last sync: {last_sync}",
        f"• Context: {_format_context(ctx)}",
        f"• Pending writes: {logging_flow.pending_count()}",
    ]
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


def _command_args(update: Update) -> str:
    text = update.effective_message.text or ""
    return text.split(" ", 1)[1].strip() if " " in text else ""


async def _sync_domain() -> None:
    await asyncio.to_thread(
        sync.sync_once,
        db_keys=sync.NOTION_SOURCE_KEYS,
    )


async def goal_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    text = _command_args(update)
    status_match = re.match(r"^(pause|resume|complete|cancel)\s+(.+)$", text, re.IGNORECASE)
    if status_match:
        action, name = status_match.groups()
        status = {
            "pause": "Paused", "resume": "Active",
            "complete": "Completed", "cancel": "Cancelled",
        }[action.lower()]
        try:
            await _sync_domain()
            await asyncio.to_thread(study_domain.update_goal_status, name, status)
            await update.effective_message.reply_text(f"Goal is now {status}.")
        except Exception as exc:
            await update.effective_message.reply_text(f"Goal was not changed: {exc}")
        return
    if not text:
        await _sync_domain()
        rows = study_domain._rows("goals", "archived=0 AND status IN ('Active','Draft')")
        if not rows:
            await update.effective_message.reply_text("No active goals yet. Try /goal 300 CY daily")
            return
        await update.effective_message.reply_text("\n".join(
            f"{i}. {r.get('title')} — {r.get('target')} {r.get('metric')} ({r.get('period')})"
            for i, r in enumerate(rows, 1)
        ))
        return
    try:
        data = await asyncio.to_thread(domain_parser.parse_goal, text)
        if data.get("needs_clarification"):
            await update.effective_message.reply_text(data.get("clarification_question") or "What target should this goal use?")
            return
        data["operation_id"] = uuid.uuid4().hex
        preview = (
            "Goal draft\n"
            f"Title: {data['title']}\nType: {data['goal_type']}\n"
            f"Target: {data['target']}\nPeriod: {data['period']}\n"
            f"Deadline: {data.get('deadline') or 'none'}\n"
            "Confirm this goal?"
        )
        draft_id = draft_store.create_draft(
            update.effective_chat.id,
            {"kind": "goal", "data": data}, [preview],
        )
        await update.effective_message.reply_text(
            preview,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Confirm", callback_data=f"domain:confirm:{draft_id}"),
                InlineKeyboardButton("Cancel", callback_data=f"domain:cancel:{draft_id}"),
            ]]),
        )
    except Exception as exc:
        logger.exception("goal command failed")
        await update.effective_message.reply_text(f"I could not create that goal safely: {exc}")


async def exam_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    text = _command_args(update)
    if not text:
        await _sync_domain()
        rows = study_domain._rows("exams", "archived=0 AND status NOT IN ('Cancelled','Analysed')")
        await update.effective_message.reply_text("\n".join(
            f"{i}. {r.get('title')} — {str(r.get('exam_date') or '')[:16]} [{r.get('status')}]"
            for i, r in enumerate(rows, 1)
        ) or "No exams recorded. Try /exam JEE Main mock on 2026-08-15")
        return
    try:
        data = await asyncio.to_thread(domain_parser.parse_exam, text)
        if data.get("needs_clarification"):
            await update.effective_message.reply_text(data.get("clarification_question") or "What is the exam date?")
            return
        data["operation_id"] = uuid.uuid4().hex
        preview = (
            "Exam draft\n"
            f"{data['title']}\nKind: {data['kind']}\nDate: {data['exam_date']}\n"
            f"Date status: {data['date_confidence']}\nTarget: {data.get('target_marks') or 'not set'}\n"
            "Confirm this exam?"
        )
        draft_id = draft_store.create_draft(
            update.effective_chat.id, {"kind": "exam", "data": data}, [preview]
        )
        await update.effective_message.reply_text(
            preview,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Confirm", callback_data=f"domain:confirm:{draft_id}"),
                InlineKeyboardButton("Cancel", callback_data=f"domain:cancel:{draft_id}"),
            ]]),
        )
    except Exception as exc:
        logger.exception("exam command failed")
        await update.effective_message.reply_text(f"I could not create that exam safely: {exc}")


async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    try:
        await _sync_domain()
        facts = await asyncio.to_thread(planner.analyze)
        lines = [f"Plan {facts['plan_date']}: {facts['outcome']}",
                 f"Expected CY: {facts['expected_cy']:g}",
                 f"Adaptive target: {facts['pace']['target']} ({facts['pace']['phase']})",
                 f"Planned minutes: {facts['planned_minutes']:g}",
                 f"CY headroom: {facts['capacity_headroom_cy']:g}",
                 f"Coaching homework: {facts['homework_planned_count']}/{facts['homework_pending_count']} planned",
                 f"Backlog: {facts['backlog_count']} tracked, {facts['unplanned_backlog_count']} unplanned"]
        for row in facts["items"]:
            lines.append(f"{int(row.get('sequence') or 0)}. {row.get('title')} [{row.get('status') or 'Planned'}]")
        lines.extend(f"Warning: {w}" for w in facts["warnings"])
        lines.extend(f"Blocked: {e}" for e in facts["errors"])
        lines.extend(
            f"Suggestion: {s.get('action')} — {s.get('reason')}"
            for s in facts.get("suggestions", [])
        )
        await update.effective_message.reply_text("\n".join(lines) or "No Notion plan items found for today.")
    except Exception as exc:
        await update.effective_message.reply_text(f"Plan analysis unavailable: {exc}")


async def next_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    try:
        await _sync_domain()
        row = await asyncio.to_thread(study_domain.activate_next_plan, update.effective_chat.id)
        if not row:
            await update.effective_message.reply_text("No unfinished sequence item is recorded for today.")
            return
        await update.effective_message.reply_text(
            f"Next: {row.get('title')}\nExit condition: {row.get('exit_condition') or 'not specified'}\n"
            f"Priority: {row.get('priority') or 'unset'}\n"
            "This item is now Active. After the block choose Complete or Carry Backlog."
        )
    except Exception as exc:
        await update.effective_message.reply_text(f"Next-item lookup unavailable: {exc}")


async def backlog_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    text = _command_args(update)
    action_match = re.match(r"^(done|dismiss)\s+(.+)$", text, re.IGNORECASE)
    if action_match:
        action, title = action_match.groups()
        try:
            status = "Completed" if action.lower() == "done" else "Dismissed"
            await asyncio.to_thread(study_domain.update_work_item_status, title, status)
            await update.effective_message.reply_text(f"Work item is now {status}.")
        except Exception as exc:
            await update.effective_message.reply_text(f"Work item was not changed: {exc}")
        return
    if text.lower().startswith("add "):
        parts = [part.strip() for part in text[4:].split("|")]
        if not parts or not parts[0]:
            await update.effective_message.reply_text(
                "Format: /backlog add title | kind | subject | due date | priority | estimated minutes"
            )
            return
        fields = ("title", "kind", "subject", "due_date", "priority", "estimated_min")
        data = {name: parts[index] for index, name in enumerate(fields) if index < len(parts) and parts[index]}
        data.update({"status": "Backlog", "operation_id": uuid.uuid4().hex})
        try:
            await asyncio.to_thread(study_domain.create_work_item, data)
            await update.effective_message.reply_text("Backlog item saved to SQLite.")
        except Exception as exc:
            await update.effective_message.reply_text(f"Backlog item was rejected: {exc}")
        return
    rows = study_domain._rows("work_items", "archived=0 AND status IN ('Backlog','Inbox')")
    rows.sort(key=lambda r: (-int(r.get("priority") or 0), str(r.get("due_date") or "9999")))
    await update.effective_message.reply_text("\n".join(
        f"{i}. {r.get('title')} — p{r.get('priority') or 0}, due {str(r.get('due_date') or 'none')[:10]}"
        for i, r in enumerate(rows[:30], 1)
    ) or "Backlog is empty.")


async def doubts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    await _sync_domain()
    rows = study_domain.eligible_doubts()
    await update.effective_message.reply_text("\n".join(
        f"{i}. {r.get('core_concept')} — {r.get('valid_attempts')} attempts"
        for i, r in enumerate(rows, 1)
    ) or "No doubts are teacher-ready. Every unresolved doubt needs two valid attempts first.")


async def attempt_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    parts = [p.strip() for p in _command_args(update).split("|")]
    if len(parts) < 4:
        await update.effective_message.reply_text(
            "Format: /attempt doubt title | minutes | approach tried | exact stuck point | outcome(optional)"
        )
        return
    try:
        result = await asyncio.to_thread(
            study_domain.record_doubt_attempt,
            parts[0], duration_min=parts[1], approach=parts[2], stuck_point=parts[3],
            outcome=parts[4] if len(parts) > 4 and parts[4] else "Unsolved",
        )
        await update.effective_message.reply_text(
            f"Attempt {result['attempt_no']} recorded. Valid attempts: {result['valid_attempts']}.\n"
            f"State: {result['workflow_state']}"
        )
    except Exception as exc:
        await update.effective_message.reply_text(f"Attempt not recorded: {exc}")


async def dismiss_doubt_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    parts = [p.strip() for p in _command_args(update).split("|", 1)]
    if len(parts) != 2:
        await update.effective_message.reply_text("Format: /dismissdoubt doubt title | reason")
        return
    try:
        await asyncio.to_thread(study_domain.dismiss_doubt, parts[0], parts[1])
        await update.effective_message.reply_text("Doubt dismissed with a recorded reason.")
    except Exception as exc:
        await update.effective_message.reply_text(f"Doubt was not dismissed: {exc}")


async def resolve_doubt_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    parts = [p.strip() for p in _command_args(update).split("|")]
    if len(parts) < 2:
        await update.effective_message.reply_text(
            "Format: /resolvedoubt doubt title | resolution | teacher (optional)"
        )
        return
    try:
        teacher = len(parts) > 2 and parts[2].lower() in ("teacher", "yes", "true")
        result = await asyncio.to_thread(
            study_domain.resolve_doubt, parts[0], parts[1], teacher_asked=teacher
        )
        await update.effective_message.reply_text(f"Doubt closed as {result['workflow_state']}.")
    except Exception as exc:
        await update.effective_message.reply_text(f"Doubt was not closed: {exc}")


async def reopen_doubt_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    try:
        await asyncio.to_thread(study_domain.reopen_doubt, _command_args(update))
        await update.effective_message.reply_text("Doubt reopened; continue the two-attempt protocol.")
    except Exception as exc:
        await update.effective_message.reply_text(f"Doubt was not reopened: {exc}")


async def timetable_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    text = _command_args(update)
    action_match = re.match(r"^(enable|disable)\s+(.+)$", text, re.IGNORECASE)
    if action_match:
        action, title = action_match.groups()
        try:
            await asyncio.to_thread(
                study_domain.set_timetable_active, title,
                active=(action.lower() == "enable"),
            )
            await update.effective_message.reply_text(
                "Timetable entry enabled." if action.lower() == "enable" else "Timetable entry disabled."
            )
        except Exception as exc:
            await update.effective_message.reply_text(f"Timetable entry was not changed: {exc}")
        return
    if text.lower().startswith("add "):
        parts = [part.strip() for part in text[4:].split("|")]
        if len(parts) < 5:
            await update.effective_message.reply_text(
                "Format: /timetable add title | weekday | start | end | kind | subject | teacher | location"
            )
            return
        fields = ("title", "weekday", "start_time", "end_time", "kind", "subject", "teacher", "location")
        data = {name: parts[index] for index, name in enumerate(fields) if index < len(parts) and parts[index]}
        data["operation_id"] = uuid.uuid4().hex
        try:
            await asyncio.to_thread(study_domain.create_timetable_entry, data)
            await update.effective_message.reply_text("Timetable entry saved to SQLite.")
        except Exception as exc:
            await update.effective_message.reply_text(f"Timetable entry was rejected: {exc}")
        return
    rows = study_domain._rows("timetable", "archived=0 AND active=1")
    rows.sort(key=lambda r: (str(r.get("weekday")), str(r.get("start_time"))))
    await update.effective_message.reply_text("\n".join(
        f"{r.get('weekday')} {r.get('start_time')}-{r.get('end_time')} — {r.get('kind')} {r.get('subject') or ''} {r.get('teacher') or ''}"
        for r in rows
    ) or "No active timetable entries found in SQLite.")


async def weak_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    await _sync_domain()
    rows = study_domain.weak_points()
    await update.effective_message.reply_text("\n".join(
        f"{i}. {r['chapter']} [{r['confidence']}] — {r['marks_lost']:g} marks lost, "
        f"{r['mistakes']} exam mistake(s), {r['unresolved_doubts']} unresolved doubt(s), "
        f"{r['blocks']} complete block(s)"
        for i, r in enumerate(rows[:15], 1)
    ) or "Not enough reviewed exam evidence to identify weak points.")


async def weekly_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    await _sync_domain()
    report = study_domain.weekly_report()
    ledger = report["ledger"]
    attempted = ledger.get("attempted") or 0
    correct = ledger.get("correct") or 0
    accuracy = (100 * correct / attempted) if attempted else None
    lines = [
        f"Weekly growth ({report['start']} to {report['end']})",
        f"CY: {ledger.get('cy', 0):g} | blocks: {ledger.get('blocks', 0)}",
        f"CY change vs previous week: {report['cy_delta']:+g}",
        f"Accuracy: {accuracy:.1f}%" if accuracy is not None else "Accuracy: no complete data",
        f"Completed work: {report['completed_work']}",
        f"Backlog: {report['backlog']}",
        f"Valid doubt attempts: {report['valid_doubt_attempts']}",
        f"Exam mistakes reviewed: {report['exam_mistakes']['n']}",
    ]
    if report.get("top_failure"):
        lines.append(
            f"Main repair target: {report['top_failure']['failure_type']} "
            f"({report['top_failure']['n']} reviewed mistake(s))"
        )
    if report["cy_delta"] > 0:
        lines.append("Growth signal: output increased. Protect the accuracy that produced it.")
    elif ledger.get("blocks", 0):
        lines.append("Growth signal: keep the next week focused on completion and repeated-error removal.")
    else:
        lines.append("Growth signal: complete data from the next block will establish the baseline.")
    await update.effective_message.reply_text("\n".join(lines))


async def finish_exam_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    try:
        result = await asyncio.to_thread(study_domain.finish_exam, _command_args(update))
        await update.effective_message.reply_text(
            "Exam marked for analysis. The first planned task is to complete the full paper before optional work."
        )
    except Exception as exc:
        await update.effective_message.reply_text(f"Could not start paper review: {exc}")


async def exam_summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    parts = [p.strip() for p in _command_args(update).split("|")]
    if len(parts) < 2:
        await update.effective_message.reply_text("Format: /exam_summary exam | marks | attempted | correct | incorrect | unattempted")
        return
    try:
        data = {k: parts[i] for i, k in enumerate(("actual_marks", "attempted", "correct", "incorrect", "unattempted"), 1) if i < len(parts) and parts[i]}
        result = await asyncio.to_thread(study_domain.record_exam_summary, parts[0], data)
        await update.effective_message.reply_text(f"Exam summary saved for {parts[0]}. Question-level review can now be recorded.")
    except Exception as exc:
        await update.effective_message.reply_text(f"Exam summary rejected: {exc}")


async def question_review_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    parts = [p.strip() for p in _command_args(update).split("|")]
    if len(parts) < 6:
        await update.effective_message.reply_text("Format: /question_review exam | qno | subject | chapter | failure type | root cause")
        return
    try:
        data = {"question_no": parts[1], "subject": parts[2] or None, "chapter": parts[3] or None, "failure_type": parts[4] or None, "root_cause": parts[5], "attempted": True, "correct": False}
        await asyncio.to_thread(study_domain.record_question_review, parts[0], data)
        await update.effective_message.reply_text("Question mistake recorded for future reattempt and weak-point analysis.")
    except Exception as exc:
        await update.effective_message.reply_text(f"Question review rejected: {exc}")


async def complete_exam_analysis_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    try:
        result = await asyncio.to_thread(study_domain.complete_exam_analysis, _command_args(update))
        await update.effective_message.reply_text(f"Exam analysis closed after {result['questions_reviewed']} question reviews.")
    except Exception as exc:
        await update.effective_message.reply_text(f"Analysis cannot close yet: {exc}")


async def on_domain_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not _is_allowed(update):
        return
    try:
        _, action, draft_id = query.data.split(":", 2)
    except ValueError:
        return
    draft = draft_store.get_draft(draft_id)
    if draft is None:
        await query.edit_message_text("This draft expired. Send the command again.")
        return
    if action == "cancel":
        draft_store.delete_draft(draft_id)
        await query.edit_message_text("Cancelled. Nothing was saved.")
        return
    if action != "confirm":
        return
    try:
        kind = draft["payload"].get("kind")
        data = draft["payload"].get("data") or {}
        if kind == "goal":
            await asyncio.to_thread(study_domain.create_goal, data)
            message = "Goal saved to SQLite."
        elif kind == "exam":
            await asyncio.to_thread(study_domain.create_exam, data)
            message = "Exam saved to SQLite."
        else:
            raise study_domain.DomainError("unknown domain draft")
        draft_store.delete_draft(draft_id)
        await query.edit_message_text(message)
    except Exception as exc:
        logger.exception("domain draft commit failed")
        await query.edit_message_text(
            f"Nothing was confirmed: {exc}. Retry this same idempotent draft?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Retry", callback_data=f"domain:confirm:{draft_id}"),
                InlineKeyboardButton("Cancel", callback_data=f"domain:cancel:{draft_id}"),
            ]]),
        )


async def on_plan_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not _is_allowed(update):
        return
    try:
        _, action = query.data.split(":", 1)
        result = await asyncio.to_thread(
            study_domain.complete_active_plan,
            update.effective_chat.id,
            carry_to_backlog=(action == "carry"),
        )
        await query.edit_message_text(
            "Plan item completed." if action == "done" else "Remaining work moved to backlog."
        )
    except Exception as exc:
        await query.edit_message_text(f"Plan state was not changed: {exc}")


async def catch_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    message = update.effective_message
    chat_id = update.effective_chat.id if update.effective_chat else None
    text = message.text if message else None
    logger.info(
        "received chat_id=%s user_id=%s text=%r",
        chat_id,
        update.effective_user.id if update.effective_user else None,
        text,
    )
    if not text:
        await message.reply_text("received")
        return

    # Gap 3: if a field-edit is pending for this chat, apply the new value.
    editing = draft_store.get_editing_draft_for_chat(chat_id)
    if editing:
        await _apply_field_edit(
            message, editing["draft_id"], editing["editing_field"], editing, text
        )
        return

    # Gap 5: if a clarification was pending, combine the original message +
    # question + this reply into a single parse so the LLM can resolve it.
    clarification = draft_store.get_pending_clarification(chat_id)
    if clarification:
        combined = (
            f"[Original message: {clarification['original_message']}]\n"
            f"[Bot asked: {clarification['question']}]\n"
            f"[User replied: {text}]"
        )
        parse_text = combined
    else:
        parse_text = text

    stored = session_context.context_for_parser(chat_id)
    try:
        intent = await asyncio.to_thread(
            parse_message, parse_text, session_context=stored
        )
    except IntentParseError:
        logger.exception("intent parse failed chat_id=%s", chat_id)
        await message.reply_text(
            "Sorry, I couldn't understand that. Try rephrasing?"
        )
        return

    if intent.needs_clarification:
        q = intent.clarification_question or "Could you clarify that?"
        draft_store.set_pending_clarification(
            chat_id,
            original_message=clarification["original_message"] if clarification else text,
            question=q,
        )
        # Parser-level clarifications are open-ended (no candidate list) — use
        # force-reply so the next message is unambiguously an answer.
        await message.reply_text(q, reply_markup=ForceReply(selective=True))
        return

    if intent.action == "set_context":
        f = intent.filters
        exercise = getattr(f, "exercise", None)
        if exercise:
            options = notion_schema.PROPERTIES_BY_DB["ledger"]["exercise_type"]["options"]
            exercise = logging_flow.normalise_option(exercise, options) or exercise
        ctx = session_context.set_context(
            chat_id, subject=f.subject, chapter=f.chapter, block=f.block,
            exercise=exercise,
        )
        await message.reply_text(
            "Context set.\n\n" + briefing.build_briefing(ctx),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if intent.action.startswith("log_"):
        original = clarification["original_message"] if clarification else text
        await _handle_log(
            update, context, intent, chat_id,
            original_message=original,
            first_round=clarification is None,
        )
        return

    if intent.action in ("query", "ask"):
        # Route both structured queries and free-form questions through the
        # LLM SQL loop — it can answer anything the mirror knows, including
        # aggregations, date ranges, and computed columns. Falls back to the
        # hand-coded query_flow only when the LLM is unreachable.
        question = text
        if intent.action == "ask":
            question = intent.fields.get("question") or text
        await _handle_question(update, question, intent=intent)
        return

    await message.reply_text("received")


async def _handle_question(update: Update, question: str, intent=None) -> None:
    """Answer a free-form question about the user's study data via the SQL loop.

    Falls back to a short apology if both the LLM and the legacy query_flow
    are unavailable.
    """
    message = update.effective_message
    try:
        import sql_query_flow
        answer = await asyncio.to_thread(sql_query_flow.answer_question, question)
        if answer.startswith("⚠️") and intent is not None and intent.action == "query":
            result = await asyncio.to_thread(query_flow.run_query, intent)
            answer = query_flow.format_result(result)
        await message.reply_text(
            answer, disable_web_page_preview=True
        )
    except Exception:
        logger.exception("SQL query loop failed for question=%r", question)
        await message.reply_text(
            "⚠️ I couldn't answer that just now. Try again in a moment?"
        )


async def _handle_log(
    update, context, intent, chat_id, *,
    original_message: str | None = None,
    first_round: bool = True,
) -> None:
    """Build a write plan and present a Confirm / Edit / Cancel preview."""
    message = update.effective_message
    try:
        plan = logging_flow.build_write_plan(intent, chat_id, first_round=first_round)
    except Exception:
        logger.exception("build_write_plan failed chat_id=%s", chat_id)
        await message.reply_text("Something went wrong preparing that entry.")
        return

    if plan.needs_clarification:
        q = plan.clarification_question or "Could you clarify that?"
        # Preserve the original message that started this flow across
        # multiple clarification rounds. If we're inside a clarification chain,
        # original_message was passed in by catch_all; otherwise it's the
        # text of this update.
        original = original_message or (message.text or "")
        # Store clarification context so the user's next reply merges back (Gap 5).
        draft_store.set_pending_clarification(chat_id, original_message=original, question=q)
        # Gap 6: closed candidate set ("Did you mean: X, Y?") -> one-time reply
        # keyboard so the user just taps an option. Open-ended questions get a
        # force-reply prompt instead. Inline keyboards are reserved for actions
        # tied to a specific message (Confirm/Edit/Cancel, field-picker).
        match = re.search(r"Did you mean:\s*(.+)\?", q)
        if match:
            candidates = [c.strip() for c in match.group(1).split(",") if c.strip()]
            if candidates:
                reply_kb = ReplyKeyboardMarkup(
                    [[c] for c in candidates[:6]],
                    one_time_keyboard=True,
                    resize_keyboard=True,
                )
                await message.reply_text(q, reply_markup=reply_kb)
                return
        # Open-ended clarification — force the reply so the next message is
        # unambiguously an answer to this question.
        await message.reply_text(q, reply_markup=ForceReply(selective=True))
        return

    payload = plan.to_payload()
    draft_id = draft_store.create_draft(
        chat_id=chat_id,
        payload=payload,
        preview_lines=plan.preview_lines,
    )
    sent = await _send_draft_preview(message, draft_id, plan.preview_lines, plan.warnings)
    draft_store.set_message_id(draft_id, getattr(sent, "message_id", None))


async def _send_draft_preview(message, draft_id: str, preview_lines: list, warnings: list | None = None):
    preview = "\n".join(preview_lines)
    if warnings:
        preview += "\n\nNote: " + "; ".join(warnings)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm", callback_data=f"log:confirm:{draft_id}"),
        InlineKeyboardButton("✏️ Edit", callback_data=f"log:edit:{draft_id}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"log:cancel:{draft_id}"),
    ]])
    return await message.reply_text(
        "Ready to log:\n\n" + preview,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


async def _apply_field_edit(message, draft_id: str, field_name: str, draft: dict, new_value: str):
    """Apply a user-supplied value to one field of a draft, then re-preview."""
    payload = draft["payload"]
    fields = payload["properties"]
    db_key = payload["db_key"]
    # Coerce numeric fields.
    from config import notion_schema
    schema = notion_schema.PROPERTIES_BY_DB.get(db_key, {})
    prop_def = schema.get(field_name)
    if prop_def and prop_def["type"] == "number":
        try:
            new_value = int(new_value) if "." not in new_value else float(new_value)
        except ValueError:
            await message.reply_text(f"That doesn't look like a number. Try again for *{field_name}*:",
                                     parse_mode=ParseMode.MARKDOWN)
            return
    # For select fields, validate against options.
    if prop_def and prop_def["type"] in ("select", "status"):
        matched = logging_flow.normalise_option(new_value, prop_def["options"])
        if matched is None:
            opts = ", ".join(prop_def["options"])
            await message.reply_text(f"Not a valid option for {field_name}. Choose one of: {opts}")
            return
        new_value = matched
    # For relation fields, re-run fuzzy resolution.
    if prop_def and prop_def["type"] == "relation" and prop_def.get("relates_to"):
        page_id, cands = logging_flow.resolve_relation(prop_def["relates_to"], new_value)
        if page_id is None:
            if cands:
                await message.reply_text(
                    f"Ambiguous — did you mean: {', '.join(cands)}? Try again:")
                return
            await message.reply_text(f"No match for '{new_value}'. Try again:")
            return
        new_value = page_id

    fields[field_name] = new_value
    payload["properties"] = fields
    # Rebuild preview via a fresh write plan.
    from intent_parser import _validate_intent
    intent = _validate_intent({
        "action": payload["action"], "database": db_key,
        "fields": fields, "filters": {},
        "needs_clarification": False, "clarification_question": None,
    })
    plan = logging_flow.build_write_plan(intent, draft["chat_id"], first_round=False)
    payload["properties"] = plan.properties
    draft_store.update_draft_payload(draft_id, payload, plan.preview_lines)
    sent = await _send_draft_preview(message, draft_id, plan.preview_lines, plan.warnings)
    draft_store.set_message_id(draft_id, getattr(sent, "message_id", None))


async def on_log_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Confirm / Edit / Cancel taps on a pending draft."""
    query = update.callback_query
    await query.answer()
    if not _is_allowed(update):
        return

    try:
        _, action, draft_id = query.data.split(":", 2)
    except ValueError:
        return

    draft = draft_store.get_draft(draft_id)
    if draft is None:
        await query.edit_message_text("This entry expired. Send it again if you still need it.")
        return

    if action == "cancel":
        draft_store.delete_draft(draft_id)
        await query.edit_message_text("Cancelled. Nothing was logged.")
        return

    if action == "edit":
        props = draft["payload"]["properties"]
        buttons = [
            [InlineKeyboardButton(name, callback_data=f"log:ef:{draft_id}:{name}")]
            for name in props
            if name not in ("task",)
        ]
        buttons.append([InlineKeyboardButton("❌ Cancel", callback_data=f"log:cancel:{draft_id}")])
        await query.edit_message_text(
            "Which field do you want to change?",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    if action == "ef":
        parts = query.data.split(":", 3)
        field_name = parts[3] if len(parts) > 3 else ""
        draft_store.set_editing_field(draft_id, field_name)
        await query.edit_message_text(
            f"Send the new value for *{field_name}*:",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if action == "confirm":
        await query.edit_message_text("Saving…")
        payload = dict(draft["payload"])
        active = None
        if payload.get("db_key") == "ledger":
            active = await asyncio.to_thread(study_domain.active_plan, draft["chat_id"])
            if active and active.get("work_item_id"):
                payload["local_work_item_id"] = active["work_item_id"]
        result = await asyncio.to_thread(
            logging_flow.commit_write, payload, do_sync=False
        )
        draft_store.delete_draft(draft_id)
        # A committed execution block ends the current stint — restart the
        # session timer so the next log gets its own elapsed time.
        if payload.get("db_key") == "ledger":
            session_context.restart_timer(draft["chat_id"])
        if result["status"] == "saved":
            keys = [draft["payload"].get("db_key")]
            if result.get("cross_page_id"):
                keys.append("doubts")
            try:
                await sync.sync_once_locked(db_keys=tuple(k for k in keys if k))
            except Exception:
                logger.exception("post-write locked sync failed")
            msg = "✅ Logged to Notion."
            if result.get("url"):
                msg += f"\n[Open entry]({result['url']})"
            if result.get("cross_url"):
                msg += f"\n↳ [Doubt cross-logged]({result['cross_url']})"
        else:
            msg = (
                "⚠️ Notion is unreachable right now — saved locally and I'll "
                "sync it automatically shortly."
            )
        markup = None
        if active:
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("Plan complete", callback_data="plan:done"),
                InlineKeyboardButton("Carry to backlog", callback_data="plan:carry"),
            ]])
        await query.edit_message_text(
            msg, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True,
            reply_markup=markup,
        )


SYNC_INTERVAL_SECONDS = 240  # 4 minutes — within the spec's 3-5 min range


async def _periodic_sync(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Background job: pull fresh data from Notion into the SQLite mirror.

    Uses sync_once_locked so it can't race with a write-triggered sync.
    """
    try:
        queued = await asyncio.to_thread(logging_flow.flush_pending, sync_after=False)
        if queued["flushed"]:
            logger.info("periodic outbox flush: %s", queued)
        counts = await sync.sync_once_locked()
        logger.info("periodic sync: %s", counts)
    except Exception:
        logger.exception("periodic sync failed")


async def _expire_drafts(context: ContextTypes.DEFAULT_TYPE) -> None:
    expired = await asyncio.to_thread(draft_store.expire_stale_drafts)
    for item in expired:
        if item.get("message_id") is None:
            continue
        try:
            await context.bot.edit_message_text(
                chat_id=item["chat_id"], message_id=item["message_id"],
                text="This entry expired. Send it again if you still need it.",
            )
        except Exception:
            logger.debug("could not edit expired draft message %s", item, exc_info=True)


async def _planning_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await sync.sync_once_locked(db_keys=("daily_plan", "goals", "work_items", "revision"))
        today = session_context.local_today_iso()
        if reminders.claim(f"planning:{today}"):
            message = await asyncio.to_thread(reminders.planning_message)
            await context.bot.send_message(chat_id=telegram_allowed_user_id(), text=message)
    except Exception:
        logger.exception("planning reminder failed")


async def _schedule_watch_scan(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await sync.sync_once_locked(
            db_keys=("daily_plan", "goals", "work_items", "revision", "exams")
        )
        change = await asyncio.to_thread(reminders.settled_plan_change)
        if change and reminders.claim(change[0]):
            await context.bot.send_message(
                chat_id=telegram_allowed_user_id(), text=change[1]
            )
    except Exception:
        logger.exception("schedule watcher failed")


async def _weekly_timetable_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        now = session_context.local_now()
        if now.weekday() != config_settings.timetable_reminder_weekday():
            return
        week = now.date().isocalendar()[:2]
        if reminders.claim(f"timetable:{week[0]}:{week[1]}"):
            await context.bot.send_message(
                chat_id=telegram_allowed_user_id(), text=reminders.weekly_timetable_message()
            )
    except Exception:
        logger.exception("timetable reminder failed")


async def _weekly_report_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        now = session_context.local_now()
        if now.weekday() != config_settings.timetable_reminder_weekday():
            return
        week = now.date().isocalendar()[:2]
        if not reminders.claim(f"weekly-report:{week[0]}:{week[1]}"):
            return
        await sync.sync_once_locked()
        report = await asyncio.to_thread(study_domain.weekly_report)
        ledger = report["ledger"]
        attempted, correct = ledger.get("attempted") or 0, ledger.get("correct") or 0
        accuracy = f"{100 * correct / attempted:.1f}%" if attempted else "no complete data"
        await context.bot.send_message(
            chat_id=telegram_allowed_user_id(),
            text=(
                f"Weekly growth: CY {ledger.get('cy',0):g}, accuracy {accuracy}, "
                f"change {report['cy_delta']:+g}, "
                f"completed work {report['completed_work']}, backlog {report['backlog']}, "
                f"valid doubt attempts {report['valid_doubt_attempts']}. Use /weekly and /weak for detail."
            ),
        )
    except Exception:
        logger.exception("weekly report job failed")


async def _exam_reminder_scan(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await sync.sync_once_locked(db_keys=("exams",))
        for exam in await asyncio.to_thread(reminders.due_exams):
            event = f"exam-finished:{exam['notion_page_id']}:{session_context.local_today_iso()}"
            if reminders.claim(event):
                await context.bot.send_message(
                    chat_id=telegram_allowed_user_id(),
                    text=(f"Has {exam.get('title')} finished? When it has, run "
                          f"/finish_exam {exam.get('title')} to put full-paper analysis first."),
                )
    except Exception:
        logger.exception("exam reminder scan failed")


async def _teacher_opportunity_scan(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await sync.sync_once_locked(db_keys=("doubts", "doubt_attempts", "timetable", "daily_plan"))
        for item in await asyncio.to_thread(reminders.teacher_opportunities):
            window, decision = item["window"], item["decision"]
            key = reminders.teacher_event_key(window, decision)
            if not reminders.claim(key):
                continue
            titles = ", ".join(str(d.get("core_concept")) for d in item["doubts"][:3])
            prefix = "Interrupt available" if decision["interrupt"] else "Teacher opportunity"
            await context.bot.send_message(
                chat_id=telegram_allowed_user_id(),
                text=(f"{prefix}: {window.get('teacher') or 'teacher'} is available for "
                      f"{decision['minutes_left']:g} more minutes. Eligible: {titles}. "
                      f"{decision['reason']}"),
            )
    except Exception:
        logger.exception("teacher opportunity scan failed")


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands(BOT_COMMANDS)
    logger.info("Registered %d bot commands", len(BOT_COMMANDS))
    await asyncio.to_thread(study_domain.ensure_system_goals)
    # Retry any writes that were queued while Notion was unreachable.
    try:
        pending = logging_flow.pending_count()
        if pending:
            logger.info("Flushing %d queued write(s) from previous run", pending)
            await asyncio.to_thread(logging_flow.flush_pending, sync_after=False)
    except Exception:
        logger.exception("startup flush_pending failed")
    # Start the periodic mirror sync (every 4 min). This catches edits made
    # directly in Notion that don't go through the bot — without it, the
    # mirror only updates on bot-initiated writes.
    if application.job_queue is not None:
        application.job_queue.run_repeating(
            _periodic_sync,
            interval=SYNC_INTERVAL_SECONDS,
            first=SYNC_INTERVAL_SECONDS,
            name="periodic_sync",
        )
        application.job_queue.run_repeating(
            _expire_drafts, interval=60, first=60, name="expire_drafts"
        )
        def _clock(raw: str) -> dt.time:
            try:
                hour, minute = (int(part) for part in raw.split(":", 1))
                return dt.time(hour, minute, tzinfo=ZoneInfo(config_settings.user_timezone()))
            except Exception as exc:
                raise ValueError(f"invalid reminder time {raw!r}") from exc

        application.job_queue.run_daily(
            _planning_reminder, time=_clock(config_settings.planning_reminder_time()),
            days=tuple(range(7)), name="planning_reminder",
        )
        application.job_queue.run_daily(
            _weekly_timetable_reminder, time=_clock("09:00"),
            days=tuple(range(7)), name="timetable_reminder",
        )
        application.job_queue.run_daily(
            _weekly_report_job, time=_clock(config_settings.weekly_report_time()),
            days=tuple(range(7)), name="weekly_report",
        )
        application.job_queue.run_repeating(
            _exam_reminder_scan, interval=600, first=120, name="exam_reminders"
        )
        application.job_queue.run_repeating(
            _teacher_opportunity_scan, interval=300, first=180, name="teacher_windows"
        )
        application.job_queue.run_repeating(
            _schedule_watch_scan, interval=300, first=90, name="schedule_watcher"
        )
        logger.info("Scheduled periodic sync every %ds", SYNC_INTERVAL_SECONDS)
    else:
        logger.warning("JobQueue unavailable — periodic sync NOT scheduled")


def build_application() -> Application:
    return (
        Application.builder()
        .token(telegram_bot_token())
        .post_init(post_init)
        .build()
    )


def _configure_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # httpx includes the Telegram token in Bot API URLs at INFO level.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    fmt = logging.Formatter(LOG_FORMAT)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)


def main() -> None:
    _configure_logging()
    app = build_application()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("newsession", newsession))
    app.add_handler(CommandHandler("settings", settings))
    app.add_handler(CommandHandler("health", health))
    app.add_handler(CommandHandler("goal", goal_command))
    app.add_handler(CommandHandler("exam", exam_command))
    app.add_handler(CommandHandler("today", today_command))
    app.add_handler(CommandHandler("next", next_command))
    app.add_handler(CommandHandler("backlog", backlog_command))
    app.add_handler(CommandHandler("attempt", attempt_command))
    app.add_handler(CommandHandler("doubts", doubts_command))
    app.add_handler(CommandHandler("dismissdoubt", dismiss_doubt_command))
    app.add_handler(CommandHandler("resolvedoubt", resolve_doubt_command))
    app.add_handler(CommandHandler("reopendoubt", reopen_doubt_command))
    app.add_handler(CommandHandler("timetable", timetable_command))
    app.add_handler(CommandHandler("weak", weak_command))
    app.add_handler(CommandHandler("weekly", weekly_command))
    app.add_handler(CommandHandler("finish_exam", finish_exam_command))
    app.add_handler(CommandHandler("exam_summary", exam_summary_command))
    app.add_handler(CommandHandler("question_review", question_review_command))
    app.add_handler(CommandHandler("complete_exam_analysis", complete_exam_analysis_command))
    app.add_handler(CallbackQueryHandler(on_domain_callback, pattern=r"^domain:"))
    app.add_handler(CallbackQueryHandler(on_plan_callback, pattern=r"^plan:"))
    app.add_handler(CallbackQueryHandler(on_log_callback, pattern=r"^log:"))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, catch_all))
    logger.info("Starting Telegram long polling")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
