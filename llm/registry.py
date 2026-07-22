"""Provider/route catalog and resolution.

Each ``Route`` is one (provider, model) endpoint the router can call. OpenAI-
compatible, Google native generateContent, and Cohere v2 adapters are wired.
Cerebras is intentionally absent because its key currently returns 402.

Quota descriptors seed the durable ledger. Provider headers replace estimates
with authoritative remaining/reset values whenever they are available.

``resolve_routes`` yields only enabled routes whose API key is present in
``ai.env``. The legacy eaon gateway is deliberately NOT in this catalog: it is
the emergency tail reached through ``config.settings.llm_*`` after every
catalog route, so it must never participate in normal selection.
"""

from __future__ import annotations

import dataclasses
from typing import Literal, Optional

from . import env_loader

AdapterKind = Literal["openai", "google", "cohere"]
AuthKind = Literal["bearer", "x-goog-api-key"]
Purpose = Literal["intent", "domain", "sql"]


@dataclasses.dataclass(frozen=True)
class Quota:
    """One tracked quota for a route (informational until the quota phase)."""

    metric: Literal["requests", "tokens"]
    scope: Literal["rpm", "rpd", "monthly", "tpm"]
    limit: int
    switch_at: float  # fraction (0.90) or absolute count (e.g. 18 RPM)
    confidence: Literal["exact", "estimated"]
    header_limit: Optional[str] = None
    header_remaining: Optional[str] = None
    header_reset: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class Route:
    id: str
    provider: str
    adapter: AdapterKind
    base_url: str
    model: str
    auth: AuthKind
    env_key: str
    quotas: tuple[Quota, ...] = ()
    # Lower rank = preferred. Per-purpose overrides live in QUALITY_BY_PURPOSE.
    quality_rank: int = 100
    enabled: bool = True

    def quality(self, purpose: str) -> int:
        return QUALITY_BY_PURPOSE.get(purpose, {}).get(self.id, self.quality_rank)


# --- Static catalog ---------------------------------------------------------

ROUTE_CATALOG: tuple[Route, ...] = (
    Route(
        id="groq:gpt-oss-120b",
        provider="groq",
        adapter="openai",
        base_url="https://api.groq.com/openai/v1",
        model="openai/gpt-oss-120b",
        auth="bearer",
        env_key="GROQ_API_KEY",
        quotas=(
            Quota("requests", "rpm", 30, 0.90, "estimated",
                  "x-ratelimit-limit-requests", "x-ratelimit-remaining-requests",
                  "x-ratelimit-reset-requests"),
            Quota("tokens", "tpm", 6000, 0.90, "estimated",
                  "x-ratelimit-limit-tokens", "x-ratelimit-remaining-tokens",
                  "x-ratelimit-reset-tokens"),
            Quota("requests", "rpd", 1000, 0.90, "estimated"),
        ),
        quality_rank=20,
    ),
    Route(
        id="openrouter:nemotron-3-ultra",
        provider="openrouter",
        adapter="openai",
        base_url="https://openrouter.ai/api/v1",
        model="nvidia/nemotron-3-ultra-550b-a55b:free",
        auth="bearer",
        env_key="OPENROUTER_API_KEY",
        quotas=(
            Quota("requests", "rpm", 20, 18, "estimated"),
            Quota("requests", "rpd", 50, 45, "estimated"),
        ),
        quality_rank=10,
    ),
    Route(
        id="cloudflare:glm-4.7-flash",
        provider="cloudflare",
        adapter="openai",
        base_url="https://api.cloudflare.com/client/v4/accounts/${CLOUDFLARE_ACCOUNT_ID}/ai/v1",
        model="@cf/zai-org/glm-4.7-flash",
        auth="bearer",
        env_key="CLOUDFLARE_API_KEY",
        quotas=(
            # Billing is in neurons, which the API does not return per request.
            # This conservative request proxy is labelled estimated explicitly.
            Quota("requests", "rpd", 1000, 0.90, "estimated"),
        ),
        quality_rank=30,
    ),
    Route(
        id="ollama:gpt-oss-20b",
        provider="ollama",
        adapter="openai",
        base_url="https://ollama.com/v1",
        model="gpt-oss:20b",
        auth="bearer",
        env_key="OLLAMA_API_KEY",
        quotas=(
            # Ollama publishes no numeric quota (GPU-time based); conservative
            # request estimates for the proven level-1 gpt-oss:20b endpoint.
            Quota("requests", "rpd", 20, 0.90, "estimated"),
            Quota("requests", "monthly", 100, 0.90, "estimated"),
        ),
        quality_rank=25,
    ),
    # --- Google: native generateContent API (adapter="google") ---
    Route(
        id="google:gemini-3.5-flash",
        provider="google",
        adapter="google",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        model="gemini-3.5-flash",
        auth="x-goog-api-key",
        env_key="GOOGLE_API_KEY",
        quotas=(
            Quota("requests", "rpm", 15, 0.90, "estimated"),
            Quota("requests", "rpd", 1000, 0.90, "estimated"),
            Quota("tokens", "tpm", 250000, 0.90, "estimated"),
        ),
        quality_rank=9,
    ),
    # --- Cohere v2 chat ---
    Route(
        id="cohere:command-a-plus",
        provider="cohere",
        adapter="cohere",
        base_url="https://api.cohere.com/v2",
        model="command-a-plus-05-2026",
        auth="bearer",
        env_key="COHERE_API_KEY",
        quotas=(
            Quota("requests", "rpm", 20, 18, "estimated"),
            Quota("requests", "monthly", 1000, 900, "estimated"),
        ),
        quality_rank=12,
        enabled=True,
    ),
)


