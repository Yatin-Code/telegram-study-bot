"""Integration tests for Phase 9-14 wiring: tools, context privacy, proactive scans.

Offline temp-mirror tests that exercise the new surface end to end:
  * agent read tools for chapter progress, next doubt, doubt interaction begin,
    score prediction and backlog/freshness status
  * the confirmed write tools update_progress and record_doubt_attempt
    (preview → run) — durable doubt writes never bypass confirmation
  * coaching_context privacy: the always-on compact summary surfaces progress
    gaps / next doubt / prediction / escalation / freshness, and sensitive
    values are redacted before they reach a prompt
  * coaching_proactive.scan_candidates: policy-approved decisions only, with
    quiet-hours / stale-data / cooldown suppression and durable record

Usage:
    .venv-test/bin/python -m pytest -q test_coaching_integration.py
"""

from __future__ import annotations

import asyncio
import datetime as dt
import sqlite3
import types

import pytest

import agent_tools
import bot
import coaching_context
import coaching_doubts
import coaching_policy
import coaching_proactive
import coaching_progress
import coaching_syllabus
import execution_discipline as ed
import ntsc_coaching
import operational_store
import reminders
import session_context
import sync

UTC = dt.timezone.utc
DAY = dt.datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
NIGHT = dt.datetime(2026, 8, 2, 23, 30, tzinfo=UTC)


def _conn(path):
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "integration.db"
    with sync.connect(path) as conn:
        sync.init_db(conn)
        operational_store.init_db(conn)
        ntsc_coaching.init_db(conn)
        coaching_doubts.init_db(conn)
        coaching_progress.init_db(conn)
        coaching_prediction_init(conn)
    monkeypatch.setattr(session_context, "local_today_iso", lambda: "2026-08-02")
    monkeypatch.setattr(session_context, "local_now", lambda: DAY)
    return path


def coaching_prediction_init(conn):
    import coaching_prediction
    coaching_prediction.init_db(conn)


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
        "last_synced_at": "2026-08-02T00:00:00+00:00",
        "raw_json": "{}",
    }
    if physical.startswith("op_"):
        base.setdefault("id", base["notion_page_id"])
        base.setdefault("created_time", "2026-07-20T00:00:00+00:00")
        base.setdefault("last_edited_time", "2026-07-20T00:00:00+00:00")
    base.update(values)
    with _conn(path) as conn:
        if physical.startswith("op_"):
            operational_store.init_db(conn)
        cols = ",".join(f'"{key}"' for key in base)
        marks = ",".join("?" for _ in base)
        conn.execute(
            f'INSERT INTO "{physical}" ({cols}) VALUES ({marks})', tuple(base.values())
        )
        conn.commit()


def seed_coaching(db, *, test_date="2026-08-15", syllabus="Physics: Kinematics, Vectors"):
    ntsc_coaching.replace_tests([
        {"id": "t1", "testName": "Weekly Test", "testDateTime": f"{test_date}T09:00:00",
         "courseId": "7", "batch": "B1", "goal": "Test", "syllabus": syllabus},
    ], db_path=db)
    coaching_syllabus.replace_syllabi([
        {"id": "t1", "syllabus": syllabus},
    ], db_path=db)


def seed_fresh(db):
    """Coaching cache + Notion sync are fresh as of DAY (12:00 UTC)."""
    with _conn(db) as conn:
        conn.execute(
            "INSERT INTO coaching_sync_runs (started_at,finished_at,status,datasets,error) "
            "VALUES (?,?,?,?,?)",
            ("2026-08-02T11:00:00+00:00", "2026-08-02T11:30:00+00:00", "success",
             '["profile","classes","tests","results"]', None),
        )
        for key in ("ledger", "doubts", "revision"):
            conn.execute(
                "INSERT OR REPLACE INTO sync_meta "
                "(db_key,last_started_at,last_completed_at,last_row_count,last_error) "
                "VALUES (?,?,?,?,?)",
                (key, "2026-08-02T11:00:00+00:00", "2026-08-02T11:30:00+00:00", 5, None),
            )
        conn.commit()


def seed_results(db):
    with _conn(db) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO coaching_results "
            "(source_id,title,attempt_date,total_marks,maximum_marks,percentile,"
            " correct,incorrect,attempted,unattempted) VALUES (?,?,?,?,?,?,0,0,0,0)",
            ("r1", "Test r1", "2026-07-01", 55.0, 100.0, None),
        )
        conn.execute(
            "INSERT OR REPLACE INTO coaching_results "
            "(source_id,title,attempt_date,total_marks,maximum_marks,percentile,"
            " correct,incorrect,attempted,unattempted) VALUES (?,?,?,?,?,?,0,0,0,0)",
            ("r2", "Test r2", "2026-07-15", 70.0, 100.0, None),
        )
        conn.commit()


