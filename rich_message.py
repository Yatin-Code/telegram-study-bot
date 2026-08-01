"""Bot API 10.1+ rich message support via raw HTTP.

python-telegram-bot 22.x lacks typed support for sendRichMessage /
sendRichMessageDraft / the rich_message param on editMessageText, so we
hand-roll the calls against the Bot API with httpx.

Streaming pattern (avoids the no-editRichMessage gotcha):
  1. first partial  -> sendRichMessage        (persists a rich message)
  2. middle partials-> sendRichMessageDraft   (ephemeral 30s preview)
  3. final          -> editMessageText(rich_message=...)  (in-place rich edit)
Never call a bare editMessageText on a rich message without rich_message —
that strips formatting back to plain text mid-stream.

All rich calls fall back to plain sendMessage / editMessageText so the bot
never goes silent if the API rejects the rich payload.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

import httpx

from config import settings as config_settings

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# Once True, skip rich API methods for this process (endpoint missing / unsupported).
_rich_unsupported: bool = False


def reset_capability_latch() -> None:
    """Test helper: clear the process-wide unsupported latch."""
    global _rich_unsupported
    _rich_unsupported = False


def _mark_unsupported(exc: BaseException) -> None:
    """Latch off rich path when the Bot API clearly lacks the method."""
    global _rich_unsupported
    msg = str(exc).lower()
    # Only hard endpoint-missing signals — not generic validation rejections.
    markers = (
        "unknown method",
        "method not found",
        "not found: method",
        "does not exist",
        "method is unavailable",
        "there is no method",
    )
    if any(m in msg for m in markers):
        if not _rich_unsupported:
            logger.warning("Rich messages unsupported on this Bot API; latching plain path: %s", exc)
        _rich_unsupported = True


def _enabled() -> bool:
    if _rich_unsupported:
        return False
    try:
        return config_settings.rich_messages_enabled()
    except Exception:
        return True


def _base_url() -> str:
    try:
        return config_settings.telegram_api_base_url().rstrip("/")
    except Exception:
        return "https://api.telegram.org"


def _api_url(token: str, method: str) -> str:
    return f"{_base_url()}/bot{token}/{method}"


def build_input_rich_message(text: str, parse_mode: str = "markdown") -> dict[str, Any]:
    """Build an InputRichMessage. Exactly one of html/markdown/blocks is set."""
    mode = (parse_mode or "markdown").lower()
    if mode in ("html",):
        return {"html": text or ""}
    # markdown, Markdown, MarkdownV2, plain → rich markdown field
    return {"markdown": sanitize_markdown(text or "")}


# ---------------------------------------------------------------------------
# Markdown sanitising
#
# Telegram's markdown parsers (both the Bot API rich markdown field and the
# classic ParseMode.MARKDOWN) reject a message outright when a `_` or `*`
# never closes, e.g. `accuracy_ratio` or `question_1` in LLM output. That used
# to make the whole fallback chain degrade the message to plain text. We
# preserve intentionally formed spans (code, bold, italic, links) and
# backslash-escape every stray special so formatting survives.
# ---------------------------------------------------------------------------

_MARKDOWN_STRAY_SPECIAL = re.compile(r"(?<!\\)_|(?<!\\)\*|(?<!\\)`|(?<!\\)\[|(?<!\\)\]")

_MARKDOWN_PROTECTED_SPAN = re.compile(
    r"```[\s\S]*?```"               # fenced code block
    r"|`[^`\n]+`"                   # inline code
    r"|(?<![A-Za-z0-9])\*\*[^*\n]+\*\*(?![A-Za-z0-9])"  # bold
    r"|(?<![A-Za-z0-9])\*[^*\n]+\*(?![A-Za-z0-9])"      # italic
    r"|(?<![A-Za-z0-9])_[^_\n]+_(?![A-Za-z0-9])"        # italic _
    r"|\[[^\]\n]+\]\([^)\n]+\)"                         # [text](url)
)


def _escape_markdown_strays(segment: str) -> str:
    return _MARKDOWN_STRAY_SPECIAL.sub(lambda m: "\\" + m.group(0), segment)


def sanitize_markdown(text: str) -> str:
    """Escape markdown specials that would break Telegram's markdown parser.

    Intentionally formed spans (fenced/inline code, ``**bold**``, ``*italic*``,
    ``_italic_``, ``[text](url)``) are preserved verbatim. Stray specials —
    e.g. the single ``_`` in ``accuracy_ratio`` or ``question_1`` — are
    backslash-escaped so an unbalanced marker cannot destroy the formatting of
    the whole message. Safe to call repeatedly (already-escaped markers are
    left alone).
    """
    out: list[str] = []
    pos = 0
    for match in _MARKDOWN_PROTECTED_SPAN.finditer(text):
        out.append(_escape_markdown_strays(text[pos : match.start()]))
        out.append(match.group(0))
        pos = match.end()
    out.append(_escape_markdown_strays(text[pos:]))
    return "".join(out)


def _serialize_reply_markup(reply_markup: Any) -> Any:
    if reply_markup is None:
        return None
    if isinstance(reply_markup, dict):
        return reply_markup
    to_dict = getattr(reply_markup, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    return reply_markup


async def _post(token: str, method: str, payload: dict[str, Any]) -> Any:
    """POST JSON to the Bot API. Returns result on ok, raises on failure."""
    url = _api_url(token, method)
    body = {k: v for k, v in payload.items() if v is not None}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(url, json=body)
        data = resp.json()
    if not data.get("ok"):
        desc = data.get("description") or f"HTTP {resp.status_code}"
        # Finalizing a stream with identical text is a no-op success, not a failure.
        if method == "editMessageText" and "message is not modified" in str(desc).lower():
            return data.get("result") or {"ok": True, "not_modified": True}
        raise RuntimeError(f"Telegram {method} failed: {desc}")
    return data.get("result")


async def send_rich_message(
    token: str,
    chat_id: int | str,
    rich_message: dict[str, Any],
    *,
    reply_to_message_id: int | None = None,
    reply_markup: Any = None,
    disable_notification: bool | None = None,
    message_thread_id: int | None = None,
    business_connection_id: str | None = None,
) -> Any:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "rich_message": rich_message,
        "disable_notification": disable_notification,
        "message_thread_id": message_thread_id,
        "business_connection_id": business_connection_id,
    }
    if reply_to_message_id is not None:
        payload["reply_parameters"] = {"message_id": reply_to_message_id}
    markup = _serialize_reply_markup(reply_markup)
    if markup is not None:
        payload["reply_markup"] = markup
    return await _post(token, "sendRichMessage", payload)


async def send_rich_message_draft(
    token: str,
    chat_id: int,
    rich_message: dict[str, Any],
    draft_id: int,
    *,
    message_thread_id: int | None = None,
) -> Any:
    if draft_id == 0:
        raise ValueError("draft_id must be non-zero")
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "draft_id": draft_id,
        "rich_message": rich_message,
        "message_thread_id": message_thread_id,
    }
    return await _post(token, "sendRichMessageDraft", payload)


async def edit_message_text_rich(
    token: str,
    chat_id: int | str,
    message_id: int,
    rich_message: dict[str, Any],
    *,
    reply_markup: Any = None,
) -> Any:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "rich_message": rich_message,
    }
    markup = _serialize_reply_markup(reply_markup)
    if markup is not None:
        payload["reply_markup"] = markup
    return await _post(token, "editMessageText", payload)


async def _send_plain(
    token: str,
    chat_id: int | str,
    text: str,
    *,
    parse_mode: str | None = None,
    reply_to_message_id: int | None = None,
    reply_markup: Any = None,
    disable_notification: bool | None = None,
    disable_web_page_preview: bool | None = None,
    message_thread_id: int | None = None,
) -> Any:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "disable_notification": disable_notification,
        "message_thread_id": message_thread_id,
    }
    if parse_mode:
        plain_mode = _ptb_parse_mode(parse_mode)
        if plain_mode == "Markdown":
            payload["text"] = sanitize_markdown(text)
        payload["parse_mode"] = plain_mode
    if reply_to_message_id is not None:
        payload["reply_parameters"] = {"message_id": reply_to_message_id}
    if disable_web_page_preview is not None:
        payload["link_preview_options"] = {"is_disabled": bool(disable_web_page_preview)}
    markup = _serialize_reply_markup(reply_markup)
    if markup is not None:
        payload["reply_markup"] = markup
    return await _post(token, "sendMessage", payload)


async def _edit_plain(
    token: str,
    chat_id: int | str,
    message_id: int,
    text: str,
    *,
    parse_mode: str | None = None,
    reply_markup: Any = None,
    disable_web_page_preview: bool | None = None,
) -> Any:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
    }
    if parse_mode:
        plain_mode = _ptb_parse_mode(parse_mode)
        if plain_mode == "Markdown":
            payload["text"] = sanitize_markdown(text)
        payload["parse_mode"] = plain_mode
    if disable_web_page_preview is not None:
        payload["link_preview_options"] = {"is_disabled": bool(disable_web_page_preview)}
    markup = _serialize_reply_markup(reply_markup)
    if markup is not None:
        payload["reply_markup"] = markup
    return await _post(token, "editMessageText", payload)


def _ptb_parse_mode(parse_mode: str | None) -> str | None:
    if not parse_mode or parse_mode.lower() in ("plain", "none", ""):
        return None
    mode = parse_mode.lower()
    if mode == "html":
        return "HTML"
    if mode in ("markdown", "md"):
        return "Markdown"
    if mode in ("markdownv2", "mdv2"):
        return "MarkdownV2"
    return parse_mode


async def send_rich(
    token: str,
    chat_id: int | str,
    text: str,
    *,
    parse_mode: str = "markdown",
    reply_to_message_id: int | None = None,
    reply_markup: Any = None,
    disable_notification: bool | None = None,
    disable_web_page_preview: bool | None = None,
    message_thread_id: int | None = None,
) -> Any:
    """Send a rich message; fall back to plain sendMessage on failure."""
    plain_mode = _ptb_parse_mode(parse_mode)
    if not _enabled():
        return await _send_plain(
            token, chat_id, text,
            parse_mode=plain_mode,
            reply_to_message_id=reply_to_message_id,
            reply_markup=reply_markup,
            disable_notification=disable_notification,
            disable_web_page_preview=disable_web_page_preview,
            message_thread_id=message_thread_id,
        )
    try:
        return await send_rich_message(
            token, chat_id,
            build_input_rich_message(text, parse_mode),
            reply_to_message_id=reply_to_message_id,
            reply_markup=reply_markup,
            disable_notification=disable_notification,
            message_thread_id=message_thread_id,
        )
    except Exception as exc:
        _mark_unsupported(exc)
        logger.warning("sendRichMessage failed, falling back to plain: %s", exc)
        return await _send_plain(
            token, chat_id, text,
            parse_mode=plain_mode,
            reply_to_message_id=reply_to_message_id,
            reply_markup=reply_markup,
            disable_notification=disable_notification,
            disable_web_page_preview=disable_web_page_preview,
            message_thread_id=message_thread_id,
        )


async def edit_rich(
    token: str,
    chat_id: int | str,
    message_id: int,
    text: str,
    *,
    parse_mode: str = "markdown",
    reply_markup: Any = None,
    disable_web_page_preview: bool | None = None,
) -> Any:
    """Edit a message via the rich path; fall back to plain editMessageText."""
    plain_mode = _ptb_parse_mode(parse_mode)
    if not _enabled():
        return await _edit_plain(
            token, chat_id, message_id, text,
            parse_mode=plain_mode,
            reply_markup=reply_markup,
            disable_web_page_preview=disable_web_page_preview,
        )
    try:
        return await edit_message_text_rich(
            token, chat_id, message_id,
            build_input_rich_message(text, parse_mode),
            reply_markup=reply_markup,
        )
    except Exception as exc:
        _mark_unsupported(exc)
        logger.warning("editMessageText(rich_message) failed, falling back to plain: %s", exc)
        return await _edit_plain(
            token, chat_id, message_id, text,
            parse_mode=plain_mode,
            reply_markup=reply_markup,
            disable_web_page_preview=disable_web_page_preview,
        )


class RichStream:
    """Gotcha-safe streaming orchestrator for rich message generation.

    first chunk  -> sendRichMessage (rich from the start)
    mid chunks   -> sendRichMessageDraft (never bare editMessageText)
    final chunk  -> editMessageText with rich_message (never plain edit)
    """

    def __init__(
        self,
        token: str,
        chat_id: int,
        *,
        parse_mode: str = "markdown",
        reply_to_message_id: int | None = None,
        reply_markup: Any = None,
        message_thread_id: int | None = None,
        draft_id: int | None = None,
    ) -> None:
        self.token = token
        self.chat_id = chat_id
        self.parse_mode = parse_mode
        self.reply_to_message_id = reply_to_message_id
        self.reply_markup = reply_markup
        self.message_thread_id = message_thread_id
        # Stable non-zero draft id so intermediate previews animate in place.
        self.draft_id = draft_id if draft_id and draft_id != 0 else (id(self) & 0x7FFFFFFF) or 1
        self.message_id: Optional[int] = None
        self._started = False
        self._finalized = False
        self._text = ""

    def _rich(self, text: str) -> dict[str, Any]:
        return build_input_rich_message(text, self.parse_mode)

    async def feed(self, text: str, *, is_final: bool = False) -> Any:
        """Push the latest full accumulated text (or a chunk that replaces it)."""
        self._text = text
        if self._finalized:
            raise RuntimeError("RichStream already finalized")

        if not self._started:
            # First chunk: persist rich from the start.
            result = await send_rich_message(
                self.token, self.chat_id, self._rich(text),
                reply_to_message_id=self.reply_to_message_id,
                reply_markup=self.reply_markup if is_final else None,
                message_thread_id=self.message_thread_id,
            )
            self._started = True
            if isinstance(result, dict):
                self.message_id = result.get("message_id")
            if is_final:
                self._finalized = True
            return result

        if is_final:
            # Finalize via the rich path — never bare editMessageText.
            if self.message_id is None:
                # No message_id from first send (shouldn't happen); re-send.
                result = await send_rich_message(
                    self.token, self.chat_id, self._rich(text),
                    reply_to_message_id=self.reply_to_message_id,
                    reply_markup=self.reply_markup,
                    message_thread_id=self.message_thread_id,
                )
            else:
                result = await edit_message_text_rich(
                    self.token, self.chat_id, self.message_id,
                    self._rich(text),
                    reply_markup=self.reply_markup,
                )
            self._finalized = True
            return result

        # Intermediate: ephemeral draft preview, never bare editMessageText.
        return await send_rich_message_draft(
            self.token, self.chat_id, self._rich(text), self.draft_id,
            message_thread_id=self.message_thread_id,
        )

    async def finalize(self, text: str | None = None) -> Any:
        if text is not None:
            self._text = text
        return await self.feed(self._text, is_final=True)
