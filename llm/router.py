"""The router: one entry point every LLM call site uses.

``complete(req)``:

1. If the ``llm`` package can't run at all (no keys, no candidates,
   no resolvable routes), raise ``RouterUnavailable`` so the caller uses
   its own legacy path.
2. Try the CURATED LADDER first (llm.ladder): hand-picked eaon/g4f models,
   re-ranked by live probes, traffic cooldowns and certification — so a
   statically pinned dumb primary can never squat at the top.
3. Next, catalog routes **certified for this purpose** (see :mod:`llm.certify`).
4. Finally the legacy env gateway tail (LLM_MODEL / LLM_FALLBACK_MODEL),
   model order re-ranked by the same ladder.
5. Try each candidate once, preserving scarce quota by moving across providers
   on 5xx/429/network failures. A validator raise == malformed output advances
   to the next route. First success returns an ``LLMResponse``.
6. Only if every stage fails do we raise ``AllRoutesExhausted``.

Secrets discipline: only route ids, providers, models, HTTP status and latency
are logged — never keys, prompts, or model output.
"""

from __future__ import annotations

import contextvars
import dataclasses
import logging
import sqlite3
import time
from collections.abc import Iterator
from typing import Any, Callable, Literal, Optional

import httpx

from config import settings

from . import adapters, certify, env_loader, ladder, quota, registry
from .errors import AllRoutesExhausted, RouterUnavailable

logger = logging.getLogger(__name__)

Purpose = Literal["intent", "domain", "sql"]
Validator = Callable[[str], Any]

# Cross-provider fallback is safer than spending a second request from the same
# constrained free-tier bucket. Circuit/cooldown state decides when it recovers.
_MAX_ATTEMPTS_PER_ROUTE = 1
_LEGACY_USER_AGENT = "study-bot/1.0"


@dataclasses.dataclass(frozen=True)
class LLMRequest:
    messages: list[dict[str, str]]
    purpose: Purpose
    max_output_tokens: int = 1024
    validator: Optional[Validator] = None
    pinned_model: Optional[str] = None
    temperature: float = 0.0


@dataclasses.dataclass(frozen=True)
class LLMResponse:
    text: str
    value: Any
    route_id: str
    provider: str
    model: str
    latency_ms: int
    attempts: int
    fallback_reason: Optional[str] = None


def _classify(exc: Exception) -> tuple[str, bool]:
    """Return (reason, transient?) for a provider-call exception."""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in (401, 402, 403):
            return (f"auth_{status}", False)
        if status == 429:
            return ("429", True)
        if status >= 500:
            return (f"http_{status}", True)
        return (f"http_{status}", False)
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return ("network", True)
    return (f"error_{type(exc).__name__}", False)


def _validate(req: LLMRequest, text: str) -> Any:
    """Apply the request validator (if any). Raise == route failure."""
    if req.validator is None:
        return text
    return req.validator(text)


def _try_route(
    req: LLMRequest, route: registry.Route, api_key: str, base_url: str,
) -> tuple[adapters.AdapterResult, int]:
    """Call one route. Returns the complete adapter result and latency.

    Raises the last exception if all attempts fail; the caller advances routes.
    """
    last: Optional[Exception] = None
    for attempt in range(1, _MAX_ATTEMPTS_PER_ROUTE + 1):
        start = time.monotonic()
        try:
            result = adapters.call(route, req, api_key, base_url)
            latency_ms = int((time.monotonic() - start) * 1000)
            return result, latency_ms
        except Exception as exc:  # noqa: BLE001 — classified below
            last = exc
            reason, transient = _classify(exc)
            logger.warning("route %s attempt %d failed: %s", route.id, attempt, reason)
            if not transient or attempt == _MAX_ATTEMPTS_PER_ROUTE:
                raise
            time.sleep(1.0 * attempt)
    assert last is not None
    raise last


