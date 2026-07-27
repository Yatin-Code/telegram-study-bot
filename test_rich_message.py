"""Tests for Bot API 10.1 rich message helpers and the streaming gotcha pattern."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import rich_message


@pytest.fixture(autouse=True)
def _reset_rich_latch():
    rich_message.reset_capability_latch()
    yield
    rich_message.reset_capability_latch()



class _FakeResponse:
    def __init__(self, data: dict, status_code: int = 200):
        self._data = data
        self.status_code = status_code

    def json(self):
        return self._data


class _FakeAsyncClient:
    """Records POSTs and returns scripted responses."""

    def __init__(self, responses: list[dict] | None = None, *, fail_methods: set[str] | None = None):
        self.calls: list[tuple[str, dict]] = []
        self._responses = list(responses or [])
        self._fail_methods = fail_methods or set()
        self._default_ok = {"ok": True, "result": {"message_id": 42, "chat": {"id": 1}}}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url: str, json: dict | None = None):
        method = url.rstrip("/").rsplit("/", 1)[-1]
        self.calls.append((method, dict(json or {})))
        if method in self._fail_methods:
            return _FakeResponse({"ok": False, "description": f"{method} rejected"}, 400)
        if self._responses:
            return _FakeResponse(self._responses.pop(0))
        return _FakeResponse(self._default_ok)


def _patch_client(client: _FakeAsyncClient):
    return patch("rich_message.httpx.AsyncClient", return_value=client)


def test_build_input_rich_message_markdown():
    assert rich_message.build_input_rich_message("hi", "markdown") == {"markdown": "hi"}
    assert rich_message.build_input_rich_message("hi", "html") == {"html": "hi"}
    assert rich_message.build_input_rich_message("hi", "plain") == {"markdown": "hi"}


@pytest.mark.asyncio
async def test_send_rich_calls_sendRichMessage(monkeypatch):
    monkeypatch.setenv("RICH_MESSAGES", "1")
    client = _FakeAsyncClient()
    with _patch_client(client):
        result = await rich_message.send_rich("tok", 123, "# Hello", parse_mode="markdown")
    assert result["message_id"] == 42
    assert len(client.calls) == 1
    method, payload = client.calls[0]
    assert method == "sendRichMessage"
    assert payload["chat_id"] == 123
    assert payload["rich_message"] == {"markdown": "# Hello"}


@pytest.mark.asyncio
async def test_send_rich_falls_back_to_plain_sendMessage(monkeypatch):
    monkeypatch.setenv("RICH_MESSAGES", "1")
    client = _FakeAsyncClient(fail_methods={"sendRichMessage"})
    with _patch_client(client):
        result = await rich_message.send_rich("tok", 123, "plain text", parse_mode="markdown")
    assert result["message_id"] == 42
    methods = [m for m, _ in client.calls]
    assert methods == ["sendRichMessage", "sendMessage"]
    plain = client.calls[1][1]
    assert plain["text"] == "plain text"
    assert plain["parse_mode"] == "Markdown"
    assert "rich_message" not in plain


@pytest.mark.asyncio
async def test_edit_rich_uses_rich_message_param(monkeypatch):
    monkeypatch.setenv("RICH_MESSAGES", "1")
    client = _FakeAsyncClient()
    with _patch_client(client):
        await rich_message.edit_rich("tok", 123, 99, "final", parse_mode="markdown")
    method, payload = client.calls[0]
    assert method == "editMessageText"
    assert payload["message_id"] == 99
    assert payload["rich_message"] == {"markdown": "final"}
    assert "text" not in payload


@pytest.mark.asyncio
async def test_edit_rich_falls_back_to_plain(monkeypatch):
    monkeypatch.setenv("RICH_MESSAGES", "1")
    # First edit (rich) fails; second (plain) succeeds.
    client = _FakeAsyncClient()
    original_post = client.post

    async def _post(url, json=None):
        method = url.rstrip("/").rsplit("/", 1)[-1]
        payload = dict(json or {})
        client.calls.append((method, payload))
        if method == "editMessageText" and "rich_message" in payload:
            return _FakeResponse({"ok": False, "description": "rich rejected"}, 400)
        return _FakeResponse({"ok": True, "result": True})

    client.post = _post  # type: ignore[method-assign]
    with _patch_client(client):
        result = await rich_message.edit_rich("tok", 123, 99, "final")
    assert result is True
    assert len(client.calls) == 2
    assert "rich_message" in client.calls[0][1]
    assert client.calls[1][1]["text"] == "final"
    assert "rich_message" not in client.calls[1][1]


@pytest.mark.asyncio
async def test_rich_stream_pattern_never_bare_edit(monkeypatch):
    """Gotcha: first=sendRichMessage, mid=draft, final=edit with rich_message."""
    monkeypatch.setenv("RICH_MESSAGES", "1")
    client = _FakeAsyncClient(
        responses=[
            {"ok": True, "result": {"message_id": 7, "chat": {"id": 1}}},
            {"ok": True, "result": True},
            {"ok": True, "result": {"message_id": 7, "chat": {"id": 1}}},
        ]
    )
    stream = rich_message.RichStream("tok", 1, parse_mode="markdown", draft_id=99)
    with _patch_client(client):
        await stream.feed("Hel")
        await stream.feed("Hello wo")
        await stream.feed("Hello world", is_final=True)

    methods = [m for m, _ in client.calls]
    assert methods == ["sendRichMessage", "sendRichMessageDraft", "editMessageText"]

    # First: rich from the start
    assert client.calls[0][1]["rich_message"] == {"markdown": "Hel"}

    # Intermediate: draft, never editMessageText
    draft = client.calls[1][1]
    assert draft["draft_id"] == 99
    assert draft["rich_message"] == {"markdown": "Hello wo"}

    # Final: edit with rich_message, never bare
    final = client.calls[2][1]
    assert final["message_id"] == 7
    assert final["rich_message"] == {"markdown": "Hello world"}
    assert "text" not in final

    # Explicit: no bare editMessageText (without rich_message) was ever called
    for method, payload in client.calls:
        if method == "editMessageText":
            assert "rich_message" in payload


@pytest.mark.asyncio
async def test_rich_stream_single_chunk_final(monkeypatch):
    """Non-streaming case: one feed with is_final=True → only sendRichMessage."""
    monkeypatch.setenv("RICH_MESSAGES", "1")
    client = _FakeAsyncClient()
    stream = rich_message.RichStream("tok", 1)
    with _patch_client(client):
        await stream.feed("Done.", is_final=True)
    assert [m for m, _ in client.calls] == ["sendRichMessage"]
    assert stream._finalized is True


@pytest.mark.asyncio
async def test_rich_messages_disabled_skips_rich(monkeypatch):
    monkeypatch.setenv("RICH_MESSAGES", "0")
    # Force re-read of env via the settings function
    client = _FakeAsyncClient()
    with _patch_client(client):
        await rich_message.send_rich("tok", 1, "hi")
    assert [m for m, _ in client.calls] == ["sendMessage"]
    assert client.calls[0][1]["text"] == "hi"


@pytest.mark.asyncio
async def test_send_rich_message_draft_rejects_zero_draft_id():
    with pytest.raises(ValueError, match="non-zero"):
        await rich_message.send_rich_message_draft("tok", 1, {"markdown": "x"}, 0)


async def test_capability_latch_skips_rich_after_unsupported(monkeypatch):
    monkeypatch.setenv("RICH_MESSAGES", "1")
    rich_message.reset_capability_latch()

    class _MissingMethodClient(_FakeAsyncClient):
        async def post(self, url: str, json: dict | None = None):
            method = url.rstrip("/").rsplit("/", 1)[-1]
            self.calls.append((method, dict(json or {})))
            if method == "sendRichMessage":
                return _FakeResponse({"ok": False, "description": "Unknown method"}, 404)
            return _FakeResponse(self._default_ok)

    client = _MissingMethodClient()
    with _patch_client(client):
        await rich_message.send_rich("tok", 1, "hi", parse_mode="markdown")
    assert rich_message._rich_unsupported is True
    # second call should go straight to plain, never retry sendRichMessage
    client2 = _FakeAsyncClient()
    with _patch_client(client2):
        await rich_message.send_rich("tok", 1, "again", parse_mode="markdown")
    methods = [m for m, _ in client2.calls]
    assert "sendRichMessage" not in methods
    assert "sendMessage" in methods
    rich_message.reset_capability_latch()
