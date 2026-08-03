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
from pathlib import Path

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
import study_domain
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


def _seed_coaching_day(db):
    """Seed templates + a real coaching day via the actual resolution path.

    A coaching class for the date plus a SUCCESS coaching_sync_runs row whose
    finished_at is recent enough that ``coaching_lifecycle.fresh`` is True, so
    ``day_type_for`` resolves to 'coaching' from real data (not a direct row).
    """
    ed.seed_templates(db_path=db)
    ntsc_coaching.replace_classes([{
        "classDate": "2026-08-02", "startTime": "09:00", "duration": 60,
        "classType": "Regular Class", "subjects": "Physics",
    }], db_path=db)
    with _conn(db) as conn:
        conn.execute(
            "INSERT INTO coaching_sync_runs (started_at,finished_at,status,datasets,error) "
            "VALUES (?,?,?,?,?)",
            ("2026-08-02T07:30:00+00:00", "2026-08-02T08:00:00+00:00", "success",
             '["profile","classes","tests","results"]', None),
        )
        for key in ("ledger", "doubts", "revision"):
            conn.execute(
                "INSERT OR REPLACE INTO sync_meta "
                "(db_key,last_started_at,last_completed_at,last_row_count,last_error) "
                "VALUES (?,?,?,?,?)",
                (key, "2026-08-02T12:00:00+00:00", "2026-08-02T12:14:00+00:00", 5, None),
            )
        conn.commit()


def test_full_day_drive(db, monkeypatch):
    """Drive a fake clock through a full coaching day across three study blocks.

    Covers every discipline path end to end: start-once, confirm via the
    callback, ledger-evidenced auto-completion, the C1 guard (a started block
    with no ledger row yields exactly ONE check-in candidate, claimed once),
    and the unconfirmed escalation ladder (push -> shame -> pending-only
    auto-skip). All offline; _llm_complete and sync_once_locked are stubbed.
    """
    _seed_coaching_day(db)
    assert ed.day_type_for("2026-08-02", db_path=db) == "coaching"
    monkeypatch.setattr(ed, "DEFAULT_DB_PATH", db)

    # --- Block A (08:30-10:00): start once -> confirm -> ledger -> complete.
    sent = asyncio.run(_run_discipline_scan(db, _at(8, 31), monkeypatch))
    assert len(sent) == 1
    assert sent[0][1] == "coach text"
    edits = []
    query = _fake_query("discipline:start:2026-08-02:coach_b02_exec_a", edits)
    asyncio.run(bot.on_discipline_callback(
        types.SimpleNamespace(callback_query=query), types.SimpleNamespace()))
    assert ed.get_state("2026-08-02", "coach_b02_exec_a", db)["status"] == "started"
    insert(db, "ledger", notion_page_id="cached-a",
           created_time="2026-08-02T09:00:00.000+00:00")
    sent = asyncio.run(_run_discipline_scan(db, _at(10, 15), monkeypatch))
    assert sent == []
    assert ed.get_state("2026-08-02", "coach_b02_exec_a", db)["status"] == "completed"

    # --- Block B (10:30-12:00): started, no ledger -> checkin claimed once.
    query = _fake_query("discipline:start:2026-08-02:coach_b04_exec_b", edits)
    asyncio.run(bot.on_discipline_callback(
        types.SimpleNamespace(callback_query=query), types.SimpleNamespace()))
    assert ed.get_state("2026-08-02", "coach_b04_exec_b", db)["status"] == "started"

    # --- Acquisition Block (12:00-14:00): unconfirmed escalation ladder.
    sent = asyncio.run(_run_discipline_scan(db, _at(12, 1), monkeypatch))
    assert len(sent) == 1
    sent = asyncio.run(_run_discipline_scan(db, _at(12, 11), monkeypatch))
    assert len(sent) == 1
    # Block B checkin is due at 12:15 (15 min after its 12:00 end); the
    # Acquisition start/push are already claimed, so exactly the checkin sends.
    sent = asyncio.run(_run_discipline_scan(db, _at(12, 15), monkeypatch))
    assert len(sent) == 1
    assert sent[0][1] == "coach text"
    sent = asyncio.run(_run_discipline_scan(db, _at(12, 16), monkeypatch))
    assert sent == []
    sent = asyncio.run(_run_discipline_scan(db, _at(12, 21), monkeypatch))
    assert len(sent) == 1
    sent = asyncio.run(_run_discipline_scan(db, _at(12, 26), monkeypatch))
    assert sent == []
    assert ed.get_state("2026-08-02", "coach_b05_acquisition", db)["status"] == "skipped"

    # --- Final block_confirmations state rows.
    assert ed.get_state("2026-08-02", "coach_b02_exec_a", db)["status"] == "completed"
    assert ed.get_state("2026-08-02", "coach_b04_exec_b", db)["status"] == "started"
    assert ed.get_state("2026-08-02", "coach_b05_acquisition", db)["status"] == "skipped"


