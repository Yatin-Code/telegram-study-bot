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

import agent
import agent_renderer
import briefing
import bot_identity
import commitments
import advisor
import memory_map
import onboarding
import user_jobs
import sql_query_flow
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
import message_templates
import exam_readiness
import reset_service
from config import settings as config_settings
from zoneinfo import ZoneInfo
from config import notion_schema
from config.settings import telegram_allowed_user_id, telegram_bot_token
from intent_parser import IntentParseError, parse_message


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_FILE = LOG_DIR / "bot.log"
logger = logging.getLogger(__name__)
_MAINTENANCE_LOCK_KEY = "_study_bot_maintenance_lock"
_fallback_maintenance_lock: asyncio.Lock | None = None

BOT_COMMANDS = [
    BotCommand(item.command, item.description) for item in bot_identity.COMMANDS
]


def _assistant_system_prompt(chat_id: int) -> str:
    context = session_context.context_for_parser(chat_id) or {}
    context_text = _format_context(context)
    import actions
    identity = actions.identity_with_actions(role="conversational fallback", context="any")
    return f"""{identity}

Current study context: {context_text}

Answer normal study questions, explain strategy, explain what the bot can do,
and guide the user to the right command. Keep the response concise and suitable
for Telegram. Do not emit a fake success acknowledgement for an action that was
not actually executed."""


def _general_assistant_answer(text: str, chat_id: int) -> str:
    """Answer a message that does not map to a structured bot intent."""
    from llm import router

    response = router.complete(router.LLMRequest(
        messages=[
            {"role": "system", "content": _assistant_system_prompt(chat_id)},
            {"role": "user", "content": text},
        ],
        # Reuse the certified general-domain route pool.  The router currently
        # certifies intent/domain/sql capabilities rather than free-form labels.
        purpose="domain",
        max_output_tokens=700,
        temperature=0.2,
    ))
    answer = response.text.strip()
    if answer.upper().startswith("ANSWER:"):
        answer = answer[7:].strip()
    if not answer:
        raise RuntimeError("assistant returned an empty response")
    return answer


async def _handle_general_assistant(update: Update, text: str, chat_id: int) -> None:
    message = update.effective_message
    try:
        answer = await asyncio.to_thread(_general_assistant_answer, text, chat_id)
    except Exception:
        logger.exception("general assistant fallback failed chat_id=%s", chat_id)
        answer = (
            "I can still help you log study sessions, track doubts and revision, "
            "plan today, and review progress. Try /help for the full command list, "
            "or ask a specific study question."
        )
    await _reply_markdown(message, answer, disable_web_page_preview=True)


def _format_context(ctx: dict | None) -> str:
    if not ctx:
        return "none set"
    bits = [f"{k}={ctx[k]}" for k in session_context.CONTEXT_KEYS if ctx.get(k)]
    return ", ".join(bits) if bits else "none set"


def _is_allowed(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id == telegram_allowed_user_id())


def _maintenance_lock(context) -> asyncio.Lock:
    """One per Application: scheduled jobs and destructive reset exclude each other."""
    application = getattr(context, "application", None)
    bot_data = getattr(application, "bot_data", None)
    if bot_data is not None:
        lock = bot_data.get(_MAINTENANCE_LOCK_KEY)
        if lock is None:
            lock = asyncio.Lock()
            bot_data[_MAINTENANCE_LOCK_KEY] = lock
        return lock
    global _fallback_maintenance_lock
    if _fallback_maintenance_lock is None:
        _fallback_maintenance_lock = asyncio.Lock()
    return _fallback_maintenance_lock


def _guard_scheduled(callback):
    async def guarded(context):
        async with _maintenance_lock(context):
            await callback(context)

    guarded.__name__ = getattr(callback, "__name__", "guarded_job")
    return guarded


def _bot_token_from(message_or_bot) -> str | None:
    """Extract a bot token from a Message, Bot, or env. None if unavailable."""
    get_bot = getattr(message_or_bot, "get_bot", None)
    if callable(get_bot):
        try:
            bot = get_bot()
            token = getattr(bot, "token", None)
            if token:
                return token
        except Exception:
            pass
    token = getattr(message_or_bot, "token", None)
    if token:
        return token
    try:
        return telegram_bot_token()
    except Exception:
        return None


def _chat_id_from(message) -> int | None:
    chat_id = getattr(message, "chat_id", None)
    if chat_id is not None:
        return chat_id
    chat = getattr(message, "chat", None)
    if chat is not None:
        return getattr(chat, "id", None)
    return None


async def _reply_markdown(message, text: str, **kwargs):
    import rich_message

    token = _bot_token_from(message)
    chat_id = _chat_id_from(message)
    if token and chat_id is not None:
        try:
            return await rich_message.send_rich(
                token, chat_id, text,
                parse_mode="markdown",
                reply_to_message_id=getattr(message, "message_id", None),
                reply_markup=kwargs.get("reply_markup"),
                disable_web_page_preview=kwargs.get("disable_web_page_preview"),
            )
        except Exception:
            logger.exception("rich reply failed; falling back to plain reply_text")
    try:
        return await message.reply_text(text, parse_mode=ParseMode.MARKDOWN, **kwargs)
    except Exception:
        return await message.reply_text(text, **kwargs)


async def _edit_markdown(message, text: str, **kwargs):
    import rich_message

    token = _bot_token_from(message)
    chat_id = _chat_id_from(message)
    message_id = getattr(message, "message_id", None)
    if token and chat_id is not None and message_id is not None:
        try:
            return await rich_message.edit_rich(
                token, chat_id, message_id, text,
                parse_mode="markdown",
                reply_markup=kwargs.get("reply_markup"),
                disable_web_page_preview=kwargs.get("disable_web_page_preview"),
            )
        except Exception:
            logger.exception("rich edit failed; falling back to plain edit_text")
    edit = getattr(message, "edit_text", None) or message.edit_message_text
    try:
        return await edit(text, parse_mode=ParseMode.MARKDOWN, **kwargs)
    except Exception:
        return await edit(text, **kwargs)


async def _send_markdown(bot, chat_id: int, text: str, **kwargs):
    import rich_message

    token = _bot_token_from(bot)
    if token:
        try:
            return await rich_message.send_rich(
                token, chat_id, text,
                parse_mode="markdown",
                reply_markup=kwargs.get("reply_markup"),
                disable_web_page_preview=kwargs.get("disable_web_page_preview"),
            )
        except Exception:
            logger.exception("rich send failed; falling back to plain send_message")
    try:
        return await bot.send_message(
            chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN, **kwargs
        )
    except Exception:
        return await bot.send_message(chat_id=chat_id, text=text, **kwargs)


async def _reject_if_unauthorized(update: Update) -> bool:
    if _is_allowed(update):
        return False
    user = update.effective_user
    logger.warning("Ignoring unauthorized user_id=%s", user.id if user else None)
    return True


def _try_pattern_match(text: str) -> Optional[Any]:
    """Fast path: regex-based pattern matching for common log messages.
    
    Returns an Intent object if the message matches a known pattern, or None
    if it should fall back to LLM parsing. This makes 90% of daily logs:
    - Instant (no network call)
    - Bulletproof (works even if LLM is down)
    - Free (no API cost)
    
    Patterns handled:
    - "solved 20 questions 15 correct 30 mins" → log_execution
    - "did 25 qs, 18 correct, 40 min" → log_execution
    - "completed 15 questions 12 correct 20 minutes" → log_execution
    - "doubt: why does X happen" → log_doubt
    - "list physics doubts" / "show doubts" → query
    - "list revisions" / "show revision" → query
    """
    from intent_parser import Intent, IntentFilters
    
    text_clean = text.strip()
    text_lower = text_clean.lower()
    
    # Pattern 1: Execution log with questions + correct + time
    # Matches: "solved 20 questions 15 correct 30 mins"
    #          "did 25 qs, 18 correct, 40 min"
    #          "20 questions 15 correct 30 minutes"
    execution_pattern = re.compile(
        r'(?:solved|did|completed?|finished)?\s*'  # optional verb
        r'(\d+)\s*(?:questions?|qs?|q)\s*'         # questions attempted
        r'(?:,?\s*)?'                               # optional comma/space
        r'(\d+)\s*(?:correct|right|✓)\s*'          # questions correct
        r'(?:,?\s*)?'                               # optional comma/space
        r'(\d+)\s*(?:min(?:ute)?s?|m)\b',          # time in minutes
        re.IGNORECASE
    )
    
    match = execution_pattern.search(text_clean)
    if match:
        attempted = int(match.group(1))
        correct = int(match.group(2))
        time_min = int(match.group(3))
        
        # Sanity check: correct can't exceed attempted
        if correct <= attempted:
            logger.info("fast-path: execution log matched (%d qs, %d correct, %d min)",
                       attempted, correct, time_min)
            return Intent(
                action='log_execution',
                database='ledger',
                fields={
                    'questions_attempted': attempted,
                    'questions_correct': correct,
                    'actual_time_min': time_min,
                },
                filters=IntentFilters(),
            )
    
    # Pattern 2: Doubt log (starts with "doubt:" or "doubt -")
    if text_lower.startswith('doubt:') or text_lower.startswith('doubt -'):
        doubt_text = text_clean.split(':', 1)[-1].strip() if ':' in text_clean else text_clean.split('-', 1)[-1].strip()
        if doubt_text:
            logger.info("fast-path: doubt log matched")
            return Intent(
                action='log_doubt',
                database='doubts',
                fields={'core_concept': doubt_text},
                filters=IntentFilters(),
            )
    
    # Pattern 3: Simple queries for listing data
    # "list doubts", "show doubts", "doubts list"
    # "list physics doubts", "show chemistry doubts"
    doubt_query_pattern = re.compile(
        r'\b(?:list|show|see|view|get)\s+(?:(?:my|all|the)\s+)?'
        r'(?:(physics|chemistry|maths|chem)\s+)?doubts?\b',
        re.IGNORECASE
    )
    match = doubt_query_pattern.search(text_lower)
    if match:
        subject = match.group(1)
        logger.info("fast-path: doubt query matched (subject=%s)", subject or "any")
        return Intent(
            action='query',
            database='doubts',
            fields={},
            filters=IntentFilters(subject=subject if subject else None),
        )
    
    # Pattern 4: Revision queries
    revision_query_pattern = re.compile(
        r'\b(?:list|show|see|view|get)\s+(?:(?:my|all|the)\s+)?'
        r'(?:(physics|chemistry|maths|chem)\s+)?revisions?\b',
        re.IGNORECASE
    )
    match = revision_query_pattern.search(text_lower)
    if match:
        subject = match.group(1)
        logger.info("fast-path: revision query matched (subject=%s)", subject or "any")
        return Intent(
            action='query',
            database='revision',
            fields={},
            filters=IntentFilters(subject=subject if subject else None),
        )
    
    # No pattern matched - return None to fall back to LLM
    return None


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
        "Commands: /help, /setup, /newsession, /health"
    )
    chat_id = update.effective_chat.id
    if not onboarding.is_complete(chat_id):
        text, markup = await asyncio.to_thread(_setup_hub_view)
        await update.effective_message.reply_text(text, reply_markup=markup)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    await update.effective_message.reply_text(
        "Commands (slash optional — you can also just say it in plain English):\n\n"
        "NL-friendly — just say it naturally:\n"
        "/newsession  /sync  /goal  /remember  /forget\n"
        "/exam  /readiness  /today  /next  /backlog  /weak  /weekly\n"
        "/attempt  /doubts  /dismissdoubt  /resolvedoubt  /reopendoubt\n"
        "/timetable  /jobs  /exam_summary  /question_review\n"
        "/finish_exam  /complete_exam_analysis\n"
        "/memory  /inspect  /health\n\n"
        "Interactive UI (use slash for full wizard):\n"
        "/start  /help  /setup  /settings  /bug  /bugs  /reset\n"
    )


