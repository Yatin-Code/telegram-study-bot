"""Rolling health probes for the curated ladder.

Certification (llm.certify) says a model is CAPABLE of our workloads; this
module says whether it's ALIVE RIGHT NOW. Every tick (wired into the bot's
job_queue at ~4.5 min) probes a small batch:

  1. TOP candidates whose probe data went stale (the models actually serving
     traffic must never rely on stale aliveness), then
  2. never-probed candidates,
  3. stalest old probes,
  4. failed models past cooldown (recovery chance).

Per candidate two tiny probes (≤16 output tokens total):
  - chat ping: "reply exactly OK" — catches dead endpoints & junk output;
  - tool-call ping: must emit parseable {"tool": ..., "arguments": {...}} JSON —
    catches models that chat fine but lack the discipline the agent loop needs.

Results land in ladder.llm_model_health and feed ladder.ordered(). All
functions are synchronous (httpx is sync in this codebase); the bot wraps
tick() in asyncio.to_thread.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

from . import adapters, env_loader, ladder

logger = logging.getLogger(__name__)

# How many candidates one tick probes; full list cycles every ~30-45 min.
BATCH_SIZE = 4
# A probe older than this makes a *top-ranked* candidate eligible for re-check
# (winners are kept warm; the loop "first checks those models that work").
TOP_FRESH_SECONDS = 600
# Any probe older than this is stale regardless of rank.
STALE_SECONDS = 3600
# After this many consecutive probe failures a candidate only gets a recovery
# probe once per this window (instead of burning tokens on a dead model).
RECOVERY_SECONDS = 1800

_CHAT_PROMPT = [{"role": "user", "content": "Reply with exactly: OK"}]
_TOOL_PROMPT = [
    {
        "role": "system",
        "content": (
            "You call tools by replying with EXACTLY one JSON object and nothing "
            'else: {"tool": "<name>", "arguments": {...}}. Available tool: '
            "get_context (no arguments) returns the user's study context."
        ),
    },
    {"role": "user", "content": "Get my current study context."},
]
_PROBE_MAX_TOKENS = 16


@dataclasses.dataclass
class _PingRequest:
    """Minimal request shape adapters.call needs (avoids importing router)."""

    messages: list[dict[str, str]]
    purpose: str = "intent"
    temperature: float = 0.0
    max_output_tokens: int = _PROBE_MAX_TOKENS


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _parse(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


def _ping(candidate: ladder.Candidate, messages: list[dict[str, str]], *,
          api_key: str, base_url: str, max_tokens: int = _PROBE_MAX_TOKENS
          ) -> tuple[str, float]:
    """One tiny completion against the candidate's gateway. Raises on failure."""
    route = ladder.to_route(candidate)
    req = _PingRequest(messages=messages, max_output_tokens=max_tokens)
    start = time.monotonic()
    result = adapters.call(route, req, api_key, base_url)
    latency_ms = (time.monotonic() - start) * 1000.0
    text = (result.text or "").strip()
    if not text:
        raise RuntimeError("empty response body")
    return text, latency_ms


def probe_candidate(
    candidate: ladder.Candidate, *, env: Optional[dict[str, str]] = None
) -> dict[str, Any]:
    """Run both pings against one candidate. Never raises."""
    api_key = ladder.api_key_for(candidate, env=env)
    if not api_key:
        return {"ok": False, "tool_ok": False, "latency_ms": None, "error": "missing_key"}
    base_url = ladder.gateway_config(candidate.gateway, env)["base_url"]
    # Some gateways (google thinking models) burn max_tokens on thoughts;
    # their candidates carry an enlarged probe budget.
    budget = candidate.probe_max_tokens
    try:
        chat_text, latency_ms = _ping(candidate, _CHAT_PROMPT, api_key=api_key,
                                      base_url=base_url, max_tokens=budget)
        if "OK" not in chat_text.upper():
            return {"ok": False, "tool_ok": False, "latency_ms": latency_ms,
                    "error": f"chat ping returned junk: {chat_text[:60]!r}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "tool_ok": False, "latency_ms": None,
                "error": f"{type(exc).__name__}: {exc}"[:200]}

    tool_ok = False
    tool_err: Optional[str] = None
    try:
        tool_text, _ = _ping(candidate, _TOOL_PROMPT, api_key=api_key,
                             base_url=base_url, max_tokens=budget)
        start = tool_text.find("{")
        data = json.loads(tool_text[start:]) if start != -1 else None
        tool_ok = isinstance(data, dict) and isinstance(data.get("tool"), str) and "arguments" in data
        if not tool_ok:
            tool_err = f"no tool JSON in reply: {tool_text[:60]!r}"
    except Exception as exc:  # noqa: BLE001
        tool_err = f"{type(exc).__name__}: {exc}"[:200]

    return {
        "ok": True,
        "tool_ok": tool_ok,
        "latency_ms": latency_ms,
        "error": tool_err,
    }


