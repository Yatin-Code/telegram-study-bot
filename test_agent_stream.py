"""Agent chat streaming: partial text extract + on_stream callback."""

from __future__ import annotations

import agent


def test_extract_partial_hides_tool_calls():
    raw = '{"tool": "sqlite_query", "arguments": {"sql": "SELECT 1"}}'
    assert agent._extract_partial_user_text(raw) is None


def test_extract_partial_hides_tool_array():
    raw = '[{"tool": "sqlite_query", "arguments": {"sql": "SELECT 1"}}]'
    assert agent._extract_partial_user_text(raw) is None


def test_extract_partial_complete_json_text():
    raw = '{"text": "Hello Yatin", "response_type": "text"}'
    assert agent._extract_partial_user_text(raw) == "Hello Yatin"


def test_extract_partial_unterminated_text_field():
    raw = '{"text": "Hello wor'
    visible = agent._extract_partial_user_text(raw)
    assert visible is not None
    assert visible.startswith("Hello wor")


def test_extract_partial_plain_prose():
    assert agent._extract_partial_user_text("Just a sentence") == "Just a sentence"


async def test_agent_on_stream_receives_visible_text(monkeypatch):
    chunks = [
        '{"text": "Hi',
        ' there", "response_type": "text"}',
    ]

    def _fake_stream(messages):
        yield from chunks

    monkeypatch.setattr(agent, "_stream_llm", _fake_stream)
    seen: list[str] = []

    async def on_stream(text: str) -> None:
        seen.append(text)

    async def _noop(_t: str) -> None:
        pass

    result = await agent.run(chat_id=1, user_text="hi", on_status=_noop, on_stream=on_stream)
    assert result["type"] == "response"
    assert result["response"].text == "Hi there"
    assert seen  # at least one partial/final visible push
    assert any("Hi" in s for s in seen)


async def test_agent_on_stream_skips_tool_json(monkeypatch):
    # First turn (no tool results yet) may stream; tool JSON must stay hidden.
    # After tools run, loop uses complete() and pushes final text once.
    def _fake_stream(messages):
        yield '{"tool": "sqlite_query", "arguments": {"sql": "SELECT 1 as n"}}'

    def _mock_llm(messages):
        if any(m.get("role") == "tool" for m in messages):
            return '{"text": "Got it.", "response_type": "text"}'
        return '{"tool": "sqlite_query", "arguments": {"sql": "SELECT 1 as n"}}'

    monkeypatch.setattr(agent, "_stream_llm", _fake_stream)
    monkeypatch.setattr(agent, "_call_llm", _mock_llm)
    monkeypatch.setattr(agent.agent_tools, "execute_tool", lambda *a, **k: {"rows": [{"n": 1}]})

    seen: list[str] = []

    async def on_stream(text: str) -> None:
        seen.append(text)

    async def _noop(_t: str) -> None:
        pass

    result = await agent.run(chat_id=1, user_text="query", on_status=_noop, on_stream=on_stream)
    assert result["type"] == "response"
    assert result["response"].text == "Got it."
    # Tool JSON must never appear in the stream
    assert not any("sqlite_query" in s for s in seen)
    assert any("Got it." in s for s in seen)


async def test_agent_tool_loop_uses_complete_not_stream(monkeypatch):
    stream_calls = [0]
    complete_calls = [0]

    def _fake_stream(messages):
        stream_calls[0] += 1
        yield "should-not-run-after-tools"

    def _mock_llm(messages):
        complete_calls[0] += 1
        if complete_calls[0] == 1:
            return '{"tool": "sqlite_query", "arguments": {"sql": "SELECT 1 as n"}}'
        return '{"text": "Done.", "response_type": "text"}'

    # Force non-stream first turn by pre-seeding is not possible; patch stream
    # empty so first turn falls back to complete, then second turn has tools.
    def _empty_stream(messages):
        stream_calls[0] += 1
        if False:
            yield ""
        return
        yield  # pragma: no cover

    monkeypatch.setattr(agent, "_stream_llm", _empty_stream)
    monkeypatch.setattr(agent, "_call_llm", _mock_llm)
    monkeypatch.setattr(agent.agent_tools, "execute_tool", lambda *a, **k: {"rows": [{"n": 1}]})

    async def _noop(_t: str) -> None:
        pass

    result = await agent.run(chat_id=1, user_text="scores", on_status=_noop, on_stream=_noop)
    assert result["response"].text == "Done."
    assert complete_calls[0] >= 2
    # After tool results, stream must not be used again for the final turn.
    assert stream_calls[0] == 1
