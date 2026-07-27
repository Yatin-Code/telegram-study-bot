"""Quota-aware multi-provider LLM router.

Public surface used by call sites (sql_query_flow, intent_parser, domain_parser):

    from llm import router
    resp = router.complete(router.LLMRequest(messages=..., purpose="sql"))

or the re-exports below. Everything else in the package is internal.

The router is an *enhancement layer*: if it cannot run (no ai.env, no certified
routes, import failure) it raises ``RouterUnavailable`` and callers fall back to
their legacy single-provider path, so behaviour never regresses.
"""

from __future__ import annotations

from .errors import AllRoutesExhausted, RouterUnavailable


def __getattr__(name: str):
    """Lazy router re-exports; keeps ``python -m llm.certify`` deterministic."""
    if name in {"LLMRequest", "LLMResponse", "complete", "stream_complete"}:
        from . import router
        return getattr(router, name)
    raise AttributeError(name)

__all__ = [
    "complete",
    "stream_complete",
    "LLMRequest",
    "LLMResponse",
    "RouterUnavailable",
    "AllRoutesExhausted",
]
