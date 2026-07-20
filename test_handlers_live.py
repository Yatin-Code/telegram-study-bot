#!/usr/bin/env python3
"""
Handler-level live test: calls the bot's REAL async handler functions with
fake Update/Message objects. Real LLM (gemini-3.5), real Notion, real SQL
query loop — no Telegram network, no extra credentials.

This runs the exact same code path as a real Telegram message:
  catch_all() → parse_message() → intent routing → SQL loop / query_flow /
  logging_flow → reply_text()

The only thing mocked is the Telegram transport (reply_text captures the
bot's response instead of sending it over the network).

USAGE:
  python3 test_handlers_live.py

PREREQUISITES:
  - .env configured (LLM, Notion, Telegram)
  - sqlite_mirror.db synced (python3 sync.py --once)
"""

from __future__ import annotations

import asyncio
import logging
import sys
import types
from typing import Any

# Configure logging so we see what the bot logs
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _check(ok: bool, label: str, cond: bool, extra: str = "") -> bool:
    print(f"  [{'OK ' if cond else 'BAD'}] {label}{(' -> ' + extra) if extra else ''}")
    return ok and cond


class FakeUser:
    def __init__(self, user_id: int):
        self.id = user_id
        self.first_name = "TestUser"


class FakeChat:
    def __init__(self, chat_id: int):
        self.id = chat_id


class FakeMessage:
    """Minimal Message mock that captures reply_text calls."""

    def __init__(self, text: str, chat_id: int, user_id: int):
        self.text = text
        self.chat = FakeChat(chat_id)
        self.from_user = FakeUser(user_id)
        self.replies: list[str] = []

    async def reply_text(self, text: str, **kwargs) -> None:
        self.replies.append(text)

    @property
    def effective_message(self):
        return self


class FakeUpdate:
    """Minimal Update mock that the bot's handlers can read."""

    def __init__(self, text: str, chat_id: int = 99999, user_id: int | None = None):
        from config import settings
        self._message = FakeMessage(text, chat_id, user_id or settings.telegram_allowed_user_id())
        self.effective_message = self._message
        self.effective_chat = self._message.chat
        self.effective_user = self._message.from_user

    @property
    def reply_text(self) -> str | None:
        """The last reply the bot sent, or None."""
        if self._message.replies:
            return self._message.replies[-1]
        return None

    @property
    def all_replies(self) -> list[str]:
        return self._message.replies