def add_doubt(db, *, concept, subject="Physics", page_id="d1"):
    insert(db, "doubts", notion_page_id=page_id, core_concept=concept,
           subject=subject, status="Unresolved",
           workflow_state="Attempting", created_time="2026-07-20T00:00:00+00:00")


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------

def test_get_chapter_progress_read_tool(db):
    seed_coaching(db)
    coaching_progress.upsert_progress({
        "subject": "Physics", "topic": "Kinematics",
        "exercise_done": 10, "exercise_total": 20,
        "verification_source": "self_reported",
    }, db_path=db)
    out = agent_tools.execute_tool("get_chapter_progress", {}, chat_id=1, db_path=db)
    assert not out.get("error"), out
    assert out["generated_with"] == "deterministic"
    assert out["coverage"]["subjects"][0]["subject"] == "Physics"
    assert any("Kinematics" in q["question"] for q in out["missing_questions"])
    assert set(out["freshness"]) == {"coaching", "ledger", "doubts"}


def test_get_next_doubt_read_tool(db):
    add_doubt(db, concept="Projectile motion sign", page_id="d1")
    add_doubt(db, concept="Thermodynamics cycle", page_id="d2")
    out = agent_tools.execute_tool("get_next_doubt", {}, chat_id=1, db_path=db)
    assert not out.get("error"), out
    assert out["open_doubt_count"] == 2
    assert out["next"]["concept"] in ("Projectile motion sign", "Thermodynamics cycle")
    assert len(out["queue"]) == 2
    assert out["next"]["confidence"] in ("high", "medium", "low")


def test_get_doubt_interaction_begins_state(db):
    add_doubt(db, concept="Moment of inertia", page_id="d1")
    out = agent_tools.execute_tool(
        "get_doubt_interaction", {"doubt_id": "d1"}, chat_id=7, db_path=db,
    )
    assert not out.get("error"), out
    assert out["session_id"]
    assert out["state"] == "shown"
    assert "d1" == out["doubt_id"]
    assert "record_doubt_attempt" in out["note"]  # durable writes stay confirmed


def test_get_score_prediction_read_tool(db):
    seed_coaching(db)
    seed_results(db)
    out = agent_tools.execute_tool("get_score_prediction", {}, chat_id=1, db_path=db)
    assert not out.get("error"), out
    assert out["status"] == "ok"
    assert out["confidence"] in ("high", "medium", "low")
    total = out["total"]["pct"]
    assert total["conservative"] <= total["likely_low"] <= total["likely_high"] <= total["stretch"]
    assert "No rank" in out["rank_statement"]
    assert out["test_title"] == "Weekly Test"


def test_get_backlog_status_read_tool(db):
    insert(db, "work_items", notion_page_id="w1", title="DPP backlog",
           kind="Backlog", status="Backlog", estimated_min=45,
           created_time="2026-08-01T00:00:00+00:00")
    out = agent_tools.execute_tool("get_backlog_status", {}, chat_id=1, db_path=db)
    assert not out.get("error"), out
    assert out["level"] in ("normal", "growing", "critical", "impossible")
    assert out["metrics"]["count"] >= 1
    assert set(out["freshness"]) == set(coaching_policy.ALL_DATASETS)


# ---------------------------------------------------------------------------
# Confirmed write tools
# ---------------------------------------------------------------------------

def test_update_progress_write_preview_and_confirm(db):
    seed_coaching(db)
    prep = agent_tools.prepare_write(
        "update_progress",
        {"subject": "Physics", "topic": "Kinematics",
         "exercise_done": 5, "exercise_total": 20,
         "verification_source": "self_reported"},
        chat_id=1, db_path=db,
    )
    assert prep["ok"], prep
    assert "Kinematics" in prep["preview"]
    out = agent_tools.run_prepared_write("update_progress", prep["run"], chat_id=1, db_path=db)
    assert not out.get("error"), out
    assert out["ok"] is True
    rows = coaching_progress.get_progress(db_path=db)
    assert len(rows) == 1
    assert rows[0]["exercise_done"] == 5