def _settings_home_view() -> tuple[str, InlineKeyboardMarkup]:
    try:
        model = config_settings.llm_model()
    except Exception:
        model = "(unset)"
    lines = [
        "⚙️ Settings — tap a category to view & edit",
        f"Timezone: {config_settings.user_timezone()} (now {session_context.local_now():%H:%M}) · Model: {model}",
        "",
        "✏️ = customised in settings.json · ⏱ = applies after restart",
        "Secrets (tokens, API keys) live only in .env 🔒",
    ]
    buttons = [
        [InlineKeyboardButton(cat, callback_data=f"settings:cat:{i}")]
        for i, cat in enumerate(config_settings.SETTINGS_CATEGORIES)
    ]
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def _settings_category_view(idx: int) -> tuple[str, InlineKeyboardMarkup]:
    cat = config_settings.SETTINGS_CATEGORIES[idx]
    entries = [e for e in config_settings.SETTINGS_REGISTRY if e["category"] == cat]
    lines = [cat, ""]
    buttons = []
    for entry in entries:
        current = config_settings.current_setting_value(entry["key"])
        marks = ""
        if config_settings.get_override(entry["key"]) is not None:
            marks += " ✏️"
        if entry["restart"]:
            marks += " ⏱"
        lines.append(f"• {entry['label']}: {current}{marks}")
        buttons.append([InlineKeyboardButton(
            f"✏️ {entry['label']}", callback_data=f"settings:edit:{entry['key']}"
        )])
    buttons.append([InlineKeyboardButton("↩ Back", callback_data="settings:home")])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def _settings_edit_prompt_view(key: str) -> tuple[str, InlineKeyboardMarkup]:
    entry = config_settings.setting_entry(key)
    current = config_settings.current_setting_value(key)
    idx = config_settings.SETTINGS_CATEGORIES.index(entry["category"])
    lines = [f"✏️ {entry['label']}", f"Current: {current}"]
    if entry.get("default"):
        lines.append(f"Default: {entry['default']}")
    if entry["type"] == "int":
        lines.append(f"Allowed: {entry.get('min', '…')}–{entry.get('max', '…')}")
    elif entry["type"] == "time":
        lines.append("Format: HH:MM (24h), e.g. 21:30")
    elif entry["type"] == "tz":
        lines.append("Format: IANA name, e.g. Asia/Kolkata")
    if entry["restart"]:
        lines.append("⏱ Applies after the bot restarts.")
    lines.append("")
    lines.append("Send the new value as a message now.")
    buttons = [[
        InlineKeyboardButton("↺ Reset to default", callback_data=f"settings:reset:{key}"),
        InlineKeyboardButton("↩ Back", callback_data=f"settings:cat:{idx}"),
    ]]
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def _settings_weekday_view(key: str) -> tuple[str, InlineKeyboardMarkup]:
    entry = config_settings.setting_entry(key)
    idx = config_settings.SETTINGS_CATEGORIES.index(entry["category"])
    names = config_settings.WEEKDAY_NAMES
    day_buttons = [
        InlineKeyboardButton(name, callback_data=f"settings:set:{key}:{i}")
        for i, name in enumerate(names)
    ]
    buttons = [day_buttons[:4], day_buttons[4:],
               [InlineKeyboardButton("↺ Reset to default", callback_data=f"settings:reset:{key}"),
                InlineKeyboardButton("↩ Back", callback_data=f"settings:cat:{idx}")]]
    text = (
        f"✏️ {entry['label']}\n"
        f"Current: {config_settings.current_setting_value(key)}\n\n"
        "Pick a day:"
    )
    return text, InlineKeyboardMarkup(buttons)


def _settings_after_change_view(key: str, *, reset: bool = False) -> tuple[str, InlineKeyboardMarkup]:
    entry = config_settings.setting_entry(key)
    idx = config_settings.SETTINGS_CATEGORIES.index(entry["category"])
    text, markup = _settings_category_view(idx)
    if reset:
        head = f"↺ {entry['label']} reset to default."
    else:
        head = f"✅ {entry['label']} → {config_settings.current_setting_value(key)}"
        if entry["restart"]:
            head += " ⏱ applies after restart"
    return head + "\n\n" + text, markup


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    draft_store.clear_pending_setting_edit(update.effective_chat.id)
    text, markup = _settings_home_view()
    await update.effective_message.reply_text(text, reply_markup=markup)


async def on_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not _is_allowed(update):
        return
    chat_id = update.effective_chat.id
    parts = query.data.split(":", 3)
    action = parts[1] if len(parts) > 1 else "home"
    try:
        if action == "home":
            draft_store.clear_pending_setting_edit(chat_id)
            text, markup = _settings_home_view()
        elif action == "cat" and len(parts) > 2:
            draft_store.clear_pending_setting_edit(chat_id)
            text, markup = _settings_category_view(int(parts[2]))
        elif action == "edit" and len(parts) > 2:
            key = parts[2]
            entry = config_settings.setting_entry(key)
            if entry is None:
                return
            if entry["type"] == "weekday":
                text, markup = _settings_weekday_view(key)
            else:
                draft_store.set_pending_setting_edit(chat_id, key)
                text, markup = _settings_edit_prompt_view(key)
        elif action == "set" and len(parts) > 3:
            key, raw = parts[2], parts[3]
            ok, result = config_settings.validate_setting(key, raw)
            if ok:
                config_settings.set_override(key, result)
            text, markup = _settings_after_change_view(key)
        elif action == "reset" and len(parts) > 2:
            key = parts[2]
            config_settings.clear_override(key)
            draft_store.clear_pending_setting_edit(chat_id)
            text, markup = _settings_after_change_view(key, reset=True)
        else:
            return
        try:
            await query.edit_message_text(text, reply_markup=markup)
        except Exception:
            pass  # "message is not modified" and similar cosmetic failures
    except Exception as exc:
        logger.exception("settings callback failed")
        try:
            await query.edit_message_text(f"⚠️ Settings action failed: {exc}")
        except Exception:
            pass


async def _apply_setting_reply(message, chat_id: int, key: str, raw: str) -> None:
    """Consume a text reply as the new value for a pending /settings edit."""
    entry = config_settings.setting_entry(key)
    if entry is None:
        await message.reply_text("That setting no longer exists.")
        return
    ok, result = config_settings.validate_setting(key, raw)
    if not ok:
        draft_store.set_pending_setting_edit(chat_id, key)
        await message.reply_text(
            f"⚠️ {result}. Send the value again, or open /settings to cancel."
        )
        return
    config_settings.set_override(key, result)
    note = " ⏱ applies after restart" if entry["restart"] else ""
    await message.reply_text(
        f"✅ {entry['label']} → {config_settings.current_setting_value(key)}{note}"
    )


def _memory_view(chat_id: int, extra_rows: list | None = None):
    rep = memory_map.report(chat_id)
    text = memory_map.render(rep)
    rows = memory_map.keyboard(rep)
    if extra_rows:
        rows = extra_rows + rows
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=data) for label, data in row]
        for row in rows if row
    ])
    return text, markup


async def memory_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    chat_id = update.effective_chat.id
    text, markup = await asyncio.to_thread(_memory_view, chat_id)
    await update.effective_message.reply_text(text, reply_markup=markup)


async def on_memory_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not _is_allowed(update):
        return
    chat_id = update.effective_chat.id
    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else "refresh"
    undo_row = None
    try:
        if action == "raw":
            text = await asyncio.to_thread(memory_map.render_raw, chat_id)
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("◀ Back", callback_data="memory:refresh")
            ]])
            try:
                await query.edit_message_text(text, reply_markup=markup)
            except Exception:
                pass
            return
        if action == "clearctx":
            session_context.clear_context(chat_id)
        elif action == "clearhist":
            draft_store.clear_qa_history(chat_id)
        elif action == "delpref" and len(parts) > 2:
            removed = commitments.deactivate_pref(chat_id, int(parts[2]))
            if removed:
                undo_row = [("↩ Undo remove", f"memory:undo:pref:{parts[2]}")]
        elif action == "pausegoal" and len(parts) > 2:
            await asyncio.to_thread(
                operational_store.update, "goals", parts[2], {"status": "Paused"}
            )
            undo_row = [("↩ Undo pause", f"memory:undo:goal:{parts[2]}")]
        elif action == "undo" and len(parts) > 3:
            if parts[2] == "pref":
                commitments.reactivate_pref(chat_id, int(parts[3]))
            else:
                await asyncio.to_thread(
                    operational_store.update, "goals", parts[3], {"status": "Active"}
                )
        text, markup = await asyncio.to_thread(
            _memory_view, chat_id, [undo_row] if undo_row else None
        )
        try:
            await query.edit_message_text(text, reply_markup=markup)
        except Exception:
            pass  # "message is not modified" on double-refresh
    except Exception as exc:
        logger.exception("memory callback failed")
        try:
            await query.edit_message_text(f"⚠️ Memory action failed: {exc}")
        except Exception:
            pass


async def newsession(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    chat_id = update.effective_chat.id
    session_context.clear_context(chat_id)
    draft_store.clear_qa_history(chat_id)
    await update.effective_message.reply_text("Session context cleared.")


def _inspect_db_path() -> Path:
    """Path to the bot's SQLite database."""
    return Path(__file__).resolve().parent / "sqlite_mirror.db"


def _inspect_table_counts() -> dict[str, int]:
    """Return row counts for every SQLite table."""
    import sqlite3
    db = _inspect_db_path()
    if not db.exists():
        return {}
    try:
        conn = sqlite3.connect(str(db))
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            counts: dict[str, int] = {}
            for (table,) in rows:
                try:
                    n = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                    counts[table] = int(n)
                except Exception:
                    counts[table] = -1
            return counts
        finally:
            conn.close()
    except Exception:
        return {}


def _inspect_safe(text: str) -> str:
    """Escape characters Telegram Markdown v1 treats as formatting."""
    return text.replace("_", "\\_").replace("*", "\\*").replace("[", "\\[").replace("]", "\\]")


def _inspect_friendly_name(table: str) -> str:
    """Turn snake_case table names into readable titles."""
    name = table.replace("op_", "").replace("_", " ").title()
    return name if not table.startswith("op_") else "Op " + name


def _inspect_format_value(val: Any, col: str) -> str:
    """Make raw DB values human-readable."""
    import datetime as _dt

    if val is None:
        return ""
    if col in ("archived", "active", "enabled", "hard_constraint"):
        return "Yes" if val else "No"
    if col in ("created_time", "last_edited_time", "last_synced_at", "created_at", "completed_at", "run_time", "last_run"):
        try:
            dt = _dt.datetime.fromisoformat(str(val))
            return dt.strftime("%d %B %Y, %I:%M %p")
        except Exception:
            pass
    if isinstance(val, str) and (val.startswith("{") or val.startswith("[")):
        try:
            import json
            parsed = json.loads(val)
            if isinstance(parsed, dict) and not parsed:
                return ""
            if isinstance(parsed, (dict, list)):
                return str(parsed)[:120]
        except Exception:
            pass
    val_str = str(val)
    if len(val_str) > 90:
        val_str = val_str[:87] + "..."
    return val_str


def _inspect_table_use(table: str) -> str:
    """Short human description of what the bot uses this table for."""
    uses = {
        "active_plan_state": "Tracks which daily/weekly plan is currently live.",
        "agent_pending_states": "Stores Confirm/Cancel previews from the AI agent.",
        "chat_context": "Current topic/subject/chapter the bot is helping you with right now.",
        "chat_qa_history": "Recent question/answer turns used for follow-up queries.",
        "commitment_checks": "Nightly checks of whether you met your daily study commitments.",
        "conversation_history": "Recent bot/user chat turns used as conversation memory.",
        "daily_plan": "Legacy Notion-mirrored daily plan rows.",
        "doubts": "Your logged doubts synced from Notion.",
        "drafts": "Pending write previews awaiting your Confirm/Edit/Cancel.",
        "exam_readiness_items": "Per-exam readiness checklist tracked by the bot.",
        "exams": "Your exams synced from Notion.",
        "goals": "Your goals synced from Notion.",
        "learner_insights": "Long-term insights the bot extracted from your patterns.",
        "learner_profiles": "Your study preferences and profile.",
        "ledger": "Your study sessions synced from Notion.",
        "onboarding_state": "Your progress through /setup.",
        "op_daily_plan": "The bot's active daily study plan for you.",
        "op_doubt_attempts": "Your attempts at solving doubts and their outcomes.",
        "op_exam_questions": "Questions the bot created for your exams.",
        "op_exams": "Exams the bot is tracking locally (scores, dates, status).",
        "op_execution_links": "Links between goals, work items, and timetable slots.",
        "op_goals": "The bot's operating goals for your JEE prep.",
        "op_timetable": "Your weekly study timetable.",
        "op_work_items": "Tasks the bot planned for you to complete.",
        "pending_clarifications": "Follow-up questions the bot asked and is waiting on.",
        "revision": "Your revision schedule synced from Notion.",
        "sync_meta": "Last sync timestamps for Notion-mirrored tables.",
        "user_jobs": "Scheduled reminders/jobs the bot runs for you.",
        "user_prefs": "Your saved preferences.",
    }
    return uses.get(table, "Internal data store used by the bot.")


def _inspect_home_view() -> tuple[str, InlineKeyboardMarkup]:
    """Main inspection menu."""
    text = (
        "🔍 *Data Inspector*\n\n"
        "See exactly what the bot knows. Use this to verify:\n"
        "• Notion sync is working\n"
        "• Reset actually cleared data\n"
        "• Bot has access to your records\n\n"
        "Choose a data source:"
    )
    buttons = [
        [InlineKeyboardButton("📊 All SQLite Tables", callback_data="inspect:menu:sqlite")],
        [InlineKeyboardButton("☁️ Notion Sync Status", callback_data="inspect:menu:notion")],
        [InlineKeyboardButton("🧠 In-Context Memory", callback_data="inspect:menu:context")],
    ]
    return text, InlineKeyboardMarkup(buttons)


def _inspect_sqlite_menu() -> tuple[str, InlineKeyboardMarkup]:
    """List every SQLite table with its row count."""
    counts = _inspect_table_counts()
    if not counts:
        text = "⚠️ SQLite read failed or no tables found."
        return text, InlineKeyboardMarkup([[InlineKeyboardButton("↩ Back", callback_data="inspect:home")]])

    text = "📊 *All SQLite Tables*\n\nTap a table to see what the bot knows:"
    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for table in sorted(counts):
        friendly = _inspect_friendly_name(table)
        label = f"{friendly} ({counts[table]})"
        btn = InlineKeyboardButton(label, callback_data=f"inspect:table:{table}")
        if len(row) == 2:
            buttons.append(row)
            row = [btn]
        else:
            row.append(btn)
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("↩ Back", callback_data="inspect:home")])
    return text, InlineKeyboardMarkup(buttons)