# ---------------------------------------------------------------------------
# Startup registration smoke test (todo 8): source-only, offline
# ---------------------------------------------------------------------------

_BOT_SOURCE = Path(bot.__file__).read_text(encoding="utf-8")


def test_discipline_job_registered_once_in_post_init():
    assert _BOT_SOURCE.count('name="execution_discipline_scan"') == 1
    assert _BOT_SOURCE.count("_guard_scheduled(_execution_discipline_scan)") == 1
    # The job is a run_repeating with interval=60, inside the job_queue guard.
    assert "run_repeating(\n            _guard_scheduled(_execution_discipline_scan),\n            interval=60," in _BOT_SOURCE
    # It sits inside the `if application.job_queue is not None:` block.
    job_guard = _BOT_SOURCE.index("if application.job_queue is not None:")
    job_site = _BOT_SOURCE.index("_guard_scheduled(_execution_discipline_scan)")
    assert job_site > job_guard


def test_ntsc_sync_uses_dedicated_lock_distinct_from_discipline():
    """Bug #7: a slow portal sync must never starve the discipline scans."""
    context = types.SimpleNamespace(
        application=types.SimpleNamespace(bot_data={})
    )
    maintenance = bot._maintenance_lock(context)
    ntsc = bot._ntsc_sync_lock(context)
    assert ntsc is not maintenance
    assert bot._NTSC_SYNC_LOCK_KEY != bot._MAINTENANCE_LOCK_KEY
    assert bot._NTSC_SYNC_LOCK_KEY in context.application.bot_data
    # The NTSC job is registered under its own guard, not the shared one.
    assert _BOT_SOURCE.count("_guard_ntsc_sync(_periodic_ntsc_sync)") == 1
    assert "_guard_scheduled(_periodic_ntsc_sync)" not in _BOT_SOURCE


def test_discipline_callback_registered_once_and_agent_preserved():
    assert _BOT_SOURCE.count(
        'CallbackQueryHandler(on_discipline_callback, pattern=r"^discipline:")'
    ) == 1
    # The existing ^agent: handler is still registered (no regression).
    assert _BOT_SOURCE.count(
        'CallbackQueryHandler(on_agent_callback, pattern=r"^agent:")'
    ) == 1
    # The discipline handler is added immediately after the agent handler.
    agent_site = _BOT_SOURCE.index(
        'CallbackQueryHandler(on_agent_callback, pattern=r"^agent:")'
    )
    discipline_site = _BOT_SOURCE.index(
        'CallbackQueryHandler(on_discipline_callback, pattern=r"^discipline:")'
    )
    assert discipline_site > agent_site


# ---------------------------------------------------------------------------
# C6 mock-prep proposal + confirm-to-write (todo 8)
# ---------------------------------------------------------------------------


def _readiness_snapshot():
    """Minimal exam-readiness snapshot; enough for the message template."""
    return {
        "exam": {"notion_page_id": "exam-x", "title": "Mock X",
                 "exam_date": "2026-08-04T09:00:00+00:00", "status": "Planned"},
        "phase": "t3", "days_until": 2,
        "doubts": [], "revision": [], "key_points": [],
        "syllabus_known": False, "excluded_doubts": [],
        "zero_attempt_count": 0, "teacher_ready_count": 0,
        "scope_uncertain_count": 0, "exam_id": "exam-x",
    }


def test_exam_reminder_scan_still_sends_readiness_reviews(db, monkeypatch):
    """Baseline (todo 8): the scan still fires readiness reviews unchanged.

    ``_exam_reminder_scan`` must keep driving ``_send_current_readiness_reviews``
    (the readiness review still sends once through ``_send_markdown``) even after
    the mock-prep proposal is added after it.
    """
    import exam_readiness
    sent = []

    async def fake_send(_bot, chat_id, text, **_kw):
        sent.append((chat_id, text))

    async def noop_sync(**_kw):
        return {}

    exam = {"notion_page_id": "exam-x", "title": "Mock X",
            "exam_date": "2026-08-04T09:00:00+00:00", "status": "Planned"}
    snapshot = _readiness_snapshot()
    monkeypatch.setattr(bot, "_send_markdown", fake_send)
    monkeypatch.setattr(bot, "_sync_domain", noop_sync)
    monkeypatch.setattr(bot, "telegram_allowed_user_id", lambda: 1)
    monkeypatch.setattr(bot.reminders, "claim", lambda key, **kw: True)
    monkeypatch.setattr(bot.reminders, "due_exams", lambda: [])
    monkeypatch.setattr(exam_readiness, "scheduled_reviews", lambda: [(exam, "t3")])
    monkeypatch.setattr(exam_readiness, "collect", lambda exam, phase: snapshot)
    context = types.SimpleNamespace(bot=types.SimpleNamespace())
    asyncio.run(bot._exam_reminder_scan(context))
    assert sent, "readiness reviews still fire through the scan"
    assert "Exam readiness" in sent[0][1] or "readiness" in sent[0][1].lower()


