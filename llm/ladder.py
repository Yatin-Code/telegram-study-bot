"""Curated candidate ladder + persistent per-model health.

Why this exists: raw request counts from proxy pools (g4f.space/v1/models) are
only a hint — the most-hit model can be cheap and bad. So the candidate list
here is CURATED by quality judgement first (evidence collected in
model-inventory-2026-08-01.md, incl. live probes: eaon's `deepseek-v4-pro` and
`glm-5.2` 502'd while `gemini-3`/`kimi-k2.7`/`deepseek-v4-flash` worked).
The static seed is then re-ranked by three live signals:

  1. PROBES (llm.health tick): cheap chat + tool-call pings every few minutes —
     says who is ALIVE right now.
  2. TRAFFIC COOLDOWNS (quota.llm_route_state): consecutive real failures
     push a model down; back-off is already durable.
  3. CERTIFICATION (llm.certify): 100%-pass on the project's own batteries
     marks a model proven FOR OUR WORKLOADS, not in general.

`ordered(purpose)` returns curated candidates best-first and is the single
source of truth both for routing (router.complete) and for the probe loop
(which re-checks the current top first, then stale unprobed entries).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import sqlite3
from pathlib import Path
from typing import Any, Optional

from . import env_loader, registry

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "sqlite_mirror.db"

G4F_DEFAULT_BASE = "https://g4f.space/v1"
EAON_DEFAULT_BASE = "https://api.eaon.dev/v1"
GOOGLE_DEFAULT_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
GROQ_DEFAULT_BASE = "https://api.groq.com/openai/v1"
OPENROUTER_DEFAULT_BASE = "https://openrouter.ai/api/v1"

# gateway -> (base-url env override, default base, key env name)
_GATEWAY_TABLE: dict[str, tuple[str, str, str]] = {
    "eaon": ("EAON_BASE_URL", EAON_DEFAULT_BASE, "LLM_API_KEY"),
    "g4f": ("G4F_BASE_URL", G4F_DEFAULT_BASE, "G4F_API_KEY"),
    "google": ("GOOGLE_BASE_URL", GOOGLE_DEFAULT_BASE, "GOOGLE_API_KEY"),
    "groq": ("GROQ_BASE_URL", GROQ_DEFAULT_BASE, "GROQ_API_KEY"),
    "openrouter": ("OPENROUTER_BASE_URL", OPENROUTER_DEFAULT_BASE, "OPENROUTER_API_KEY"),
}

# Router-side safety: at most this many ladder candidates are tried per request
# before falling through to the certified catalog / legacy tail (each dead
# upstream can cost a timeout).
MAX_ATTEMPTS_PER_REQUEST = 6


@dataclasses.dataclass(frozen=True)
class Candidate:
    """One curated (gateway, model) the bot may serve traffic with."""

    gateway: str            # "eaon" | "g4f" | "google" | "groq" | "openrouter"
    model: str
    seed: int               # static quality rank, lower = better
    purposes: tuple[str, ...] = ("intent", "domain", "sql")
    # Google thinking models consume max_tokens on thoughts — tiny probe
    # budgets return empty bodies. Bigger probe budget for those only.
    probe_max_tokens: int = 16

    @property
    def id(self) -> str:
        return f"{self.gateway}:{self.model}"


# --- The curated list --------------------------------------------------------
# seed: quality judgement per evidence in model-inventory-2026-08-01.md.
# g4f request counts noted as (g4f:N) — popular is NOT automatically good;
# cheap-but-dumb popular models are deliberately seeded LOW. Purposes default
# to all three; weaker models are restricted to the cheap purposes.

CANDIDATES: tuple[Candidate, ...] = (
    # --- eaon (verified live 2026-08-01) ---
    Candidate("eaon", "kimi-k2.7", 10),                  # live-verified OK
    Candidate("eaon", "gemini-3", 12),                   # live-verified OK
    Candidate("eaon", "deepseek-v4-flash", 14),          # live-verified OK
    Candidate("eaon", "gemini-3.5", 16),
    Candidate("eaon", "deepseek-v4-pro", 18),            # 502'd at probe time
    Candidate("eaon", "gemini-3.1-flash-lite", 20),      # 502'd at probe time
    Candidate("eaon", "glm-5.2", 22),                    # 502'd at probe time
    Candidate("eaon", "gemini-2.5-pro", 24),
    Candidate("eaon", "kimi-k3", 26),
    Candidate("eaon", "gemini-3.1-pro", 28),
    Candidate("eaon", "minimax-m3", 30),
    Candidate("eaon", "minimax-m2.7", 32, ("intent", "domain")),
    Candidate("eaon", "mimo-v2.5-pro", 34),
    Candidate("eaon", "hermes-4-70b", 36),
    # --- g4f (live-verified: llama-3.3-70b-versatile OK w/ key) ---
    Candidate("g4f", "llama-3.3-70b-versatile", 11),     # g4f:1504 via groq
    Candidate("g4f", "openai/gpt-oss-120b", 13),         # g4f:353 (groq+nvidia)
    Candidate("g4f", "models/gemini-3-flash-preview", 15),  # g4f:217
    Candidate("g4f", "gemini-2.5-flash", 17),            # g4f:150
    Candidate("g4f", "models/gemini-3.1-flash-lite", 19),  # g4f:46+41
    Candidate("g4f", "z-ai/glm-5.2", 21),                # g4f:344 (nvidia)
    Candidate("g4f", "deepseek-ai/deepseek-v4-pro", 23), # g4f:234 (nvidia)
    Candidate("g4f", "minimax-m3", 25),                  # g4f:382
    Candidate("g4f", "nemotron-3-super", 27),            # g4f:208
    Candidate("g4f", "gemma4:31b", 33, ("intent", "domain")),  # g4f:223
    Candidate("g4f", "gpt-4o-mini", 35, ("intent", "domain")),  # g4f:322 — cheap, weak
    Candidate("g4f", "deepseek-v3.2", 37),               # g4f:188
    Candidate("g4f", "opus-4.7", 40),                    # g4f:178 — no catalog metadata; unknown quality
    # --- direct free tiers (brutal-tested 2026-08-01: 4/4 on chat+tool+intent+sql) ---
    # groq: fastest verified direct gateway (0.3-0.6s full passes)
    Candidate("groq", "llama-3.3-70b-versatile", 11),    # 4/4 @ 0.52s
    Candidate("groq", "llama-3.1-8b-instant", 21),       # 4/4 @ 0.40s — tiny ultrafast fallback
    Candidate("groq", "groq/compound-mini", 33),         # 4/4 @ 1.15s — agentic bundle
    # google AI Studio free tier (per-model daily quota; pro tier 429'd at test)
    Candidate("google", "gemini-3.6-flash", 23, probe_max_tokens=1400),   # 4/4 @ 2.4s
    Candidate("google", "gemini-3.5-flash-lite", 25, probe_max_tokens=128),  # 4/4 @ 1.3s
    Candidate("google", "gemini-3.1-flash-lite", 27, probe_max_tokens=128),  # 4/4 @ 1.3s
    Candidate("google", "gemini-2.5-flash", 29, probe_max_tokens=1400),   # 4/4 @ 2.0s
    Candidate("google", "gemini-3.5-flash", 31, probe_max_tokens=1400),   # 4/4 @ 3.3s
    Candidate("google", "gemini-3-flash-preview", 37, probe_max_tokens=1400),  # 4/4 @ 4.7s
    # openrouter last-resort: account ~50 free requests/day
    Candidate("openrouter", "google/gemma-4-26b-a4b-it:free", 39),        # 4/4 @ 2.6s
)

_BY_ID = {c.id: c for c in CANDIDATES}

_GATEWAY_PRIORITY: dict[str, int] = {
    "eaon": 0, "google": 1, "groq": 2, "openrouter": 3, "g4f": 4,
}


def get(candidate_id: str) -> Candidate | None:
    return _BY_ID.get(candidate_id)


def all_candidates() -> list[Candidate]:
    return list(CANDIDATES)


# --- Gateway wiring ------------------------------------------------------------

def gateway_config(gateway: str, env: Optional[dict[str, str]] = None) -> dict[str, str]:
    """Resolve (base_url, env_key) for a gateway. Key presence = resolvable."""
    env = env_loader.load() if env is None else env
    entry = _GATEWAY_TABLE.get(gateway)
    if entry is None:
        raise ValueError(f"unknown gateway: {gateway}")
    base_var, default_base, env_key = entry
    return {
        "base_url": (env.get(base_var) or "").strip() or default_base,
        "env_key": env_key,
    }


def to_route(candidate: Candidate) -> registry.Route:
    """Adapt a curated candidate into a first-class routable Route."""
    cfg = gateway_config(candidate.gateway)
    return registry.Route(
        id=candidate.id,
        provider=candidate.gateway,
        adapter="openai",
        base_url=cfg["base_url"],
        model=candidate.model,
        auth="bearer",
        env_key=cfg["env_key"],
        quotas=(),
        quality_rank=candidate.seed,
    )


def api_key_for(candidate: Candidate, env: Optional[dict[str, str]] = None) -> str:
    env = env_loader.load() if env is None else env
    return (env.get(gateway_config(candidate.gateway)["env_key"]) or "").strip()


# --- Persistent health ---------------------------------------------------------

def _connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS llm_model_health (
            candidate_id TEXT PRIMARY KEY,
            gateway TEXT NOT NULL,
            model TEXT NOT NULL,
            seed INTEGER NOT NULL,
            probe_ok_streak INTEGER NOT NULL DEFAULT 0,
            probe_fail_streak INTEGER NOT NULL DEFAULT 0,
            probe_ok_total INTEGER NOT NULL DEFAULT 0,
            probe_fail_total INTEGER NOT NULL DEFAULT 0,
            last_probe_at TEXT,
            last_probe_error TEXT,
            latency_ema_ms REAL,
            certified_purposes TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(value: dt.datetime) -> str:
    return value.isoformat()


def _parse(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


def _upsert_stub(conn: sqlite3.Connection, candidate: Candidate, now: dt.datetime) -> None:
    """Make sure a health row exists for a curated candidate."""
    conn.execute(
        """INSERT INTO llm_model_health (candidate_id, gateway, model, seed, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(candidate_id) DO UPDATE SET seed=excluded.seed""",
        (candidate.id, candidate.gateway, candidate.model, candidate.seed, _iso(now)),
    )
    conn.commit()


def record_probe(
    candidate_id: str,
    *,
    ok: bool,
    tool_ok: bool,
    latency_ms: Optional[float] = None,
    error: Optional[str] = None,
    db_path: str | Path | None = None,
    now: Optional[dt.datetime] = None,
) -> None:
    """Persist one probe outcome (called by llm.health.tick)."""
    candidate = get(candidate_id)
    if candidate is None:
        return
    now = now or _utcnow()
    with _connect(db_path) as conn:
        _upsert_stub(conn, candidate, now)
        row = conn.execute(
            "SELECT latency_ema_ms FROM llm_model_health WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
        ema = row["latency_ema_ms"] if row else None
        if ok and latency_ms is not None:
            ema = latency_ms if ema is None else 0.7 * float(ema) + 0.3 * latency_ms
        # A probe that chats but can't emit a tool call counts as half-alive:
        # ok streak only grows when BOTH pings pass.
        full_ok = ok and tool_ok
        conn.execute(
            """UPDATE llm_model_health SET
                 probe_ok_streak = CASE WHEN ? THEN probe_ok_streak + 1 ELSE 0 END,
                 probe_fail_streak = CASE WHEN ? THEN 0 ELSE probe_fail_streak + 1 END,
                 probe_ok_total = probe_ok_total + ?,
                 probe_fail_total = probe_fail_total + ?,
                 last_probe_at = ?,
                 last_probe_error = ?,
                 latency_ema_ms = ?,
                 updated_at = ?
               WHERE candidate_id = ?""",
            (
                full_ok, full_ok, 1 if ok else 0, 0 if ok else 1,
                _iso(now), None if ok else (error or "probe failed")[:200],
                ema, _iso(now), candidate_id,
            ),
        )
        conn.commit()


def mark_certified(
    candidate_id: str, purpose: str, certified: bool,
    *, db_path: str | Path | None = None,
) -> None:
    candidate = get(candidate_id)
    if candidate is None:
        return
    now = _utcnow()
    with _connect(db_path) as conn:
        _upsert_stub(conn, candidate, now)
        row = conn.execute(
            "SELECT certified_purposes FROM llm_model_health WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
        current = set((row["certified_purposes"] or "").split(",")) if row else set()
        current.discard("")
        if certified:
            current.add(purpose)
        else:
            current.discard(purpose)
        conn.execute(
            "UPDATE llm_model_health SET certified_purposes=?, updated_at=? WHERE candidate_id=?",
            (",".join(sorted(current)), _iso(now), candidate_id),
        )
        conn.commit()


def health_rows(db_path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM llm_model_health").fetchall()
    return {r["candidate_id"]: dict(r) for r in rows}


# --- Ranking -------------------------------------------------------------------

_COOLDOWN_PENALTY = 10_000.0
_FAIL_STREAK_PENALTY = 40.0
_OK_STREAK_BONUS = 2.0       # per consecutive successful probe, capped
_OK_STREAK_BONUS_CAP = 20.0
_CERTIFIED_BONUS = 8.0
_LATENCY_PENALTY_PER_S = 1.5
_LATENCY_PENALTY_CAP = 15.0


def _cooldown_active(candidate_id: str, conn: sqlite3.Connection, now: dt.datetime) -> bool:
    """Read natural-traffic cooldown from quota's llm_route_state (same db)."""
    try:
        row = conn.execute(
            "SELECT cooldown_until, disabled FROM llm_route_state WHERE route_id=?",
            (candidate_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return False  # quota tables not created yet — treat as available
    if not row:
        return False
    if row["disabled"]:
        return True
    cooldown = _parse(row["cooldown_until"])
    return bool(cooldown and cooldown > now)


def _score_from_row(
    candidate: Candidate,
    row: Optional[dict[str, Any]],
    cooldown: bool,
    purpose: str,
) -> float:
    score = float(candidate.seed)
    if cooldown:
        return score + _COOLDOWN_PENALTY
    if row is None:
        return score
    certified = set((row.get("certified_purposes") or "").split(","))
    if purpose in certified:
        score -= _CERTIFIED_BONUS
    score += min(_FAIL_STREAK_PENALTY * (row.get("probe_fail_streak") or 0), 200.0)
    score -= min(_OK_STREAK_BONUS * (row.get("probe_ok_streak") or 0), _OK_STREAK_BONUS_CAP)
    ema = row.get("latency_ema_ms")
    if ema is not None:
        score += min(float(ema) / 1000.0 * _LATENCY_PENALTY_PER_S, _LATENCY_PENALTY_CAP)
    return score


def ordered(
    purpose: str,
    *,
    db_path: str | Path | None = None,
    now: Optional[dt.datetime] = None,
) -> list[Candidate]:
    """Curated candidates for a purpose, best first (seed re-ranked by health)."""
    now = now or _utcnow()
    candidates = [c for c in CANDIDATES if purpose in c.purposes]
    with _connect(db_path) as conn:
        rows = health_rows(db_path)
        scored = [
            (_score_from_row(c, rows.get(c.id), _cooldown_active(c.id, conn, now), purpose), c)
            for c in candidates
        ]
    scored.sort(key=lambda t: (t[0], _GATEWAY_PRIORITY.get(t[1].gateway, 99), t[1].id))
    return [c for _, c in scored]