async def run_test() -> int:
    # Import after logging is configured
    import bot
    import session_context
    import sync
    from config import settings

    ALLOWED_ID = settings.telegram_allowed_user_id()
    CHAT_ID = 88888

    # Ensure mirror is fresh
    print("Syncing mirror...")
    sync.sync_once()
    print()

    ok = True
    passed = 0
    failed = 0

    # ------------------------------------------------------------------
    # Test 1: /health command
    # ------------------------------------------------------------------
    print("--- Test 1: /health ---")
    update = FakeUpdate("/health", chat_id=CHAT_ID, user_id=ALLOWED_ID)
    ctx = types.SimpleNamespace()
    await bot.health(update, ctx)
    reply = update.reply_text
    print(f"  Reply: {reply[:200] if reply else '(none)'}")
    ok = _check(ok, "/health responded", reply is not None and "Bot Health" in (reply or ""), reply[:60] if reply else "no reply")
    if reply and "Bot Health" in reply:
        passed += 1
    else:
        failed += 1
    print()

    # Clear context so tests don't interfere
    session_context.clear_context(CHAT_ID)

    # ------------------------------------------------------------------
    # Test 2: set_context
    # ------------------------------------------------------------------
    print("--- Test 2: set_context ---")
    update = FakeUpdate("starting physics, chapter kinematics", chat_id=CHAT_ID, user_id=ALLOWED_ID)
    ctx = types.SimpleNamespace()
    await bot.catch_all(update, ctx)
    reply = update.reply_text
    print(f"  Reply: {reply[:200] if reply else '(none)'}")
    ok = _check(ok, "set_context acknowledged", reply is not None and "context" in (reply or "").lower(), reply[:60] if reply else "no reply")
    if reply and "context" in reply.lower():
        passed += 1
    else:
        failed += 1
    print()

    # ------------------------------------------------------------------
    # Test 3: ask question (SQL query loop — real LLM)
    # ------------------------------------------------------------------
    print("--- Test 3: ask 'how many doubts are unresolved?' ---")
    update = FakeUpdate("how many doubts are unresolved?", chat_id=CHAT_ID, user_id=ALLOWED_ID)
    ctx = types.SimpleNamespace()
    await bot.catch_all(update, ctx)
    reply = update.reply_text
    print(f"  Reply: {reply[:300] if reply else '(none)'}")
    has_num = reply is not None and any(c.isdigit() for c in reply)
    ok = _check(ok, "ask: reply has a number", has_num, reply[:60] if reply else "no reply")
    if has_num:
        passed += 1
    else:
        failed += 1
    print()

    # ------------------------------------------------------------------
    # Test 4: ask comparison (SQL query loop — real LLM)
    # ------------------------------------------------------------------
    print("--- Test 4: ask 'compare chemistry vs physics accuracy' ---")
    update = FakeUpdate("compare my chemistry vs physics accuracy", chat_id=CHAT_ID, user_id=ALLOWED_ID)
    ctx = types.SimpleNamespace()
    await bot.catch_all(update, ctx)
    reply = update.reply_text
    print(f"  Reply: {reply[:300] if reply else '(none)'}")
    has_both = reply is not None and "chem" in (reply or "").lower() and "physics" in (reply or "").lower()
    ok = _check(ok, "compare: mentions both subjects", has_both, reply[:60] if reply else "no reply")
    if has_both:
        passed += 1
    else:
        failed += 1
    print()

    # ------------------------------------------------------------------
    # Test 5: query (list doubts — routes through SQL loop)
    # ------------------------------------------------------------------
    print("--- Test 5: query 'show me my doubts' ---")
    update = FakeUpdate("show me my doubts", chat_id=CHAT_ID, user_id=ALLOWED_ID)
    ctx = types.SimpleNamespace()
    await bot.catch_all(update, ctx)
    reply = update.reply_text
    print(f"  Reply: {reply[:300] if reply else '(none)'}")
    mentions_doubts = reply is not None and ("doubt" in (reply or "").lower() or "no " in (reply or "").lower() or "unresolved" in (reply or "").lower())
    ok = _check(ok, "query doubts: response about doubts", mentions_doubts, reply[:60] if reply else "no reply")
    if mentions_doubts:
        passed += 1
    else:
        failed += 1
    print()

    # ------------------------------------------------------------------
    # Test 6: ask cognitive yield (SQL query loop — computed columns)
    # ------------------------------------------------------------------
    print("--- Test 6: ask 'cognitive yield for past year' ---")
    update = FakeUpdate("what's my cognitive yield for the past year?", chat_id=CHAT_ID, user_id=ALLOWED_ID)
    ctx = types.SimpleNamespace()
    await bot.catch_all(update, ctx)
    reply = update.reply_text
    print(f"  Reply: {reply[:300] if reply else '(none)'}")
    has_yield = reply is not None and ("yield" in (reply or "").lower() or any(c.isdigit() for c in (reply or "")))
    ok = _check(ok, "cognitive yield: has data", has_yield, reply[:60] if reply else "no reply")
    if has_yield:
        passed += 1
    else:
        failed += 1
    print()

    # ------------------------------------------------------------------
    # Test 7: ask revision (SQL query loop)
    # ------------------------------------------------------------------
    print("--- Test 7: ask 'which chapters due for revision' ---")
    update = FakeUpdate("which chapters are due for revision?", chat_id=CHAT_ID, user_id=ALLOWED_ID)
    ctx = types.SimpleNamespace()
    await bot.catch_all(update, ctx)
    reply = update.reply_text
    print(f"  Reply: {reply[:300] if reply else '(none)'}")
    has_revision = reply is not None and any(kw in (reply or "").lower() for kw in ["due", "revision", "chapter", "pending", "no "])
    ok = _check(ok, "revision: response about chapters", has_revision, reply[:60] if reply else "no reply")
    if has_revision:
        passed += 1
    else:
        failed += 1
    print()

    # ------------------------------------------------------------------
    # Test 8: ask total time (SQL query loop — aggregation)
    # ------------------------------------------------------------------
    print("--- Test 8: ask 'total time spent studying' ---")
    update = FakeUpdate("give me my total time spent studying", chat_id=CHAT_ID, user_id=ALLOWED_ID)
    ctx = types.SimpleNamespace()
    await bot.catch_all(update, ctx)
    reply = update.reply_text
    print(f"  Reply: {reply[:300] if reply else '(none)'}")
    has_time = reply is not None and any(c.isdigit() for c in (reply or "")) and any(kw in (reply or "").lower() for kw in ["min", "hour", "time", "spent", "total"])
    ok = _check(ok, "total time: has time data", has_time, reply[:60] if reply else "no reply")
    if has_time:
        passed += 1
    else:
        failed += 1
    print()

    # ------------------------------------------------------------------
    # Test 9: unauthorized user rejected
    # ------------------------------------------------------------------
    print("--- Test 9: unauthorized user ---")
    update = FakeUpdate("how many doubts?", chat_id=CHAT_ID, user_id=12345)
    ctx = types.SimpleNamespace()
    await bot.catch_all(update, ctx)
    reply = update.reply_text
    # Unauthorized users get no reply (handler returns early)
    ok = _check(ok, "unauthorized user ignored", reply is None, f"got reply: {reply}")
    if reply is None:
        passed += 1
    else:
        failed += 1
    print()

    # Cleanup
    session_context.clear_context(CHAT_ID)

    print("=" * 60)
    print(f"  HANDLER LIVE TEST: {passed} passed, {failed} failed (of {passed + failed})")
    print("=" * 60)
    return 0 if ok else 1


def main() -> int:
    return asyncio.run(run_test())


if __name__ == "__main__":
    sys.exit(main())
