"""Preview helpers: validated single/bundled writes, dedupe guard."""

from __future__ import annotations

import pytest

import agent
from agent import PendingWrite, ToolCall


def _pw(tool: str, args: dict, preview: str) -> PendingWrite:
    return PendingWrite(tool_call=ToolCall(tool=tool, arguments=args), preview=preview, run={})


def test_build_bundle_preview_single_is_plain():
    pw = _pw("create_goal", {"title": "AIR1", "target": 1}, "📝 Create goal\n• title: AIR1")
    assert agent._build_bundle_preview([pw]) == "📝 Create goal\n• title: AIR1"


def test_build_bundle_preview_multiple():
    pws = [
        _pw("set_context", {"subject": "Physics"}, "📝 Set session context\n• subject: Physics"),
        _pw("create_goal", {"title": "b"}, "📝 Create goal\n• title: b"),
    ]
    preview = agent._build_bundle_preview(pws)
    assert "2" in preview and "things" in preview
    assert "subject: Physics" in preview
    assert "title: b" in preview


def test_dedupe_tool_calls_drops_exact_duplicates():
    calls = [
        ToolCall(tool="set_context", arguments={"subject": "Physics"}),
        ToolCall(tool="set_context", arguments={"subject": "Physics"}),
        ToolCall(tool="set_context", arguments={"subject": "Maths"}),
        ToolCall(tool="sql_select", arguments={"sql": "SELECT 1"}),
    ]
    unique = agent._dedupe_tool_calls(calls)
    assert len(unique) == 3
    assert [c.arguments.get("subject") for c in unique[:2]] == ["Physics", "Maths"]


@pytest.mark.asyncio
async def test_agent_multi_write_returns_one_preview(monkeypatch):
    payload = """[
      {"tool": "set_context", "arguments": {"subject": "Physics"}},
      {"tool": "schedule_reminder", "arguments": {"schedule_kind": "daily", "time": "21:30", "action_kind": "message", "action_text": "revise"}}
    ]"""

    def _mock_llm(messages):
        return payload

    monkeypatch.setattr(agent, "_call_llm", _mock_llm)

    async def _noop(_t: str) -> None:
        pass

    result = await agent.run(chat_id=123, user_text="set context and remind me", on_status=_noop)
    assert result["type"] == "preview"
    assert "2" in result["preview"] and "things" in result["preview"]
    assert result["state_id"]


@pytest.mark.asyncio
async def test_agent_mixed_valid_and_invalid_writes(monkeypatch):
    """Valid writes preview; invalid ones are fed back — both in one turn."""
    payload = """[
      {"tool": "set_context", "arguments": {"subject": "Physics"}},
      {"tool": "create_goal", "arguments": {"title": "bad", "goal_type": "zzz", "target": 5}}
    ]"""
    call_count = [0]

    def _mock_llm(messages):
        call_count[0] += 1
        return payload

    monkeypatch.setattr(agent, "_call_llm", _mock_llm)

    async def _noop(_t: str) -> None:
        pass

    result = await agent.run(chat_id=124, user_text="ctx + goal", on_status=_noop)
    # The valid write pauses for confirmation...
    assert result["type"] == "preview"
    assert call_count[0] == 1