def _inspect_ops_menu() -> tuple[str, InlineKeyboardMarkup]:
    """Operational / Memory tables are now shown from the all-tables menu."""
    text = "️ *Operational / Memory*\n\nThese tables are now shown in *All SQLite Tables*."
    buttons = [
        [InlineKeyboardButton("📊 All SQLite Tables", callback_data="inspect:menu:sqlite")],
        [InlineKeyboardButton("↩ Back", callback_data="inspect:home")],
    ]
    return text, InlineKeyboardMarkup(buttons)


def _inspect_table_view(table: str, limit: int = 5) -> tuple[str, InlineKeyboardMarkup]:
    """Show recent records from any SQLite table in a human-readable way."""
    import sqlite3

    db = _inspect_db_path()
    back_menu = "inspect:menu:sqlite"
    friendly = _inspect_friendly_name(table)
    use = _inspect_table_use(table)

    if not db.exists():
        return "⚠️ DB not found.", InlineKeyboardMarkup([[InlineKeyboardButton(" Back", callback_data=back_menu)]])

    try:
        conn = sqlite3.connect(str(db))
        try:
            conn.row_factory = sqlite3.Row
            col_info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
            col_names = {c["name"] for c in col_info}
            order_by = next(
                (c for c in ("created_at", "created_time", "last_edited_time", "last_synced_at", "id", "rowid") if c in col_names),
                "rowid",
            )
            if "rowid" not in col_names and order_by == "rowid":
                rows = conn.execute(f'SELECT *, rowid FROM "{table}" ORDER BY {order_by} DESC LIMIT ?', (limit,)).fetchall()
            else:
                rows = conn.execute(f'SELECT * FROM "{table}" ORDER BY "{order_by}" DESC LIMIT ?', (limit,)).fetchall()
        finally:
            conn.close()

        header = f"📊 *{friendly}*"
        if use:
            header += f"\n_{use}_"
        if not rows:
            text = f"{header}\n\nNo records found."
        else:
            lines = [header, f"Showing {len(rows)} most recent records:\n"]
            for i, row in enumerate(rows, 1):
                lines.append(f"\n*{i}.*")
                for col in row.keys():
                    val = row[col]
                    if val is None or val == "":
                        continue
                    val_str = _inspect_format_value(val, col)
                    if not val_str:
                        continue
                    lines.append(f"  • `{col}`: {_inspect_safe(val_str)}")
            text = "\n".join(lines)
    except Exception as e:
        text = f"⚠️ Failed to read {table}: {_inspect_safe(str(e))}"

    buttons = [
        [InlineKeyboardButton(" Back to Tables", callback_data=back_menu)],
        [InlineKeyboardButton(" Home", callback_data="inspect:home")],
    ]
    return text, InlineKeyboardMarkup(buttons)


def _inspect_notion_view() -> tuple[str, InlineKeyboardMarkup]:
    """Notion sync status view."""
    import sqlite3
    db = Path(__file__).resolve().parent / "sqlite_mirror.db"
    
    try:
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        sync_rows = conn.execute(
            "SELECT * FROM sync_meta ORDER BY last_completed_at DESC"
        ).fetchall()
        conn.close()
        
        if not sync_rows:
            text = "☁️ *Notion Sync Status*\n\nNo sync history found."
        else:
            lines = ["☁️ *Notion Sync Status*\n"]
            for row in sync_rows:
                db_key = row["db_key"]
                completed = row["last_completed_at"] or "never"
                count = row["last_row_count"] or 0
                error = row["last_error"]
                
                status = "✅" if not error else "⚠️"
                lines.append(f"\n{status} *{db_key}*")
                lines.append(f"  • Last sync: {completed}")
                lines.append(f"  • Records synced: {count}")
                if error:
                    lines.append(f"  • Error: {error[:100]}")
            text = "\n".join(lines)
    except Exception as e:
        text = f"⚠️ Failed to read sync status: {e}"
    
    buttons = [
        [InlineKeyboardButton("🔄 Force Sync Now", callback_data="inspect:action:sync")],
        [InlineKeyboardButton("↩ Back", callback_data="inspect:home")],
    ]
    return text, InlineKeyboardMarkup(buttons)


def _inspect_context_view(chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """In-context memory view."""
    ctx = session_context.get_context(chat_id)
    elapsed = session_context.elapsed_minutes(chat_id)
    history = draft_store.recent_qa(chat_id, limit_pairs=3)
    
    lines = ["🧠 *In-Context Memory*\n"]
    
    # Session context
    lines.append("*Current Session:*")
    if ctx and any(ctx.values()):
        for key in session_context.CONTEXT_KEYS:
            if ctx.get(key):
                lines.append(f"  • {key}: {ctx[key]}")
        if elapsed is not None:
            lines.append(f"  • elapsed: {round(elapsed)} min")
    else:
        lines.append("  (no active session)")
    
    # Recent Q&A history
    lines.append("\n*Recent Q&A History:*")
    if history:
        for i, turn in enumerate(history, 1):
            q = turn["question"][:60] + "..." if len(turn["question"]) > 60 else turn["question"]
            lines.append(f"  {i}. Q: {q}")
    else:
        lines.append("  (no recent queries)")
    
    # Pending drafts
    try:
        import sqlite3
        db = Path(__file__).resolve().parent / "sqlite_mirror.db"
        conn = sqlite3.connect(str(db))
        draft_count = conn.execute(
            "SELECT COUNT(*) AS n FROM drafts WHERE chat_id = ?", (chat_id,)
        ).fetchone()[0]
        conn.close()
        lines.append(f"\n*Pending Drafts:* {draft_count}")
    except:
        pass
    
    text = "\n".join(lines)
    buttons = [
        [InlineKeyboardButton("🗑 Clear Session", callback_data="inspect:action:clearsession")],
        [InlineKeyboardButton("↩ Back", callback_data="inspect:home")],
    ]
    return text, InlineKeyboardMarkup(buttons)


async def inspect_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Main /inspect command handler."""
    if await _reject_if_unauthorized(update):
        return
    text, markup = _inspect_home_view()
    await update.effective_message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)


async def on_inspect_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inspect inline keyboard navigation."""
    query = update.callback_query
    await query.answer()
    if not _is_allowed(update):
        return
    
    chat_id = update.effective_chat.id
    parts = query.data.split(":", 3)
    action = parts[1] if len(parts) > 1 else "home"
    param = parts[2] if len(parts) > 2 else None
    
    try:
        if action == "home":
            text, markup = _inspect_home_view()
        elif action == "menu":
            if param == "sqlite":
                text, markup = _inspect_sqlite_menu()
            elif param == "ops":
                text, markup = _inspect_ops_menu()
            elif param == "notion":
                text, markup = _inspect_notion_view()
            elif param == "context":
                text, markup = _inspect_context_view(chat_id)
            else:
                return
        elif action == "table" and param:
            text, markup = _inspect_table_view(param)
        elif action == "action":
            if param == "sync":
                await query.edit_message_text("🔄 Syncing from Notion...")
                try:
                    counts = await asyncio.to_thread(sync.sync_once)
                    total = sum(counts.values())
                    detail = ", ".join(f"{k}={v}" for k, v in counts.items())
                    text, markup = _inspect_notion_view()
                    text = f"✅ Synced {total} records ({detail})\n\n" + text
                except Exception as e:
                    text = f"⚠️ Sync failed: {e}"
                    _, markup = _inspect_notion_view()
            elif param == "clearsession":
                session_context.clear_context(chat_id)
                draft_store.clear_qa_history(chat_id)
                text, markup = _inspect_context_view(chat_id)
                text = "✅ Session cleared.\n\n" + text
            else:
                return
        else:
            return
        
        try:
            await query.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)
        except Exception as exc:
            # Markdown parse errors (unescaped underscores etc.) should not silently kill the UI.
            err = str(exc).lower()
            if "can't parse" in err or "parse" in err or "entities" in err:
                try:
                    plain = _inspect_safe(text)
                    await query.edit_message_text(plain, reply_markup=markup)
                except Exception:
                    logger.exception("inspect fallback plain edit failed")
            elif "not modified" in err:
                pass
            else:
                logger.exception("inspect edit failed")
    except Exception as exc:
        logger.exception("inspect callback failed")
        try:
            await query.edit_message_text(f"⚠️ Inspect action failed: {exc}")
        except Exception:
            pass


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
                  for t in ("ledger", "doubts", "revision")}
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
        f"• Notion mirror: ledger={counts['ledger']}, doubts={counts['doubts']}, revision={counts['revision']}",
        f"• SQLite: {operations['integrity']}, schema v{operations['schema_version']}, goals={operations['counts'].get('goals', 0)}, backlog={operations['counts'].get('work_items', 0)}, plan={operations['counts'].get('daily_plan', 0)}",
        f"• Last sync: {last_sync}",
        f"• Context: {_format_context(ctx)}",
        f"• Pending writes: {logging_flow.pending_count()} "
        f"({logging_flow.exhausted_pending_count()} exhausted)",
        f"• Errors last 24h: {_errors_last_24h()}",
    ]
    try:
        from llm import quota as llm_quota
        llm_state = llm_quota.health(db)
        active = llm_state.get("active")
        if active:
            lines.append(
                f"• LLM route: {active['route_id']} ({active['status']})"
            )
        else:
            lines.append("• LLM route: legacy/unobserved")
        windows = llm_state.get("windows") or []
        if windows:
            compact = []
            for window in windows[:6]:
                compact.append(
                    f"{window['route_id']} {window['scope']} "
                    f"{window['remaining']}/{window['limit_value']} "
                    f"({window['confidence']}, reset {window['reset_at']})"
                )
            lines.append("• LLM quota: " + " | ".join(compact))
        unavailable = [
            rid for rid, state in llm_state.get("states", {}).items()
            if not state.get("available", True)
        ]
        lines.append(
            "• LLM fallback: " + ("active — " + ", ".join(unavailable) if unavailable else "ready")
        )
    except Exception as exc:
        logger.warning("LLM health unavailable: %s", type(exc).__name__)
        lines.append("• LLM routing: health unavailable")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def sync_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Force an immediate Notion→SQLite mirror refresh. No LLM involved."""
    if await _reject_if_unauthorized(update):
        return
    status = await update.effective_message.reply_text("🔄 Syncing from Notion…")
    try:
        counts = await asyncio.to_thread(
            sync.sync_once, db_keys=sync.NOTION_SOURCE_KEYS
        )
    except Exception as e:
        logger.exception("manual /sync failed")
        try:
            await status.edit_text(f"⚠️ Sync failed: {e}")
        except Exception:
            await update.effective_message.reply_text(f"⚠️ Sync failed: {e}")
        return
    total = sum(counts.values())
    detail = ", ".join(f"{k}={v}" for k, v in counts.items()) or "nothing to sync"
    try:
        await status.edit_text(f"✅ Synced {total} record(s): {detail}")
    except Exception:
        await update.effective_message.reply_text(f"✅ Synced {total} record(s): {detail}")


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


def _readiness_markup(snapshot: dict) -> InlineKeyboardMarkup:
    rows = []
    doubts = list(snapshot.get("doubts") or [])
    phase = snapshot.get("phase")
    if phase == "day":
        doubts = [row for row in doubts if row.get("readiness") == "ready"][:5]
    elif phase == "t1":
        doubts = doubts[:6]
    else:
        doubts = doubts[:10]
    for doubt in doubts:
        token = doubt.get("readiness_token")
        if not token:
            continue
        title = str(doubt.get("core_concept") or "doubt")[:18]
        rows.append([
            InlineKeyboardButton(
                f"Open · {title}", callback_data=f"ready:open:{token}"
            ),
            InlineKeyboardButton(
                f"Solved · {title}", callback_data=f"ready:solve:{token}"
            ),
            InlineKeyboardButton(
                f"Not here · {title}", callback_data=f"ready:exclude:{token}"
            ),
        ])
    for doubt in (snapshot.get("excluded_doubts") or [])[:5]:
        token = doubt.get("readiness_token")
        if token:
            title = str(doubt.get("core_concept") or "doubt")[:24]
            rows.append([InlineKeyboardButton(
                f"↩ Include: {title}", callback_data=f"ready:open:{token}"
            )])
    exam_id = str(snapshot.get("exam_id") or "")
    if exam_id:
        rows.append([InlineKeyboardButton(
            "↻ Refresh evidence", callback_data=f"ready:refresh:{exam_id}"
        )])
    return InlineKeyboardMarkup(rows)


async def _send_readiness_snapshot(
    bot, chat_id: int, snapshot: dict, *, claim_event: bool = False,
) -> bool:
    event = None
    if claim_event:
        event = exam_readiness.event_key(snapshot["exam"], snapshot["phase"])
        if not reminders.claim(event):
            return False
    try:
        await _send_markdown(
            bot, chat_id, message_templates.exam_readiness(snapshot),
            reply_markup=_readiness_markup(snapshot),
        )
    except Exception:
        if event is not None:
            await asyncio.to_thread(reminders.release, event)
        raise
    return True


async def _send_current_readiness_reviews(
    bot, chat_id: int, *, sync_first: bool = True,
) -> int:
    """Send every currently due readiness window, deduplicated durably."""
    if sync_first:
        await _sync_domain()
    sent = 0
    for exam, phase in await asyncio.to_thread(exam_readiness.scheduled_reviews):
        snapshot = await asyncio.to_thread(
            exam_readiness.collect, exam, phase=phase
        )
        if await _send_readiness_snapshot(
            bot, chat_id, snapshot, claim_event=True
        ):
            sent += 1
    return sent


async def readiness_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    try:
        await _sync_domain()
        raw = _command_args(update)
        query, separator, syllabus = raw.partition("|")
        exam = await asyncio.to_thread(exam_readiness.select_exam, query.strip())
        if separator:
            syllabus = syllabus.strip()
            if not syllabus:
                raise study_domain.DomainError(
                    "after |, list the chapters or subjects in this exam"
                )
            exam = await asyncio.to_thread(
                operational_store.update, "exams", exam["notion_page_id"],
                {"syllabus": syllabus},
            )
        snapshot = await asyncio.to_thread(exam_readiness.collect, exam)
        await _reply_markdown(
            update.effective_message,
            message_templates.exam_readiness(snapshot),
            reply_markup=_readiness_markup(snapshot),
        )
    except Exception as exc:
        logger.exception("readiness command failed")
        await update.effective_message.reply_text(f"Readiness audit unavailable: {exc}")


async def on_readiness_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()
    if not _is_allowed(update):
        return
    try:
        _, action, value = query.data.split(":", 2)
        if action == "refresh":
            await _sync_domain()
            exam = await asyncio.to_thread(exam_readiness.exam_by_id, value)
        else:
            item = await asyncio.to_thread(exam_readiness.item_for_token, value)
            exam = await asyncio.to_thread(
                exam_readiness.exam_by_id, str(item["exam_id"])
            )
            if action == "open":
                await asyncio.to_thread(exam_readiness.set_decision, value, "open")
            elif action == "exclude":
                await asyncio.to_thread(
                    exam_readiness.set_decision, value, "not_in_exam"
                )
            elif action == "solve":
                await asyncio.to_thread(
                    exam_readiness.start_resolution, update.effective_chat.id, value
                )
                doubt = study_domain._row(
                    "doubts", "notion_page_id=?", (str(item["doubt_id"]),)
                ) or {}
                await update.effective_message.reply_text(
                    f"To mark ‘{doubt.get('core_concept') or 'this doubt'}’ solved, "
                    "send the corrected idea or method you now understand. "
                    "A status-only answer such as ‘done’ will be rejected. "
                    "Send ‘cancel’ to leave it open.",
                    reply_markup=ForceReply(selective=True),
                )
                return
            else:
                return
        snapshot = await asyncio.to_thread(exam_readiness.collect, exam)
        await _edit_markdown(
            query,
            message_templates.exam_readiness(snapshot),
            reply_markup=_readiness_markup(snapshot),
        )
    except Exception as exc:
        logger.exception("readiness callback failed")
        try:
            await query.edit_message_text(f"Readiness action was not applied: {exc}")
        except Exception:
            pass


def _reset_menu() -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "🧨 Guarded reset\n\n"
        "Choose a scope. The next screen gives an exact full sentence with a "
        "one-time token; a button alone can never delete anything. A verified "
        "backup is created before mutation.\n\n"
        "• SQLite data — erase rows; preserve the file, tables, indexes and schemas.\n"
        "  Notion-mirror rows can return on sync because this scope preserves Notion pages.\n"
        "• Notion pages — archive every page; preserve every database, schema and ID.\n"
        "• Context — erase active session/conversation drafts only; keep study records.\n"
        "• Everything — archive Notion pages, then erase local data and restart onboarding. "
        "It stops before local deletion if any Notion page fails."
    )
    markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("SQLite data", callback_data="reset:select:sqlite"),
            InlineKeyboardButton("Notion pages", callback_data="reset:select:notion"),
        ],
        [
            InlineKeyboardButton("Context", callback_data="reset:select:context"),
            InlineKeyboardButton("Everything", callback_data="reset:select:everything"),
        ],
        [InlineKeyboardButton("Cancel", callback_data="reset:cancel:none")],
    ])
    return text, markup


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    reset_service.cancel_confirmation(update.effective_chat.id)
    text, markup = _reset_menu()
    await update.effective_message.reply_text(text, reply_markup=markup)


async def on_reset_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()
    if not _is_allowed(update):
        return
    try:
        _, action, scope = query.data.split(":", 2)
        if action == "cancel":
            reset_service.cancel_confirmation(update.effective_chat.id)
            await query.edit_message_text("Reset cancelled. Nothing was changed.")
            return
        if action != "select" or scope not in reset_service.SCOPES:
            return
        pending = await asyncio.to_thread(
            reset_service.create_confirmation, update.effective_chat.id, scope
        )
        await query.edit_message_text(
            f"⚠️ Final confirmation · {reset_service.SCOPE_LABELS[scope]}\n\n"
            "Copy and send the following sentence exactly, including punctuation "
            f"and token, within {reset_service.CONFIRM_TTL_MINUTES} minutes:\n\n"
            f"{pending['sentence']}\n\n"
            "Nothing has been deleted yet.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                "Cancel reset", callback_data="reset:cancel:none"
            )]]),
        )
    except Exception as exc:
        logger.exception("reset callback failed")
        try:
            await query.edit_message_text(f"Reset was not armed: {exc}")
        except Exception:
            pass


def _relevance_warning(parsed: dict) -> list[str]:
    """One honest line when a capture doesn't look study-related. Never blocks."""
    if parsed.get("study_related", True):
        return []
    note = str(parsed.get("relevance_note") or "").strip()
    line = "⚠️ This doesn't look related to your studies — keep it anyway?"
    return [f"{line} ({note})" if note else line]