def _candidate_routes(
    req: LLMRequest, resolved: list[registry.Route], estimated_tokens: int | None = None,
) -> list[registry.Route]:
    """Certified routes ordered by quality, with 90%-reserve routes at the tail."""
    certified = certify.certified_route_ids(req.purpose)
    pool = [r for r in resolved if r.id in certified]
    ordered = registry.ordered_for_purpose(pool, req.purpose)
    if req.pinned_model:
        pinned = [r for r in ordered if r.model == req.pinned_model]
        rest = [r for r in ordered if r.model != req.pinned_model]
        ordered = pinned + rest
    if estimated_tokens is None:
        return ordered
    normal: list[registry.Route] = []
    reserve: list[registry.Route] = []
    for route in ordered:
        state = quota.availability(route, estimated_tokens)
        if not state.usable:
            continue
        (reserve if state.advisory else normal).append(route)
    return normal + reserve


def _ladder_routes(
    req: LLMRequest, env: dict[str, str]
) -> list[tuple[registry.Route, str, str]]:
    """Curated candidates (health-ranked) with a resolvable key, as
    (route, api_key, base_url) triples ready for the shared attempt loop.

    Capped at ladder.MAX_ATTEMPTS_PER_REQUEST so a bad day across many dead
    proxies can't stack timeouts before the catalog/tail get a turn.
    Never raises — a broken ladder must not take routing down.
    """
    out: list[tuple[registry.Route, str, str]] = []
    try:
        for candidate in ladder.ordered(req.purpose, db_path=quota.DEFAULT_DB_PATH):
            if len(out) >= ladder.MAX_ATTEMPTS_PER_REQUEST:
                break
            cfg = ladder.gateway_config(candidate.gateway, env)
            api_key = (env.get(cfg["env_key"]) or "").strip()
            if not api_key:
                continue
            out.append((ladder.to_route(candidate), api_key, cfg["base_url"]))
    except Exception:  # noqa: BLE001
        logger.exception("ladder unavailable; continuing without it")
        return []
    return out


# --- Forced-route mode (certification) --------------------------------------
# When certifying a model we must run the REAL code paths (parse_message,
# answer_question, parse_job) but pin every router call to one route, ignoring
# the certified pool and the legacy tail — so a failure is that model's failure,
# not a silent fall-through to another provider. A ContextVar prevents one
# concurrent certification task from pinning unrelated traffic.

_FORCED_ROUTE: contextvars.ContextVar[Optional[registry.Route]] = contextvars.ContextVar(
    "llm_forced_route", default=None,
)


class force_route:
    """Context manager: pin every ``complete()`` call to one route.

    Used only by :mod:`llm.certify`. Inside the block the certified-pool filter
    and the legacy tail are bypassed; a call that the route can't satisfy raises
    ``AllRoutesExhausted`` (or ``RouterUnavailable`` if the route has no key).
    """

    def __init__(self, route: registry.Route) -> None:
        self.route = route
        self._token: contextvars.Token | None = None

    def __enter__(self) -> "force_route":
        self._token = _FORCED_ROUTE.set(self.route)
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._token is not None:
            _FORCED_ROUTE.reset(self._token)


