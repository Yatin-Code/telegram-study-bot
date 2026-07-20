"""settings.json override layer + registry + pending-edit state. No Telegram."""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3

import pytest

import advisor
import draft_store
import sync
from config import settings

KEY_TO_ACCESSOR = {
    "PLANNING_REMINDER_TIME": settings.planning_reminder_time,
    "WEEKLY_REPORT_TIME": settings.weekly_report_time,
    "TIMETABLE_REMINDER_WEEKDAY": settings.timetable_reminder_weekday,
    "COMMITMENT_CHECK_TIME": settings.commitment_check_time,
    "COMMITMENT_NUDGE_TIME": settings.commitment_nudge_time,
    "SYNC_INTERVAL_SECONDS": settings.sync_interval_seconds,
    "DAILY_CY_BASELINE": settings.daily_cy_baseline,
    "DAILY_CY_CEILING": settings.daily_cy_ceiling,
    "MAX_DAILY_COMMITTED_MINUTES": settings.max_daily_committed_minutes,
    "QUERY_MAX_ITERATIONS": settings.query_max_iterations,
    "QUERY_HISTORY_PAIRS": settings.query_history_pairs,
    "QA_HISTORY_TTL_MINUTES": settings.qa_history_ttl_minutes,
    "MEMORY_CONTEXT_BUDGET_CHARS": settings.memory_context_budget,
    "COMMITMENT_WARN_AFTER": settings.commitment_warn_after,
    "ACCURACY_DROP_PTS": settings.accuracy_drop_pts,
    "LOW_ADHERENCE_PCT": settings.low_adherence_pct,
    "LLM_MODEL": settings.llm_model,
    "LLM_FALLBACK_MODEL": settings.llm_fallback_models,
    "USER_TIMEZONE": settings.user_timezone,
    "DRAFT_TTL_MINUTES": settings.draft_ttl_minutes,
}


@pytest.fixture(autouse=True)
def _isolated_overrides(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "_OVERRIDES_PATH", str(tmp_path / "settings.json"))


def test_override_roundtrip_and_atomic_file(monkeypatch):
    monkeypatch.delenv("QUERY_MAX_ITERATIONS", raising=False)
    assert settings.query_max_iterations() == 12
    settings.set_override("QUERY_MAX_ITERATIONS", "3")
    assert settings.query_max_iterations() == 3
    with open(settings._OVERRIDES_PATH, encoding="utf-8") as fh:
        assert json.load(fh) == {"QUERY_MAX_ITERATIONS": "3"}
    settings.clear_override("QUERY_MAX_ITERATIONS")
    assert settings.query_max_iterations() == 12
    with open(settings._OVERRIDES_PATH, encoding="utf-8") as fh:
        assert json.load(fh) == {}


def test_override_beats_env(monkeypatch):
    monkeypatch.setenv("DAILY_CY_BASELINE", "200")
    assert settings.daily_cy_baseline() == 200
    settings.set_override("DAILY_CY_BASELINE", "260")
    assert settings.daily_cy_baseline() == 260


def test_secrets_ignore_overrides(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "env-secret")
    settings.set_override("LLM_API_KEY", "injected-from-json")
    assert settings.llm_api_key() == "env-secret"


def test_registry_complete_and_defaults(monkeypatch):
    registry_keys = {e["key"] for e in settings.SETTINGS_REGISTRY}
    assert registry_keys == set(KEY_TO_ACCESSOR)
    for entry in settings.SETTINGS_REGISTRY:
        assert entry["category"] in settings.SETTINGS_CATEGORIES
        if entry["key"].startswith("LLM_"):
            continue  # env-required / free-form
        monkeypatch.delenv(entry["key"], raising=False)
        value = KEY_TO_ACCESSOR[entry["key"]]()
        assert value is not None
        ok, canonical = settings.validate_setting(entry["key"], str(entry["default"]))
        assert ok, f"{entry['key']} default fails its own validator: {canonical}"