def test_mockprep_proposal_wired_into_scan():
    """_mock_prep_proposal is invoked from _exam_reminder_scan after readiness."""
    assert "async def _mock_prep_proposal(" in _BOT_SOURCE
    assert "await _mock_prep_proposal(context)" in _BOT_SOURCE
    scan_site = _BOT_SOURCE.index("async def _exam_reminder_scan")
    call_site = _BOT_SOURCE.index("await _mock_prep_proposal(context)")
    assert call_site > scan_site


def _button_data(markup):
    if markup is None:
        return []
    return [btn.callback_data for row in markup.inline_keyboard for btn in row]


def _two_day_plan():
    """Deterministic 2-day plan dict matching the planner's block shape."""
    return {
        "plan_type": "weekly", "start_date": "2026-08-03", "end_date": "2026-08-04",
        "dates": ["2026-08-03", "2026-08-04"],
        "blocks": [
            {"kind": "Mock Prep", "title": "Mock Prep: Weekly Test", "date": "2026-08-03",
             "start": "08:00", "end": "09:00", "duration_min": 60, "priority": 90,
             "placed": True, "reason": "Mock prep for Weekly Test on 2026-08-04",
             "evidence": {}, "id": "mock1", "source": "mock-prep:portal:t1:2026-08-03"},
            {"kind": "Test Prep", "title": "Test prep: Weekly Test", "date": "2026-08-04",
             "start": "08:00", "end": "08:45", "duration_min": 45, "priority": 78,
             "placed": True, "reason": "Prepare for Weekly Test on 2026-08-04",
             "evidence": {}, "id": "tp1", "source": "test-prep:portal:t1:2026-08-04"},
        ],
        "unplaced": [], "warnings": [], "capacity": {},
        "sources": {"mock_prep_blocks": 1, "test_prep_blocks": 1},
        "generated_with": "deterministic", "llm_involved": False,
    }


async def _run_mockprep_proposal(db, monkeypatch, *, fresh=True, score=None,
                                 coverage=None, plan_outcome="ready",
                                 today="2026-08-02"):
    sent = []

    async def fake_send(_bot, chat_id, text, **_kw):
        sent.append((chat_id, text, _kw.get("reply_markup")))

    import coaching_lifecycle
    import coaching_prediction
    import study_domain
    monkeypatch.setattr(bot, "_send_markdown", fake_send)
    monkeypatch.setattr(bot, "telegram_allowed_user_id", lambda: 1)
    monkeypatch.setattr(ed, "DEFAULT_DB_PATH", db)
    monkeypatch.setattr(session_context, "local_now", lambda: DAY)
    monkeypatch.setattr(session_context, "local_today_iso", lambda: today)
    monkeypatch.setattr(coaching_lifecycle, "fresh", lambda **kw: fresh)
    monkeypatch.setattr(
        coaching_prediction, "project_coaching_score",
        lambda **kw: score if score is not None
        else {"confidence": "high", "evidence_count": 4},
    )
    monkeypatch.setattr(
        coaching_syllabus, "coverage_snapshot",
        lambda **kw: coverage if coverage is not None
        else [{"test_date": "2026-08-04", "coverage": {"known": True}}],
    )
    monkeypatch.setattr(
        study_domain, "plan_facts",
        lambda date, **kw: {"outcome": plan_outcome},
    )
    monkeypatch.setattr(ed, "day_type_for", lambda date, **kw: "coaching")
    context = types.SimpleNamespace(bot=types.SimpleNamespace())
    await bot._mock_prep_proposal(context)
    return sent


def test_mockprep_gate_pass_sends_one_proposal_claimed_once(db, monkeypatch):
    seed_coaching(db, test_date="2026-08-04")
    sent = asyncio.run(_run_mockprep_proposal(db, monkeypatch))
    assert len(sent) == 1
    assert "2-day Mock Prep" in sent[0][1]
    data = _button_data(sent[0][2])
    assert any(d.startswith("mockprep:confirm:") for d in data)
    assert "mockprep:dismiss" in data
    sent2 = asyncio.run(_run_mockprep_proposal(db, monkeypatch))
    assert sent2 == []


def test_mockprep_gate_fail_missing_chapters_sends_still_need(db, monkeypatch):
    seed_coaching(db, test_date="2026-08-04")
    sent = asyncio.run(_run_mockprep_proposal(
        db, monkeypatch,
        coverage=[{"test_date": "2026-08-04", "coverage": {"known": False}}],
    ))
    assert len(sent) == 1
    assert "I want to plan your 2 days before" in sent[0][1]
    assert "chapters" in sent[0][1]
    assert not _button_data(sent[0][2])
    sent2 = asyncio.run(_run_mockprep_proposal(
        db, monkeypatch,
        coverage=[{"test_date": "2026-08-04", "coverage": {"known": False}}],
    ))
    assert sent2 == []


def test_mockprep_gate_fail_stale_portal_lists_portal(db, monkeypatch):
    seed_coaching(db, test_date="2026-08-04")
    sent = asyncio.run(_run_mockprep_proposal(db, monkeypatch, fresh=False))
    assert len(sent) == 1
    assert "I want to plan your 2 days before" in sent[0][1]
    assert "portal" in sent[0][1].lower()


