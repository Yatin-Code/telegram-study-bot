"""Adversarial tests for durable quota accounting and proactive fallback."""

from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from llm import adapters, certify, quota, registry, router


UTC = dt.timezone.utc


def _route(*, limit: int = 10, switch: float = .9, headers: bool = False) -> registry.Route:
    return registry.Route(
        id="test:tiny", provider="test", adapter="openai", base_url="https://test/v1",
        model="tiny", auth="bearer", env_key="TEST_KEY",
        quotas=(registry.Quota(
            "requests", "rpm", limit, switch, "estimated",
            "x-ratelimit-limit-requests" if headers else None,
            "x-ratelimit-remaining-requests" if headers else None,
            "x-ratelimit-reset-requests" if headers else None,
        ),),
    )


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "quota.db"
    monkeypatch.setattr(quota, "DEFAULT_DB_PATH", path)
    return path


def test_atomic_reservations_never_oversubscribe(db):
    route = _route(limit=100, headers=True)
    # Establish an exact provider-reported allowance of ten total requests.
    first = quota.reserve(route, "sql", 1, db)
    assert first
    quota.reconcile(first, route, success=True, rate_headers={
        "x-ratelimit-limit-requests": "10",
        "x-ratelimit-remaining-requests": "9",
        "x-ratelimit-reset-requests": "60s",
    }, db_path=db)
    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(lambda _: quota.reserve(route, "sql", 1, db), range(30)))
    accepted = [r for r in results if r is not None]
    assert len(accepted) == 9
    state = quota.health(db)
    assert state["windows"][0]["reserved"] == 9
    assert state["windows"][0]["remaining"] == 0


def test_estimated_limit_is_advisory_and_never_a_hard_wall(db):
    route = _route(limit=10, switch=.9)
    reservations = [quota.reserve(route, "intent", 1, db) for _ in range(9)]
    assert all(reservations)
    state = quota.availability(route, 1, db)
    assert state.usable and state.advisory and "reserve" in (state.reason or "")
    tenth = quota.reserve(route, "intent", 1, db)
    eleventh = quota.reserve(route, "intent", 1, db)
    assert tenth is not None and tenth.advisory
    assert eleventh is not None and eleventh.advisory


def test_authoritative_headers_override_estimate_and_reset(db):
    route = _route(limit=100, headers=True)
    now = dt.datetime(2026, 7, 22, 10, 0, 0, tzinfo=UTC)
    reservation = quota.reserve(route, "sql", 1, db, now=now)
    assert reservation
    quota.reconcile(
        reservation, route, success=True,
        rate_headers={
            "X-RateLimit-Limit-Requests": "5",
            "X-RateLimit-Remaining-Requests": "0",
            "X-RateLimit-Reset-Requests": "30s",
        }, db_path=db, now=now,
    )
    blocked = quota.availability(route, 1, db, now=now + dt.timedelta(seconds=5))
    assert not blocked.usable and "authoritative" in (blocked.reason or "")
    recovered = quota.availability(route, 1, db, now=now + dt.timedelta(seconds=31))
    assert recovered.usable


def test_429_cooldown_and_automatic_recovery(db):
    route = _route()
    now = dt.datetime(2026, 7, 22, 10, 0, tzinfo=UTC)
    quota.record_route_result(
        route.id, success=False, reason="429", retry_after="45", db_path=db, now=now,
    )
    assert not quota.availability(route, 1, db, now=now + dt.timedelta(seconds=44)).usable
    assert quota.availability(route, 1, db, now=now + dt.timedelta(seconds=46)).usable


def test_auth_failure_disables_until_explicit_reset(db):
    route = _route()
    quota.record_route_result(route.id, success=False, reason="auth_403", db_path=db)
    assert not quota.availability(route, 1, db).usable
    quota.reset_route(route.id, db)
    assert quota.availability(route, 1, db).usable


def test_stale_reservation_is_recovered_after_crash(db):
    route = _route(limit=1)
    now = dt.datetime(2026, 7, 22, 10, 0, tzinfo=UTC)
    assert quota.reserve(route, "sql", 1, db, now=now)
    # Same quota window is artificial here; recovery occurs before window logic.
    later = now + dt.timedelta(minutes=6)
    assert quota.reserve(route, "sql", 1, db, now=later)
    state = quota.health(db)
    assert any(w["reserved"] == 1 for w in state["windows"])