def _complete_forced(req: LLMRequest, route: registry.Route) -> LLMResponse:
    env = env_loader.load()
    api_key = env.get(route.env_key, "")
    if not api_key:
        raise RouterUnavailable(f"no key for {route.id}")
    base_url = registry.resolve_base_url(route, env)
    estimated = quota.estimate_tokens(req.messages, req.max_output_tokens)
    try:
        reservation = quota.reserve(route, req.purpose, estimated)
    except sqlite3.OperationalError as exc:
        raise RouterUnavailable(f"quota db locked for {route.id}: {exc}") from exc
    if reservation is None:
        raise AllRoutesExhausted([(route.id, "quota_or_cooldown")])
    try:
        result, latency_ms = _try_route(req, route, api_key, base_url)
    except Exception as exc:  # noqa: BLE001
        reason, _ = _classify(exc)
        headers = dict(exc.response.headers) if isinstance(exc, httpx.HTTPStatusError) else {}
        quota.reconcile(reservation, route, success=False, rate_headers=headers, reason=reason)
        quota.record_route_result(
            route.id, success=False, reason=reason,
            retry_after=headers.get("retry-after"),
        )
        raise AllRoutesExhausted([(route.id, reason)])
    try:
        value = _validate(req, result.text)
    except Exception:  # noqa: BLE001
        quota.reconcile(
            reservation, route, success=True, prompt_tokens=result.usage_prompt,
            completion_tokens=result.usage_completion, rate_headers=result.rate_headers,
            reason="validation_failed",
        )
        quota.record_route_result(route.id, success=False, reason="validation_failed")
        raise AllRoutesExhausted([(route.id, "validation_failed")])
    quota.reconcile(
        reservation, route, success=True, prompt_tokens=result.usage_prompt,
        completion_tokens=result.usage_completion, rate_headers=result.rate_headers,
    )
    quota.record_route_result(route.id, success=True)
    return LLMResponse(
        text=result.text, value=value, route_id=route.id, provider=route.provider,
        model=route.model, latency_ms=latency_ms, attempts=1, fallback_reason=None,
    )


def complete(req: LLMRequest, *, _certify_route: Optional[registry.Route] = None) -> LLMResponse:
    """Route one request to the best available provider. See module docstring.

    ``_certify_route`` is internal: the certifier passes a specific route to
    exercise exactly that provider, bypassing the certified-pool filter and the
    legacy tail. The ``force_route`` context manager does the same for calls that
    go through the real parse/answer code paths.
    """
    route = _certify_route or _FORCED_ROUTE.get()
    if route is not None:
        return _complete_forced(req, route)

    env = env_loader.load()

    resolved = registry.resolve_routes(env)
    legacy_ok = _legacy_configured()
    ladder_routes = _ladder_routes(req, env)

    if not resolved and not legacy_ok and not ladder_routes:
        # Router genuinely cannot run — caller uses its own legacy path.
        raise RouterUnavailable("no resolvable routes and no gateway keys configured")

    attempts_log: list[tuple[str, str]] = []
    estimated = quota.estimate_tokens(req.messages, req.max_output_tokens)

    # 1. Curated ladder (health-ranked), capped per request.
    candidates: list[tuple[registry.Route, str, str]] = list(ladder_routes)
    # 2. Certified catalog routes.
    for route in _candidate_routes(req, resolved, estimated):
        api_key = env.get(route.env_key, "")
        if not api_key:
            attempts_log.append((route.id, "missing_key"))
            continue
        candidates.append((route, api_key, registry.resolve_base_url(route, env)))

    for route, api_key, base_url in candidates:
        try:
            reservation = quota.reserve(route, req.purpose, estimated)
        except sqlite3.OperationalError as exc:
            logger.warning("quota.reserve db locked for %s: %s", route.id, exc)
            attempts_log.append((route.id, "quota_db_locked"))
            continue
        if reservation is None:
            attempts_log.append((route.id, "quota_or_cooldown"))
            continue
        try:
            result, latency_ms = _try_route(req, route, api_key, base_url)
        except Exception as exc:  # noqa: BLE001 — provider/transport failure
            reason, _ = _classify(exc)
            headers = dict(exc.response.headers) if isinstance(exc, httpx.HTTPStatusError) else {}
            quota.reconcile(
                reservation, route, success=False, rate_headers=headers, reason=reason,
            )
            quota.record_route_result(
                route.id, success=False, reason=reason,
                retry_after=headers.get("retry-after"),
            )
            attempts_log.append((route.id, reason))
            logger.info("route %s failed (%s); advancing", route.id, reason)
            continue
        try:
            value = _validate(req, result.text)
        except Exception:  # noqa: BLE001 — validator rejected the output
            quota.reconcile(
                reservation, route, success=True, prompt_tokens=result.usage_prompt,
                completion_tokens=result.usage_completion, rate_headers=result.rate_headers,
                reason="validation_failed",
            )
            quota.record_route_result(route.id, success=False, reason="validation_failed")
            attempts_log.append((route.id, "validation_failed"))
            logger.info("route %s output rejected by validator; advancing", route.id)
            continue
        quota.reconcile(
            reservation, route, success=True, prompt_tokens=result.usage_prompt,
            completion_tokens=result.usage_completion, rate_headers=result.rate_headers,
        )
        quota.record_route_result(route.id, success=True)
        return LLMResponse(
            text=result.text, value=value, route_id=route.id, provider=route.provider,
            model=route.model, latency_ms=latency_ms, attempts=len(attempts_log) + 1,
            fallback_reason=_summarise(attempts_log) or None,
        )

    # 3. Legacy env tail (models ladder-ranked inside _legacy_call's loop).
    if legacy_ok:
        try:
            text, latency_ms = _legacy_call(req)
            value = _validate(req, text)
            quota.record_unmetered_request(
                "eaon:legacy", req.purpose, estimated, success=True,
            )
            try:
                model_name = settings.llm_model()
            except Exception:
                model_name = "eaon-legacy"
            return LLMResponse(
                text=text, value=value, route_id="eaon:legacy", provider="legacy",
                model=model_name, latency_ms=latency_ms, attempts=len(attempts_log) + 1,
                fallback_reason=_summarise(attempts_log) or None,
            )
        except Exception as exc:  # noqa: BLE001
            reason, _ = _classify(exc)
            quota.record_unmetered_request(
                "eaon:legacy", req.purpose, estimated, success=False, reason=reason,
            )
            attempts_log.append(("eaon:legacy", reason))

    raise AllRoutesExhausted(attempts_log)