# Quality-first ordering. Lower rank = preferred. The roster leads with frontier
# models everywhere; the split between purposes only reshuffles within the
# frontier tier (SQL rewards big context + reasoning; intent/domain reward fast
# structured frontier responders). Unlisted (route, purpose) pairs fall back to
# Route.quality_rank.
QUALITY_BY_PURPOSE: dict[str, dict[str, int]] = {
    "sql": {
        "openrouter:nemotron-3-ultra": 1,   # 550B frontier, 1M ctx — best for deep SQL drilling
        "ollama:gpt-oss-20b": 7,             # proven available; conservative fallback
        "google:gemini-3.5-flash": 3,        # 1M ctx workhorse
        "groq:gpt-oss-120b": 4,              # fast frontier reasoning
        "cloudflare:glm-4.7-flash": 8,       # flash backup
    },
    "intent": {
        "ollama:gpt-oss-20b": 6,
        "openrouter:nemotron-3-ultra": 2,
        "google:gemini-3.5-flash": 3,
        "groq:gpt-oss-120b": 4,              # very fast — strong practical default
        "cloudflare:glm-4.7-flash": 8,
    },
    "domain": {
        "ollama:gpt-oss-20b": 6,
        "openrouter:nemotron-3-ultra": 2,
        "google:gemini-3.5-flash": 3,
        "groq:gpt-oss-120b": 4,
        "cloudflare:glm-4.7-flash": 8,
    },
}


def resolve_base_url(route: Route, env: dict[str, str]) -> str:
    """Expand ``${VAR}`` placeholders in a base URL from ai.env (e.g. Cloudflare account id)."""
    url = route.base_url
    if "${CLOUDFLARE_ACCOUNT_ID}" in url:
        acct = env.get("CLOUDFLARE_ACCOUNT_ID", "")
        url = url.replace("${CLOUDFLARE_ACCOUNT_ID}", acct)
    return url


def resolve_routes(env: Optional[dict[str, str]] = None) -> list[Route]:
    """Return enabled catalog routes whose API key is present in ai.env.

    Order is the catalog order; the router re-sorts by per-purpose quality.
    """
    if env is None:
        env = env_loader.load()
    out: list[Route] = []
    for route in ROUTE_CATALOG:
        if not route.enabled:
            continue
        key = env.get(route.env_key)
        if not key or not key.strip():
            continue
        if "${CLOUDFLARE_ACCOUNT_ID}" in route.base_url and not env.get("CLOUDFLARE_ACCOUNT_ID"):
            # Can't build a valid URL without the account id.
            continue
        out.append(route)
    return out


def ordered_for_purpose(routes: list[Route], purpose: str) -> list[Route]:
    """Sort routes by quality for a purpose (lower rank first, stable by id)."""
    return sorted(routes, key=lambda r: (r.quality(purpose), r.id))
