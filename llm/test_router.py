"""Unit tests for the multi-provider router.

All tests patch the single transport seam ``llm.adapters._http_post`` — no live
network. The certified pool is controlled via ``llm.certify.certified_route_ids``
so selection is deterministic regardless of any real ``.certified.json``.
"""

from __future__ import annotations

import httpx
import pytest

from llm import adapters, certify, quota, registry, router
from llm.errors import AllRoutesExhausted, RouterUnavailable


# --- helpers ----------------------------------------------------------------

def _ok(text: str, status: int = 200) -> httpx.Response:
    req = httpx.Request("POST", "http://test/chat/completions")
    body = {"choices": [{"message": {"content": text}}], "usage": {}}
    return httpx.Response(status, json=body, request=req)


def _http_error(status: int) -> httpx.Response:
    req = httpx.Request("POST", "http://test/chat/completions")
    return httpx.Response(status, json={"error": "x"}, request=req)


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    monkeypatch.setattr(router.time, "sleep", lambda s: None)


@pytest.fixture()
def env(monkeypatch, tmp_path):
    """Give every catalog route a key so resolve_routes returns them all."""
    fake = {r.env_key: "k" for r in registry.ROUTE_CATALOG}
    fake["CLOUDFLARE_ACCOUNT_ID"] = "acct"
    monkeypatch.setattr(router.env_loader, "load", lambda path=None: dict(fake))
    monkeypatch.setattr(quota, "DEFAULT_DB_PATH", tmp_path / "quota.db")
    return fake


def _certify(monkeypatch, mapping: dict[str, set[str]]):
    monkeypatch.setattr(
        certify, "certified_route_ids",
        lambda purpose, path=None: set(mapping.get(purpose, set())),
    )


def _no_legacy(monkeypatch):
    monkeypatch.setattr(router, "_legacy_configured", lambda: False)


# --- tests ------------------------------------------------------------------

def test_router_unavailable_when_no_routes_and_no_legacy(monkeypatch):
    monkeypatch.setattr(router.env_loader, "load", lambda path=None: {})
    _no_legacy(monkeypatch)
    with pytest.raises(RouterUnavailable):
        router.complete(router.LLMRequest(messages=[], purpose="sql"))


def test_eaon_is_primary_even_with_no_certified_pool(monkeypatch, env):
    _certify(monkeypatch, {})  # nothing certified
    monkeypatch.setattr(router, "_legacy_configured", lambda: True)
    monkeypatch.setattr(router, "_legacy_call", lambda req: ("legacy answer", 5))
    resp = router.complete(router.LLMRequest(messages=[{"role": "user", "content": "hi"}], purpose="sql"))
    assert resp.text == "legacy answer"
    assert resp.route_id == "eaon:legacy"


def test_only_certified_routes_are_selected(monkeypatch, env):
    # Certify only ollama for sql; assert the call goes to ollama's model.
    _certify(monkeypatch, {"sql": {"ollama:gpt-oss-20b"}})
    _no_legacy(monkeypatch)
    seen = {}
    def fake_post(url, *, headers, json, timeout):
        seen["model"] = json["model"]
        return _ok("done")
    monkeypatch.setattr(adapters, "_http_post", fake_post)
    resp = router.complete(router.LLMRequest(messages=[{"role": "user", "content": "x"}], purpose="sql"))
    assert resp.route_id == "ollama:gpt-oss-20b"
    assert seen["model"] == "gpt-oss:20b"


def test_quality_order_prefers_higher_ranked_route(monkeypatch, env):
    # Both certified; for sql, nemotron(openrouter, rank5) beats groq(rank12).
    _certify(monkeypatch, {"sql": {"openrouter:nemotron-3-ultra", "groq:gpt-oss-120b"}})
    _no_legacy(monkeypatch)
    monkeypatch.setattr(adapters, "_http_post", lambda *a, **k: _ok("ok"))
    resp = router.complete(router.LLMRequest(messages=[{"role": "user", "content": "x"}], purpose="sql"))
    assert resp.route_id == "openrouter:nemotron-3-ultra"