@pytest.mark.parametrize("key,bad", [
    ("PLANNING_REMINDER_TIME", "25:99"),
    ("PLANNING_REMINDER_TIME", "evening"),
    ("TIMETABLE_REMINDER_WEEKDAY", "7"),
    ("QUERY_MAX_ITERATIONS", "0"),
    ("QUERY_MAX_ITERATIONS", "banana"),
    ("LOW_ADHERENCE_PCT", "150"),
    ("USER_TIMEZONE", "Nope/Nope"),
])
def test_validator_rejects(key, bad):
    ok, _ = settings.validate_setting(key, bad)
    assert ok is False


def test_validator_canonicalises():
    assert settings.validate_setting("WEEKLY_REPORT_TIME", "9:05") == (True, "09:05")
    assert settings.validate_setting("TIMETABLE_REMINDER_WEEKDAY", "0") == (True, "0")
    assert settings.validate_setting("USER_TIMEZONE", "Asia/Kolkata") == (True, "Asia/Kolkata")


def test_draft_ttl_honours_override(tmp_path):
    settings.set_override("DRAFT_TTL_MINUTES", "1")
    db = tmp_path / "drafts.db"
    draft_id = draft_store.create_draft(1, {"kind": "x"}, ["p"], db_path=db)
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT created_at, expires_at FROM drafts WHERE draft_id=?", (draft_id,)
        ).fetchone()
    delta = dt.datetime.fromisoformat(row["expires_at"]) - dt.datetime.fromisoformat(row["created_at"])
    assert 50 <= delta.total_seconds() <= 70


def test_qa_ttl_honours_override(tmp_path):
    db = tmp_path / "qa.db"
    draft_store.record_qa(1, "q1", "a1", db_path=db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE chat_qa_history SET created_at = ?",
            ((dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=30)).isoformat(),),
        )
        conn.commit()
    assert len(draft_store.recent_qa(1, db_path=db)) == 1  # default TTL 60
    settings.set_override("QA_HISTORY_TTL_MINUTES", "10")
    assert draft_store.recent_qa(1, db_path=db) == []


def test_advisor_threshold_honours_override(tmp_path):
    db = tmp_path / "adv.db"
    with sync.connect(db) as conn:
        sync.init_db(conn)

    def insert(notion_page_id, date, attempted, correct):
        with sqlite3.connect(db) as conn:
            conn.execute(
                'INSERT INTO ledger (notion_page_id, archived, last_synced_at, raw_json, '
                'date, questions_attempted, questions_correct) VALUES (?,0,?,"{}",?,?,?)',
                (notion_page_id, "2026-07-21T00:00:00+00:00", date, attempted, correct),
            )
            conn.commit()

    for i in range(2):  # prior week 80%
        insert(f"p{i}", f"2026-07-{9 + i:02d}", 20, 16)
    for i in range(2):  # recent week 65% -> 15-pt drop
        insert(f"r{i}", f"2026-07-{17 + i:02d}", 20, 13)
    assert any("accuracy" in w for w in advisor.trajectory_warnings(today="2026-07-21", db_path=db))
    settings.set_override("ACCURACY_DROP_PTS", "20")
    assert not any("accuracy" in w for w in advisor.trajectory_warnings(today="2026-07-21", db_path=db))


def test_pending_setting_edit_roundtrip_and_ttl(tmp_path):
    db = tmp_path / "pend.db"
    draft_store.set_pending_setting_edit(5, "WEEKLY_REPORT_TIME", db_path=db)
    assert draft_store.get_pending_setting_edit(5, db_path=db) == "WEEKLY_REPORT_TIME"
    assert draft_store.get_pending_setting_edit(5, db_path=db) is None  # fetch clears
    draft_store.set_pending_setting_edit(5, "DAILY_CY_BASELINE", db_path=db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE pending_setting_edits SET asked_at = ?",
            ((dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)).isoformat(),),
        )
        conn.commit()
    assert draft_store.get_pending_setting_edit(5, db_path=db) is None  # stale
    draft_store.set_pending_setting_edit(5, "DAILY_CY_BASELINE", db_path=db)
    draft_store.clear_pending_setting_edit(5, db_path=db)
    assert draft_store.get_pending_setting_edit(5, db_path=db) is None