def test_update_progress_validation(db):
    bad = agent_tools.prepare_write(
        "update_progress", {"topic": "Kinematics", "confidence": 500},
        chat_id=1, db_path=db,
    )
    assert not bad["ok"]
    assert "subject" in bad["error"]
    bad2 = agent_tools.prepare_write(
        "update_progress",
        {"subject": "Physics", "topic": "Kinematics", "pyq_done": 3, "pyq_total": 2},
        chat_id=1, db_path=db,
    )
    assert not bad2["ok"]


def test_record_doubt_attempt_is_confirmed_and_durable(db):
    add_doubt(db, concept="Relative velocity", page_id="d1")
    prep = agent_tools.prepare_write(
        "record_doubt_attempt",
        {"doubt": "relative velocity", "duration_min": 15,
         "approach": "drew the vectors", "stuck_point": "sign of the sum",
         "outcome": "Unsolved"},
        chat_id=1, db_path=db,
    )
    assert prep["ok"], prep
    assert "duration_min" in prep["preview"]
    out = agent_tools.run_prepared_write("record_doubt_attempt", prep["run"], chat_id=1, db_path=db)
    # record_doubt_attempt writes the op_doubt_attempts row locally; Notion
    # summary updates may be pending, but the durable attempt must exist.
    assert not out.get("error"), out
    assert out.get("attempt_no") == 1
    rows = operational_store.rows("doubt_attempts", "archived=0", db_path=db)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "Unsolved"


def test_record_doubt_attempt_bad_args_rejected(db):
    bad = agent_tools.prepare_write(
        "record_doubt_attempt",
        {"doubt": "no such doubt", "duration_min": 5,
         "approach": "x", "stuck_point": "y"},
        chat_id=1, db_path=db,
    )
    assert not bad["ok"]
    assert "no doubt matches" in bad["error"]
    bad2 = agent_tools.prepare_write(
        "record_doubt_attempt",
        {"doubt": "relative velocity", "duration_min": 0,
         "approach": "x", "stuck_point": "y"},
        chat_id=1, db_path=db,
    )
    assert not bad2["ok"]


def test_registry_covers_new_tools():
    spec_names = {s["name"] for s in agent_tools.TOOL_SPECS}
    assert spec_names == agent_tools.WRITE_TOOLS | agent_tools.READ_TOOLS
    for name in ("update_progress", "record_doubt_attempt"):
        assert name in agent_tools.WRITE_TOOLS
        assert name in agent_tools._PREP_HANDLERS
        assert name in agent_tools._RUN_HANDLERS
    for name in ("get_chapter_progress", "get_next_doubt", "get_doubt_interaction",
                 "get_score_prediction", "get_backlog_status"):
        assert name in agent_tools.READ_TOOLS


# ---------------------------------------------------------------------------
# Context privacy (always-on compact summary)
# ---------------------------------------------------------------------------

def test_context_compact_surfaces_phase9_to_14_signals(db):
    seed_coaching(db)
    seed_results(db)
    seed_fresh(db)
    add_doubt(db, concept="Kinematics friction edge case", page_id="d1")
    coaching_progress.upsert_progress({
        "subject": "Physics", "topic": "Vectors",
        "exercise_done": 2, "exercise_total": 10,
        "verification_source": "self_reported",
    }, db_path=db)
    text = coaching_context.render_compact(chat_id=1, db_path=db)
    assert "Local date" in text
    assert "Progress gaps" in text
    assert "Next doubt" in text
    assert "Score projection" in text
    assert "Backlog" in text
    assert "Data freshness" in text
    assert "coaching=fresh" in text


def test_context_privacy_redacts_sensitive_values(db):
    seed_coaching(db)
    seed_results(db)
    seed_fresh(db)
    # A doubt whose concept embeds a parent phone number must be redacted
    # before it reaches any prompt.
    add_doubt(db, concept="Call tutor at +91 98765 43210 about capacitors", page_id="d1")
    text = coaching_context.render_compact(chat_id=1, db_path=db)
    assert "98765" not in text
    assert coaching_policy.REDACTED in text
    # Study numbers and dates survive.
    assert "2026-08-02" in text
    assert "Weekly Test" in text


# ---------------------------------------------------------------------------
# Proactive scan: policy-approved decisions only
# ---------------------------------------------------------------------------