def test_cross_provider_fallback_on_5xx(monkeypatch, env):
    _certify(monkeypatch, {"sql": {"openrouter:nemotron-3-ultra", "groq:gpt-oss-120b"}})
    _no_legacy(monkeypatch)
    calls = []
    def fake_post(url, *, headers, json, timeout):
        calls.append(json["model"])
        # openrouter (first by rank) always 502s -> both attempts fail; groq succeeds.
        if json["model"].startswith("nvidia/"):
            resp = _http_error(502)
            resp.raise_for_status()
        return _ok("recovered")
    monkeypatch.setattr(adapters, "_http_post", fake_post)
    resp = router.complete(router.LLMRequest(messages=[{"role": "user", "content": "x"}], purpose="sql"))
    assert resp.route_id == "groq:gpt-oss-120b"
    assert resp.text == "recovered"
    # Preserve scarce free quota: one attempt, then a different provider.
    assert calls.count("nvidia/nemotron-3-ultra-550b-a55b:free") == 1


def test_transient_failure_moves_to_another_provider(monkeypatch, env):
    _certify(monkeypatch, {"intent": {"groq:gpt-oss-120b", "cloudflare:glm-4.7-flash"}})
    _no_legacy(monkeypatch)
    n = {"c": 0}
    def fake_post(url, *, headers, json, timeout):
        n["c"] += 1
        if n["c"] == 1:
            resp = _http_error(502)
            resp.raise_for_status()
        return _ok("second time")
    monkeypatch.setattr(adapters, "_http_post", fake_post)
    resp = router.complete(router.LLMRequest(messages=[{"role": "user", "content": "x"}], purpose="intent"))
    assert resp.route_id == "cloudflare:glm-4.7-flash"
    assert resp.text == "second time" and n["c"] == 2


def test_validator_failure_advances_to_next_route(monkeypatch, env):
    _certify(monkeypatch, {"domain": {"openrouter:nemotron-3-ultra", "groq:gpt-oss-120b"}})
    _no_legacy(monkeypatch)
    def fake_post(url, *, headers, json, timeout):
        # openrouter returns "bad", groq returns "good"
        return _ok("bad" if json["model"].startswith("nvidia/") else "good")
    monkeypatch.setattr(adapters, "_http_post", fake_post)
    def validator(text):
        if text != "good":
            raise ValueError("nope")
        return {"ok": True}
    resp = router.complete(router.LLMRequest(
        messages=[{"role": "user", "content": "x"}], purpose="domain", validator=validator))
    assert resp.route_id == "groq:gpt-oss-120b"
    assert resp.value == {"ok": True}


def test_all_routes_fail_and_no_legacy_raises_exhausted(monkeypatch, env):
    _certify(monkeypatch, {"sql": {"groq:gpt-oss-120b"}})
    _no_legacy(monkeypatch)
    def fake_post(url, *, headers, json, timeout):
        resp = _http_error(500)
        resp.raise_for_status()
    monkeypatch.setattr(adapters, "_http_post", fake_post)
    with pytest.raises(AllRoutesExhausted):
        router.complete(router.LLMRequest(messages=[{"role": "user", "content": "x"}], purpose="sql"))


def test_eaon_primary_wins_before_certified_routes(monkeypatch, env):
    _certify(monkeypatch, {"sql": {"groq:gpt-oss-120b"}})
    monkeypatch.setattr(router, "_legacy_configured", lambda: True)
    monkeypatch.setattr(router, "_legacy_call", lambda req: ("legacy saved it", 3))
    monkeypatch.setattr(router.settings, "llm_model", lambda: "deepseek-v4-pro")
    def fake_post(url, *, headers, json, timeout):
        pytest.fail("certified external route must not run while Eaon succeeds")
    monkeypatch.setattr(adapters, "_http_post", fake_post)
    resp = router.complete(router.LLMRequest(messages=[{"role": "user", "content": "x"}], purpose="sql"))
    assert resp.route_id == "eaon:legacy" and resp.text == "legacy saved it"