def test_mockprep_confirm_writes_daily_plan_rows(db, monkeypatch):
    seed_coaching(db, test_date="2026-08-04")
    import coaching_planner
    monkeypatch.setattr(coaching_planner, "build_plan", lambda **kw: _two_day_plan())
    sent = asyncio.run(_run_mockprep_proposal(db, monkeypatch))
    assert len(sent) == 1
    edits = []
    query = _fake_query("mockprep:confirm:2026-08-02:2026-08-03", edits)
    update = types.SimpleNamespace(callback_query=query)
    asyncio.run(bot.on_mockprep_callback(update, types.SimpleNamespace()))
    rows = study_domain._rows("daily_plan", "archived=0", db_path=db)
    assert len(rows) == 2
    assert edits and "2-day mock plan written" in edits[0]


def test_mockprep_plans_t2_and_t1_not_exam_day(db, monkeypatch):
    """Bug #5: the 2-day proposal covers [T-2, T-1], never the exam day."""
    seed_coaching(db, test_date="2026-08-16")
    import coaching_planner
    monkeypatch.setattr(coaching_planner, "build_plan", lambda **kw: _two_day_plan())
    sent = asyncio.run(_run_mockprep_proposal(db, monkeypatch, today="2026-08-14"))
    assert len(sent) == 1
    data = _button_data(sent[0][2])
    assert "mockprep:confirm:2026-08-14:2026-08-15" in data
    assert not any("2026-08-16" in d for d in data), (
        "the exam day must never be inside the mock-prep plan window"
    )


def test_mockprep_dismiss_no_rows_and_reply(db, monkeypatch):
    seed_coaching(db, test_date="2026-08-04")
    monkeypatch.setattr(ed, "DEFAULT_DB_PATH", db)
    edits = []
    query = _fake_query("mockprep:dismiss", edits)
    update = types.SimpleNamespace(callback_query=query)
    asyncio.run(bot.on_mockprep_callback(update, types.SimpleNamespace()))
    assert study_domain._rows("daily_plan", "archived=0", db_path=db) == []
    assert edits and "Okay, skipping the pre-mock plan." in edits[0]


def test_mockprep_non_t2_sends_nothing(db, monkeypatch):
    seed_coaching(db, test_date="2026-08-10")
    sent = asyncio.run(_run_mockprep_proposal(db, monkeypatch))
    assert sent == []


def test_mockprep_callback_registered_once_near_discipline():
    assert _BOT_SOURCE.count(
        'CallbackQueryHandler(on_mockprep_callback, pattern=r"^mockprep:")'
    ) == 1
    discipline_site = _BOT_SOURCE.index(
        'CallbackQueryHandler(on_discipline_callback, pattern=r"^discipline:")'
    )
    mockprep_site = _BOT_SOURCE.index(
        'CallbackQueryHandler(on_mockprep_callback, pattern=r"^mockprep:")'
    )
    assert mockprep_site > discipline_site


# ---------------------------------------------------------------------------
# C4 out-of-the-box execution check (todo 5): new behavior (failing-first)
# ---------------------------------------------------------------------------

async def _run_out_of_box_check(db, now, monkeypatch, *, ntsc_raises=False):
    sent = []

    async def fake_send(_bot, chat_id, text, **_kw):
        sent.append((chat_id, text, _kw.get("reply_markup")))

    async def noop_sync(**_kw):
        return {}

    def ntsc_noop():
        return {"status": "ok"}

    def ntsc_fail():
        raise RuntimeError("ntsc down")

    monkeypatch.setattr(bot, "_send_markdown", fake_send)
    monkeypatch.setattr(bot.sync, "sync_once_locked", noop_sync)
    monkeypatch.setattr(
        bot.ntsc_sync, "sync_once", ntsc_noop if not ntsc_raises else ntsc_fail
    )
    monkeypatch.setattr(bot, "telegram_allowed_user_id", lambda: 1)
    monkeypatch.setattr(session_context, "local_now", lambda: now)
    monkeypatch.setattr(ed, "DEFAULT_DB_PATH", db)
    context = types.SimpleNamespace(bot=types.SimpleNamespace())
    await bot._out_of_box_execution_check(1, context)
    return sent


def test_out_of_box_pending_study_block_sends_one_start_with_buttons(db, monkeypatch):
    """(a) At 08:45 on a seeded coaching day with a pending study block the
    check sends exactly ONE 'time to start' message with the discipline
    Started/Skip buttons; a second call sends nothing (claim-dedup)."""
    _seed_discipline(db)
    sent = asyncio.run(_run_out_of_box_check(db, _at(8, 45), monkeypatch))
    assert len(sent) == 1
    assert "time to start" in sent[0][1]
    assert "Execution Block A" in sent[0][1]
    data = _button_data(sent[0][2])
    assert "discipline:start:2026-08-02:coach_b02_exec_a" in data
    assert "discipline:skip:2026-08-02:coach_b02_exec_a" in data
    sent2 = asyncio.run(_run_out_of_box_check(db, _at(8, 45), monkeypatch))
    assert sent2 == []