def stream_complete(req: LLMRequest) -> Iterator[str]:
    """Yield text deltas for agent chat streaming.

    Same route selection as ``complete`` (Eaon first, then certified pool), but
    stops at the first route that streams successfully. Non-streaming adapters
    yield one full chunk. Tool-call / structured paths should keep using
    ``complete`` — this is for user-visible final replies only.
    """
    route = _FORCED_ROUTE.get()
    if route is not None:
        env = env_loader.load()
        api_key = env.get(route.env_key, "")
        if not api_key:
            raise RouterUnavailable(f"no key for {route.id}")
        base_url = registry.resolve_base_url(route, env)
        estimated = quota.estimate_tokens(req.messages, req.max_output_tokens)
        reservation = quota.reserve(route, req.purpose, estimated)
        if reservation is None:
            raise AllRoutesExhausted([(route.id, "quota_or_cooldown")])
        try:
            parts: list[str] = []
            for delta in adapters.stream_call(route, req, api_key, base_url):
                parts.append(delta)
            text = "".join(parts)
            try:
                _validate(req, text)
            except Exception:
                quota.reconcile(reservation, route, success=True, reason="validation_failed")
                quota.record_route_result(route.id, success=False, reason="validation_failed")
                raise AllRoutesExhausted([(route.id, "validation_failed")])
            for delta in parts:
                yield delta
            quota.reconcile(reservation, route, success=True)
            quota.record_route_result(route.id, success=True)
        except AllRoutesExhausted:
            raise
        except Exception as exc:  # noqa: BLE001
            reason, _ = _classify(exc)
            headers = dict(exc.response.headers) if isinstance(exc, httpx.HTTPStatusError) else {}
            quota.reconcile(reservation, route, success=False, rate_headers=headers, reason=reason)
            quota.record_route_result(route.id, success=False, reason=reason)
            raise AllRoutesExhausted([(route.id, reason)]) from exc
        return

    env = env_loader.load()
    resolved = registry.resolve_routes(env)
    legacy_ok = _legacy_configured()
    ladder_routes = _ladder_routes(req, env)
    if not resolved and not legacy_ok and not ladder_routes:
        raise RouterUnavailable("no resolvable routes and no gateway keys configured")

    attempts_log: list[tuple[str, str]] = []
    estimated = quota.estimate_tokens(req.messages, req.max_output_tokens)

    candidates: list[tuple[registry.Route, str, str]] = list(ladder_routes)
    for route in _candidate_routes(req, resolved, estimated):
        api_key = env.get(route.env_key, "")
        if not api_key:
            attempts_log.append((route.id, "missing_key"))
            continue
        candidates.append((route, api_key, registry.resolve_base_url(route, env)))

    for route, api_key, base_url in candidates:
        try:
            reservation = quota.reserve(route, req.purpose, estimated)
        except sqlite3.OperationalError as exc:
            attempts_log.append((route.id, "quota_db_locked"))
            continue
        if reservation is None:
            attempts_log.append((route.id, "quota_or_cooldown"))
            continue
        try:
            parts = []
            for delta in adapters.stream_call(route, req, api_key, base_url):
                parts.append(delta)
            text = "".join(parts)
            try:
                _validate(req, text)
            except Exception:
                quota.reconcile(reservation, route, success=True, reason="validation_failed")
                quota.record_route_result(route.id, success=False, reason="validation_failed")
                attempts_log.append((route.id, "validation_failed"))
                continue
            for delta in parts:
                yield delta
            quota.reconcile(reservation, route, success=True)
            quota.record_route_result(route.id, success=True)
            return
        except Exception as exc:  # noqa: BLE001
            reason, _ = _classify(exc)
            headers = dict(exc.response.headers) if isinstance(exc, httpx.HTTPStatusError) else {}
            quota.reconcile(reservation, route, success=False, rate_headers=headers, reason=reason)
            quota.record_route_result(
                route.id, success=False, reason=reason,
                retry_after=headers.get("retry-after"),
            )
            attempts_log.append((route.id, reason))
            logger.info("stream route %s failed (%s); advancing", route.id, reason)
            continue

    # Legacy env stream is the final tail.
    if legacy_ok:
        try:
            for delta in _legacy_stream(req):
                yield delta
            quota.record_unmetered_request(
                "eaon:legacy", req.purpose, estimated, success=True,
            )
            return
        except Exception as exc:  # noqa: BLE001
            reason, _ = _classify(exc)
            quota.record_unmetered_request(
                "eaon:legacy", req.purpose, estimated, success=False, reason=reason,
            )
            attempts_log.append(("eaon:legacy", reason))

    raise AllRoutesExhausted(attempts_log)


