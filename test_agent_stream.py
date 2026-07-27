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
    def _fake_stream(messages):
        yield '{"tool": "sqlite_query", "arguments": {"sql": "SELECT 1 as n"}}'

    call_count = [0]

    def _mock_llm(messages):
        call_count[0] += 1
        if call_count[0] == 1:
            return '{"tool": "sqlite_query", "arguments": {"sql": "SELECT 1 as n"}}'
        return '{"text": "Got it.", "response_type": "text"}'

    monkeypatch.setattr(agent, "_stream_llm", _fake_stream)
    # Second loop iteration uses _call_llm when? Actually with on_stream always uses _stream_llm.
    # So second turn also goes through _stream_llm — re-patch after first yield path.
    streams = [
        ['{"tool": "sqlite_query", "arguments": {"sql": "SELECT 1 as n"}}'],
        ['{"text": "Got it.", "response_type": "text"}'],
    ]
    idx = [0]

    def _multi_stream(messages):
        batch = streams[min(idx[0], len(streams) - 1)]
        idx[0] += 1
        yield from batch

    monkeypatch.setattr(agent, "_stream_llm", _multi_stream)
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
