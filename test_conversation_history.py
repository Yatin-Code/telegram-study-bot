"""Tests for conversation history persistence."""

from __future__ import annotations

import pytest

import conversation_history
import agent


@pytest.fixture
def tmp_db(tmp_path):
    return tmp_path / "conv.db"


async def test_save_and_load_history(tmp_db):
    conversation_history.save_message(1, "user", "hello", db_path=tmp_db)
    conversation_history.save_message(1, "assistant", "hi there", db_path=tmp_db)
    msgs = conversation_history.recent_messages(1, limit=10, db_path=tmp_db)
    assert len(msgs) == 2
    assert msgs[0] == {"role": "user", "content": "hello"}
    assert msgs[1] == {"role": "assistant", "content": "hi there"}


async def test_history_window_limits(tmp_db):
    for i in range(20):
        conversation_history.save_message(1, "user", f"msg{i}", db_path=tmp_db)
    msgs = conversation_history.recent_messages(1, limit=5, db_path=tmp_db)
    assert len(msgs) == 5
    assert msgs[0]["content"] == "msg15"
    assert msgs[-1]["content"] == "msg19"


async def test_history_isolated_by_chat(tmp_db):
    conversation_history.save_message(1, "user", "chat1", db_path=tmp_db)
    conversation_history.save_message(2, "user", "chat2", db_path=tmp_db)
    assert len(conversation_history.recent_messages(1, db_path=tmp_db)) == 1
    assert conversation_history.recent_messages(1, db_path=tmp_db)[0]["content"] == "chat1"


async def test_tool_result_summarized(tmp_db):
    conversation_history.save_message(
        1, "tool", '{"rows": [{"a": 1}, {"a": 2}, {"a": 3}, {"a": 4}]}',
        db_path=tmp_db
    )
    msgs = conversation_history.recent_messages(1, db_path=tmp_db)
    assert "4 row(s)" in msgs[0]["content"]


async def test_agent_run_saves_exchange(monkeypatch, tmp_db):
    calls = []

    def _mock_llm(messages):
        calls.append([m["content"] for m in messages])
        return '{"text": "Got it."}'

    monkeypatch.setattr(agent, "_call_llm", _mock_llm)
    monkeypatch.setattr(conversation_history, "DEFAULT_DB_PATH", tmp_db)

    result = await agent.run(chat_id=123, user_text="first")
    assert result["response"].text == "Got it."

    result2 = await agent.run(chat_id=123, user_text="second")
    assert result2["response"].text == "Got it."

    # Two runs, each one _call_llm call (no tools). calls[1] is the second run.
    assert len(calls) == 2
    second_messages = calls[1]
    assert "first" in second_messages
    assert "Got it." in second_messages
    assert "second" in second_messages


async def test_agent_run_with_history_includes_context(monkeypatch, tmp_db):
    conversation_history.save_message(7, "user", "my name is Yatin", db_path=tmp_db)
    conversation_history.save_message(7, "assistant", "Nice to meet you, Yatin.", db_path=tmp_db)
    monkeypatch.setattr(conversation_history, "DEFAULT_DB_PATH", tmp_db)

    last_call = []

    def _mock_llm(messages):
        nonlocal last_call
        last_call = list(messages)
        return '{"text": "Hello Yatin."}'

    monkeypatch.setattr(agent, "_call_llm", _mock_llm)

    result = await agent.run(chat_id=7, user_text="what is my name?")
    assert result["response"].text == "Hello Yatin."
    roles = [m["role"] for m in last_call]
    assert roles == ["system", "user", "assistant", "user"]
    assert any("Yatin" in m.get("content", "") for m in last_call)