def test_eaon_failure_falls_back_to_certified_provider(monkeypatch, env):
    _certify(monkeypatch, {"domain": {"groq:gpt-oss-120b"}})
    monkeypatch.setattr(router, "_legacy_configured", lambda: True)
    monkeypatch.setattr(router, "_legacy_call", lambda req: (_ for _ in ()).throw(
        httpx.ConnectError("eaon unavailable")
    ))
    monkeypatch.setattr(adapters, "_http_post", lambda *a, **k: _ok("external recovered"))
    response = router.complete(router.LLMRequest(
        messages=[{"role": "user", "content": "x"}], purpose="domain",
    ))
    assert response.route_id == "groq:gpt-oss-120b"
    assert response.text == "external recovered"
    assert response.attempts == 2
    assert "eaon:legacy:network" in (response.fallback_reason or "")


def test_eaon_validator_rejection_falls_back(monkeypatch, env):
    _certify(monkeypatch, {"intent": {"groq:gpt-oss-120b"}})
    monkeypatch.setattr(router, "_legacy_configured", lambda: True)
    monkeypatch.setattr(router, "_legacy_call", lambda req: ("bad", 1))
    monkeypatch.setattr(adapters, "_http_post", lambda *a, **k: _ok("good"))

    def validator(text):
        if text != "good":
            raise ValueError("invalid structured output")
        return {"ok": True}

    response = router.complete(router.LLMRequest(
        messages=[{"role": "user", "content": "x"}], purpose="intent",
        validator=validator,
    ))
    assert response.route_id == "groq:gpt-oss-120b"
    assert response.value == {"ok": True}


def test_auth_error_is_not_retried(monkeypatch, env):
    _certify(monkeypatch, {"intent": {"groq:gpt-oss-120b"}})
    _no_legacy(monkeypatch)
    n = {"c": 0}
    def fake_post(url, *, headers, json, timeout):
        n["c"] += 1
        resp = _http_error(403)
        resp.raise_for_status()
    monkeypatch.setattr(adapters, "_http_post", fake_post)
    with pytest.raises(AllRoutesExhausted):
        router.complete(router.LLMRequest(messages=[{"role": "user", "content": "x"}], purpose="intent"))
    assert n["c"] == 1  # 403 is terminal — no per-route retry


def test_google_native_adapter_shapes_request_and_parses(monkeypatch, env):
    # Certify only Google for sql: exercises the native generateContent adapter.
    _certify(monkeypatch, {"sql": {"google:gemini-3.5-flash"}})
    _no_legacy(monkeypatch)
    captured = {}

    def fake_post(url, *, headers, json, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        req = httpx.Request("POST", url)
        body = {
            "candidates": [{"content": {"parts": [{"text": "hi from gemini"}]}}],
            "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 4},
        }
        return httpx.Response(200, json=body, request=req)

    monkeypatch.setattr(adapters, "_http_post", fake_post)
    resp = router.complete(router.LLMRequest(
        messages=[
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "hello"},
        ],
        purpose="sql",
    ))
    assert resp.text == "hi from gemini"
    assert resp.route_id == "google:gemini-3.5-flash"
    # native URL carries the model + method, not /chat/completions
    assert captured["url"].endswith("/models/gemini-3.5-flash:generateContent")
    # native auth header, not Bearer
    assert captured["headers"]["x-goog-api-key"] == "k"
    assert "Authorization" not in captured["headers"]
    # system split out; user role preserved
    assert captured["json"]["system_instruction"]["parts"][0]["text"] == "be terse"
    assert captured["json"]["contents"][0]["role"] == "user"


# --- force_route (certification plumbing) -----------------------------------

