"""Router error taxonomy.

`RouterUnavailable` is the signal every call site keys on: when it is raised the
router could not be used at all (no ai.env, empty registry, import failure), so
the caller must fall through to its legacy single-provider path. It is
deliberately distinct from `AllRoutesExhausted`, which means the router *did*
run but every candidate (and the legacy tail) failed — a genuine outage, not a
configuration gap.
"""

from __future__ import annotations


class RouterError(RuntimeError):
    """Base class for router failures."""


class RouterUnavailable(RouterError):
    """The router cannot serve this call — caller should use its legacy path.

    Raised when ai.env is missing/empty, no routes resolve, or the package
    fails to initialise. Never raised for provider-side failures; those advance
    to the next route and, if all fail, surface as AllRoutesExhausted.
    """


class AllRoutesExhausted(RouterError):
    """Every certified route AND the legacy tail failed for this request.

    `attempts` is an ordered list of (route_id, reason) so callers/telemetry can
    see why each candidate was skipped or failed, without exposing prompts or
    keys.
    """

    def __init__(self, attempts: list[tuple[str, str]] | None = None) -> None:
        self.attempts = attempts or []
        detail = "; ".join(f"{rid}: {why}" for rid, why in self.attempts) or "no routes tried"
        super().__init__(f"all LLM routes exhausted ({detail})")