def test_out_of_box_started_block_sends_mid_block_once(db, monkeypatch):
    """(b) A block already started → 'mid-block' message once."""
    _seed_discipline(db)
    ed.confirm_start("2026-08-02", "coach_b02_exec_a", db)
    sent = asyncio.run(_run_out_of_box_check(db, _at(8, 45), monkeypatch))
    assert len(sent) == 1
    assert "mid-block" in sent[0][1]
    assert "Execution Block A" in sent[0][1]
    sent2 = asyncio.run(_run_out_of_box_check(db, _at(8, 45), monkeypatch))
    assert sent2 == []


def test_out_of_box_sleep_block_sends_nothing(db, monkeypatch):
    """(c) Block kind sleep → nothing (not even the empty-mirror prompt)."""
    _seed_discipline(db)
    insert(db, "work_items", notion_page_id="w1", title="DPP backlog",
           kind="Backlog", status="Backlog")
    sent = asyncio.run(_run_out_of_box_check(db, _at(2, 0), monkeypatch))
    assert sent == []


def test_out_of_box_skipped_block_sends_nothing(db, monkeypatch):
    """A skipped study block → nothing."""
    _seed_discipline(db)
    ed.confirm_skip("2026-08-02", "coach_b02_exec_a", db)
    sent = asyncio.run(_run_out_of_box_check(db, _at(8, 45), monkeypatch))
    assert sent == []


def test_out_of_box_empty_mirror_sends_guidance_once(db, monkeypatch):
    """(d) Empty Notion mirror (no ledger rows and no work items) → the
    one-line 'I don't know anything yet' prompt, once."""
    sent = asyncio.run(_run_out_of_box_check(db, _at(12, 0), monkeypatch))
    assert len(sent) == 1
    assert "I don't know anything yet" in sent[0][1]
    assert "physics kinematics 20q 15c 25min" in sent[0][1]
    sent2 = asyncio.run(_run_out_of_box_check(db, _at(12, 0), monkeypatch))
    assert sent2 == []


def test_out_of_box_ntsc_sync_exception_does_not_propagate(db, monkeypatch):
    """(e) An exception in the best-effort NTSC sync must not propagate and
    must not block the rest of the check (the start message still sends)."""
    _seed_discipline(db)
    sent = asyncio.run(
        _run_out_of_box_check(db, _at(8, 45), monkeypatch, ntsc_raises=True)
    )
    assert len(sent) == 1
    assert "time to start" in sent[0][1]


def test_out_of_box_check_wired_into_start_and_catch_all():
    """The check is wired into /start (after the hub render) and the catch_all
    first-run hint, both behind the incomplete-onboarding gates."""
    assert "async def _out_of_box_execution_check(" in _BOT_SOURCE
    assert _BOT_SOURCE.count(
        "await _out_of_box_execution_check(chat_id, context)"
    ) == 2
    start_site = _BOT_SOURCE.index("async def start(update:")
    start_gate = _BOT_SOURCE.index("if not onboarding.is_complete(chat_id):", start_site)
    start_call = _BOT_SOURCE.index("await _out_of_box_execution_check(chat_id, context)", start_site)
    assert start_call > start_gate
    catch_site = _BOT_SOURCE.index("async def catch_all(update:")
    hint = _BOT_SOURCE.index('reminders.claim("onboarding-hint:v1")', catch_site)
    catch_call = _BOT_SOURCE.rindex("await _out_of_box_execution_check(chat_id, context)", catch_site)
    assert catch_call > hint


def test_start_sends_hub_and_invokes_out_of_box_check(db, monkeypatch):
    """End-to-end /start: incomplete onboarding renders the hub AND invokes the
    out-of-box check (plain empty mirror → the one-line guidance fires)."""
    replies = []
    sent = []

    class Message:
        async def reply_text(self, text, **kw):
            replies.append(text)

    async def fake_send(_bot, chat_id, text, **_kw):
        sent.append((chat_id, text))

    async def noop_sync(**_kw):
        return {}

    def ntsc_noop():
        return {"status": "ok"}

    async def _allowed(_u):
        return False

    update = types.SimpleNamespace(
        effective_message=Message(),
        effective_chat=types.SimpleNamespace(id=42),
    )
    monkeypatch.setattr(bot, "_reject_if_unauthorized", _allowed)
    monkeypatch.setattr(bot.onboarding, "is_complete", lambda chat: False)
    monkeypatch.setattr(bot, "_setup_hub_view", lambda: ("HUB TEXT", None))
    monkeypatch.setattr(bot, "_send_markdown", fake_send)
    monkeypatch.setattr(bot.sync, "sync_once_locked", noop_sync)
    monkeypatch.setattr(bot.ntsc_sync, "sync_once", ntsc_noop)
    monkeypatch.setattr(bot, "telegram_allowed_user_id", lambda: 1)
    monkeypatch.setattr(session_context, "local_now", lambda: DAY)
    monkeypatch.setattr(ed, "DEFAULT_DB_PATH", db)
    asyncio.run(bot.start(update, types.SimpleNamespace(bot=types.SimpleNamespace())))
    assert "HUB TEXT" in replies
    assert len(sent) == 1
    assert "I don't know anything yet" in sent[0][1]


