"""Unit tests for the curated ladder: health persistence + ranking."""

from __future__ import annotations

import datetime as dt

import pytest

from llm import ladder, quota


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "mirror.db"
    monkeypatch.setattr(ladder, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(quota, "DEFAULT_DB_PATH", path)
    return path


def _candidate(cid: str) -> ladder.Candidate:
    c = ladder.get(cid)
    assert c is not None, cid
    return c


# --- persistence ---------------------------------------------------------------

def test_record_probe_success_streak_and_latency(db):
    for i in range(3):
        ladder.record_probe("eaon:gemini-3", ok=True, tool_ok=True, latency_ms=1000 + i * 100, db_path=db)
    row = ladder.health_rows(db)["eaon:gemini-3"]
    assert row["probe_ok_streak"] == 3
    assert row["probe_fail_streak"] == 0
    assert row["probe_ok_total"] == 3
    assert row["last_probe_error"] is None
    # EMA blends first sample with the rest
    assert row["latency_ema_ms"] is not None
    assert 1000 <= row["latency_ema_ms"] <= 1600


def test_record_probe_chat_ok_but_tool_junk_counts_half_alive(db):
    ladder.record_probe("eaon:gemini-3", ok=True, tool_ok=False, latency_ms=500, db_path=db)
    row = ladder.health_rows(db)["eaon:gemini-3"]
    assert row["probe_ok_total"] == 1
    assert row["probe_ok_streak"] == 0  # streak requires BOTH pings


def test_record_probe_failure_then_recovery(db):
    ladder.record_probe("eaon:gemini-3", ok=False, tool_ok=False, error="http_502", db_path=db)
    ladder.record_probe("eaon:gemini-3", ok=False, tool_ok=False, error="http_502", db_path=db)
    row = ladder.health_rows(db)["eaon:gemini-3"]
    assert row["probe_fail_streak"] == 2
    assert row["last_probe_error"] == "http_502"
    ladder.record_probe("eaon:gemini-3", ok=True, tool_ok=True, latency_ms=800, db_path=db)
    row = ladder.health_rows(db)["eaon:gemini-3"]
    assert row["probe_ok_streak"] == 1
    assert row["probe_fail_streak"] == 0
    assert row["last_probe_error"] is None


def test_mark_certified_roundtrip(db):
    ladder.mark_certified("eaon:gemini-3", "intent", True, db_path=db)
    ladder.mark_certified("eaon:gemini-3", "sql", True, db_path=db)
    row = ladder.health_rows(db)["eaon:gemini-3"]
    assert set(row["certified_purposes"].split(",")) == {"intent", "sql"}
    ladder.mark_certified("eaon:gemini-3", "sql", False, db_path=db)
    row = ladder.health_rows(db)["eaon:gemini-3"]
    assert row["certified_purposes"] == "intent"


def test_record_probe_unknown_candidate_is_noop(db):
    ladder.record_probe("eaon:no-such-model", ok=True, tool_ok=True, db_path=db)
    assert "eaon:no-such-model" not in ladder.health_rows(db)


# --- ranking -------------------------------------------------------------------

def test_seed_order_without_health_data(db):
    ranked = ladder.ordered("domain", db_path=db)
    seeds = [c.seed for c in ranked]
    assert seeds == sorted(seeds)
    assert ranked[0].seed == min(c.seed for c in ladder.CANDIDATES if "domain" in c.purposes)


def test_purpose_filters_candidates(db):
    ranked = ladder.ordered("sql", db_path=db)
    assert all("sql" in c.purposes for c in ranked)
    intent_only = ladder.get("g4f:gpt-4o-mini")
    assert intent_only not in ranked


def test_consistent_worker_outranks_seed_equal_but_failed(db):
    """A model that keeps working rises above a same-seed one that keeps failing."""
    # give both candidates a synthetic same seed via monkeypatched list
    a = ladder.Candidate("eaon", "gemini-3", 50)
    b = ladder.Candidate("g4f", "llama-3.3-70b-versatile", 50)
    monkeypatch_list = (a, b)
    import llm.ladder as lad
    original = lad.CANDIDATES
    lad.CANDIDATES = monkeypatch_list
    try:
        lad._BY_ID = {c.id: c for c in monkeypatch_list}
        for _ in range(3):
            ladder.record_probe(a.id, ok=True, tool_ok=True, latency_ms=500, db_path=db)
            ladder.record_probe(b.id, ok=False, tool_ok=False, error="http_502", db_path=db)
        ranked = ladder.ordered("intent", db_path=db)
        assert ranked[0].id == a.id
        assert ranked[-1].id == b.id
    finally:
        lad.CANDIDATES = original
        lad._BY_ID = {c.id: c for c in original}


def test_traffic_cooldown_sinks_candidate(db):
    candidate = _candidate("eaon:gemini-3")
    route = ladder.to_route(candidate)
    before = ladder.ordered("domain", db_path=db)
    assert candidate in before[:5]  # seed 12 is near the top of the full ladder
    for _ in range(5):  # record_route_result escalates to cooldown
        quota.record_route_result(route.id, success=False, reason="http_500", db_path=db)
    ranked = ladder.ordered("domain", db_path=db)
    # cooldowned → sinks below any non-cooldown candidate
    assert ranked[-1].id == candidate.id


def test_certified_candidate_gets_bonus(db):
    a = ladder.Candidate("eaon", "gemini-3", 50)
    b = ladder.Candidate("g4f", "llama-3.3-70b-versatile", 51)
    import llm.ladder as lad
    original = lad.CANDIDATES
    lad.CANDIDATES = (a, b)
    lad._BY_ID = {c.id: c for c in (a, b)}
    try:
        ladder.mark_certified(a.id, "intent", True, db_path=db)
        ranked = ladder.ordered("intent", db_path=db)
        assert ranked[0].id == a.id  # 50 - 8 < 51
    finally:
        lad.CANDIDATES = original
        lad._BY_ID = {c.id: c for c in original}


# --- gateway wiring --------------------------------------------------------------

def test_to_route_shapes_and_env_keys():
    c = _candidate("g4f:llama-3.3-70b-versatile")
    route = ladder.to_route(c)
    assert route.adapter == "openai"
    assert route.base_url.endswith("/v1")
    assert route.env_key == "G4F_API_KEY"
    route2 = ladder.to_route(_candidate("eaon:gemini-3"))
    assert route2.env_key == "LLM_API_KEY"
    env = {"G4F_API_KEY": "abc", "LLM_API_KEY": "def"}
    assert ladder.api_key_for(c, env=env) == "abc"
    assert ladder.api_key_for(_candidate("eaon:gemini-3"), env=env) == "def"


def test_candidate_ids_cover_all_specs():
    ids = [c.id for c in ladder.CANDIDATES]
    assert len(ids) == len(set(ids))
    assert all(g in ladder._GATEWAY_TABLE for g in {c.gateway for c in ladder.CANDIDATES})


def test_gateway_config_table_new_providers(db):
    for gateway, env_var in (
        ("google", "GOOGLE_API_KEY"),
        ("groq", "GROQ_API_KEY"),
        ("openrouter", "OPENROUTER_API_KEY"),
    ):
        cfg = ladder.gateway_config(gateway, env={})
        assert cfg["env_key"] == env_var
        assert cfg["base_url"].startswith("https://")
    # env override for base_url still works
    cfg = ladder.gateway_config("google", env={"GOOGLE_BASE_URL": "http://x/v1"})
    assert cfg["base_url"] == "http://x/v1"


def test_google_thinking_candidates_get_enlarged_probe_budget():
    thinking = ladder.get("google:gemini-3.6-flash")
    assert thinking.probe_max_tokens >= 1000
    # flash-lites need a modest buffer (64 verified live; 128 for headroom)
    assert ladder.get("google:gemini-3.5-flash-lite").probe_max_tokens >= 64
    # non-thinking gateways keep the tiny default
    assert ladder.get("groq:llama-3.3-70b-versatile").probe_max_tokens == 16