async def _handle_remember(update: Update, statement: str, chat_id: int) -> None:
    """Parse a commitment/preference, show conflicts, and ask for confirmation."""
    message = update.effective_message
    statement = (statement or "").strip()
    if not statement:
        await message.reply_text(
            "What should I remember? e.g. /remember from now on I'll do PYQs every day"
        )
        return
    try:
        data = await asyncio.to_thread(domain_parser.parse_commitment, statement)
    except Exception as exc:
        logger.exception("commitment parse failed")
        await message.reply_text(f"I couldn't understand that safely: {exc}")
        return
    if data.get("needs_clarification"):
        await message.reply_text(
            data.get("clarification_question") or "What exactly should I track?"
        )
        return
    if data.get("kind") == "bot_instruction":
        # "every weekday tell me X" is a job for the bot, not a study
        # commitment — route it into the /jobs scheduler with its confirm.
        await _handle_job_create(
            update, chat_id, statement,
            intro="That's a scheduled job for me, not a study commitment — here's what I'll set up:",
        )
        return
    if data.get("kind") == "preference":
        pref_text = str(data.get("title") or statement).strip()
        lines = ["Preference to remember", f"“{pref_text}”"]
        lines.extend(_relevance_warning(data))
        lines.append("I'll keep this in mind when advising. Save?")
        preview = "\n".join(lines)
        draft_id = draft_store.create_draft(
            chat_id,
            {"kind": "preference", "data": {"chat_id": chat_id, "text": pref_text}},
            [preview],
        )
        await message.reply_text(
            preview,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Save", callback_data=f"domain:confirm:{draft_id}"),
                InlineKeyboardButton("Cancel", callback_data=f"domain:cancel:{draft_id}"),
            ]]),
        )
        return
    goal_data = {
        key: data.get(key)
        for key in ("title", "goal_type", "metric", "target", "period", "subject", "source_text")
    }
    goal_data["operation_id"] = uuid.uuid4().hex
    await _sync_domain()
    conflicts = await asyncio.to_thread(commitments.capture_conflicts, goal_data)
    verifiable = (
        goal_data.get("goal_type") in commitments._GOAL_TYPE_EXPR
        and commitments.ledger_filter_for_goal(goal_data) is not None
    )
    lines = [
        "Commitment draft",
        f"Title: {goal_data['title']}",
        f"Track: {commitments.format_target(goal_data['target'], goal_data.get('metric'), goal_data['period'])}",
    ]
    if goal_data.get("subject"):
        lines.append(f"Subject: {goal_data['subject']}")
    replace_goal_id = None
    for conflict in conflicts:
        lines.append(f"⚠️ {conflict['message']}")
        if conflict.get("goal_id") and replace_goal_id is None:
            replace_goal_id = conflict["goal_id"]
    lines.extend(_relevance_warning(data))
    if verifiable:
        lines.append("I'll verify this nightly against your ledger and track your streak.")
        if re.search(r"\b(morning|evening|afternoon|night|noon)\b", statement, re.IGNORECASE):
            lines.append(
                "ℹ️ Honest note: I verify this per-DAY from your logs — "
                "I can't check the time of day yet."
            )
    else:
        lines.append(
            "⚠️ I can't auto-verify this from your logged sessions — "
            "I'll only watch your daily plan for it."
        )
    lines.append("Save this commitment?")
    preview = "\n".join(lines)
    payload: dict = {"kind": "commitment", "data": goal_data}
    if replace_goal_id:
        payload["replace_goal_id"] = replace_goal_id
    draft_id = draft_store.create_draft(chat_id, payload, [preview])
    buttons = [InlineKeyboardButton("Save", callback_data=f"domain:confirm:{draft_id}")]
    if replace_goal_id:
        buttons.append(
            InlineKeyboardButton("Replace old", callback_data=f"domain:replace:{draft_id}")
        )
    buttons.append(InlineKeyboardButton("Cancel", callback_data=f"domain:cancel:{draft_id}"))
    await message.reply_text(preview, reply_markup=InlineKeyboardMarkup([buttons]))


async def remember_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    text = _command_args(update)
    chat_id = update.effective_chat.id
    if text:
        await _handle_remember(update, text, chat_id)
        return
    await _sync_domain()
    lines: list[str] = []
    goals = study_domain._rows(
        "goals", "archived=0 AND status='Active' AND period IN ('Daily','Weekly')"
    )
    if goals:
        lines.append("Commitments:")
        today = session_context.local_today_iso()
        for i, goal in enumerate(goals, 1):
            entry = (
                f"{i}. {goal.get('title')} — "
                f"{commitments.format_target(goal.get('target'), goal.get('metric'), goal.get('period'))}"
            )
            goal_id = goal.get("notion_page_id")
            if goal_id and goal.get("period") == "Daily":
                days = commitments.streak(goal_id, as_of=today)
                stats = commitments.adherence(goal_id, as_of=today)
                if stats["total"]:
                    entry += f" — streak {days}, last 7d {stats['met']}/{stats['total']}"
            lines.append(entry)
    prefs = commitments.active_prefs(chat_id)
    if prefs:
        lines.append("Preferences:")
        lines.extend(f"#{p['id']} {p['text']}" for p in prefs)
    await update.effective_message.reply_text(
        "\n".join(lines)
        or "Nothing remembered yet. Try /remember from now on I'll do PYQs every day"
    )


async def forget_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    text = _command_args(update)
    chat_id = update.effective_chat.id
    if not text:
        await update.effective_message.reply_text(
            "Tell me what to forget: /forget <preference text or #id, or commitment title>"
        )
        return
    pref = commitments.deactivate_pref(chat_id, text.lstrip("#").strip())
    if pref:
        await update.effective_message.reply_text(f"Forgotten: “{pref['text']}”")
        return
    try:
        await asyncio.to_thread(study_domain.update_goal_status, text, "Paused")
        await update.effective_message.reply_text(
            "Commitment paused — nightly checks stop. Use /goal resume <name> to restart it."
        )
    except Exception as exc:
        await update.effective_message.reply_text(f"Nothing matched that: {exc}")