def test_catch_all_first_run_hint_still_works_and_invokes_check(db, monkeypatch):
    """End-to-end catch_all: the onboarding-hint:v1 claim still fires and the
    out-of-box check is invoked right after it."""
    replies = []
    claims = []
    sent = []

    class Message:
        text = "hello"

        async def reply_text(self, text, **kw):
            replies.append(text)

    async def fake_send(_bot, chat_id, text, **_kw):
        sent.append((chat_id, text))

    async def noop_sync(**_kw):
        return {}

    def ntsc_noop():
        return {"status": "ok"}

    async def _allowed(_u):
        return False

    update = types.SimpleNamespace(
        effective_message=Message(),
        effective_chat=types.SimpleNamespace(id=42),
        effective_user=types.SimpleNamespace(id=42),
    )
    monkeypatch.setattr(bot, "_reject_if_unauthorized", _allowed)
    monkeypatch.setattr(bot.reset_service, "pending_confirmation", lambda _chat: None)
    monkeypatch.setattr(bot.exam_readiness, "pending_resolution", lambda _chat: None)
    monkeypatch.setattr(bot.draft_store, "get_pending_doubt_resolution", lambda _chat: None)
    monkeypatch.setattr(bot.draft_store, "get_pending_setting_edit", lambda _chat: None)
    monkeypatch.setattr(bot.user_jobs, "get_pending_edit", lambda _chat: None)
    monkeypatch.setattr(bot.draft_store, "get_session_debrief", lambda _chat: None)
    monkeypatch.setattr(bot.onboarding, "active_section", lambda _chat: None)
    monkeypatch.setattr(bot.onboarding, "is_complete", lambda _chat: False)
    monkeypatch.setattr(bot.draft_store, "get_editing_draft_for_chat", lambda _chat: None)
    monkeypatch.setattr(bot.draft_store, "get_pending_clarification", lambda _chat: None)

    async def _agent(_u, chat, text):
        return None

    monkeypatch.setattr(bot, "_handle_agent_text", _agent)
    monkeypatch.setattr(bot.reminders, "claim", lambda key, **kw: claims.append(key) or True)
    monkeypatch.setattr(bot, "_send_markdown", fake_send)
    monkeypatch.setattr(bot.sync, "sync_once_locked", noop_sync)
    monkeypatch.setattr(bot.ntsc_sync, "sync_once", ntsc_noop)
    monkeypatch.setattr(bot, "telegram_allowed_user_id", lambda: 1)
    monkeypatch.setattr(session_context, "local_now", lambda: DAY)
    monkeypatch.setattr(ed, "DEFAULT_DB_PATH", db)
    asyncio.run(bot.catch_all(update, types.SimpleNamespace(bot=types.SimpleNamespace())))
    assert "onboarding-hint:v1" in claims
    assert any("First time? Run /setup" in r for r in replies)
    assert any(k.startswith("onboarding-oob") for k in claims)
    assert len(sent) == 1
    assert "I don't know anything yet" in sent[0][1]


# ---------------------------------------------------------------------------
# C5 weekly gap-filling scan (todo 6): baseline characterization + new behavior
# ---------------------------------------------------------------------------

GAP_DAY = "2026-08-02"          # Sunday (weekday 6) = the weekly-report weekday
GAP_WEEKDAY = 6


def _gap_status(*, chapters_missing=(), commitments_ok=True, optional_missing=()):
    """Deterministic onboarding.status() dict for the gap-scan tests.

    ``backlog`` is reported ok regardless of the DB — the scan re-checks the DB
    itself (0 Backlog/Inbox work items AND 0 ledger rows). ``optional_missing``
    marks the never-re-asked sections (next_mock, capacity, rhythm, prefs,
    portal) as not ok.
    """
    missing = set(optional_missing)
    return {
        "chapters": {
            "ok": not chapters_missing,
            "missing_subjects": list(chapters_missing),
            "suggested_topics": {},
        },
        "backlog": {"ok": True, "detail": "0 item(s) tracked"},
        "commitments": {
            "ok": commitments_ok,
            "detail": "2 daily commitment(s)" if commitments_ok else "none",
        },
        "next_mock": {"ok": "next_mock" not in missing, "detail": ""},
        "capacity": {"ok": "capacity" not in missing, "detail": ""},
        "rhythm": {"ok": "rhythm" not in missing, "detail": ""},
        "prefs": {"ok": "prefs" not in missing, "detail": ""},
        "portal": {"ok": "portal" not in missing, "detail": ""},
    }


