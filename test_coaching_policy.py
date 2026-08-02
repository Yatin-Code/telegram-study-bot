"""Phase 12 + 14 core tests — backlog escalation, privacy, freshness, notifications.

Covers the new ``coaching_policy`` module:
  * privacy: sensitive field/value redaction (Aadhaar, parent phones, address,
    password/token/API keys, raw sensitive JSON), non-sensitive data preserved
  * freshness: per-dataset fresh/stale/failed/never_synced classification from
    the Notion mirror meta, coaching sync history and local op_* tables
  * notifications: quiet hours, cooldowns, relevance/data gating, per-day
    budget, durable decision/audit table, no sends
  * backlog escalation: count/age/hours/growth metrics, adherence/capacity and
    coverage risk, normal/growing/critical/impossible levels, bounded suggested
    minute increase, no automatic plan writes

Usage:
    python test_coaching_policy.py
    .venv-test/bin/python -m pytest -q test_coaching_policy.py
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

import pytest

import coaching_policy as pol
import ntsc_coaching
import operational_store
import session_context
import sync

NOW = dt.datetime(2026, 8, 2, 12, 0, tzinfo=dt.timezone.utc)
NIGHT = dt.datetime(2026, 8, 2, 23, 30, tzinfo=dt.timezone.utc)
DISC_NIGHT = dt.datetime(2026, 8, 2, 23, 0, tzinfo=dt.timezone.utc)


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "policy.db"
    with sync.connect(path) as conn:
        sync.init_db(conn)
        operational_store.init_db(conn)
        ntsc_coaching.init_db(conn)
    return path


def insert(path, table, **values):
    physical = {
        "goals": "op_goals",
        "work_items": "op_work_items",
        "exams": "op_exams",
        "exam_questions": "op_exam_questions",
        "doubt_attempts": "op_doubt_attempts",
        "timetable": "op_timetable",
        "daily_plan": "op_daily_plan",
    }.get(table, table)
    base = {
        "notion_page_id": values.pop("notion_page_id", f"{table}-id"),
        "archived": 0,
        "raw_json": "{}",
    }
    if physical.startswith("op_"):
        base.setdefault("id", base["notion_page_id"])
        base.setdefault("created_time", "2026-07-20T00:00:00+00:00")
        base.setdefault("last_edited_time", "2026-07-20T00:00:00+00:00")
        base.setdefault("last_synced_at", "2026-08-02T11:30:00+00:00")
    base.update(values)
    with sqlite3.connect(path) as conn:
        if physical.startswith("op_"):
            operational_store.init_db(conn)
        cols = ",".join(f'"{key}"' for key in base)
        marks = ",".join("?" for _ in base)
        conn.execute(
            f'INSERT INTO "{physical}" ({cols}) VALUES ({marks})', tuple(base.values())
        )
        conn.commit()


def sync_meta(path, db_key, *, completed="2026-08-02T00:00:00+00:00", error=None):
    with sqlite3.connect(path) as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO sync_meta "
            "(db_key,last_started_at,last_completed_at,last_row_count,last_error) "
            "VALUES (?,?,?,?,?)",
            (db_key, "2026-08-02T00:00:00+00:00", completed, 5, error),
        )
        conn.commit()


def coaching_run(path, status, *, finished="2026-08-02T00:00:00+00:00", error=None):
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO coaching_sync_runs (started_at,finished_at,status,datasets,error) "
            "VALUES (?,?,?,?,?)",
            ("2026-08-02T00:00:00+00:00", finished, status, '["profile","classes"]', error),
        )
        conn.commit()


def add_backlog(path, ident, *, created="2026-07-28T00:00:00+00:00", estimated_min=45):
    if created is None:
        created = "unknown"
    insert(
        path, "work_items", notion_page_id=ident, title=f"Backlog {ident}",
        kind="Backlog", status="Backlog", estimated_min=estimated_min,
        created_time=created, last_edited_time=created,
    )


# ---------------------------------------------------------------------------
# privacy policy
# ---------------------------------------------------------------------------

def test_sensitive_field_names_are_recognised():
    assert pol.is_sensitive_field("aadhaar_no")
    assert pol.is_sensitive_field("parent_phone")
    assert pol.is_sensitive_field("guardian mobile")
    assert pol.is_sensitive_field("Home Address")
    assert pol.is_sensitive_field("password")
    assert pol.is_sensitive_field("api_key")
    assert pol.is_sensitive_field("auth_token")
    assert pol.is_sensitive_field("accessToken")
    assert not pol.is_sensitive_field("title")
    assert not pol.is_sensitive_field("subject")
    assert not pol.is_sensitive_field("chapter")
    assert not pol.is_sensitive_field("estimated_min")
    assert not pol.is_sensitive_field("created_time")


def test_redact_row_preserves_study_data_and_strips_sensitive_fields():
    row = {
        "title": "Kinematics DPP",
        "subject": "Physics",
        "estimated_min": 45,
        "parent_phone": "9876543210",
        "aadhaar": "1234 5678 9012",
        "address": "Flat 21, Green Park Colony, Delhi 110016",
        "password": "s3cret!",
        "api_key": "sk-abcdef123456",
        "raw_json": json.dumps({"student": {"mobile": "9988776655"}, "marks": 120}),
    }
    redacted = pol.redact_row(row)
    assert redacted["title"] == "Kinematics DPP"
    assert redacted["subject"] == "Physics"
    assert redacted["estimated_min"] == 45
    assert redacted["parent_phone"] == pol.REDACTED
    assert redacted["aadhaar"] == pol.REDACTED
    assert redacted["address"] == pol.REDACTED
    assert redacted["password"] == pol.REDACTED
    assert redacted["api_key"] == pol.REDACTED
    parsed = json.loads(redacted["raw_json"])
    assert parsed["student"]["mobile"] == pol.REDACTED
    assert parsed["marks"] == 120
    assert row["title"] == "Kinematics DPP"  # original untouched


def test_redact_rows_maps_over_list():
    rows = [{"parent_phone": "9876543210", "title": "a"}, {"title": "b"}]
    out = pol.redact_rows(rows)
    assert out[0]["parent_phone"] == pol.REDACTED
    assert out[1]["title"] == "b"


def test_redact_text_redacts_values_but_keeps_study_numbers():
    text = (
        "Call parent at +91 98765 43210 or 9123456789. Aadhaar 123456789012. "
        "email: p@x.com, bearer abc123xyz, key sk-abcdef1234567890, pin 110016. "
        "Got 120/180 marks, id 8f14e45fceea167a5a36dedd4bea2543."
    )
    out = pol.redact_text(text)
    assert "98765" not in out.replace("+91 ", "")
    assert pol.REDACTED in out
    assert "sk-" not in out
    assert "p@x.com" not in out
    assert "110016" not in out
    assert "120/180" in out
    assert "8f14e45fceea167a5a36dedd4bea2543" in out  # study page ids survive


def test_redact_text_nukes_full_address_only_for_clear_addresses():
    address = "Flat 21, Green Park Colony, New Delhi 110016"
    assert pol.redact_text(address) == pol.REDACTED
    study = "Solve the chapter on Kinematics, exercise 4B, page 110016 not relevant"
    assert pol._looks_like_full_address(study) is False


def test_privacy_policy_block_mentions_all_categories():
    block = pol.privacy_policy_block()
    for needle in ("Aadhaar", "parent", "address", "password", "token"):
        assert needle in block


# ---------------------------------------------------------------------------
# freshness classification
# ---------------------------------------------------------------------------

def test_freshness_never_synced_on_empty_db(db):
    data = pol.classify_freshness(now=NOW, db_path=db)
    assert set(data) == set(pol.ALL_DATASETS)
    for dataset in pol.ALL_DATASETS:
        assert data[dataset]["status"] in pol.FRESH_LABELS
        assert data[dataset]["label"] == data[dataset]["status"]
    assert data["ledger"]["status"] == "never_synced"
    assert data["coaching"]["status"] == "never_synced"


def test_freshness_notion_fresh_and_stale(db):
    sync_meta(db, "ledger", completed="2026-08-02T11:55:00+00:00")
    assert pol.classify_freshness(now=NOW, db_path=db)["ledger"]["status"] == "fresh"
    sync_meta(db, "ledger", completed="2026-07-30T00:00:00+00:00")
    assert pol.classify_freshness(now=NOW, db_path=db)["ledger"]["status"] == "stale"


def test_freshness_failed_and_never(db):
    sync_meta(db, "doubts", completed=None, error="HTTP 429")
    assert pol.classify_freshness(now=NOW, db_path=db)["doubts"]["status"] == "failed"
    sync_meta(db, "doubts", completed=None, error=None)
    assert pol.classify_freshness(now=NOW, db_path=db)["doubts"]["status"] == "never_synced"


def test_freshness_coaching_statuses(db):
    coaching_run(db, "success", finished="2026-08-02T11:30:00+00:00")
    assert pol.classify_freshness(now=NOW, db_path=db)["coaching"]["status"] == "fresh"
    coaching_run(db, "success", finished="2026-07-01T00:00:00+00:00")
    assert pol.classify_freshness(now=NOW, db_path=db)["coaching"]["status"] == "stale"
    coaching_run(db, "failed", error="login failed")
    assert pol.classify_freshness(now=NOW, db_path=db)["coaching"]["status"] == "failed"


def test_freshness_operational_is_local_fresh(db):
    add_backlog(db, "b1", created="2026-08-01T10:00:00+00:00")
    data = pol.classify_freshness(now=NOW, db_path=db)
    assert data["operational"]["status"] == "fresh"
    assert data["operational"]["detail"] == "1 local operational records"


def test_freshness_block_lists_every_dataset(db):
    block = pol.freshness_block(now=NOW, db_path=db)
    for dataset in pol.ALL_DATASETS:
        assert f"- {dataset}: " in block


# ---------------------------------------------------------------------------
# notification policy
# ---------------------------------------------------------------------------

def test_quiet_hours_block_and_allow(db):
    decided = pol.decide_notification(
        kind="planning", now=NIGHT, chat_id=1, db_path=db,
    )
    assert decided["allow"] is False
    assert "quiet_hours" in decided["blocked_by"]
    assert any("quiet hours" in r for r in decided["reasons"])
    assert decided["sends_nothing"] is True

    daytime = pol.decide_notification(
        kind="planning", now=NOW, chat_id=1, db_path=db,
    )
    assert daytime["allow"] is True
    assert "quiet_hours" not in daytime["blocked_by"]


def test_is_quiet_hours_overnight_and_disabled():
    active, window = pol.is_quiet_hours(NIGHT)
    assert active is True
    assert window == ("22:00", "08:00")
    assert pol.is_quiet_hours(NOW)[0] is False
    active, _ = pol.is_quiet_hours(NIGHT, start_hhmm="00:00", end_hhmm="00:00")
    assert active is False


def test_urgent_kind_bypasses_quiet_hours_but_not_cooldown(db):
    decided = pol.decide_notification(
        kind="system_alert", now=NIGHT, db_path=db,
    )
    assert decided["allow"] is True
    assert decided["quiet_hours"]["bypassed"] is True


def test_cooldown_blocks_repeat_same_kind(db):
    first = pol.decide_notification(kind="backlog", now=NOW, event_key="w1", db_path=db)
    assert first["allow"] is True
    pol.record_decision(first, db_path=db)
    second = pol.decide_notification(
        kind="backlog", now=NOW + dt.timedelta(minutes=10),
        event_key="w2", db_path=db,
    )
    assert second["allow"] is False
    assert "cooldown" in second["blocked_by"]
    assert second["cooldown"]["cooling"] is True


def test_daily_budget_bounds_sends(db):
    now = NOW
    for index in range(3):
        decision = pol.decide_notification(
            kind=f"kind_{index}", now=now, event_key=f"e{index}",
            chat_id=1, db_path=db, cooldown_min=0, budget_per_day=3,
        )
        assert decision["allow"] is True
        pol.record_decision(decision, db_path=db)
    over = pol.decide_notification(
        kind="kind_extra", now=now + dt.timedelta(hours=1), event_key="e3",
        chat_id=1, db_path=db, cooldown_min=0, budget_per_day=3,
    )
    assert over["allow"] is False
    assert "budget" in over["blocked_by"]
    assert pol.daily_notification_count(local_date="2026-08-02", db_path=db) == 3


def test_stale_data_gates_gated_kinds_but_not_local(db):
    gated = pol.decide_notification(kind="teacher", now=NOW, db_path=db)
    assert gated["allow"] is False
    assert "stale_data" in gated["blocked_by"]
    assert gated["relevance"]["data_fresh"] is False

    local = pol.decide_notification(kind="backlog", now=NOW, db_path=db)
    assert local["allow"] is True
    assert local["relevance"]["data_fresh"] is True


def test_decision_is_durable_and_auditable(db):
    decision = pol.decide_notification(
        kind="planning", now=NOW, event_key="k1", chat_id=7, db_path=db,
    )
    stored = pol.record_decision(decision, db_path=db)
    assert stored["audit_row"]["kind"] == "planning"
    assert stored["audit_row"]["allow"] == 1
    assert stored["audit_row"]["local_date"] == "2026-08-02"
    assert json.loads(stored["audit_row"]["reasons"])
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            f"SELECT * FROM {pol.DECISIONS_TABLE}"
        ).fetchall()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# execution-discipline kinds (Phase 15)
# ---------------------------------------------------------------------------

def test_discipline_kinds_registered_correctly():
    for kind in ("discipline_start", "discipline_push", "discipline_shame",
                 "discipline_checkin"):
        assert kind in pol.KIND_DATASETS
        assert kind in pol.QUIET_BYPASS_KINDS
    assert pol.KIND_DATASETS["discipline_start"] == ()
    assert pol.KIND_DATASETS["discipline_push"] == ()
    assert pol.KIND_DATASETS["discipline_shame"] == ()
    assert pol.KIND_DATASETS["discipline_checkin"] == ("ledger",)
    # C4 trap guard: start/push/shame depend on the local template only and
    # must NOT be data-gated (that would silently disable them whenever the
    # coaching cache is not fresh). Only checkin is ledger-freshness gated.
    for kind in ("discipline_start", "discipline_push", "discipline_shame"):
        assert kind not in pol.DATA_GATED_KINDS
    assert "discipline_checkin" in pol.DATA_GATED_KINDS
    assert pol.KIND_COOLDOWN_MIN["discipline_start"] == 0
    assert pol.KIND_COOLDOWN_MIN["discipline_push"] == 10
    assert pol.KIND_COOLDOWN_MIN["discipline_shame"] == 10
    assert pol.KIND_COOLDOWN_MIN["discipline_checkin"] == 60


def test_discipline_kind_bypasses_quiet_hours_but_planning_does_not(db):
    discipline = pol.decide_notification(
        kind="discipline_start", now=DISC_NIGHT, db_path=db,
    )
    assert discipline["allow"] is True
    assert "quiet_hours" not in discipline["blocked_by"]

    planning = pol.decide_notification(
        kind="planning", now=DISC_NIGHT, db_path=db,
    )
    assert planning["allow"] is False
    assert "quiet_hours" in planning["blocked_by"]


def test_discipline_start_allowed_when_coaching_cache_never_synced(db):
    decided = pol.decide_notification(kind="discipline_start", now=NOW, db_path=db)
    assert decided["allow"] is True
    assert "stale_data" not in decided["blocked_by"]
    assert decided["relevance"]["stale_datasets"] == []


def test_discipline_checkin_blocked_when_ledger_stale(db):
    sync_meta(db, "ledger", completed="2026-07-30T00:00:00+00:00")
    decided = pol.decide_notification(kind="discipline_checkin", now=NOW, db_path=db)
    assert decided["allow"] is False
    assert "stale_data" in decided["blocked_by"]
    assert "ledger=stale" in decided["relevance"]["stale_datasets"]


def test_discipline_checkin_allowed_with_fresh_ledger_inside_quiet_hours(db):
    sync_meta(db, "ledger", completed="2026-08-02T22:55:00+00:00")
    decided = pol.decide_notification(
        kind="discipline_checkin", now=DISC_NIGHT, db_path=db,
    )
    assert decided["allow"] is True
    assert "quiet_hours" not in decided["blocked_by"]
    assert decided["relevance"]["data_fresh"] is True


def test_discipline_kinds_still_respect_cooldown(db):
    first = pol.decide_notification(
        kind="discipline_push", now=NOW, event_key="p1", db_path=db,
    )
    assert first["allow"] is True
    pol.record_decision(first, db_path=db)
    second = pol.decide_notification(
        kind="discipline_push", now=NOW + dt.timedelta(minutes=5),
        event_key="p2", db_path=db,
    )
    assert second["allow"] is False
    assert "cooldown" in second["blocked_by"]
    assert second["cooldown"]["min_gap_min"] == 10


def test_discipline_kinds_still_respect_daily_budget(db):
    sync_meta(db, "ledger", completed="2026-08-02T11:55:00+00:00")
    now = NOW
    for index in range(3):
        decision = pol.decide_notification(
            kind="discipline_checkin", now=now, event_key=f"c{index}",
            chat_id=1, db_path=db, cooldown_min=0, budget_per_day=3,
        )
        assert decision["allow"] is True
        pol.record_decision(decision, db_path=db)
    over = pol.decide_notification(
        kind="discipline_checkin", now=now + dt.timedelta(hours=1),
        event_key="c_over", chat_id=1, db_path=db, cooldown_min=0,
        budget_per_day=3,
    )
    assert over["allow"] is False
    assert "budget" in over["blocked_by"]


def test_budget_param_30_respected_for_planning(db):
    now = NOW
    for index in range(30):
        decision = pol.decide_notification(
            kind="planning", now=now, event_key=f"b{index}",
            chat_id=1, db_path=db, cooldown_min=0, budget_per_day=30,
        )
        assert decision["allow"] is True
        pol.record_decision(decision, db_path=db)
    over = pol.decide_notification(
        kind="planning", now=now + dt.timedelta(hours=1), event_key="b_over",
        chat_id=1, db_path=db, cooldown_min=0, budget_per_day=30,
    )
    assert over["allow"] is False
    assert "budget" in over["blocked_by"]
    assert over["budget"]["per_day"] == 30


def test_default_budget_12_still_caps_planning(db):
    now = NOW
    for index in range(pol.NOTIFICATIONS_MAX_PER_DAY):
        decision = pol.decide_notification(
            kind="planning", now=now, event_key=f"d{index}",
            chat_id=1, db_path=db, cooldown_min=0,
        )
        assert decision["allow"] is True
        pol.record_decision(decision, db_path=db)
    over = pol.decide_notification(
        kind="planning", now=now + dt.timedelta(hours=1), event_key="d_over",
        chat_id=1, db_path=db, cooldown_min=0,
    )
    assert over["allow"] is False
    assert "budget" in over["blocked_by"]
    assert over["budget"]["per_day"] == pol.NOTIFICATIONS_MAX_PER_DAY


# ---------------------------------------------------------------------------
# backlog escalation (Phase 12)
# ---------------------------------------------------------------------------

def test_backlog_normal_level_with_metrics(db):
    add_backlog(db, "b1", created="2026-07-20T00:00:00+00:00", estimated_min=45)
    result = pol.backlog_escalation(today="2026-08-02", db_path=db)
    assert result["level"] == "normal"
    assert result["metrics"]["count"] == 1
    assert result["metrics"]["estimated_minutes"] == 45
    assert result["metrics"]["estimated_hours"] == 0.8
    assert result["metrics"]["age_days"]["oldest_days"] == 13
    assert result["escalation"]["suggested_minutes"] == 0
    assert result["escalation"]["no_automatic_plan_write"] is True
    assert result["sends_nothing"] is True
    assert result["policy"] == "backlog_escalation"
    assert "no escalation" in result["recommendation"].lower()


def test_backlog_growing_from_3day_net_growth(db):
    add_backlog(db, "b1", created="2026-07-31T00:00:00+00:00")
    add_backlog(db, "b2", created="2026-08-01T00:00:00+00:00")
    result = pol.backlog_escalation(today="2026-08-02", db_path=db)
    assert result["level"] == "growing"
    assert result["metrics"]["growth_3d"]["net"] == 2
    assert result["metrics"]["growth_7d"]["net"] == 2
    assert result["escalation"]["candidate_minutes"] == 15
    assert result["escalation"]["suggested_minutes"] == 15


def test_backlog_critical_by_count(db):
    for index in range(13):
        add_backlog(db, f"b{index}", created="2026-07-20T00:00:00+00:00")
    result = pol.backlog_escalation(today="2026-08-02", db_path=db)
    assert result["level"] == "critical"
    assert result["escalation"]["candidate_minutes"] == 30


def test_backlog_impossible_when_hours_exceed_week_horizon(db):
    add_backlog(db, "b1", created="2026-07-01T00:00:00+00:00", estimated_min=5000)
    result = pol.backlog_escalation(today="2026-08-02", db_path=db)
    assert result["level"] == "impossible"
    assert result["escalation"]["suggested_minutes"] == 0
    assert "Re-scope" in result["recommendation"]


def test_escalation_bounded_by_max_daily_committed_minutes(db):
    for index in range(13):
        add_backlog(db, f"b{index}", created="2026-07-20T00:00:00+00:00")
    insert(
        db, "goals", notion_page_id="g1", title="Study time",
        goal_type="Duration", period="Daily", status="Active", target=595,
    )
    result = pol.backlog_escalation(today="2026-08-02", db_path=db)
    assert result["level"] == "critical"
    suggested = result["escalation"]["suggested_minutes"]
    max_daily = result["plan"]["max_daily_minutes"]
    committed = result["plan"]["committed_minutes"]
    assert suggested <= max_daily - committed
    assert result["escalation"]["respects_max_daily_minutes"] is True
    assert "max_daily_committed_minutes" in result["escalation"]["bounded_by"]


def test_escalation_bounded_by_day_window_when_plan_full(db):
    for index in range(13):
        add_backlog(db, f"b{index}", created="2026-07-20T00:00:00+00:00")
    insert(
        db, "daily_plan", notion_page_id="p1", title="Full day",
        plan_date="2026-08-02", sequence=1, status="Planned",
        estimated_min=850, expected_cy=200,
    )
    result = pol.backlog_escalation(today="2026-08-02", db_path=db)
    suggested = result["escalation"]["suggested_minutes"]
    assert suggested == 0
    assert result["escalation"]["respects_day_window"] is True
    assert "day_window" in result["escalation"]["bounded_by"]


def test_escalation_plan_section_and_no_writes(db):
    add_backlog(db, "b1", created="2026-07-30T00:00:00+00:00")
    insert(
        db, "daily_plan", notion_page_id="p1", title="Plan A",
        plan_date="2026-08-01", sequence=1, status="Completed", estimated_min=60,
    )
    insert(
        db, "daily_plan", notion_page_id="p2", title="Plan B",
        plan_date="2026-08-01", sequence=2, status="Completed", estimated_min=60,
    )
    insert(
        db, "daily_plan", notion_page_id="p3", title="Plan C",
        plan_date="2026-08-01", sequence=3, status="Skipped", estimated_min=60,
    )
    with sqlite3.connect(db) as conn:
        before = conn.execute("SELECT COUNT(*) FROM op_daily_plan").fetchone()[0]
    result = pol.backlog_escalation(today="2026-08-02", db_path=db)
    assert result["plan"]["unplanned_backlog_count"] == 1
    assert result["plan"]["coverage_risk"] == "medium"
    assert result["plan"]["adherence_pct"] == 67
    assert result["plan"]["verified_days"] == 1
    with sqlite3.connect(db) as conn:
        after = conn.execute("SELECT COUNT(*) FROM op_daily_plan").fetchone()[0]
    assert before == after == 3  # never writes a plan


def test_escalation_no_evidence_growth_is_none(db):
    add_backlog(db, "b1", created=None, estimated_min=45)
    result = pol.backlog_escalation(today="2026-08-02", db_path=db)
    assert result["metrics"]["growth_3d"] is None
    assert result["metrics"]["growth_7d"] is None
    assert result["metrics"]["age_days"]["oldest_days"] is None
    assert result["level"] == "normal"


def test_escalation_deterministic(db):
    add_backlog(db, "b1", created="2026-07-30T00:00:00+00:00")
    first = pol.backlog_escalation(today="2026-08-02", db_path=db)
    second = pol.backlog_escalation(today="2026-08-02", db_path=db)
    assert first == second


def main() -> int:
    import sys

    import pytest

    return pytest.main([__file__, "-q"])


if __name__ == "__main__":
    sys.exit(main())