def test_proactive_scan_suppresses_quiet_hours(db):
    seed_coaching(db)
    seed_fresh(db)
    coaching_progress.upsert_progress({
        "subject": "Physics", "topic": "Kinematics",
        "verification_source": "unknown",
    }, db_path=db)
    candidates = coaching_proactive.scan_candidates(now=NIGHT, chat_id=1, db_path=db)
    assert candidates == []
    # The blocked decisions were still recorded (audit trail, no sends).
    with _conn(db) as conn:
        rows = conn.execute(
            f"SELECT * FROM {coaching_policy.DECISIONS_TABLE}"
        ).fetchall()
    assert rows
    assert all(not r["allow"] for r in rows)


def test_proactive_scan_approved_when_fresh_and_daytime(db):
    seed_coaching(db, test_date="2026-08-05")
    seed_fresh(db)
    seed_results(db)
    coaching_progress.upsert_progress({
        "subject": "Physics", "topic": "Kinematics",
        "verification_source": "self_reported",
        "exercise_done": 5, "exercise_total": 10,
    }, db_path=db)
    candidates = coaching_proactive.scan_candidates(now=DAY, chat_id=1, db_path=db)
    kinds = {c["kind"] for c in candidates}
    assert "coaching_progress" in kinds
    assert "readiness" in kinds
    for c in candidates:
        assert c["allow"] is True
        assert c["blocked_by"] == []
        assert c["decision"]["allow"] is True


def test_proactive_scan_stale_data_suppresses_gated_kinds(db):
    # No coaching_sync_runs / sync_meta → coaching+doubts are never_synced,
    # so the data-gated kinds (coaching_progress, readiness) must be suppressed.
    seed_coaching(db, test_date="2026-08-05")
    seed_results(db)
    coaching_progress.upsert_progress({
        "subject": "Physics", "topic": "Kinematics",
        "verification_source": "self_reported",
        "exercise_done": 5, "exercise_total": 10,
    }, db_path=db)
    candidates = coaching_proactive.scan_candidates(now=DAY, chat_id=1, db_path=db)
    kinds = {c["kind"] for c in candidates}
    assert "coaching_progress" not in kinds
    assert "readiness" not in kinds
    # The decisions were recorded with stale_data blocked.
    with _conn(db) as conn:
        rows = conn.execute(
            f"SELECT * FROM {coaching_policy.DECISIONS_TABLE}"
        ).fetchall()
    blocked_kinds = {r["kind"] for r in rows if not r["allow"]}
    assert "coaching_progress" in blocked_kinds


def test_proactive_scan_cooldown_and_claim_dedup(db):
    seed_coaching(db, test_date="2026-08-05")
    seed_fresh(db)
    seed_results(db)
    coaching_progress.upsert_progress({
        "subject": "Physics", "topic": "Kinematics",
        "verification_source": "self_reported",
        "exercise_done": 5, "exercise_total": 10,
    }, db_path=db)
    first = coaching_proactive.scan_candidates(now=DAY, chat_id=1, db_path=db)
    assert first
    # The bot claims each approved event key; a second claim is a no-op.
    for candidate in first:
        assert reminders.claim(candidate["event_key"], db_path=db) is True
        assert reminders.claim(candidate["event_key"], db_path=db) is False
    # Cooldown: re-scanning shortly after must not approve the same kind again.
    soon = DAY + dt.timedelta(minutes=5)
    again = coaching_proactive.scan_candidates(now=soon, chat_id=1, db_path=db)
    assert again == []


# ---------------------------------------------------------------------------
# Execution-discipline wiring (todo 7): scan + inline callback
# ---------------------------------------------------------------------------

_TZ = session_context.local_now().tzinfo


def _at(hour, minute):
    return dt.datetime(2026, 8, 2, hour, minute, tzinfo=_TZ)


def _seed_discipline(db, *, day_type="coaching"):
    ed.seed_templates(db_path=db)
    with _conn(db) as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO {ed.DAY_TYPES_TABLE} "
            "(local_date, day_type, resolved_at) VALUES (?, ?, ?)",
            ("2026-08-02", day_type, "2026-08-02T12:00:00+00:00"),
        )
        conn.commit()


def _decision_rows(db):
    with _conn(db) as conn:
        return conn.execute(
            f"SELECT * FROM {coaching_policy.DECISIONS_TABLE}"
        ).fetchall()


async def _run_discipline_scan(db, now, monkeypatch):
    sent = []

    async def fake_send(_bot, chat_id, text, **_kw):
        sent.append((chat_id, text))

    async def noop_sync(**_kw):
        return {}

    monkeypatch.setattr(bot, "_send_markdown", fake_send)
    monkeypatch.setattr(bot.sync, "sync_once_locked", noop_sync)
    monkeypatch.setattr(bot, "telegram_allowed_user_id", lambda: 1)
    monkeypatch.setattr(ed, "_llm_complete", lambda messages: "coach text")
    monkeypatch.setattr(session_context, "local_now", lambda: now)
    monkeypatch.setattr(ed, "DEFAULT_DB_PATH", db)
    context = types.SimpleNamespace(bot=types.SimpleNamespace())
    await bot._execution_discipline_scan(context)
    return sent