def _claims(db):
    with _conn(db) as conn:
        return [r[0] for r in conn.execute(
            "SELECT event_key FROM reminder_events ORDER BY event_key"
        )]


async def _run_weekly_gap_scan(db, monkeypatch, *, weekday=GAP_WEEKDAY, status=None):
    sent = []

    async def fake_send(_bot, chat_id, text, **_kw):
        sent.append((chat_id, text, _kw.get("reply_markup")))

    def _status(**kw):
        return status

    monkeypatch.setattr(bot, "_send_markdown", fake_send)
    monkeypatch.setattr(bot, "telegram_allowed_user_id", lambda: 1)
    monkeypatch.setattr(
        bot.config_settings, "timetable_reminder_weekday", lambda: weekday
    )
    monkeypatch.setattr(bot.onboarding, "status", _status)
    monkeypatch.setattr(bot.onboarding, "DEFAULT_DB_PATH", db)
    monkeypatch.setattr(
        bot.onboarding, "chapters_prompt",
        lambda **kw: "Which chapter are you currently on in Physics?",
    )
    context = types.SimpleNamespace(bot=types.SimpleNamespace())
    await bot._weekly_gap_scan(context)
    return sent


def test_weekly_report_weekday_gate_pattern_exists():
    """The weekly-report weekday gate pattern is shared by the weekly jobs
    (timetable reminder + report) and the gap scan reuses it."""
    assert "_weekly_timetable_reminder" in _BOT_SOURCE
    assert "_weekly_report_job" in _BOT_SOURCE
    assert "config_settings.timetable_reminder_weekday()" in _BOT_SOURCE


def test_weekly_gap_weekday_missing_chapters_sends_one(db, monkeypatch):
    """(a) On the configured weekday with chapters missing → exactly ONE
    'I still need to know' message carrying an onb:sec:chapters button."""
    sent = asyncio.run(_run_weekly_gap_scan(
        db, monkeypatch, status=_gap_status(chapters_missing=["Physics"]),
    ))
    assert len(sent) == 1
    assert "I still need to know" in sent[0][1]
    assert "Current chapters" in sent[0][1]
    data = _button_data(sent[0][2])
    assert "onb:sec:chapters" in data


def test_weekly_gap_second_call_same_week_sends_nothing(db, monkeypatch):
    """(b) Claim-deduped per (section, week): a second scan the same week sends
    nothing."""
    kw = dict(status=_gap_status(chapters_missing=["Physics"]))
    sent = asyncio.run(_run_weekly_gap_scan(db, monkeypatch, **kw))
    assert len(sent) == 1
    sent2 = asyncio.run(_run_weekly_gap_scan(db, monkeypatch, **kw))
    assert sent2 == []


def test_weekly_gap_non_weekday_sends_nothing(db, monkeypatch):
    """(c) On any other weekday the scan sends nothing."""
    other = (GAP_WEEKDAY + 1) % 7
    sent = asyncio.run(_run_weekly_gap_scan(
        db, monkeypatch, weekday=other,
        status=_gap_status(chapters_missing=["Physics"]),
    ))
    assert sent == []


def test_weekly_gap_backlog_empty_and_no_ledger_sends_backlog(db, monkeypatch):
    """Backlog is picked when chapters are placed but there are 0 Backlog/Inbox
    work items AND 0 ledger rows."""
    sent = asyncio.run(_run_weekly_gap_scan(
        db, monkeypatch, status=_gap_status(),
    ))
    assert len(sent) == 1
    assert "I still need to know" in sent[0][1]
    assert "Backlog" in sent[0][1]
    data = _button_data(sent[0][2])
    assert "onb:sec:backlog" in data


def test_weekly_gap_commitments_missing_sends_commitments(db, monkeypatch):
    """Commitments is picked when chapters + backlog are satisfied but there
    are no active daily commitments."""
    insert(db, "work_items", notion_page_id="w1", title="DPP backlog",
           kind="Backlog", status="Backlog")
    sent = asyncio.run(_run_weekly_gap_scan(
        db, monkeypatch, status=_gap_status(commitments_ok=False),
    ))
    assert len(sent) == 1
    assert "I still need to know" in sent[0][1]
    assert "Daily commitments" in sent[0][1]
    data = _button_data(sent[0][2])
    assert "onb:sec:commitments" in data


def test_weekly_gap_all_satisfied_claims_all_and_sends_nothing(db, monkeypatch):
    """(d) Everything genuinely needed is satisfied → no message and the
    onboarding-gap:all:{date} claim is recorded."""
    insert(db, "work_items", notion_page_id="w1", title="DPP backlog",
           kind="Backlog", status="Backlog")
    sent = asyncio.run(_run_weekly_gap_scan(
        db, monkeypatch, status=_gap_status(),
    ))
    assert sent == []
    assert f"onboarding-gap:all:{GAP_DAY}" in _claims(db)