async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    try:
        await _sync_domain()
        facts = await asyncio.to_thread(planner.analyze)
        lines = [
            f"*Plan {facts['plan_date']}: {facts['outcome']}*",
            f"Expected CY: `{facts['expected_cy']:g}`",
            f"Adaptive target: `{facts['pace']['target']}` ({facts['pace']['phase']})",
            f"Planned minutes: `{facts['planned_minutes']:g}`",
            f"CY headroom: `{facts['capacity_headroom_cy']:g}`",
            f"Coaching homework: `{facts['homework_planned_count']}/{facts['homework_pending_count']}` planned",
            f"Backlog: `{facts['backlog_count']}` tracked, `{facts['unplanned_backlog_count']}` unplanned",
            "",
        ]
        for row in facts["items"]:
            lines.append(
                f"{int(row.get('sequence') or 0)}. {row.get('title')} "
                f"[{row.get('status') or 'Planned'}]"
            )
        if facts["warnings"]:
            lines.append("")
            lines.extend(f"⚠️ {w}" for w in facts["warnings"])
        if facts["errors"]:
            lines.append("")
            lines.extend(f"🚫 {e}" for e in facts["errors"])
        suggestions = facts.get("suggestions", [])
        if suggestions:
            lines.append("")
            lines.append("*Suggestions:*")
            for s in suggestions:
                line = f"• {s.get('action')} — {s.get('reason')}"
                if s.get("kind") == "goal" and s.get("goal"):
                    try:
                        row = next(
                            (g for g in commitments.active_daily_goals()
                             if str(g.get("title")) == str(s["goal"])), None,
                        )
                        days = commitments.streak(row["notion_page_id"]) if row and row.get("notion_page_id") else 0
                        if days:
                            line += f" ({days}-day streak at risk)"
                    except Exception:
                        pass
                lines.append(line)
        await _reply_markdown(
            update.effective_message,
            "\n".join(lines) or "No Notion plan items found for today.",
        )
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
    rows = study_domain.doubt_queue()
    await _reply_markdown(update.effective_message, message_templates.doubt_dashboard(rows))


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
        await _reply_markdown(
            update.effective_message,
            message_templates.attempt_result(result, parts[0])
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
        doubt_title = parts[0]
        resolution = parts[1]
        doubt = await asyncio.to_thread(study_domain._title_match, "doubts", doubt_title)
        if not doubt:
            await update.effective_message.reply_text(f"No doubt matches {doubt_title!r}")
            return
        doubt_id = str(doubt["notion_page_id"])
        attempts = await asyncio.to_thread(study_domain._attempt_rows, doubt_id)
        valid_attempts = sum(1 for a in attempts if a.get("valid"))
        if teacher and valid_attempts < 2:
            await update.effective_message.reply_text(
                "Teacher escalation requires two valid attempts first."
            )
            return
        verification = await asyncio.to_thread(
            _generate_verification_question, doubt_title, resolution, doubt
        )
        draft_store.set_pending_doubt_resolution(
            update.effective_chat.id, doubt_id, doubt_title,
            resolution, teacher, verification,
        )
        await update.effective_message.reply_text(
            f"Before I mark this as solved, verify you really understand:\n\n"
            f"{verification}\n\n"
            f"_Reply with your answer, or type 'skip' to resolve without verification._"
        )
    except Exception as exc:
        await update.effective_message.reply_text(f"Doubt was not closed: {exc}")


def _generate_verification_question(
    doubt_title: str, resolution: str, doubt: dict
) -> str:
    try:
        from llm import router
        prompt = (
            f"The user claims to have resolved this doubt: '{doubt_title}'.\n"
            f"Their resolution: '{resolution}'.\n"
            f"Generate ONE short verification question that tests whether they "
            f"truly understand the concept. The question should be answerable in "
            f"one sentence. Return only the question, no preamble."
        )
        response = router.complete(router.LLMRequest(
            messages=[{"role": "user", "content": prompt}],
            purpose="domain", max_output_tokens=200, temperature=0.3,
        ))
        return response.text.strip()
    except Exception:
        return (
            f"In your own words, explain the key insight that resolved "
            f"'{doubt_title}'. What was the core misunderstanding?"
        )


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
    doubt_access = re.match(r"^doubts\s+(on|off)\s+(.+)$", text, re.IGNORECASE)
    if doubt_access:
        value, title = doubt_access.groups()
        try:
            await asyncio.to_thread(
                study_domain.set_timetable_questions_allowed,
                title, value.lower() == "on",
            )
            await update.effective_message.reply_text(
                f"Question access {'enabled' if value.lower() == 'on' else 'disabled'} for {title}."
            )
        except Exception as exc:
            await update.effective_message.reply_text(f"Timetable entry was not changed: {exc}")
        return
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
                "Format: /timetable add title | weekday | start | end | kind | subject | teacher | location | questions yes/no"
            )
            return
        fields = (
            "title", "weekday", "start_time", "end_time", "kind", "subject",
            "teacher", "location", "questions_allowed",
        )
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
        f"{r.get('weekday')} {r.get('start_time')}-{r.get('end_time')} — "
        f"{r.get('kind')} {r.get('subject') or ''} {r.get('teacher') or ''}"
        f"{' · questions allowed' if r.get('questions_allowed') else ''}"
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
        f"*Weekly growth ({report['start']} → {report['end']})*",
        f"CY: `{ledger.get('cy', 0):g}` | blocks: `{ledger.get('blocks', 0)}`",
        f"CY change vs previous week: `{report['cy_delta']:+g}`",
        f"Accuracy: `{accuracy:.1f}%`" if accuracy is not None else "Accuracy: no complete data",
        f"Completed work: `{report['completed_work']}`",
        f"Backlog: `{report['backlog']}`",
        f"Valid doubt attempts: `{report['valid_doubt_attempts']}`",
        f"Exam mistakes reviewed: `{report['exam_mistakes']['n']}`",
    ]
    if report.get("top_failure"):
        lines.append(
            f"Main repair target: `{report['top_failure']['failure_type']}` "
            f"({report['top_failure']['n']} reviewed mistake(s))"
        )
    lines.append("")
    if report["cy_delta"] > 0:
        lines.append("📈 Growth signal: output increased. Protect the accuracy that produced it.")
    elif ledger.get("blocks", 0):
        lines.append("📊 Growth signal: keep the next week focused on completion and repeated-error removal.")
    else:
        lines.append("📊 Growth signal: complete data from the next block will establish the baseline.")
    await _reply_markdown(update.effective_message, "\n".join(lines))


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
    if action not in ("confirm", "replace"):
        return
    try:
        created_exam = None
        setup_created_exam = False
        kind = draft["payload"].get("kind")
        data = draft["payload"].get("data") or {}
        if kind == "goal":
            await asyncio.to_thread(study_domain.create_goal, data)
            message = "Goal saved to SQLite."
        elif kind == "exam":
            created_exam = await asyncio.to_thread(study_domain.create_exam, data)
            message = "Exam saved to SQLite."
        elif kind == "commitment":
            replaced = draft["payload"].get("replace_goal_id")
            if action == "replace" and replaced:
                await asyncio.to_thread(
                    operational_store.update, "goals", replaced, {"status": "Cancelled"}
                )
            await asyncio.to_thread(study_domain.create_goal, data)
            message = (
                "Commitment saved — I'll verify it nightly against your ledger "
                "and nudge you each morning."
            )
        elif kind == "preference":
            await asyncio.to_thread(
                commitments.add_pref, data["chat_id"], data["text"]
            )
            message = "Preference remembered."
        elif kind == "setup_ai":
            setup_created_exam = any(
                action.get("type") == "create_exam"
                for action in (data.get("actions") or [])
                if isinstance(action, dict)
            )
            results, skip = await asyncio.to_thread(
                onboarding.apply_ai_actions, draft["chat_id"], data.get("actions") or []
            )
            message = "🤖 Done:\n" + "\n".join(results)
            if skip:
                nxt = await asyncio.to_thread(onboarding.advance, draft["chat_id"])
                if nxt:
                    next_text, next_markup = await asyncio.to_thread(_setup_section_view, nxt)
                else:
                    next_text, next_markup = await asyncio.to_thread(_setup_hub_view)
                await context.bot.send_message(
                    chat_id=draft["chat_id"], text=next_text, reply_markup=next_markup
                )
        elif kind == "job":
            job = await asyncio.to_thread(
                user_jobs.create_job, draft["chat_id"], data
            )
            message = f"⏰ Job created — {user_jobs.describe(job)}"
            # A job whose slot already passed today would fire at the next
            # 60-s scan; pre-claim today so the first run is the next slot.
            now = session_context.local_now()
            if user_jobs.should_preclaim_today(job, now):
                reminders.claim(f"user-job:{job['id']}:{now.date().isoformat()}")
                message += "\nFirst run: the next scheduled slot (not today — that time already passed)."
            message += "\nManage it anytime with /jobs."
        else:
            raise study_domain.DomainError("unknown domain draft")
        if kind in ("commitment", "preference"):
            try:
                warn = await asyncio.to_thread(
                    memory_map.budget_warning, draft["chat_id"]
                )
                if warn:
                    message += "\n" + warn
            except Exception:
                logger.debug("budget warning failed", exc_info=True)
        draft_store.delete_draft(draft_id)
        await query.edit_message_text(message)
        if created_exam is not None:
            phase = exam_readiness.immediate_phase(created_exam)
            if phase:
                try:
                    await _sync_domain()
                    snapshot = await asyncio.to_thread(
                        exam_readiness.collect, created_exam, phase=phase
                    )
                    await _send_readiness_snapshot(
                        context.bot, draft["chat_id"], snapshot, claim_event=True
                    )
                except Exception:
                    # The confirmed exam remains saved.  A readiness rendering
                    # problem must never turn that successful write into a
                    # misleading retry prompt.
                    logger.exception("immediate exam readiness audit failed")
        elif setup_created_exam:
            try:
                await _send_current_readiness_reviews(
                    context.bot, draft["chat_id"], sync_first=True
                )
            except Exception:
                logger.exception("setup AI readiness audit failed")
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


def _setup_hub_view() -> tuple[str, InlineKeyboardMarkup]:
    stats = onboarding.status()
    completed = sum(1 for s in onboarding.SECTIONS if stats.get(s["id"], {}).get("ok"))
    total = len(onboarding.SECTIONS)
    lines = [
        "*🚀 Setup — what I need to run every engine*",
        f"Progress: `{completed}/{total}` complete",
        "_Ledger, doubts & revision sync from Notion automatically — these "
        "are the things I can't discover myself._",
        "",
    ]
    for section in onboarding.SECTIONS:
        st = stats.get(section["id"], {"ok": False, "detail": ""})
        mark = "✅" if st["ok"] else "⚠️"
        lines.append(f"{mark} *{section['title']}* — {st['detail']}")
    lines.append("")
    lines.append("Tap a section to fill it, or run everything in order.")
    sec_buttons = [
        InlineKeyboardButton(s["title"], callback_data=f"onb:sec:{s['id']}")
        for s in onboarding.SECTIONS
    ]
    rows = [sec_buttons[i:i + 2] for i in range(0, len(sec_buttons), 2)]
    rows.append([
        InlineKeyboardButton("▶ Run full setup", callback_data="onb:runall"),
        InlineKeyboardButton("✔ Finish", callback_data="onb:finish"),
    ])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _setup_section_view(section_id: str, *, mode: str = "single") -> tuple[str, InlineKeyboardMarkup]:
    section = onboarding.section_by_id(section_id)
    prompt = section["prompt"]
    if section_id == "chapters":
        prompt = onboarding.chapters_prompt()
    try:
        personalized = onboarding.personalized_prompt(section_id)
        if personalized and personalized != prompt:
            prompt = personalized
    except Exception:
        pass
    lines = []
    if mode == "run_all":
        idx = onboarding.SECTION_IDS.index(section_id) + 1
        total = len(onboarding.SECTIONS)
        lines.append(f"*Step {idx}/{total}*")
        lines.append("")
    lines.append(f"*{section['title']}*")
    lines.append("")
    lines.append(prompt)
    if section.get("hint"):
        lines.append(f"_{section['hint']}_")
    lines.append("💡 Free-form? Start with `ai ` and describe — I'll work out what to do.")
    rows: list[list[InlineKeyboardButton]] = []
    if section["kind"] == "buttons":
        opts = [
            InlineKeyboardButton(label, callback_data=f"onb:pick:{section_id}:{value}")
            for label, value in section.get("options", [])
        ]
        rows.extend(opts[i:i + 2] for i in range(0, len(opts), 2))
    if section_id == "rhythm":
        rows.append([InlineKeyboardButton("⏰ Open reminder settings", callback_data="settings:cat:0")])
    last = []
    if mode == "run_all" and onboarding.SECTION_IDS.index(section_id) > 0:
        last.append(InlineKeyboardButton("◀ Back", callback_data=f"onb:back:{section_id}"))
    if section["kind"] == "loop":
        last.append(InlineKeyboardButton("Done ✅", callback_data="onb:done"))
    last.append(InlineKeyboardButton("Skip ▸", callback_data="onb:skip"))
    last.append(InlineKeyboardButton("↩ Hub", callback_data="onb:hub"))
    rows.append(last)
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _setup_finish_summary() -> str:
    stats = onboarding.status()
    lines = ["*🎉 Setup done — here's where you stand:*", ""]
    for section in onboarding.SECTIONS:
        st = stats.get(section["id"], {"ok": False, "detail": ""})
        lines.append(f"{'✅' if st['ok'] else '⚠️'} *{section['title']}* — {st['detail']}")
    lines.append("")
    lines.append("Edit anytime: /setup · /settings · /memory. Now just study and talk to me normally.")
    return "\n".join(lines)


async def setup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    onboarding.clear(update.effective_chat.id)
    text, markup = await asyncio.to_thread(_setup_hub_view)
    await _reply_markdown(update.effective_message, text, reply_markup=markup)


async def _setup_after_answer(chat_id: int, reply: str, advance_now: bool):
    if advance_now:
        nxt = await asyncio.to_thread(onboarding.advance, chat_id)
    else:
        state = onboarding.active_section(chat_id)
        nxt = state[0] if state else None
    mode = "single"
    if nxt:
        state = onboarding.active_section(chat_id)
        if state and state[1] == "run_all":
            mode = "run_all"
        text, markup = await asyncio.to_thread(_setup_section_view, nxt, mode=mode)
    else:
        text, markup = await asyncio.to_thread(_setup_hub_view)
    return reply + "\n\n" + text, markup