def test_force_route_pins_all_calls_and_ignores_pool(monkeypatch, env):
    # No route certified, no legacy — yet force_route must still drive the call
    # to exactly the forced model through the real complete() path.
    _certify(monkeypatch, {})
    _no_legacy(monkeypatch)
    seen = {}
    def fake_post(url, *, headers, json, timeout):
        seen["model"] = json["model"]
        return _ok("forced ok")
    monkeypatch.setattr(adapters, "_http_post", fake_post)
    groq = next(r for r in registry.ROUTE_CATALOG if r.id == "groq:gpt-oss-120b")
    with router.force_route(groq):
        resp = router.complete(router.LLMRequest(
            messages=[{"role": "user", "content": "x"}], purpose="sql"))
    assert resp.route_id == "groq:gpt-oss-120b"
    assert seen["model"] == "openai/gpt-oss-120b"


def test_force_route_failure_raises_exhausted_not_fallthrough(monkeypatch, env):
    # A forced route that fails must NOT fall through to another provider/legacy.
    _certify(monkeypatch, {"sql": {"openrouter:nemotron-3-ultra"}})
    monkeypatch.setattr(router, "_legacy_configured", lambda: True)
    monkeypatch.setattr(router, "_legacy_call", lambda req: ("legacy", 1))
    def fake_post(url, *, headers, json, timeout):
        resp = _http_error(500)
        resp.raise_for_status()
    monkeypatch.setattr(adapters, "_http_post", fake_post)
    groq = next(r for r in registry.ROUTE_CATALOG if r.id == "groq:gpt-oss-120b")
    with pytest.raises(AllRoutesExhausted):
        with router.force_route(groq):
            router.complete(router.LLMRequest(
                messages=[{"role": "user", "content": "x"}], purpose="sql"))


def test_force_route_restores_previous_state(monkeypatch, env):
    groq = next(r for r in registry.ROUTE_CATALOG if r.id == "groq:gpt-oss-120b")
    assert router._FORCED_ROUTE.get() is None
    with router.force_route(groq):
        assert router._FORCED_ROUTE.get() is groq
    assert router._FORCED_ROUTE.get() is None


# --- certify persistence + battery wiring -----------------------------------

def test_certify_persistence_roundtrip(tmp_path):
    p = str(tmp_path / "cert.json")
    assert certify.certified_route_ids("sql", p) == set()
    certify._mark("sql", "groq:gpt-oss-120b", True, p)
    certify._mark("sql", "ollama:gpt-oss-20b", True, p)
    certify._mark("intent", "groq:gpt-oss-120b", True, p)
    assert certify.certified_route_ids("sql", p) == {"groq:gpt-oss-120b", "ollama:gpt-oss-20b"}
    assert certify.certified_route_ids("intent", p) == {"groq:gpt-oss-120b"}
    # demotion removes it
    certify._mark("sql", "groq:gpt-oss-120b", False, p)
    assert certify.certified_route_ids("sql", p) == {"ollama:gpt-oss-20b"}


def test_certify_batteries_are_loadable():
    # Every purpose has a runnable battery harvested from the repo test suites.
    assert set(certify._BATTERIES) == {"intent", "sql", "domain"}
    # The source suites import and expose the expected case counts (guards against
    # a battery silently going empty if a suite is refactored).
    import test_phase5_parser as p
    import test_answers_groundtruth as gt
    assert len(p.CASES) >= 20
    assert len(gt.QUESTIONS) == 30


def test_certifier_does_not_score_infrastructure_as_model_failure():
    assert certify._infrastructure_only([
        "case: AllRoutesExhausted (x: quota_or_cooldown)",
        "case: AllRoutesExhausted (x: http_503)",
    ])
    assert not certify._infrastructure_only([
        "case: AllRoutesExhausted (x: validation_failed)",
        "case: AllRoutesExhausted (x: quota_or_cooldown)",
    ])
