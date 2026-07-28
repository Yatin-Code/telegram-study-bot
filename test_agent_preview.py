"""Preview helpers: table name, follow-ups, multi-write bundle."""

from __future__ import annotations

import pytest

import agent
from agent import ToolCall


def test_infer_sql_table_insert():
    assert agent._infer_sql_table("INSERT INTO user_prefs (chat_id, text) VALUES (1, 'x')") == "user_prefs"


def test_infer_sql_table_update():
    assert agent._infer_sql_table('UPDATE "op_goals" SET status=\'Done\'') == "op_goals"


def test_build_preview_includes_table_and_followups():
    tc = ToolCall(
        tool="sqlite_execute",
        arguments={"sql": "INSERT INTO op_goals (title, status) VALUES ('AIR1', 'Active')"},
    )
    preview = agent._build_preview(tc)
    assert "`op_goals`" in preview
    assert "goals and targets" in preview
    assert "After this I can:" in preview
    assert "/jobs" in preview or "/weekly" in preview


def test_build_bundle_preview_single_is_plain():
    tc = ToolCall(
        tool="sqlite_execute",
        arguments={"sql": "INSERT INTO user_prefs (text) VALUES ('mornings')"},
    )
    assert agent._build_bundle_preview([tc]) == agent._build_preview(tc)


def test_build_bundle_preview_multiple():
    tcs = [
        ToolCall(tool="sqlite_execute", arguments={"sql": "INSERT INTO user_prefs (text) VALUES ('a')"}),
        ToolCall(tool="sqlite_execute", arguments={"sql": "INSERT INTO op_goals (title) VALUES ('b')"}),
    ]
    preview = agent._build_bundle_preview(tcs)
    assert "2" in preview and "things" in preview
    assert "`user_prefs`" in preview
    assert "`op_goals`" in preview


@pytest.mark.asyncio
async def test_agent_multi_write_returns_one_preview(monkeypatch):
    payload = """[
      {"tool": "sqlite_execute", "arguments": {"sql": "INSERT INTO user_prefs (text) VALUES (\'x\')"}},
      {"tool": "sqlite_execute", "arguments": {"sql": "INSERT INTO op_goals (title) VALUES (\'y\')"}}
    ]"""

    def _mock_llm(messages):
        return payload

    monkeypatch.setattr(agent, "_call_llm", _mock_llm)

    async def _noop(_t: str) -> None:
        pass

    result = await agent.run(chat_id=123, user_text="remember and set goal", on_status=_noop)
    assert result["type"] == "preview"
    assert "2" in result["preview"] and "things" in result["preview"]
    assert result["state_id"]