async def on_onboarding_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not _is_allowed(update):
        return
    chat_id = update.effective_chat.id
    parts = query.data.split(":", 3)
    action = parts[1] if len(parts) > 1 else "hub"
    try:
        if action == "hub":
            onboarding.clear(chat_id)
            text, markup = await asyncio.to_thread(_setup_hub_view)
        elif action == "sec" and len(parts) > 2:
            onboarding.start(chat_id, parts[2], "single")
            text, markup = await asyncio.to_thread(_setup_section_view, parts[2], mode="single")
        elif action == "runall":
            stats = await asyncio.to_thread(onboarding.status)
            first = next(
                (sid for sid in onboarding.SECTION_IDS if not stats[sid]["ok"]), None
            )
            if first is None:
                onboarding.mark_complete(chat_id)
                text, markup = await asyncio.to_thread(_setup_hub_view)
                text = "🎉 Everything is already set!\n\n" + text
            else:
                onboarding.start(chat_id, first, "run_all")
                text, markup = await asyncio.to_thread(_setup_section_view, first, mode="run_all")
        elif action == "pick" and len(parts) > 3:
            _ok, reply, adv = await asyncio.to_thread(
                onboarding.apply_answer, chat_id, parts[2], parts[3]
            )
            text, markup = await _setup_after_answer(chat_id, reply, adv)
        elif action == "back" and len(parts) > 2:
            section_id = parts[2]
            idx = onboarding.SECTION_IDS.index(section_id) if section_id in onboarding.SECTION_IDS else 0
            prev_idx = max(0, idx - 1)
            prev_id = onboarding.SECTION_IDS[prev_idx]
            onboarding.start(chat_id, prev_id, "run_all")
            text, markup = await asyncio.to_thread(_setup_section_view, prev_id, mode="run_all")
        elif action in ("skip", "done"):
            nxt = await asyncio.to_thread(onboarding.advance, chat_id)
            if nxt:
                state = onboarding.active_section(chat_id)
                mode = "run_all" if state and state[1] == "run_all" else "single"
                text, markup = await asyncio.to_thread(_setup_section_view, nxt, mode=mode)
            else:
                text, markup = await asyncio.to_thread(_setup_hub_view)
        elif action == "finish":
            onboarding.mark_complete(chat_id)
            summary = await asyncio.to_thread(_setup_finish_summary)
            try:
                await _edit_markdown(query, summary)
            except Exception:
                pass
            return
        else:
            return
        try:
            await _edit_markdown(query, text, reply_markup=markup)
        except Exception:
            pass  # "message is not modified" and similar cosmetic failures
    except Exception as exc:
        logger.exception("onboarding callback failed")
        try:
            await query.edit_message_text(f"⚠️ Setup action failed: {exc}")
        except Exception:
            pass


async def _handle_setup_ai(update: Update, chat_id: int, section_id: str, text: str) -> None:
    """AI escape hatch: free-form setup answer → bounded action plan → confirm."""
    message = update.effective_message
    section = onboarding.section_by_id(section_id) or {"title": section_id, "prompt": ""}
    prompt = section.get("prompt") or (
        onboarding.chapters_prompt() if section_id == "chapters" else ""
    )
    status_msg = await message.reply_text("🤖 Working out what to do…")
    try:
        parsed = await asyncio.to_thread(
            domain_parser.parse_setup_ai, section["title"], prompt, text
        )
    except Exception as exc:
        logger.exception("setup AI parse failed")
        await status_msg.edit_text(f"⚠️ AI couldn't process that: {exc}")
        return
    if parsed.get("needs_clarification"):
        await status_msg.edit_text(
            parsed.get("clarification_question") or "Could you clarify that?"
        )
        return
    actions, errors = onboarding.validate_ai_actions(parsed.get("actions") or [])
    reply = str(parsed.get("reply") or "").strip()
    if not actions:
        note = ("\n(nothing safe to do: " + "; ".join(errors) + ")") if errors else ""
        await status_msg.edit_text(
            (reply or "I couldn't find anything actionable there.") + note
        )
        return
    lines = [f"🤖 {reply}" if reply else "🤖 Here's what I'll do:", ""]
    lines.extend(onboarding.describe_ai_actions(actions))
    lines.extend(_relevance_warning(parsed))
    if errors:
        lines.append("")
        lines.append("⚠️ Ignored (invalid): " + "; ".join(errors))
    lines.append("")
    lines.append("Go ahead?")
    preview = "\n".join(lines)
    draft_id = draft_store.create_draft(
        chat_id, {"kind": "setup_ai", "data": {"actions": actions}}, [preview]
    )
    await status_msg.edit_text(
        preview,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Confirm ✅", callback_data=f"domain:confirm:{draft_id}"),
            InlineKeyboardButton("Cancel", callback_data=f"domain:cancel:{draft_id}"),
        ]]),
    )


def _jobs_list_view(chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    jobs = user_jobs.list_jobs(chat_id)
    lines = ["⏰ Your scheduled jobs"]
    rows: list[list[InlineKeyboardButton]] = []
    if jobs:
        lines.append("")
        for job in jobs:
            state = "▶" if job["enabled"] else "⏸"
            lines.append(f"{state} #{job['id']} {job['title']} — {user_jobs.schedule_text(job)}")
            rows.append([InlineKeyboardButton(
                f"⚙ {job['title'][:28]}", callback_data=f"jobs:view:{job['id']}"
            )])
    else:
        lines.append("")
        lines.append("No jobs yet.")
    lines.append("")
    lines.append("Create one in plain words:\n/jobs every weekday at 21:00 tell me my overall week cognitive yield")
    rows.append([InlineKeyboardButton("🔄 Refresh", callback_data="jobs:list")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _job_detail_view(job_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    job = user_jobs.get_job(job_id)
    if job is None:
        return None
    lines = [
        f"⚙ Job #{job['id']}: {job['title']}",
        f"When: {user_jobs.schedule_text(job)}",
        f"Does: {'🤖 answers' if job['action_kind'] == 'ask' else '🔔 sends'} “{job['action_text']}”",
        f"State: {'active ▶' if job['enabled'] else 'paused ⏸'}"
        + (f" · last ran {str(job['last_run'])[:16]}" if job.get("last_run") else ""),
    ]
    toggle = ("⏸ Pause", "pause") if job["enabled"] else ("▶ Resume", "resume")
    rows = [
        [InlineKeyboardButton(toggle[0], callback_data=f"jobs:toggle:{job_id}"),
         InlineKeyboardButton("▶ Run now", callback_data=f"jobs:run:{job_id}")],
        [InlineKeyboardButton("✏️ Time", callback_data=f"jobs:edittime:{job_id}"),
         InlineKeyboardButton("✏️ Text", callback_data=f"jobs:edittext:{job_id}"),
         InlineKeyboardButton("🗑 Delete", callback_data=f"jobs:del:{job_id}")],
        [InlineKeyboardButton("↩ All jobs", callback_data="jobs:list")],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def _handle_job_create(
    update: Update, chat_id: int, text: str, intro: str | None = None
) -> None:
    message = update.effective_message
    status_msg = await message.reply_text("⏰ Working out the schedule…")
    try:
        parsed = await asyncio.to_thread(domain_parser.parse_job, text)
    except Exception as exc:
        logger.exception("job parse failed")
        await status_msg.edit_text(f"⚠️ Couldn't parse that job: {exc}")
        return
    if parsed.get("needs_clarification"):
        await status_msg.edit_text(
            parsed.get("clarification_question") or "What time should this run?"
        )
        return
    data, error = user_jobs.validate_parsed(parsed)
    if data is None:
        await status_msg.edit_text(f"⚠️ {error}")
        return
    lines = [intro] if intro else []
    lines += [
        "⏰ Job draft",
        f"Title: {data['title']}",
        f"When: {user_jobs.schedule_text(data)}",
        f"Does: {'🤖 answer' if data['action_kind'] == 'ask' else '🔔 send'} “{data['action_text']}”",
    ]
    for overlap in user_jobs.builtin_overlaps(data):
        lines.append(f"⚠️ {overlap}")
    lines.extend(_relevance_warning(parsed))
    note = str(parsed.get("note") or "").strip()
    if note:
        lines.append(f"ℹ️ {note}")
    lines.append("Create this job?")
    preview = "\n".join(lines)
    draft_id = draft_store.create_draft(
        chat_id, {"kind": "job", "data": data}, [preview]
    )
    await status_msg.edit_text(
        preview,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Create ✅", callback_data=f"domain:confirm:{draft_id}"),
            InlineKeyboardButton("Cancel", callback_data=f"domain:cancel:{draft_id}"),
        ]]),
    )


async def jobs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    chat_id = update.effective_chat.id
    text = _command_args(update)
    if text:
        await _handle_job_create(update, chat_id, text)
        return
    user_jobs.clear_pending_edit(chat_id)
    view_text, markup = await asyncio.to_thread(_jobs_list_view, chat_id)
    await update.effective_message.reply_text(view_text, reply_markup=markup)


async def _run_user_job(
    job: dict, context: ContextTypes.DEFAULT_TYPE, *, consume_once: bool = True
) -> None:
    chat_id = int(job["chat_id"])
    if job["action_kind"] == "message":
        await context.bot.send_message(chat_id=chat_id, text=f"🔔 {job['title']}\n{job['action_text']}")
    else:
        answer = await asyncio.to_thread(
            sql_query_flow.answer_question, job["action_text"], chat_id=chat_id
        )
        await context.bot.send_message(chat_id=chat_id, text=f"⏰ {job['title']}\n{answer}")
    await asyncio.to_thread(user_jobs.mark_ran, job["id"], consume_once=consume_once)


async def on_jobs_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not _is_allowed(update):
        return
    chat_id = update.effective_chat.id
    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else "list"
    job_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
    try:
        if action == "view" and job_id is not None:
            detail = _job_detail_view(job_id)
            if detail is None:
                text, markup = await asyncio.to_thread(_jobs_list_view, chat_id)
            else:
                text, markup = detail
        elif action == "toggle" and job_id is not None:
            job = user_jobs.get_job(job_id)
            if job:
                user_jobs.set_enabled(job_id, not job["enabled"])
            text, markup = _job_detail_view(job_id) or await asyncio.to_thread(_jobs_list_view, chat_id)
        elif action == "run" and job_id is not None:
            job = user_jobs.get_job(job_id)
            if job:
                await _run_user_job(job, context, consume_once=False)
            text, markup = _job_detail_view(job_id) or await asyncio.to_thread(_jobs_list_view, chat_id)
            text = "✅ Ran it — result sent below.\n\n" + text
        elif action == "del" and job_id is not None:
            job = user_jobs.get_job(job_id)
            title = job["title"] if job else "?"
            text = f"Delete job “{title}” for good?"
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("🗑 Yes, delete", callback_data=f"jobs:delyes:{job_id}"),
                InlineKeyboardButton("↩ Keep", callback_data=f"jobs:view:{job_id}"),
            ]])
        elif action == "delyes" and job_id is not None:
            user_jobs.delete_job(job_id)
            text, markup = await asyncio.to_thread(_jobs_list_view, chat_id)
            text = "🗑 Deleted.\n\n" + text
        elif action in ("edittime", "edittext") and job_id is not None:
            field = "time" if action == "edittime" else "text"
            user_jobs.set_pending_edit(chat_id, job_id, field)
            prompt = (
                "Send the new time as HH:MM (24h)." if field == "time"
                else "Send the new question/reminder text."
            )
            text = f"✏️ Editing job #{job_id} {field}.\n{prompt}"
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("↩ Cancel", callback_data=f"jobs:view:{job_id}")
            ]])
        else:  # list
            user_jobs.clear_pending_edit(chat_id)
            text, markup = await asyncio.to_thread(_jobs_list_view, chat_id)
        try:
            await query.edit_message_text(text, reply_markup=markup)
        except Exception:
            pass
    except Exception as exc:
        logger.exception("jobs callback failed")
        try:
            await query.edit_message_text(f"⚠️ Jobs action failed: {exc}")
        except Exception:
            pass


_NOTES_PROMPT = (
    "📝 Key takeaways from this block? (mistakes, formulas, insights — "
    "saved onto this ledger entry)"
)


def _notes_prompt_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✔ Skip", callback_data="debrief:done"),
    ]])


async def on_debrief_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not _is_allowed(update):
        return
    chat_id = update.effective_chat.id
    action = query.data.split(":", 1)[1] if ":" in query.data else "done"
    try:
        state = draft_store.get_session_debrief(chat_id)
        if action == "next" and state and state["stage"] == "doubts":
            draft_store.advance_session_debrief(chat_id)
            await query.edit_message_text(_NOTES_PROMPT, reply_markup=_notes_prompt_markup())
            return
        draft_store.clear_session_debrief(chat_id)
        await query.edit_message_text("✅ Block wrapped up.")
    except Exception as exc:
        logger.exception("debrief callback failed")
        try:
            await query.edit_message_text(f"⚠️ Debrief failed: {exc}")
        except Exception:
            pass


_DEBRIEF_SKIP_WORDS = {"no", "none", "nope", "nah", "skip", "no doubts", "nothing"}


async def _handle_debrief_text(message, chat_id: int, state: dict, text: str) -> None:
    if state["stage"] == "doubts":
        if text.strip().lower() in _DEBRIEF_SKIP_WORDS:
            draft_store.advance_session_debrief(chat_id)
            await message.reply_text(_NOTES_PROMPT, reply_markup=_notes_prompt_markup())
            return
        doubt_id, url = await asyncio.to_thread(
            logging_flow.add_session_debrief_doubt, state["ledger_page_id"], text
        )
        if doubt_id:
            await message.reply_text(
                "❓ Doubt logged & linked to this block. Another? "
                "(or tap ✔ No doubts above)"
            )
        else:
            await message.reply_text(
                "⚠️ Couldn't save that doubt right now — it was NOT stored. "
                "Try again or log it later with 'doubt: …'."
            )
        return
    # notes stage: one message, saved onto the entry.
    if text.strip().lower() in _DEBRIEF_SKIP_WORDS:
        draft_store.clear_session_debrief(chat_id)
        await message.reply_text("✅ Block wrapped up.")
        return
    try:
        await asyncio.to_thread(
            logging_flow.append_session_notes, state["ledger_page_id"], text
        )
        draft_store.clear_session_debrief(chat_id)
        await message.reply_text("📝 Saved onto this session. ✅ Block wrapped up.")
    except Exception as exc:
        logger.exception("debrief notes failed")
        draft_store.clear_session_debrief(chat_id)
        await message.reply_text(f"⚠️ Couldn't save the notes: {exc}")