def _legacy_stream(req: LLMRequest) -> Iterator[str]:
    """Stream from the Eaon OpenAI-compatible gateway; fall back to full call."""
    provider = settings.llm_provider()
    if provider == "anthropic":
        text, _latency = _legacy_call(req)
        if text:
            yield text
        return

    timeout = adapters._timeout_for(req.purpose)
    models = _legacy_models()
    if req.pinned_model and req.pinned_model not in models:
        models = [req.pinned_model, *models]
    last: Optional[Exception] = None
    for model in models:
        try:
            url = settings.llm_base_url().rstrip("/") + "/chat/completions"
            payload = {
                "model": model,
                "messages": req.messages,
                "temperature": req.temperature,
                "max_tokens": req.max_output_tokens,
                "stream": True,
            }
            with httpx.Client(timeout=timeout) as client:
                with client.stream(
                    "POST", url, json=payload, headers=_legacy_headers_openai(),
                ) as resp:
                    resp.raise_for_status()
                    for line in resp.iter_lines():
                        if not line:
                            continue
                        delta = adapters._openai_delta_from_sse_line(line)
                        if delta:
                            yield delta
            return
        except Exception as exc:  # noqa: BLE001
            last = exc
            reason, _ = _classify(exc)
            logger.warning("legacy stream model %s failed: %s", model, reason)
            continue
    # Do NOT fall through to _legacy_call here — that double-burns the full
    # model list (stream fail × N + complete × N). Callers already fall back
    # to complete() once after stream_complete raises.
    if last is not None:
        raise last
    raise RuntimeError("legacy stream produced no models")