def test_weekly_gap_optional_sections_never_send(db, monkeypatch):
    """(e) Optional sections (next_mock/capacity/rhythm/prefs/portal) never
    produce messages even when their status is not ok."""
    insert(db, "work_items", notion_page_id="w1", title="DPP backlog",
           kind="Backlog", status="Backlog")
    sent = asyncio.run(_run_weekly_gap_scan(
        db, monkeypatch,
        status=_gap_status(optional_missing=["next_mock", "capacity", "rhythm",
                                             "prefs", "portal"]),
    ))
    assert sent == []
    assert f"onboarding-gap:all:{GAP_DAY}" in _claims(db)


def test_weekly_gap_job_registered_in_post_init():
    """The run_daily job is registered inside the job_queue guard with the
    deterministic 09:30 clock across all weekdays (weekday gate is internal)."""
    assert _BOT_SOURCE.count('name="weekly_gap_scan"') == 1
    assert _BOT_SOURCE.count("_guard_scheduled(_weekly_gap_scan)") == 1
    assert (
        'run_daily(\n            _guard_scheduled(_weekly_gap_scan), time=_clock("09:30"),'
        in _BOT_SOURCE
    )
    job_guard = _BOT_SOURCE.index("if application.job_queue is not None:")
    job_site = _BOT_SOURCE.index('name="weekly_gap_scan"')
    assert job_site > job_guard


def main() -> int:
    import sys
    import pytest as _pytest
    return _pytest.main([__file__, "-q"])


# ---------------------------------------------------------------------------
# Bug #3 regression: claim → send failure must release the claim so the
# notification can be retried (never silently lost). Representative coverage
# over the 8 legacy claim-without-release paths: _planning_reminder,
# _schedule_watch_scan, _exam_reminder_scan (identical pattern for the rest).
# ---------------------------------------------------------------------------

def test_planning_reminder_releases_claim_on_send_failure(db, monkeypatch):
    """A failed planning-reminder send releases ``planning:{today}``."""
    released = []

    async def failing_send(*_a, **_kw):
        raise RuntimeError("network")

    async def noop_sync(**_kw):
        return {}

    monkeypatch.setattr(bot.sync, "sync_once_locked", noop_sync)
    monkeypatch.setattr(bot.reminders, "claim", lambda key, **kw: True)
    monkeypatch.setattr(bot.reminders, "release", lambda key, **kw: released.append(key))
    monkeypatch.setattr(bot.reminders, "planning_message", lambda **kw: "plan")
    monkeypatch.setattr(bot, "telegram_allowed_user_id", lambda: 1)
    context = types.SimpleNamespace(bot=types.SimpleNamespace(send_message=failing_send))
    asyncio.run(bot._planning_reminder(context))
    assert released == [f"planning:{session_context.local_today_iso()}"]


def test_schedule_watch_scan_releases_claim_on_send_failure(db, monkeypatch):
    """A failed schedule-watch send releases the plan-analysis claim."""
    released = []

    async def failing_send(*_a, **_kw):
        raise RuntimeError("network")

    async def noop_sync(**_kw):
        return {}

    change = ("plan-analysis:2026-08-02:2026-08-02T10:00:00", "Today's sequence is ready")
    monkeypatch.setattr(bot.sync, "sync_once_locked", noop_sync)
    monkeypatch.setattr(bot.reminders, "settled_plan_change", lambda **kw: change)
    monkeypatch.setattr(bot.reminders, "claim", lambda key, **kw: True)
    monkeypatch.setattr(bot.reminders, "release", lambda key, **kw: released.append(key))
    monkeypatch.setattr(bot, "telegram_allowed_user_id", lambda: 1)
    context = types.SimpleNamespace(bot=types.SimpleNamespace(send_message=failing_send))
    asyncio.run(bot._schedule_watch_scan(context))
    assert released == [change[0]]


def test_exam_reminder_scan_releases_claim_on_send_failure(db, monkeypatch):
    """A failed exam-finished send releases the exam-finished claim."""
    released = []

    async def failing_send(*_a, **_kw):
        raise RuntimeError("network")

    async def noop(*_a, **_kw):
        return None

    exam = {"notion_page_id": "exam-x", "title": "Mock X",
            "exam_date": "2026-08-04T09:00:00+00:00", "status": "Planned"}
    monkeypatch.setattr(bot, "_send_current_readiness_reviews", noop)
    monkeypatch.setattr(bot, "_mock_prep_proposal", noop)
    monkeypatch.setattr(bot.reminders, "due_exams", lambda: [exam])
    monkeypatch.setattr(bot.reminders, "claim", lambda key, **kw: True)
    monkeypatch.setattr(bot.reminders, "release", lambda key, **kw: released.append(key))
    monkeypatch.setattr(bot, "telegram_allowed_user_id", lambda: 1)
    context = types.SimpleNamespace(bot=types.SimpleNamespace(send_message=failing_send))
    asyncio.run(bot._exam_reminder_scan(context))
    expected = f"exam-finished:exam-x:{session_context.local_today_iso()}"
    assert released == [expected]


if __name__ == "__main__":
    sys.exit(main())
