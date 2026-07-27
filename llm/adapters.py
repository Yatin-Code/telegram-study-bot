"""Provider adapters + the single HTTP seam.

OpenAI-compatible providers share one adapter; Google and Cohere use their
native request/response schemas.

Every provider call funnels through ``_http_post`` — the one function tests
monkeypatch to simulate 429/5xx/timeout/malformed across all providers, so we no
longer patch per-module transport helpers.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator
from typing import Any, Optional, Protocol

import httpx

from .registry import Route

USER_AGENT = "study-bot/1.0"  # eaon sits behind Cloudflare, which 1010s a blank UA.

# Per-purpose timeouts mirror the existing module constants (sql was 45s, the
# parsers 30s) so behaviour is unchanged when routed.
_TIMEOUTS = {"sql": 45.0, "intent": 30.0, "domain": 30.0}
_DEFAULT_TIMEOUT = 30.0


def _timeout_for(purpose: str) -> float:
    return _TIMEOUTS.get(purpose, _DEFAULT_TIMEOUT)


@dataclasses.dataclass
class AdapterResult:
    text: str
    usage_prompt: Optional[int] = None
    usage_completion: Optional[int] = None
    rate_headers: dict[str, str] = dataclasses.field(default_factory=dict)
    http_status: int = 200


class Adapter(Protocol):
    def build(self, route: Route, req: Any, api_key: str, base_url: str) -> tuple[str, dict, dict]:
        """Return (url, json_body, headers) shaped for this provider."""

    def parse(self, resp: httpx.Response) -> AdapterResult:
        """Extract text (+ usage/rate headers) from this provider's response."""


def _http_post(url: str, *, headers: dict, json: dict, timeout: float) -> httpx.Response:
    """The single transport seam. Tests monkeypatch this.

    Kept intentionally thin: build the client, POST, return the raw response.
    Retry/backoff and status interpretation live in the router, not here.
    """
    with httpx.Client(timeout=timeout) as client:
        return client.post(url, headers=headers, json=json)


