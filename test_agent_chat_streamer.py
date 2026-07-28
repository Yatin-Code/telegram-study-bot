"""AgentChatStreamer → RichStream bridge (throttled drafts)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.asyncio

import agent_renderer
import rich_message
from agent import AgentResponse


@pytest.fixture(autouse=True)
def _reset_latch():
    rich_message.reset_capability_latch()
    yield
    rich_message.reset_capability_latch()


async def test_streamer_feeds_and_finalizes(monkeypatch):
    monkeypatch.setenv("RICH_MESSAGES", "1")
    feeds: list[tuple[str, bool]] = []

    class _FakeStream:
        def __init__(self, *a, **k):
            self.reply_markup = None
            self.message_id = 42

        async def feed(self, text, *, is_final=False):
            feeds.append((text, is_final))
            return {"message_id": 42}

        async def finalize(self, text=None):
            feeds.append((text or "", True))
            return {"message_id": 42}

    monkeypatch.setattr(rich_message, "RichStream", _FakeStream)
    monkeypatch.setattr(agent_renderer, "_token_from", lambda m: "tok")
    # Disable throttle for the test
    monkeypatch.setattr(agent_renderer, "_STREAM_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(agent_renderer, "_STREAM_MIN_CHARS", 0)

    msg = SimpleNamespace(chat_id=1, message_id=9, chat=SimpleNamespace(id=1))
    streamer = agent_renderer.AgentChatStreamer(msg)
    await streamer.on_stream("Hel")
    await streamer.on_stream("Hello")
    assert streamer.started
    result = await streamer.finalize(AgentResponse(text="Hello world"))
    assert result is not None
    assert feeds[0] == ("Hel", False)
    assert feeds[-1][1] is True
    assert feeds[-1][0] == "Hello world"


async def test_streamer_finalize_none_when_never_started(monkeypatch):
    monkeypatch.setenv("RICH_MESSAGES", "1")
    msg = SimpleNamespace(chat_id=1, message_id=9, chat=SimpleNamespace(id=1))
    streamer = agent_renderer.AgentChatStreamer(msg)
    out = await streamer.finalize(AgentResponse(text="done"))
    assert out is None  # caller falls back to render()