async def bug_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Soak instrumentation: capture anything odd the moment it's noticed."""
    if await _reject_if_unauthorized(update):
        return
    chat_id = update.effective_chat.id
    text = _command_args(update)
    if not text:
        await update.effective_message.reply_text(
            "Describe what went wrong: /bug the answer said 20 but I logged 10"
        )
        return
    bug_id = draft_store.add_bug_report(chat_id, text)
    await update.effective_message.reply_text(
        f"🐛 Noted as #{bug_id}. See all with /bugs — thank you, this is how the bot earns trust."
    )


async def bugs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    chat_id = update.effective_chat.id
    args = _command_args(update)
    done_match = re.match(r"^done\s+#?(\d+)$", args, re.IGNORECASE)
    if done_match:
        ok = draft_store.close_bug_report(chat_id, int(done_match.group(1)))
        await update.effective_message.reply_text(
            "✅ Closed." if ok else "No open bug with that number."
        )
        return
    bugs = draft_store.list_bug_reports(chat_id)
    if not bugs:
        await update.effective_message.reply_text(
            "No open bug notes. Report one anytime: /bug <what happened>"
        )
        return
    lines = ["🐛 Open bug notes (close with /bugs done <n>):"]
    lines += [f"#{b['id']} [{str(b['created_at'])[:10]}] {b['text']}" for b in bugs]
    await update.effective_message.reply_text("\n".join(lines))


async def _legacy_catch_all_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    text: str,
    clarification: dict | None,
) -> None:
    """Fallback legacy intent-parser / handler flow."""
    message = update.effective_message
    parse_text = text
    if clarification:
        parse_text = (
            f"[Original message: {clarification['original_message']}]\n"
            f"[Bot asked: {clarification['question']}]\n"
            f"[User replied: {text}]"
        )
    stored = session_context.context_for_parser(chat_id)
    intent = None
    if not clarification:
        intent = _try_pattern_match(text)
    if intent is None:
        try:
            intent = await asyncio.to_thread(parse_message, parse_text, session_context=stored)
        except IntentParseError:
            logger.exception("intent parse failed chat_id=%s", chat_id)
            await message.reply_text("Sorry, I couldn't understand that. Try rephrasing?")
            return
    if intent.action == "unknown":
        await _handle_general_assistant(update, text, chat_id)
        return
    if intent.needs_clarification:
        q = intent.clarification_question or "Could you clarify that?"
        draft_store.set_pending_clarification(
            chat_id,
            original_message=clarification["original_message"] if clarification else text,
            question=q,
        )
        await message.reply_text(q, reply_markup=ForceReply(selective=True))
        return
    if intent.action == "set_context":
        f = intent.filters
        exercise = getattr(f, "exercise", None)
        if exercise:
            options = notion_schema.PROPERTIES_BY_DB["ledger"]["exercise_type"]["options"]
            exercise = logging_flow.normalise_option(exercise, options) or exercise
        ctx = session_context.set_context(
            chat_id, subject=f.subject, chapter=f.chapter, block=f.block, exercise=exercise
        )
        await message.reply_text("Context set.\n\n" + briefing.build_briefing(ctx), parse_mode=ParseMode.MARKDOWN)
        return
    if intent.action.startswith("log_"):
        original = clarification["original_message"] if clarification else text
        await _handle_log(
            update, context, intent, chat_id,
            original_message=original, first_round=clarification is None,
        )
        return
    if intent.action == "remember":
        statement = str(intent.fields.get("statement") or "").strip() or text
        await _handle_remember(update, statement, chat_id)
        return
    if intent.action in ("query", "ask"):
        question = text
        if intent.action == "ask":
            question = intent.fields.get("question") or text
        await _handle_question(update, question, intent=intent, chat_id=chat_id)
        return
    await _handle_general_assistant(update, text, chat_id)


async def _handle_agent_text(update: Update, chat_id: int, text: str) -> None:
    """Route a free-form user message through the agentic loop."""
    message = update.effective_message
    status_msg = None
    streamer = agent_renderer.AgentChatStreamer(message)

    async def on_status(status_text: str) -> None:
        nonlocal status_msg
        try:
            if status_msg is None:
                status_msg = await message.reply_text(status_text)
            else:
                await status_msg.edit_text(status_text)
        except Exception:
            pass

    result = await agent.run(
        chat_id, text, on_status=on_status, on_stream=streamer.on_stream,
    )

    if result["type"] == "preview":
        preview = result["preview"]
        state_id = result["state_id"]
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Confirm", callback_data=f"agent:confirm:{state_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"agent:cancel:{state_id}"),
        ]])
        if status_msg is not None:
            try:
                await status_msg.delete()
            except Exception:
                pass
        await message.reply_text(f"{preview}\n\nProceed?", reply_markup=keyboard)
        return

    # Final response — one message only (stream finalize OR plain render).
    if status_msg is not None:
        try:
            await status_msg.delete()
        except Exception:
            pass
    if streamer.started:
        await streamer.finalize(result["response"])
    else:
        await agent_renderer.render(update, result["response"])