class OpenAICompatAdapter:
    """OpenAI ``/chat/completions`` shape (Groq, OpenRouter, Cloudflare, Ollama, eaon)."""

    def build(self, route: Route, req: Any, api_key: str, base_url: str) -> tuple[str, dict, dict]:
        url = base_url.rstrip("/") + "/chat/completions"
        body = {
            "model": route.model,
            "messages": req.messages,
            "temperature": req.temperature,
            "max_tokens": req.max_output_tokens,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        return url, body, headers

    def parse(self, resp: httpx.Response) -> AdapterResult:
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
        usage = data.get("usage") or {}
        rate = {k: v for k, v in resp.headers.items() if k.lower().startswith("x-ratelimit-")}
        return AdapterResult(
            text=text,
            usage_prompt=usage.get("prompt_tokens"),
            usage_completion=usage.get("completion_tokens"),
            rate_headers=rate,
            http_status=resp.status_code,
        )


class GoogleNativeAdapter:
    """Google Gemini native ``generateContent`` API.

    Differs from the OpenAI shape in three ways handled here:
    - endpoint carries the model: ``{base}/models/{model}:generateContent``;
    - auth is the ``x-goog-api-key`` header (not a Bearer token);
    - messages split into a separate ``system_instruction`` plus ``contents``
      whose roles are ``user``/``model`` (assistant → ``model``).
    Google exposes no live rate-limit headers, so the route stays estimated.
    """

    def build(self, route: Route, req: Any, api_key: str, base_url: str) -> tuple[str, dict, dict]:
        url = f"{base_url.rstrip('/')}/models/{route.model}:generateContent"
        system_parts = [
            m["content"] for m in req.messages if m.get("role") == "system" and m.get("content")
        ]
        contents = []
        for m in req.messages:
            role = m.get("role")
            if role == "system":
                continue
            contents.append({
                "role": "model" if role == "assistant" else "user",
                "parts": [{"text": m.get("content", "")}],
            })
        body: dict = {
            "contents": contents,
            "generationConfig": {
                "temperature": req.temperature,
                "maxOutputTokens": req.max_output_tokens,
            },
        }
        if system_parts:
            body["system_instruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}
        headers = {
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        return url, body, headers

    def parse(self, resp: httpx.Response) -> AdapterResult:
        data = resp.json()
        candidates = data.get("candidates") or []
        text = ""
        if candidates:
            parts = (candidates[0].get("content") or {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts)
        usage = data.get("usageMetadata") or {}
        return AdapterResult(
            text=text,
            usage_prompt=usage.get("promptTokenCount"),
            usage_completion=usage.get("candidatesTokenCount"),
            rate_headers={},  # Google exposes no live rate-limit headers
            http_status=resp.status_code,
        )


class CohereV2Adapter:
    """Cohere v2 Chat API adapter."""

    def build(self, route: Route, req: Any, api_key: str, base_url: str) -> tuple[str, dict, dict]:
        return base_url.rstrip("/") + "/chat", {
            "model": route.model,
            "messages": req.messages,
            "temperature": req.temperature,
            "max_tokens": req.max_output_tokens,
        }, {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }

    def parse(self, resp: httpx.Response) -> AdapterResult:
        data = resp.json()
        parts = (data.get("message") or {}).get("content") or []
        text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
        usage = data.get("usage") or {}
        billed = usage.get("billed_units") or {}
        rate = {
            k: v for k, v in resp.headers.items()
            if k.lower().startswith(("x-ratelimit-", "x-trial-"))
        }
        return AdapterResult(
            text=text,
            usage_prompt=billed.get("input_tokens"),
            usage_completion=billed.get("output_tokens"),
            rate_headers=rate,
            http_status=resp.status_code,
        )


# Registry of adapter kind -> singleton instance.
_ADAPTERS: dict[str, Adapter] = {
    "openai": OpenAICompatAdapter(),
    "google": GoogleNativeAdapter(),
    "cohere": CohereV2Adapter(),
}


def adapter_for(route: Route) -> Adapter:
    try:
        return _ADAPTERS[route.adapter]
    except KeyError as exc:
        raise NotImplementedError(f"no adapter for kind {route.adapter!r}") from exc


def call(route: Route, req: Any, api_key: str, base_url: str) -> AdapterResult:
    """Build + POST + parse one provider call. Raises httpx errors on failure."""
    adapter = adapter_for(route)
    url, body, headers = adapter.build(route, req, api_key, base_url)
    resp = _http_post(url, headers=headers, json=body, timeout=_timeout_for(req.purpose))
    resp.raise_for_status()
    return adapter.parse(resp)


def _openai_delta_from_sse_line(line: str) -> str | None:
    """Parse one SSE line from an OpenAI-compatible stream. Return delta text or None."""
    line = line.strip()
    if not line.startswith("data:"):
        return None
    data = line[5:].strip()
    if not data or data == "[DONE]":
        return None
    try:
        chunk = json.loads(data)
    except Exception:
        return None
    choices = chunk.get("choices") or []
    if not choices:
        return None
    delta = choices[0].get("delta") or {}
    content = delta.get("content")
    return content if isinstance(content, str) and content else None


def stream_call(route: Route, req: Any, api_key: str, base_url: str) -> Iterator[str]:
    """Yield text deltas for one provider call.

    OpenAI-compatible routes use SSE ``stream=True``. Other adapters fall back
    to a full non-streaming call and yield the complete text once.
    """
    adapter = adapter_for(route)
    url, body, headers = adapter.build(route, req, api_key, base_url)
    timeout = _timeout_for(req.purpose)

    if route.adapter == "openai":
        stream_body = dict(body)
        stream_body["stream"] = True
        with httpx.Client(timeout=timeout) as client:
            with client.stream("POST", url, headers=headers, json=stream_body) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line:
                        continue
                    delta = _openai_delta_from_sse_line(line)
                    if delta:
                        yield delta
        return

    # Non-streaming providers: one-shot yield.
    result = call(route, req, api_key, base_url)
    if result.text:
        yield result.text