def _ok(text: str) -> httpx.Response:
    request = httpx.Request("POST", "https://test/chat/completions")
    return httpx.Response(
        200, request=request,
        json={"choices": [{"message": {"content": text}}], "usage": {
            "prompt_tokens": 2, "completion_tokens": 1,
        }},
    )


def test_router_switches_before_limit_not_after_429(db, monkeypatch):
    env = {r.env_key: "k" for r in registry.ROUTE_CATALOG}
    env["CLOUDFLARE_ACCOUNT_ID"] = "acct"
    monkeypatch.setattr(router.env_loader, "load", lambda path=None: dict(env))
    monkeypatch.setattr(router, "_legacy_configured", lambda: False)
    monkeypatch.setattr(
        certify, "certified_route_ids",
        lambda purpose, path=None: {"openrouter:nemotron-3-ultra", "groq:gpt-oss-120b"},
    )
    openrouter = next(r for r in registry.ROUTE_CATALOG if r.id == "openrouter:nemotron-3-ultra")
    # Consume the 18 RPM advisory headroom without hitting the provider.
    for _ in range(18):
        reservation = quota.reserve(openrouter, "sql", 1, db)
        assert reservation
        quota.reconcile(reservation, openrouter, success=True, prompt_tokens=1, db_path=db)
    seen: list[str] = []
    monkeypatch.setattr(
        adapters, "_http_post",
        lambda url, *, headers, json, timeout: (seen.append(json["model"]) or _ok("safe")),
    )
    response = router.complete(router.LLMRequest(
        messages=[{"role": "user", "content": "hi"}], purpose="sql", max_output_tokens=8,
    ))
    assert response.route_id == "groq:gpt-oss-120b"
    assert seen == ["openai/gpt-oss-120b"]


def test_router_429_falls_back_and_skips_cooldown_next_call(db, monkeypatch):
    env = {r.env_key: "k" for r in registry.ROUTE_CATALOG}
    env["CLOUDFLARE_ACCOUNT_ID"] = "acct"
    monkeypatch.setattr(router.env_loader, "load", lambda path=None: dict(env))
    monkeypatch.setattr(router, "_legacy_configured", lambda: False)
    monkeypatch.setattr(
        certify, "certified_route_ids",
        lambda purpose, path=None: {"openrouter:nemotron-3-ultra", "groq:gpt-oss-120b"},
    )
    calls: list[str] = []

    def transport(url, *, headers, json, timeout):
        calls.append(json["model"])
        if json["model"].startswith("nvidia/"):
            req = httpx.Request("POST", url)
            response = httpx.Response(429, request=req, headers={"Retry-After": "120"})
            response.raise_for_status()
        return _ok("fallback")

    monkeypatch.setattr(adapters, "_http_post", transport)
    request = router.LLMRequest(
        messages=[{"role": "user", "content": "hi"}], purpose="sql", max_output_tokens=8,
    )
    assert router.complete(request).route_id == "groq:gpt-oss-120b"
    assert router.complete(request).route_id == "groq:gpt-oss-120b"
    assert calls.count("nvidia/nemotron-3-ultra-550b-a55b:free") == 1


def test_cohere_v2_shape_and_usage(monkeypatch):
    route = next(r for r in registry.ROUTE_CATALOG if r.id == "cohere:command-a-plus")
    captured = {}

    def transport(url, *, headers, json, timeout):
        captured.update(url=url, headers=headers, body=json)
        req = httpx.Request("POST", url)
        return httpx.Response(200, request=req, json={
            "message": {"content": [{"type": "text", "text": "OK"}]},
            "usage": {"billed_units": {"input_tokens": 3, "output_tokens": 1}},
        })

    monkeypatch.setattr(adapters, "_http_post", transport)
    req = router.LLMRequest(messages=[{"role": "user", "content": "hi"}], purpose="intent")
    result = adapters.call(route, req, "secret", route.base_url)
    assert captured["url"] == "https://api.cohere.com/v2/chat"
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert result.text == "OK" and result.usage_prompt == 3 and result.usage_completion == 1
