"""Render an AgentResponse into Telegram API calls."""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ForceReply, CallbackQuery
from telegram.constants import ParseMode

from agent import AgentResponse
from config.settings import telegram_bot_token
import rich_message

logger = logging.getLogger(__name__)


def _parse_mode(mode: str) -> str | None:
    if mode == "html":
        return ParseMode.HTML
    if mode == "markdown":
        return ParseMode.MARKDOWN
    return None


def _rich_parse_mode(mode: str) -> str:
    if mode == "html":
        return "html"
    if mode in ("markdown", "md", "Markdown"):
        return "markdown"
    return "markdown"


def _token_from(message) -> str:
    get_bot = getattr(message, "get_bot", None)
    if callable(get_bot):
        try:
            return get_bot().token
        except Exception:
            pass
    return telegram_bot_token()


async def render(update_or_message, response: AgentResponse):
    """Render an AgentResponse and send it via Telegram.

    Accepts a Telegram Update, Message, or CallbackQuery object.
    Prefer Bot API 10.1 sendRichMessage for structured content; fall back
    to plain reply_text if the rich path fails.
    """
    if isinstance(update_or_message, CallbackQuery):
        message = update_or_message.message
    else:
        message = getattr(update_or_message, "effective_message", update_or_message)
    if message is None:
        raise ValueError("Cannot render without a message")

    text = response.text or "Done."
    parse_mode = _parse_mode(response.parse_mode)
    rich_mode = _rich_parse_mode(response.parse_mode)
    chat_id = message.chat_id if hasattr(message, "chat_id") else message.chat.id
    reply_to = getattr(message, "message_id", None)

    if response.response_type == "poll":
        await message.reply_text(text, parse_mode=parse_mode)
        return await message.chat.send_poll(
            question=response.poll_question or "Poll",
            options=response.poll_options or ["Yes", "No"],
            is_anonymous=False,
        )

    markup = None
    if response.response_type == "inline_keyboard":
        rows = response.inline_buttons or []
        markup = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton(btn["text"], callback_data=btn["callback_data"])
                for btn in row
            ] for row in rows]
        )
    elif response.response_type == "reply_keyboard":
        options = response.reply_options or []
        markup = ReplyKeyboardMarkup(
            [[opt] for opt in options],
            one_time_keyboard=True,
            resize_keyboard=True,
        )
    elif response.response_type == "force_reply":
        markup = ForceReply(selective=True)

    try:
        token = _token_from(message)
        return await rich_message.send_rich(
            token, chat_id, text,
            parse_mode=rich_mode,
            reply_to_message_id=reply_to,
            reply_markup=markup,
        )
    except Exception:
        logger.exception("rich agent render failed; falling back to plain reply_text")
        try:
            return await message.reply_text(text, reply_markup=markup, parse_mode=parse_mode)
        except Exception:
            return await message.reply_text(text, reply_markup=markup)