def _last_probe_age(row: Optional[dict[str, Any]], now: dt.datetime) -> float:
    """Seconds since the last probe; inf when never probed."""
    stamp = _parse(row.get("last_probe_at")) if row else None
    if stamp is None:
        return float("inf")
    return (now - stamp).total_seconds()


def pick_batch(
    purpose: str = "intent",
    *,
    limit: int = BATCH_SIZE,
    db_path: str | Path | None = None,
    now: Optional[dt.datetime] = None,
) -> list[ladder.Candidate]:
    """Choose which candidates this tick probes: warm tops, then unprobed,
    then stale, then recovery. Deterministic given the health table."""
    now = now or _utcnow()
    rows = ladder.health_rows(db_path)
    ranked = ladder.ordered(purpose, db_path=db_path, now=now)

    picks: list[ladder.Candidate] = []
    seen: set[str] = set()

    def take(candidate: ladder.Candidate) -> None:
        if candidate.id not in seen and len(picks) < limit:
            picks.append(candidate)
            seen.add(candidate.id)

    # 1. keep the current winners warm (top-3 stale > TOP_FRESH_SECONDS)
    for candidate in ranked[:3]:
        if _last_probe_age(rows.get(candidate.id), now) > TOP_FRESH_SECONDS:
            take(candidate)

    # 2. recovery: consecutive failures, but the recovery window passed
    #    (checked before long-tail discovery — a recovering winner matters
    #    more than probing the 20th never-tried model)
    for candidate in ranked:
        row = rows.get(candidate.id)
        if row and (row.get("probe_fail_streak") or 0) > 0:
            if _last_probe_age(row, now) > RECOVERY_SECONDS:
                take(candidate)

    # 3. never probed at all
    for candidate in ranked:
        row = rows.get(candidate.id)
        if row is None or row.get("last_probe_at") is None:
            take(candidate)

    # 4. general stale coverage (round-robin by oldest probe)
    stale_sorted = sorted(
        ranked,
        key=lambda c: _last_probe_age(rows.get(c.id), now),
        reverse=True,
    )
    for candidate in stale_sorted:
        if _last_probe_age(rows.get(candidate.id), now) > STALE_SECONDS:
            take(candidate)

    return picks


def tick(
    *,
    db_path: str | Path | None = None,
    env: Optional[dict[str, str]] = None,
    now: Optional[dt.datetime] = None,
) -> dict[str, Any]:
    """One probe cycle. Returns a summary (also logged). Never raises."""
    now = now or _utcnow()
    summary: dict[str, Any] = {"probed": [], "skipped": 0}
    try:
        batch = pick_batch(db_path=db_path, now=now)
    except Exception:
        logger.exception("health tick: batch selection failed")
        return {"probed": [], "error": "pick_batch failed"}

    for candidate in batch:
        result = probe_candidate(candidate, env=env)
        try:
            ladder.record_probe(
                candidate.id,
                ok=result["ok"],
                tool_ok=result["tool_ok"],
                latency_ms=result.get("latency_ms"),
                error=result.get("error"),
                db_path=db_path,
                now=now,
            )
        except Exception:
            logger.exception("health tick: failed to persist probe for %s", candidate.id)
        summary["probed"].append({
            "candidate": candidate.id,
            "ok": result["ok"],
            "tool_ok": result["tool_ok"],
            "latency_ms": result.get("latency_ms"),
            "error": result.get("error"),
        })
        logger.info(
            "probe %s: ok=%s tool_ok=%s latency=%sms err=%s",
            candidate.id, result["ok"], result["tool_ok"],
            int(result["latency_ms"] or 0), result.get("error"),
        )
    return summary
