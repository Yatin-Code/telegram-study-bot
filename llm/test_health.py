"""Unit tests for the rolling probe loop: batch rotation + ping verdicts."""

from __future__ import annotations

import datetime as dt

import pytest

from llm import health, ladder, quota


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "mirror.db"
    monkeypatch.setattr(ladder, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(quota, "DEFAULT_DB_PATH", path)
    return path


def _age_probe(candidate_id: str, seconds: float, db, *, ok=True):
    """Back-date a probe result so rotation logic sees an old stamp."""
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds)
    ladder.record_probe(candidate_id, ok=ok, tool_ok=ok, latency_ms=800,
                        error=None if ok else "http_502", db_path=db, now=old)


# --- batch selection ----------------------------------------------------------

def test_first_tick_picks_top_seed_candidates(db):
    batch = health.pick_batch(db_path=db)
    ids = [c.id for c in batch]
    assert len(ids) == health.BATCH_SIZE
    # seed-best overall candidates (never probed) come first
    assert ids[0] == "eaon:kimi-k2.7"           # seed 10
    assert "g4f:llama-3.3-70b-versatile" in ids  # seed 11


def test_fresh_probes_are_not_reprobed_immediately(db):
    for c in ladder.CANDIDATES[:6]:
        _age_probe(c.id, 60, db)  # fresh (just probed)
    batch = health.pick_batch(db_path=db)
    # fresh top candidates stay home; rotation moves to unprobed ones
    fresh_ids = {c.id for c in ladder.CANDIDATES[:6]}
    assert all(c.id not in fresh_ids for c in batch)


def test_failures_wait_for_recovery_window(db):
    c = ladder.CANDIDATES[0]
    _age_probe(c.id, 200, db, ok=False)  # failed 200s ago — not yet recovery-aged
    batch = health.pick_batch(db_path=db)
    assert c.id not in [b.id for b in batch]

    _age_probe(c.id, health.RECOVERY_SECONDS + 10, db, ok=False)
    batch = health.pick_batch(db_path=db)
    assert c.id in [b.id for b in batch]


def test_batch_is_deterministic(db):
    assert [c.id for c in health.pick_batch(db_path=db)] == [
        c.id for c in health.pick_batch(db_path=db)
    ]


# --- ping verdicts (transport faked) ------------------------------------------

def _fake_call_factory(chat_reply="OK", tool_reply='{"tool": "get_context", "arguments": {}}'):
    def _call(route, req, api_key, base_url):
        text = tool_reply if len(req.messages) > 1 else chat_reply
        class _R:
            pass
        r = _R()
        r.text = text
        return r
    return _call


def test_probe_candidate_full_pass(db, monkeypatch):
    monkeypatch.setattr(health.adapters, "call", _fake_call_factory())
    c = ladder.get("eaon:gemini-3")
    out = health.probe_candidate(c, env={"LLM_API_KEY": "k"})
    assert out == {"ok": True, "tool_ok": True, "latency_ms": out["latency_ms"], "error": None}
    assert out["latency_ms"] is not None


def test_probe_candidate_chat_junk_fails(db, monkeypatch):
    monkeypatch.setattr(health.adapters, "call", _fake_call_factory(chat_reply="sure, here you go"))
    c = ladder.get("eaon:gemini-3")
    out = health.probe_candidate(c, env={"LLM_API_KEY": "k"})
    assert out["ok"] is False
    assert "junk" in out["error"]


def test_probe_candidate_chat_ok_but_no_tool_json(db, monkeypatch):
    monkeypatch.setattr(health.adapters, "call", _fake_call_factory(tool_reply="let me think about that..."))
    c = ladder.get("eaon:gemini-3")
    out = health.probe_candidate(c, env={"LLM_API_KEY": "k"})
    assert out["ok"] is True and out["tool_ok"] is False


def test_probe_candidate_missing_key(db):
    c = ladder.get("eaon:gemini-3")
    out = health.probe_candidate(c, env={})
    assert out["ok"] is False and out["error"] == "missing_key"


def test_tick_persists_and_rotates(db, monkeypatch):
    monkeypatch.setattr(health.adapters, "call", _fake_call_factory())
    # All gateways keyed so every seeded candidate in the batch is probeable.
    env = {"LLM_API_KEY": "k", "G4F_API_KEY": "g", "GROQ_API_KEY": "x",
           "GOOGLE_API_KEY": "x", "OPENROUTER_API_KEY": "x"}
    summary = health.tick(db_path=db, env=env)
    assert len(summary["probed"]) == health.BATCH_SIZE
    assert all(p["ok"] and p["tool_ok"] for p in summary["probed"])
    rows = ladder.health_rows(db)
    assert len(rows) == health.BATCH_SIZE
    assert all(r["probe_ok_streak"] == 1 for r in rows.values() if r["probe_ok_streak"])
    # second tick moves to the next unprobed batch
    summary2 = health.tick(db_path=db, env=env)
    first_ids = {p["candidate"] for p in summary["probed"]}
    second_ids = {p["candidate"] for p in summary2["probed"]}
    assert first_ids.isdisjoint(second_ids)
