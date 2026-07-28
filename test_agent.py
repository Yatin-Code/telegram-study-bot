"""Tests for the agentic loop (no Telegram, mocked LLM)."""

from __future__ import annotations

import asyncio
import pytest

pytestmark = pytest.mark.asyncio

import agent
import agent_tools


class _FakeMessage:
    """Tiny async sink for status updates."""

    def __init__(self):
        self.statuses: list[str] = []

    async def reply_text(self, text: str):
        self.statuses.append(text)
        return self


async def _noop_status(text: str) -> None:
    pass


async def _collect_status(collected: list[str], text: str) -> None:
    collected.append(text)


async def test_agent_read_query_returns_response(monkeypatch):
    """A read-only query should execute and return a final response."""
    calls = []

    async def _run():
        monkeypatch.setattr(
            agent,
            "_call_llm",
            lambda messages: '{"tool": "sqlite_query", "arguments": {"sql": "SELECT 1 as n"}}',
        )
        # Second iteration final response
        call_count = [0]

        def _mock_llm(messages):
            call_count[0] += 1
            if call_count[0] == 1:
                return '{"tool": "sqlite_query", "arguments": {"sql": "SELECT 1 as n"}}'
            return '{"text": "Got it.", "response_type": "text"}'

        monkeypatch.setattr(agent, "_call_llm", _mock_llm)
        return await agent.run(chat_id=123, user_text="test", on_status=_noop_status)

    result = await _run()
    assert result["type"] == "response"
    assert result["response"].text == "Got it."


async def test_agent_write_returns_preview(monkeypatch):
    """A write should pause and return a preview."""
    sql = """INSERT INTO user_jobs (chat_id, title) VALUES (123, 'x')"""
    payload = '{"tool": "sqlite_execute", "arguments": {"sql": "' + sql.replace('"', '\\"') + '"}}'

    def _mock_lln(messages):
        return payload

    monkeypatch.setattr(agent, "_call_llm", _mock_lln)
    result = await agent.run(chat_id=123, user_text="test", on_status=_noop_status)
    assert result["type"] == "preview"
    assert "sqlite" in result["preview"].lower()
    assert result["state_id"]


async def test_agent_continue_confirmed_executes_write(monkeypatch):
    """Confirming a preview should execute the write."""
    sql = """INSERT INTO user_jobs (chat_id, title) VALUES (123, 'x')"""
    payload = '{"tool": "sqlite_execute", "arguments": {"sql": "' + sql.replace('"', '\\"') + '"}}'

    def _mock_llm(messages):
        return payload

    monkeypatch.setattr(agent, "_call_llm", _mock_llm)
    result = await agent.run(chat_id=123, user_text="test", on_status=_noop_status)
    assert result["type"] == "preview"

    # Patch the second LLM call to final response
    def _final_llm(messages):
        return '{"text": "Created job.", "response_type": "text"}'

    monkeypatch.setattr(agent, "_call_llm", _final_llm)
    result2 = await agent.continue_run(result["state_id"], confirmed=True, on_status=_noop_status)
    assert result2["type"] == "response"
    assert result2["response"].text == "Created job."


async def test_agent_cancel_returns_to_llm(monkeypatch):
    """Cancelling should inform the LLM and let it respond."""
    sql = """INSERT INTO user_jobs (chat_id, title) VALUES (123, 'x')"""
    payload = '{"tool": "sqlite_execute", "arguments": {"sql": "' + sql.replace('"', '\\"') + '"}}'

    def _mock_llm(messages):
        return payload

    monkeypatch.setattr(agent, "_call_llm", _mock_llm)
    result = await agent.run(chat_id=123, user_text="test", on_status=_noop_status)

    def _final_llm(messages):
        return '{"text": "No problem.", "response_type": "text"}'

    monkeypatch.setattr(agent, "_call_llm", _final_llm)
    result2 = await agent.continue_run(result["state_id"], confirmed=False, on_status=_noop_status)
    assert result2["type"] == "response"
    assert result2["response"].text == "No problem."