def test_discipline_scan_sends_one_claimed_and_records(db, monkeypatch):
    _seed_discipline(db)
    sent = asyncio.run(_run_discipline_scan(db, _at(8, 30), monkeypatch))
    assert len(sent) == 1
    assert sent[0][1] == "coach text"
    rows = _decision_rows(db)
    assert len(rows) == 1
    assert rows[0]["allow"] == 1
    assert rows[0]["kind"] == "discipline_start"


def test_discipline_scan_second_scan_sends_nothing_no_double_record(db, monkeypatch):
    _seed_discipline(db)
    sent = asyncio.run(_run_discipline_scan(db, _at(8, 30), monkeypatch))
    assert len(sent) == 1
    sent2 = asyncio.run(_run_discipline_scan(db, _at(8, 30), monkeypatch))
    assert sent2 == []
    # The same decision_key is INSERT OR REPLACE'd, so only one row remains.
    assert len(_decision_rows(db)) == 1


def test_discipline_scan_budget_blocks_31st(db, monkeypatch):
    _seed_discipline(db)
    for i in range(30):
        decision = coaching_policy.decide_notification(
            kind="discipline_start",
            now=_at(8, 0),
            event_key=f"discipline:2026-08-02:coach_b02_exec_a:start:{i}",
            chat_id=1,
            db_path=db,
            budget_per_day=30,
        )
        coaching_policy.record_decision(decision, db_path=db)
    sent = asyncio.run(_run_discipline_scan(db, _at(8, 30), monkeypatch))
    assert sent == []
    blocked = [r for r in _decision_rows(db) if not r["allow"]]
    assert blocked
    assert all("budget" in (r["blocked_by"] or "") for r in blocked)


def test_discipline_scan_sleep_block_emits_nothing(db, monkeypatch):
    _seed_discipline(db)
    sent = asyncio.run(_run_discipline_scan(db, _at(2, 0), monkeypatch))
    assert sent == []


def _fake_query(data, edits):
    class Query:
        def __init__(self):
            self.data = data

        async def answer(self):
            pass

        async def edit_message_text(self, text, **_kw):
            edits.append(text)

    return Query()


def test_discipline_callback_start_records_and_edits(db, monkeypatch):
    _seed_discipline(db)
    monkeypatch.setattr(ed, "DEFAULT_DB_PATH", db)
    edits = []
    query = _fake_query("discipline:start:2026-08-02:coach_b02_exec_a", edits)
    update = types.SimpleNamespace(callback_query=query)
    asyncio.run(bot.on_discipline_callback(update, types.SimpleNamespace()))
    assert ed.get_state("2026-08-02", "coach_b02_exec_a", db)["status"] == "started"
    assert edits and "time to start" in edits[0]


def test_discipline_callback_start_on_skipped_shows_skipped(db, monkeypatch):
    _seed_discipline(db)
    monkeypatch.setattr(ed, "DEFAULT_DB_PATH", db)
    ed.confirm_skip("2026-08-02", "coach_b02_exec_a", db)
    edits = []
    query = _fake_query("discipline:start:2026-08-02:coach_b02_exec_a", edits)
    update = types.SimpleNamespace(callback_query=query)
    asyncio.run(bot.on_discipline_callback(update, types.SimpleNamespace()))
    assert edits and "skipped" in edits[0]
    assert "time to start" not in edits[0]


def test_discipline_callback_skip_records_skipped(db, monkeypatch):
    _seed_discipline(db)
    monkeypatch.setattr(ed, "DEFAULT_DB_PATH", db)
    edits = []
    query = _fake_query("discipline:skip:2026-08-02:coach_b02_exec_a", edits)
    update = types.SimpleNamespace(callback_query=query)
    asyncio.run(bot.on_discipline_callback(update, types.SimpleNamespace()))
    assert ed.get_state("2026-08-02", "coach_b02_exec_a", db)["status"] == "skipped"
    assert edits and "skipped" in edits[0]


def main() -> int:
    import sys
    import pytest as _pytest
    return _pytest.main([__file__, "-q"])


if __name__ == "__main__":
    sys.exit(main())