def _summarise(attempts: list[tuple[str, str]]) -> str:
    return "; ".join(f"{rid}:{why}" for rid, why in attempts)


# --- Legacy Eaon primary ----------------------------------------------------
# A self-contained reproduction of the pre-router single-gateway behaviour
# (primary model + fallback models, provider-aware) so the router never depends
# on the call-site modules. It is tried before every certified provider. Its
# activity is tracked, but it has no locally mapped quota.

def _legacy_configured() -> bool:
    try:
        return bool(settings.llm_api_key() and settings.llm_model())
    except Exception:
        return False


def _legacy_models() -> list[str]:
    """Legacy tail model order: ladder-ranked eaon models first, env list after.

    The env variable order no longer decides anything by itself — a model has
    to earn its position through probes/traffic. Env entries not in the curated
    ladder still work, appended as a neutral tail.
    """
    env_models: list[str] = []
    try:
        for m in [settings.llm_model(), *settings.llm_fallback_models()]:
            if m and m not in env_models:
                env_models.append(m)
    except Exception:  # noqa: BLE001
        pass
    try:
        ranked = [c.model for c in ladder.ordered("domain") if c.gateway == "eaon"]
    except Exception:  # noqa: BLE001
        ranked = []
    merged: list[str] = []
    for m in ranked + env_models:
        if m and m not in merged:
            merged.append(m)
    return merged


def _legacy_call(req: LLMRequest) -> tuple[str, int]:
    """Call the legacy eaon gateway with model-fallback. Returns (text, latency_ms)."""
    provider = settings.llm_provider()
    timeout = adapters._timeout_for(req.purpose)
    models = _legacy_models()
    if req.pinned_model and req.pinned_model not in models:
        models = [req.pinned_model, *models]
    last: Optional[Exception] = None
    with httpx.Client(timeout=timeout) as client:
        for model in models:
            start = time.monotonic()
            try:
                if provider == "anthropic":
                    text = _legacy_anthropic(client, model, req)
                else:
                    text = _legacy_openai(client, model, req)
                return text, int((time.monotonic() - start) * 1000)
            except Exception as exc:  # noqa: BLE001
                last = exc
                reason, _ = _classify(exc)
                logger.warning("legacy model %s failed: %s", model, reason)
                continue
    assert last is not None
    raise last


def _legacy_headers_openai() -> dict:
    return {
        "Authorization": f"Bearer {settings.llm_api_key()}",
        "Content-Type": "application/json",
        "User-Agent": _LEGACY_USER_AGENT,
    }


def _legacy_openai(client: httpx.Client, model: str, req: LLMRequest) -> str:
    url = settings.llm_base_url().rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": req.messages,
        "temperature": req.temperature,
        "max_tokens": req.max_output_tokens,
    }
    resp = client.post(url, json=payload, headers=_legacy_headers_openai())
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def _legacy_anthropic(client: httpx.Client, model: str, req: LLMRequest) -> str:
    url = settings.llm_base_url().rstrip("/") + "/messages"
    if "api.openai.com" in url:
        url = "https://api.anthropic.com/v1/messages"
    system_text = "\n\n".join(m["content"] for m in req.messages if m["role"] == "system")
    convo = [m for m in req.messages if m["role"] != "system"]
    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": req.max_output_tokens,
        "messages": convo,
    }
    if system_text:
        payload["system"] = system_text
    headers = {
        "x-api-key": settings.llm_api_key(),
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
        "User-Agent": _LEGACY_USER_AGENT,
    }
    resp = client.post(url, json=payload, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    return "".join(
        block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
    )
