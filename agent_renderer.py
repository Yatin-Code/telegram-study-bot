"""Render an AgentResponse into Telegram API calls."""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ForceReply, CallbackQuery
from telegram.constants import ParseMode

from agent import AgentResponse
from config.settings import telegram_bot_token
import rich_message
from config import settings as config_settings

logger = logging.getLogger(__name__)

# Throttle draft updates so we do not spam sendRichMessageDraft.
_STREAM_MIN_INTERVAL_S = 0.35
_STREAM_MIN_CHARS = 24


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


def _message_from(update_or_message):
    if isinstance(update_or_message, CallbackQuery):
        return update_or_message.message
    return getattr(update_or_message, "effective_message", update_or_message)


def _build_markup(response: AgentResponse):
    if response.response_type == "inline_keyboard":
        rows = response.inline_buttons or []
        return InlineKeyboardMarkup(
            [[
                InlineKeyboardButton(btn["text"], callback_data=btn["callback_data"])
                for btn in row
            ] for row in rows]
        )
    if response.response_type == "reply_keyboard":
        options = response.reply_options or []
        return ReplyKeyboardMarkup(
            [[opt] for opt in options],
            one_time_keyboard=True,
            resize_keyboard=True,
        )
    if response.response_type == "force_reply":
        return ForceReply(selective=True)
    return None


class AgentChatStreamer:
    """Bridge agent on_stream → Bot API 10.1 RichStream (Hermes-style drafts).

    first visible text → sendRichMessage
    mid updates        → sendRichMessageDraft (throttled)
    final              → editMessageText(rich_message=...)
    """

    def __init__(self, message, *, parse_mode: str = "markdown") -> None:
        self.message = message
        self.parse_mode = parse_mode
        self._stream: Optional[rich_message.RichStream] = None
        self._last_push = 0.0
        self._last_text = ""
        self._started = False
        self._failed = False

    @property
    def started(self) -> bool:
        return self._started

    def _ensure(self) -> rich_message.RichStream | None:
        if self._failed:
            return None
        if self._stream is not None:
            return self._stream
        try:
            if not config_settings.rich_messages_enabled():
                self._failed = True
                return None
        except Exception:
            pass
        try:
            token = _token_from(self.message)
            chat_id = self.message.chat_id if hasattr(self.message, "chat_id") else self.message.chat.id
            reply_to = getattr(self.message, "message_id", None)
            self._stream = rich_message.RichStream(
                token,
                chat_id,
                parse_mode=self.parse_mode,
                reply_to_message_id=reply_to,
            )
            return self._stream
        except Exception:
            logger.exception("could not start RichStream")
            self._failed = True
            return None

    async def on_stream(self, visible: str) -> None:
        """Receive full accumulated user-visible text from the agent loop."""
        if not visible or self._failed:
            return
        now = time.monotonic()
        grew = len(visible) - len(self._last_text)
        if self._started and grew < _STREAM_MIN_CHARS and (now - self._last_push) < _STREAM_MIN_INTERVAL_S:
            self._last_text = visible
            return
        stream = self._ensure()
        if stream is None:
            return
        try:
            await stream.feed(visible, is_final=False)
            self._started = True
            self._last_push = now
            self._last_text = visible
        except Exception:
            logger.exception("RichStream feed failed; disabling stream for this turn")
            self._failed = True

    async def finalize(self, response: AgentResponse) -> Any:
        """Finish the rich stream, or fall back to a normal render."""
        text = response.text or self._last_text or "Done."
        markup = _build_markup(response)
        if response.response_type == "poll" or not self._started or self._stream is None:
            return None  # caller should use render()
        try:
            self._stream.reply_markup = markup
            return await self._stream.finalize(text)
        except Exception:
            logger.exception("RichStream finalize failed")
            return None


async def render(update_or_message, response: AgentResponse):
    """Render an AgentResponse and send it via Telegram.

    Accepts a Telegram Update, Message, or CallbackQuery object.
    Prefer Bot API 10.1 sendRichMessage for structured content; fall back
    to plain reply_text if the rich path fails.
    """
    message = _message_from(update_or_message)
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

    markup = _build_markup(response)

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
