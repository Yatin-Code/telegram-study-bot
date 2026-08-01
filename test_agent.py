"""Tests for the agentic loop (no Telegram, mocked LLM)."""

from __future__ import annotations

import agent
import agent_tools


async def _noop_status(text: str) -> None:
    pass


async def test_agent_read_query_returns_response(monkeypatch):
    """A read-only query should execute and return a final response."""
    call_count = [0]

    def _mock_llm(messages):
        call_count[0] += 1
        if call_count[0] == 1:
            return '{"tool": "sql_select", "arguments": {"sql": "SELECT 1 as n"}}'
        return '{"text": "Got it.", "response_type": "text"}'

    monkeypatch.setattr(agent, "_call_llm", _mock_llm)
    result = await agent.run(chat_id=123, user_text="test", on_status=_noop_status)
    assert result["type"] == "response"
    assert result["response"].text == "Got it."


async def test_agent_write_returns_preview(monkeypatch):
    """A write should be validated and paused with a preview."""
    payload = '{"tool": "set_context", "arguments": {"subject": "Physics"}}'

    def _mock_llm(messages):
        return payload

    monkeypatch.setattr(agent, "_call_llm", _mock_llm)
    result = await agent.run(chat_id=123, user_text="test", on_status=_noop_status)
    assert result["type"] == "preview"
    assert "subject" in result["preview"].lower()
    assert result["state_id"]


async def test_agent_invalid_write_is_fed_back_not_previewed(monkeypatch):
    """An invalid write must NOT produce a Confirm card — it goes back to the model."""
    call_count = [0]

    def _mock_llm(messages):
        call_count[0] += 1
        if call_count[0] == 1:
            # bad enum + missing target: must fail preview-time validation
            return '{"tool": "create_goal", "arguments": {"title": "x", "goal_type": "zzz"}}'
        return '{"text": "Fixed my mistake.", "response_type": "text"}'

    monkeypatch.setattr(agent, "_call_llm", _mock_llm)
    result = await agent.run(chat_id=123, user_text="set a goal", on_status=_noop_status)
    assert result["type"] == "response"
    assert result["response"].text == "Fixed my mistake."
    assert call_count[0] == 2  # model got one error feedback turn


async def test_agent_continue_confirmed_executes_write(monkeypatch):
    """Confirming a preview should execute the prepared write."""
    payload = '{"tool": "set_context", "arguments": {"subject": "Chem"}}'

    def _mock_llm(messages):
        return payload

    monkeypatch.setattr(agent, "_call_llm", _mock_llm)
    result = await agent.run(chat_id=999, user_text="switch subject", on_status=_noop_status)
    assert result["type"] == "preview"

    def _final_llm(messages):
        return '{"text": "Context updated.", "response_type": "text"}'

    monkeypatch.setattr(agent, "_call_llm", _final_llm)
    result2 = await agent.continue_run(result["state_id"], confirmed=True, on_status=_noop_status)
    assert result2["type"] == "response"
    assert result2["response"].text == "Context updated."

    import session_context
    ctx = session_context.get_context(999)
    assert ctx is not None
    assert ctx["subject"] == "Chem"


async def test_agent_cancel_returns_to_llm(monkeypatch):
    """Cancelling should inform the LLM and let it respond without writing."""
    payload = '{"tool": "set_context", "arguments": {"subject": "Maths"}}'

    def _mock_llm(messages):
        return payload

    monkeypatch.setattr(agent, "_call_llm", _mock_llm)
    result = await agent.run(chat_id=998, user_text="test", on_status=_noop_status)

    def _final_llm(messages):
        # The cancelled write must be visible to the model as a tool result.
        assert any("cancelled" in str(m.get("content", "")) for m in messages)
        return '{"text": "No problem.", "response_type": "text"}'

    monkeypatch.setattr(agent, "_call_llm", _final_llm)
    result2 = await agent.continue_run(result["state_id"], confirmed=False, on_status=_noop_status)
    assert result2["type"] == "response"
    assert result2["response"].text == "No problem."


async def test_agent_duplicate_calls_are_deduped(monkeypatch):
    """Exact duplicate tool calls in one turn collapse to one write."""
    payload = """[
      {"tool": "set_context", "arguments": {"subject": "Physics"}},
      {"tool": "set_context", "arguments": {"subject": "Physics"}}
    ]"""

    def _mock_llm(messages):
        return payload

    monkeypatch.setattr(agent, "_call_llm", _mock_llm)
    result = await agent.run(chat_id=997, user_text="ctx", on_status=_noop_status)
    assert result["type"] == "preview"
    # one unique write → single (un-numbered) preview, not a bundle
    assert "2" not in result["preview"] or "things" not in result["preview"]


def test_raw_sql_write_tool_is_rejected():
    """Raw SQL writes are gone from the tool surface entirely."""
    assert "sqlite_execute" not in agent_tools.WRITE_TOOLS
    assert "sqlite_query" not in agent_tools.WRITE_TOOLS
    out = agent_tools.execute_tool("sqlite_execute", {"sql": "DELETE FROM ledger"}, chat_id=1)
    assert out["error"]
    out = agent_tools.execute_tool("notion_api", {"method": "GET", "path": "/x"}, chat_id=1)
    assert out["error"]