def _errors_last_24h() -> int:
    cutoff = dt.datetime.now() - dt.timedelta(hours=24)
    count = 0
    try:
        with open(LOG_FILE, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if " ERROR " not in line:
                    continue
                try:
                    stamp = dt.datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                if stamp >= cutoff:
                    count += 1
    except OSError:
        return 0
    return count


async def on_agent_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Confirm/Cancel on agent write previews."""
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    parts = data.split(":", 2)
    if len(parts) != 3:
        return
    _, action, state_id = parts
    confirmed = action == "confirm"

    status_msg = None
    streamer = agent_renderer.AgentChatStreamer(query.message)

    async def on_status(text: str) -> None:
        nonlocal status_msg
        try:
            if status_msg is None:
                status_msg = await query.edit_message_text(text)
            else:
                await status_msg.edit_text(text)
        except Exception:
            pass

    result = await agent.continue_run(
        state_id, confirmed=confirmed, on_status=on_status, on_stream=streamer.on_stream,
    )

    if result["type"] == "preview":
        preview = result["preview"]
        new_state_id = result["state_id"]
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Confirm", callback_data=f"agent:confirm:{new_state_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"agent:cancel:{new_state_id}"),
        ]])
        if status_msg is not None:
            try:
                await status_msg.delete()
            except Exception:
                pass
        await query.message.reply_text(f"{preview}\n\nProceed?", reply_markup=keyboard)
        return

    if status_msg is not None:
        try:
            await status_msg.delete()
        except Exception:
            pass
    if streamer.started:
        await streamer.finalize(result["response"])
    else:
        await agent_renderer.render(query, result["response"])


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
        await message.reply_text(
            "I currently understand text messages. Send a study question or use /help."
        )
        return

    # A destructive reset confirmation has the highest routing priority.
    # While armed, no text can fall through to an LLM or ordinary command
    # parser.  Only exact, case-sensitive full-sentence equality executes it.
    pending_reset = reset_service.pending_confirmation(chat_id)
    if pending_reset:
        if text.strip().lower() == "cancel":
            reset_service.cancel_confirmation(chat_id)
            await message.reply_text("Reset cancelled. Nothing was changed.")
            return
        consumed = reset_service.consume_confirmation(chat_id, text)
        if consumed is None:
            await message.reply_text(
                "That did not exactly match, so nothing was changed. Send this "
                "full sentence exactly, or send ‘cancel’:\n\n"
                f"{pending_reset['sentence']}"
            )
            return
        await message.reply_text(
            "Confirmation matched. Creating and verifying the backup before any mutation…"
        )
        try:
            async with _maintenance_lock(context):
                result = await asyncio.to_thread(
                    reset_service.execute, consumed["scope"], chat_id=chat_id
                )
            await message.reply_text(reset_service.format_result(result))
        except Exception as exc:
            logger.exception("confirmed reset failed")
            await message.reply_text(
                f"⚠️ Reset stopped with an error: {exc}. Check the backup/report before retrying."
            )
        return

    pending_resolution = exam_readiness.pending_resolution(chat_id)
    if pending_resolution:
        if text.strip().lower() == "cancel":
            exam_readiness.cancel_resolution(chat_id)
            await message.reply_text("Resolution cancelled. The doubt remains open.")
            return
        try:
            result = await asyncio.to_thread(
                exam_readiness.complete_resolution, chat_id, text
            )
            await message.reply_text(
                "✅ Resolution evidence saved and the doubt is now solved. "
                "Run /readiness to refresh the exam audit."
            )
        except Exception as exc:
            await message.reply_text(
                f"The doubt remains open: {exc}. Send stronger resolution evidence, "
                "or send ‘cancel’."
            )
        return

    # Pending doubt resolution verification: user must answer the verification
    # question before the doubt is marked as solved.
    pending_resolve = draft_store.get_pending_doubt_resolution(chat_id)
    if pending_resolve:
        if text.strip().lower() in ("skip", "cancel"):
            draft_store.clear_pending_doubt_resolution(chat_id)
            try:
                result = await asyncio.to_thread(
                    study_domain.resolve_doubt_id,
                    pending_resolve["doubt_id"], pending_resolve["resolution"],
                    teacher_asked=bool(pending_resolve["teacher_asked"]),
                )
                await message.reply_text(
                    f"Doubt closed as {result['workflow_state']} (skipped verification)."
                )
            except Exception as exc:
                await message.reply_text(f"Doubt was not closed: {exc}")
            return
        try:
            result = await asyncio.to_thread(
                study_domain.resolve_doubt_id,
                pending_resolve["doubt_id"],
                f"{pending_resolve['resolution']}\n\n[Verification answer: {text}]",
                teacher_asked=bool(pending_resolve["teacher_asked"]),
            )
            draft_store.clear_pending_doubt_resolution(chat_id)
            await message.reply_text(
                f"✅ Doubt closed as {result['workflow_state']}. "
                f"Your verification answer was saved with the resolution."
            )
        except Exception as exc:
            await message.reply_text(f"Doubt was not closed: {exc}")
        return

    # /settings edit in progress: the next text message is the new value.
    pending_setting = draft_store.get_pending_setting_edit(chat_id)
    if pending_setting:
        await _apply_setting_reply(message, chat_id, pending_setting, text)
        return

    # /jobs edit in progress: the next text message is the new time/text.
    pending_job = user_jobs.get_pending_edit(chat_id)
    if pending_job:
        job_id, field = pending_job
        ok, result = await asyncio.to_thread(user_jobs.update_field, job_id, field, text)
        if ok:
            job = user_jobs.get_job(job_id)
            await message.reply_text(
                f"✅ Job #{job_id} updated — {user_jobs.describe(job)}" if job
                else f"✅ Job #{job_id} updated."
            )
        else:
            user_jobs.set_pending_edit(chat_id, job_id, field)
            await message.reply_text(f"⚠️ {result}. Send it again, or open /jobs to cancel.")
        return

    # Block-close debrief in progress: texts are doubts, then takeaways.
    debrief = draft_store.get_session_debrief(chat_id)
    if debrief:
        await _handle_debrief_text(message, chat_id, debrief, text)
        return

    # /setup wizard in progress: route the text to the active section.
    onb_state = onboarding.active_section(chat_id)
    if onb_state:
        section_id, _mode = onb_state
        lowered = text.lstrip().lower()
        if lowered.startswith(("ai ", "ai:", "ai,")):
            stripped = text.lstrip()[2:].lstrip(" :,").strip()
            await _handle_setup_ai(update, chat_id, section_id, stripped or text)
            return
        if section_id == "commitments":
            # Reuses the /remember preview+confirm; the wizard loop stays
            # active so the next message adds another commitment.
            await _handle_remember(update, text, chat_id)
            return
        _ok, reply, adv = await asyncio.to_thread(
            onboarding.apply_answer, chat_id, section_id, text
        )
        if not _ok:
            reply += "\n💡 Or start your message with `ai ` and describe it — I'll figure out what to do."
        if adv:
            reply_text, markup = await _setup_after_answer(chat_id, reply, True)
            await _reply_markdown(message, reply_text, reply_markup=markup)
        else:
            await _reply_markdown(message, reply)
        if _ok and section_id == "next_mock":
            try:
                await _send_current_readiness_reviews(
                    context.bot, chat_id, sync_first=True
                )
            except Exception:
                # The mock was saved successfully; a notification problem must
                # not misreport or roll back that onboarding answer.
                logger.exception("onboarding mock readiness audit failed")
        return

    # One-time nudge toward /setup for a fresh install.
    if not onboarding.is_complete(chat_id) and reminders.claim("onboarding-hint:v1"):
        await message.reply_text(
            "🚀 First time? Run /setup — 2 minutes, and every engine "
            "(planner, streaks, teacher alerts) comes alive."
        )

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

    # Agent path: route free-form text through the agentic loop.
    try:
        await _handle_agent_text(update, chat_id, text)
    except Exception:
        logger.exception("agent path failed chat_id=%s, falling back to legacy flow", chat_id)
        await _legacy_catch_all_handler(update, context, chat_id, text, clarification)


async def _handle_question(
    update: Update, question: str, intent=None, chat_id: int | None = None
) -> None:
    """Answer a free-form question about the user's study data via the SQL loop.

    Falls back to a short apology if both the LLM and the legacy query_flow
    are unavailable. When chat_id is given, a rolling window of recent
    (question, answer) turns is loaded for follow-up context and the new turn
    is saved after a successful answer.
    """
    message = update.effective_message
    if chat_id is None:
        chat_id = update.effective_chat.id
    try:
        from config import settings as _cfg
        try:
            pairs = _cfg.query_history_pairs()
        except Exception:
            pairs = draft_store.QA_HISTORY_MAX_PAIRS
        history = await asyncio.to_thread(
            draft_store.recent_qa, chat_id, limit_pairs=pairs
        )

        # Live progress: edit a single status message in place as the loop
        # drills. Steps arrive from a worker thread, so hand them to the event
        # loop via call_soon_threadsafe.
        loop = asyncio.get_running_loop()
        status_msg = await message.reply_text("🔍 Thinking…")
        steps: list[str] = []
        _edit_seq = 0  # monotonic counter guards against stale edits overwriting the answer

        def _render() -> str:
            shown = steps[-6:]
            return "🔍 " + "\n".join(shown) if shown else "🔍 Thinking…"

        async def _update_status(text: str, seq: int) -> None:
            nonlocal _edit_seq
            if seq != _edit_seq:
                return  # a newer edit (or the final answer) already won
            try:
                await status_msg.edit_text(text)
            except Exception:
                pass  # ignore "message not modified" / rate limits

        def _on_step(kind: str, detail: str) -> None:
            nonlocal _edit_seq
            steps.append(detail)
            _edit_seq += 1
            seq = _edit_seq
            loop.call_soon_threadsafe(
                lambda: asyncio.create_task(_update_status(_render(), seq))
            )

        answer = await asyncio.to_thread(
            sql_query_flow.answer_question,
            question, history=history, on_step=_on_step, chat_id=chat_id,
        )
        _edit_seq += 1  # invalidate any pending status-edit tasks before writing the answer
        _err_prefix = sql_query_flow.ANSWER_ERROR_PREFIX
        if answer.startswith(_err_prefix) and intent is not None and intent.action == "query":
            result = await asyncio.to_thread(query_flow.run_query, intent)
            answer = query_flow.format_result(result)
        # Only remember successful answers, so an error turn never poisons the
        # follow-up window.  Use the structured prefix (not bare ⚠️) so a valid
        # answer that happens to start with the emoji is not misclassified.
        if not answer.startswith(_err_prefix):
            await asyncio.to_thread(draft_store.record_qa, chat_id, question, answer)
        try:
            await _edit_markdown(status_msg, answer, disable_web_page_preview=True)
        except Exception:
            await _reply_markdown(message, answer, disable_web_page_preview=True)
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
        # Block-close debrief: capture doubts + takeaways while they're fresh,
        # linked to the entry that was just saved.
        if (
            payload.get("db_key") == "ledger"
            and result.get("status") == "saved"
            and result.get("page_id")
        ):
            draft_store.set_session_debrief(draft["chat_id"], str(result["page_id"]))
            await context.bot.send_message(
                chat_id=draft["chat_id"],
                text=(
                    "❓ Any doubts from this block? Send them one per message — "
                    "I'll link each to this session."
                ),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✔ No doubts", callback_data="debrief:next"),
                ]]),
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
        await sync.sync_once_locked(db_keys=("revision", "doubts", "ledger"))
        today = session_context.local_today_iso()
        if reminders.claim(f"planning:{today}"):
            message = await asyncio.to_thread(reminders.planning_message)
            await context.bot.send_message(chat_id=telegram_allowed_user_id(), text=message)
    except Exception:
        logger.exception("planning reminder failed")


async def _schedule_watch_scan(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await sync.sync_once_locked(
            db_keys=("revision", "doubts", "ledger")
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
        text = (
            f"Weekly growth: CY {ledger.get('cy',0):g}, accuracy {accuracy}, "
            f"change {report['cy_delta']:+g}, "
            f"completed work {report['completed_work']}, backlog {report['backlog']}, "
            f"valid doubt attempts {report['valid_doubt_attempts']}. Use /weekly and /weak for detail."
        )
        # Weekly-period commitments: same deterministic ledger verification as
        # the daily checks, summed over the week.
        try:
            today = session_context.local_today_iso()
            weekly_lines = []
            for goal in await asyncio.to_thread(
                study_domain._rows, "goals",
                "archived=0 AND status='Active' AND period='Weekly'",
            ):
                check = await asyncio.to_thread(
                    commitments.verify_weekly_goal, goal, today
                )
                if check["met"] is None:
                    continue
                mark = "✅" if check["met"] else "❌"
                weekly_lines.append(
                    f"{mark} {check['title']}: {check['value']:g}/{check['target']:g} this week"
                )
            if weekly_lines:
                text += "\n\nWeekly commitments:\n" + "\n".join(weekly_lines)
        except Exception:
            logger.exception("weekly commitment section failed")
        try:
            open_bugs = draft_store.list_bug_reports(telegram_allowed_user_id())
            if open_bugs:
                text += f"\n🐛 {len(open_bugs)} open bug note(s) this week — /bugs to triage."
        except Exception:
            logger.exception("weekly bug note failed")
        await context.bot.send_message(chat_id=telegram_allowed_user_id(), text=text)
    except Exception:
        logger.exception("weekly report job failed")


async def _exam_reminder_scan(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await _send_current_readiness_reviews(
            context.bot, telegram_allowed_user_id(), sync_first=True
        )
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
        await sync.sync_once_locked(db_keys=("doubts", "revision", "ledger"))
        for item in await asyncio.to_thread(reminders.teacher_opportunities):
            window, decision = item["window"], item["decision"]
            key = reminders.teacher_event_key(window, decision, item.get("doubts"))
            if not reminders.claim(key):
                continue
            await _send_markdown(
                context.bot,
                telegram_allowed_user_id(),
                message_templates.teacher_opportunity(item),
            )
    except Exception:
        logger.exception("teacher opportunity scan failed")


def _backup_state(
    db_path: Path, settings_path: Path, backup_root: Path, day: str,
) -> Path:
    """Create an atomic, WAL-consistent backup and retain seven daily copies."""
    return reset_service.backup_state(
        db_path, settings_path, backup_root, day, retain=7
    )


def _nightly_backup() -> None:
    """Back up the SQLite operational brain and settings, keeping seven days."""
    root = Path(__file__).resolve().parent
    _backup_state(
        root / "sqlite_mirror.db",
        root / "settings.json",
        root / "backups",
        session_context.local_today_iso(),
    )


async def _commitment_verify_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Nightly: record today's commitment adherence from the ledger + backup."""
    try:
        await sync.sync_once_locked(db_keys=("ledger",))
        today = session_context.local_today_iso()
        await asyncio.to_thread(commitments.run_checks_for_date, today)
    except Exception:
        logger.exception("commitment verify job failed")
    try:
        await asyncio.to_thread(_nightly_backup)
    except Exception:
        logger.exception("nightly backup failed")


async def _commitment_nudge_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Morning: backfill recent days (absorbs downtime + late logging), then nudge."""
    try:
        await sync.sync_once_locked(db_keys=("ledger",))
        # Re-verify the last 3 days so a night with the bot offline doesn't
        # leave gap days that break streaks the user actually kept.
        await asyncio.to_thread(commitments.backfill_checks, days=3)
        yesterday = (
            dt.date.fromisoformat(session_context.local_today_iso())
            - dt.timedelta(days=1)
        ).isoformat()
        nudge = await asyncio.to_thread(advisor.morning_nudge, yesterday)
        if nudge is None or not reminders.claim(f"commitment-nudge:{yesterday}"):
            return
        drift = await asyncio.to_thread(advisor.trajectory_warnings)
        if drift:
            nudge = message_templates.insert_section(nudge, "Risks", drift)
        await _send_markdown(context.bot, telegram_allowed_user_id(), nudge)
    except Exception:
        logger.exception("commitment nudge job failed")


async def _nightly_insight_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Nightly: derive a learner profile and extract one new insight."""
    try:
        import learner_profile
        chat_id = telegram_allowed_user_id()
        insight = await asyncio.to_thread(
            learner_profile.nightly_insight, chat_id, use_llm=True
        )
        if insight is None:
            return
        if not insight.get("created"):
            return
        await _send_markdown(
            context.bot, chat_id,
            f"🌙 *Nightly insight*\n\n{insight['text']}\n\n"
            f"_confidence: {insight.get('confidence', 'low')} · "
            f"source: {insight.get('source', 'deterministic')}_"
        )
    except Exception:
        logger.exception("nightly insight job failed")


async def _user_jobs_scan(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fire due user-created jobs (dedup via reminders.claim per job per day)."""
    try:
        now = session_context.local_now()
        for job in await asyncio.to_thread(user_jobs.due_jobs, now):
            if reminders.claim(f"user-job:{job['id']}:{now.date().isoformat()}"):
                try:
                    await _run_user_job(job, context)
                except Exception:
                    logger.exception("user job %s failed", job["id"])
    except Exception:
        logger.exception("user jobs scan failed")


async def post_init(application: Application) -> None:
    application.bot_data.setdefault(_MAINTENANCE_LOCK_KEY, asyncio.Lock())
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
        sync_secs = config_settings.sync_interval_seconds()
        application.job_queue.run_repeating(
            _guard_scheduled(_periodic_sync),
            interval=sync_secs,
            first=sync_secs,
            name="periodic_sync",
        )
        application.job_queue.run_repeating(
            _guard_scheduled(_expire_drafts), interval=60, first=60, name="expire_drafts"
        )
        def _clock(raw: str) -> dt.time:
            try:
                hour, minute = (int(part) for part in raw.split(":", 1))
                return dt.time(hour, minute, tzinfo=ZoneInfo(config_settings.user_timezone()))
            except Exception as exc:
                raise ValueError(f"invalid reminder time {raw!r}") from exc

        application.job_queue.run_daily(
            _guard_scheduled(_planning_reminder), time=_clock(config_settings.planning_reminder_time()),
            days=tuple(range(7)), name="planning_reminder",
        )
        application.job_queue.run_daily(
            _guard_scheduled(_weekly_timetable_reminder), time=_clock("09:00"),
            days=tuple(range(7)), name="timetable_reminder",
        )
        application.job_queue.run_daily(
            _guard_scheduled(_weekly_report_job), time=_clock(config_settings.weekly_report_time()),
            days=tuple(range(7)), name="weekly_report",
        )
        application.job_queue.run_daily(
            _guard_scheduled(_commitment_verify_job), time=_clock(config_settings.commitment_check_time()),
            days=tuple(range(7)), name="commitment_verify",
        )
        application.job_queue.run_daily(
            _guard_scheduled(_commitment_nudge_job), time=_clock(config_settings.commitment_nudge_time()),
            days=tuple(range(7)), name="commitment_nudge",
        )
        application.job_queue.run_daily(
            _guard_scheduled(_nightly_insight_job), time=_clock(config_settings.nightly_insight_time()),
            days=tuple(range(7)), name="nightly_insight",
        )
        application.job_queue.run_repeating(
            _guard_scheduled(_exam_reminder_scan), interval=600, first=120, name="exam_reminders"
        )
        application.job_queue.run_repeating(
            _guard_scheduled(_teacher_opportunity_scan), interval=300, first=180, name="teacher_windows"
        )
        application.job_queue.run_repeating(
            _guard_scheduled(_schedule_watch_scan), interval=300, first=90, name="schedule_watcher"
        )
        application.job_queue.run_repeating(
            _guard_scheduled(_user_jobs_scan), interval=60, first=75, name="user_jobs"
        )
        logger.info("Scheduled periodic sync every %ds", sync_secs)
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
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("memory", memory_command))
    app.add_handler(CommandHandler("inspect", inspect_command))
    app.add_handler(CommandHandler("setup", setup_command))
    app.add_handler(CommandHandler("jobs", jobs_command))
    app.add_handler(CommandHandler("bug", bug_command))
    app.add_handler(CommandHandler("bugs", bugs_command))
    app.add_handler(CommandHandler("health", health))
    app.add_handler(CommandHandler("sync", sync_command))
    app.add_handler(CommandHandler("goal", goal_command))
    app.add_handler(CommandHandler("remember", remember_command))
    app.add_handler(CommandHandler("forget", forget_command))
    app.add_handler(CommandHandler("exam", exam_command))
    app.add_handler(CommandHandler("readiness", readiness_command))
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
    app.add_handler(CommandHandler("reset", reset_command))
    app.add_handler(CallbackQueryHandler(on_domain_callback, pattern=r"^domain:"))
    app.add_handler(CallbackQueryHandler(on_readiness_callback, pattern=r"^ready:"))
    app.add_handler(CallbackQueryHandler(on_reset_callback, pattern=r"^reset:"))
    app.add_handler(CallbackQueryHandler(on_memory_callback, pattern=r"^memory:"))
    app.add_handler(CallbackQueryHandler(on_settings_callback, pattern=r"^settings:"))
    app.add_handler(CallbackQueryHandler(on_inspect_callback, pattern=r"^inspect:"))
    app.add_handler(CallbackQueryHandler(on_onboarding_callback, pattern=r"^onb:"))
    app.add_handler(CallbackQueryHandler(on_jobs_callback, pattern=r"^jobs:"))
    app.add_handler(CallbackQueryHandler(on_debrief_callback, pattern=r"^debrief:"))
    app.add_handler(CallbackQueryHandler(on_plan_callback, pattern=r"^plan:"))
    app.add_handler(CallbackQueryHandler(on_log_callback, pattern=r"^log:"))
    app.add_handler(CallbackQueryHandler(on_agent_callback, pattern=r"^agent:"))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, catch_all))
    logger.info("Starting Telegram long polling")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
