"""Load secrets from the gitignored ``ai.env`` WITHOUT touching ``os.environ``.

The multi-provider router reads its keys from ``ai.env`` (Groq, OpenRouter,
Cloudflare, Ollama, …). Two hard rules from the spec:

- **Never override existing environment variables.** We parse the file into a
  private dict and hand values out on request; we do not call ``os.environ`` or
  ``load_dotenv`` so the legacy ``LLM_*`` config in ``.env`` is untouched.
- **Never log values.** Only key *names* ever appear in logs.

A missing ``ai.env`` yields an empty mapping, which the registry reads as "no
routes resolvable" — the router then reports itself unavailable and callers use
their legacy path.
"""

from __future__ import annotations

import os
from functools import lru_cache

# ai.env lives in the project root (one level up from this package).
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_AI_ENV_PATH = os.path.join(_PROJECT_ROOT, "ai.env")


def _parse(path: str) -> dict[str, str]:
    """Parse a KEY=VALUE .env-style file. Blank lines and ``#`` comments skipped.

    Values are taken verbatim (stripped of surrounding whitespace and matching
    single/double quotes). Malformed lines are ignored rather than raising, so a
    stray comment can never take the router down.
    """
    out: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if not key:
                    continue
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                out[key] = value
    except FileNotFoundError:
        return {}
    except OSError:
        return {}
    return out


@lru_cache(maxsize=1)
def load(path: str | None = None) -> dict[str, str]:
    """Return the parsed ai.env mapping (cached). Missing file → ``{}``."""
    return _parse(path or _AI_ENV_PATH)


def get(name: str, path: str | None = None) -> str | None:
    """Return the value for ``name`` from ai.env, or None if absent/empty."""
    val = load(path).get(name)
    return val if val and val.strip() else None


def has_any_keys(path: str | None = None) -> bool:
    """True if ai.env parsed to at least one non-empty value."""
    return any(v and v.strip() for v in load(path).values())


def clear_cache() -> None:
    """Drop the cached parse (used by tests that write a temporary ai.env)."""
    load.cache_clear()
