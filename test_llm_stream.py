"""LLM router/adapter streaming (SSE) unit tests — no live network."""

from __future__ import annotations

import httpx
import pytest

from llm import adapters, certify, quota, registry, router


def test_openai_delta_from_sse_line():
    line = 'data: {"choices":[{"delta":{"content":"Hel"}}]}'
    assert adapters._openai_delta_from_sse_line(line) == "Hel"
    assert adapters._openai_delta_from_sse_line("data: [DONE]") is None
    assert adapters._openai_delta_from_sse_line("event: ping") is None


def test_stream_call_openai_yields_deltas(monkeypatch):
    route = registry.Route(
        id="test:openai",
        provider="test",
        adapter="openai",
        base_url="https://example.com/v1",
        model="m",
        auth="bearer",
        env_key="K",
        quality_rank=1,
    )

    class _FakeStreamResp:
        status_code = 200

        def raise_for_status(self):
            return None

        def iter_lines(self):
            yield 'data: {"choices":[{"delta":{"content":"A"}}]}'
            yield 'data: {"choices":[{"delta":{"content":"B"}}]}'
            yield "data: [DONE]"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def stream(self, *a, **k):
            return _FakeStreamResp()

    monkeypatch.setattr(adapters.httpx, "Client", _FakeClient)
    req = router.LLMRequest(messages=[{"role": "user", "content": "x"}], purpose="domain")
    deltas = list(adapters.stream_call(route, req, "key", "https://example.com/v1"))
    assert deltas == ["A", "B"]


def test_stream_complete_legacy_primary(monkeypatch, tmp_path):
    monkeypatch.setattr(quota, "DEFAULT_DB_PATH", tmp_path / "q.db")
    monkeypatch.setattr(router, "_legacy_configured", lambda: True)
    monkeypatch.setattr(router, "_legacy_stream", lambda req: iter(["Hello", " world"]))
    monkeypatch.setattr(certify, "certified_route_ids", lambda purpose, path=None: set())
    monkeypatch.setattr(router.env_loader, "load", lambda path=None: {})

    req = router.LLMRequest(messages=[{"role": "user", "content": "hi"}], purpose="domain")
    assert "".join(router.stream_complete(req)) == "Hello world"


def test_stream_complete_falls_through_on_legacy_fail(monkeypatch, tmp_path, env=None):
    monkeypatch.setattr(quota, "DEFAULT_DB_PATH", tmp_path / "q.db")
    fake = {r.env_key: "k" for r in registry.ROUTE_CATALOG}
    fake["CLOUDFLARE_ACCOUNT_ID"] = "acct"
    monkeypatch.setattr(router.env_loader, "load", lambda path=None: dict(fake))
    monkeypatch.setattr(router, "_legacy_configured", lambda: True)

    def boom(req):
        raise httpx.HTTPStatusError(
            "err",
            request=httpx.Request("POST", "http://x"),
            response=httpx.Response(500, request=httpx.Request("POST", "http://x")),
        )
        yield  # pragma: no cover

    monkeypatch.setattr(router, "_legacy_stream", boom)
    monkeypatch.setattr(
        certify, "certified_route_ids",
        lambda purpose, path=None: {"ollama:gpt-oss-20b"},
    )

    def fake_stream(route, req, api_key, base_url):
        yield "from-ollama"

    monkeypatch.setattr(adapters, "stream_call", fake_stream)
    req = router.LLMRequest(messages=[{"role": "user", "content": "hi"}], purpose="domain")
    assert "".join(router.stream_complete(req)) == "from-ollama"
