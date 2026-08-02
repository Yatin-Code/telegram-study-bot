"""Constrained agent tools: validation at preview time + safe execution.

All tests run on isolated tmp databases — no network, no real mirror file.
"""

from __future__ import annotations

import sqlite3

import pytest

import agent_tools
import study_domain


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "mirror.db"
    # Touch the file so read-only connections can open it.
    sqlite3.connect(path).close()
    return path


# ---------------------------------------------------------------------------
# sql_select — hard read-only enforcement
# ---------------------------------------------------------------------------

def test_sql_select_rejects_writes(db):
    out = agent_tools.sql_select("DELETE FROM ledger", db_path=db)
    assert out["error"]
    out = agent_tools.sql_select("INSERT INTO ledger (task) VALUES ('x')", db_path=db)
    assert out["error"]
    out = agent_tools.sql_select("DROP TABLE ledger", db_path=db)
    assert out["error"]
    out = agent_tools.sql_select("SELECT 1; DROP TABLE ledger", db_path=db)
    assert out["error"]


def test_sql_select_runs_select(db):
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (41)")
    out = agent_tools.sql_select("SELECT x FROM t", db_path=db)
    assert out["rows"] == [{"x": 41}]
    assert out["row_count"] == 1


def test_get_schema_hides_legacy_bare_sql_mirrors(db):
    """The agent must never see empty `goals`/`work_items` mirrors — only op_*."""
    with sqlite3.connect(db) as conn:
        for name in ("ledger", "op_goals", "goals", "work_items", "op_work_items"):
            conn.execute(f"CREATE TABLE {name} (x INTEGER)")
    out = agent_tools.get_schema(db_path=db)
    tables = out["tables"]
    assert "ledger" in tables
    assert "op_goals" in tables
    assert "op_work_items" in tables
    for bare in ("goals", "work_items"):
        assert bare not in tables


def test_raw_sql_cannot_sneak_through_select_tool(db):
    """The bug that motivated this refactor: a write via the 'read' tool
    must be impossible even when the statement parses."""
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
    out = agent_tools.sql_select("UPDATE t SET x = 1", db_path=db)
    assert out["error"]
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# set_context — merge semantics, chat_id injected by the loop
# ---------------------------------------------------------------------------

def test_set_context_merges_and_clears(db):
    prep = agent_tools.prepare_write(
        "set_context", {"subject": "Physics", "chapter": "Kinematics"},
        chat_id="ctx1", db_path=db,
    )
    assert prep["ok"], prep
    assert "subject: Physics" in prep["preview"]
    agent_tools.run_prepared_write("set_context", prep["run"], chat_id="ctx1", db_path=db)

    # Partial update: chapter changes, subject is KEPT (merge semantics).
    prep2 = agent_tools.prepare_write(
        "set_context", {"chapter": "Wave Optics"}, chat_id="ctx1", db_path=db,
    )
    assert prep2["ok"]
    assert prep2["run"]["context"]["subject"] == "Physics"
    assert prep2["run"]["context"]["chapter"] == "Wave Optics"
    agent_tools.run_prepared_write("set_context", prep2["run"], chat_id="ctx1", db_path=db)

    # Clearing with a "none" token.
    prep3 = agent_tools.prepare_write(
        "set_context", {"subject": "none"}, chat_id="ctx1", db_path=db,
    )
    assert prep3["ok"]
    assert prep3["run"]["context"]["subject"] is None
    assert prep3["run"]["context"]["chapter"] == "Wave Optics"  # still kept


def test_set_context_no_changes(db):
    prep = agent_tools.prepare_write("set_context", {}, chat_id="ctx1", db_path=db)
    assert not prep["ok"]
    assert "nothing to change" in prep["error"]


# ---------------------------------------------------------------------------
# Domain write validation — bad args never reach a Confirm card
# ---------------------------------------------------------------------------

def test_create_goal_validation(db):
    bad = agent_tools.prepare_write(
        "create_goal", {"title": "x", "goal_type": "zzz", "target": 5},
        chat_id=1, db_path=db,
    )
    assert not bad["ok"]
    assert "goal_type" in bad["error"]

    missing = agent_tools.prepare_write(
        "create_goal", {"title": "x"}, chat_id=1, db_path=db,
    )
    assert not missing["ok"]
    assert "target" in missing["error"]

    ok = agent_tools.prepare_write(
        "create_goal",
        {"title": "300 qs", "goal_type": "cy", "target": 300, "period": "daily"},
        chat_id=1, db_path=db,
    )
    assert ok["ok"], ok
    assert "Create goal" in ok["preview"]
    # enums canonicalised to live option casing
    assert ok["run"]["data"]["goal_type"] == "CY"
    assert ok["run"]["data"]["period"] == "Daily"

    result = agent_tools.run_prepared_write("create_goal", ok["run"], chat_id=1, db_path=db)
    assert not result.get("error"), result
    # operation_id idempotency: re-running the same prepared write is a no-op
    again = agent_tools.run_prepared_write("create_goal", ok["run"], chat_id=1, db_path=db)
    assert again["id"] == result["id"]


def test_create_exam_and_record_result(db):
    prep = agent_tools.prepare_write(
        "create_exam",
        {"title": "Main Mock 1", "kind": "jee main mock", "exam_date": "2026-08-10",
         "max_marks": 300, "target_marks": 200},
        chat_id=1, db_path=db,
    )
    assert prep["ok"], prep
    assert prep["run"]["data"]["kind"] == "JEE Main Mock"
    result = agent_tools.run_prepared_write("create_exam", prep["run"], chat_id=1, db_path=db)
    assert not result.get("error")

    bad = agent_tools.prepare_write(
        "record_exam_result",
        {"exam": "Main Mock 1", "actual_marks": 310},
        chat_id=1, db_path=db,
    )
    assert bad["ok"]  # prep only checks shape; the run is authoritative
    out = agent_tools.run_prepared_write("record_exam_result", bad["run"], chat_id=1, db_path=db)
    assert out.get("error")  # 310 > max_marks 300 — authoritative rejection

    good = agent_tools.prepare_write(
        "record_exam_result",
        {"exam": "main mock", "actual_marks": 187, "attempted": 70, "correct": 55, "incorrect": 15},
        chat_id=1, db_path=db,
    )
    assert good["ok"]
    out = agent_tools.run_prepared_write("record_exam_result", good["run"], chat_id=1, db_path=db)
    assert not out.get("error"), out
    assert out["status"] == "Analysing"


def test_record_exam_result_unknown_exam(db):
    prep = agent_tools.prepare_write(
        "record_exam_result", {"exam": "nope", "actual_marks": 10},
        chat_id=1, db_path=db,
    )
    assert not prep["ok"]
    assert "no exam matches" in prep["error"]


def test_schedule_reminder_validation_and_run(db):
    bad = agent_tools.prepare_write(
        "schedule_reminder",
        {"schedule_kind": "hourly", "time": "25:99", "action_kind": "yell", "action_text": "x"},
        chat_id=1, db_path=db,
    )
    assert not bad["ok"]

    ok = agent_tools.prepare_write(
        "schedule_reminder",
        {"schedule_kind": "daily", "time": "9pm", "action_kind": "message", "action_text": "revise"},
        chat_id=1, db_path=db,
    )
    assert not ok["ok"]  # bad time rejected by validate_parsed

    ok = agent_tools.prepare_write(
        "schedule_reminder",
        {"schedule_kind": "daily", "time": "21:30", "action_kind": "message", "action_text": "revise kinematics"},
        chat_id=1, db_path=db,
    )
    assert ok["ok"], ok
    assert "21:30" in ok["preview"]
    assert "every day" in ok["preview"]
    out = agent_tools.run_prepared_write("schedule_reminder", ok["run"], chat_id=1, db_path=db)
    assert out.get("status") == "saved", out
    assert "revise kinematics" in out["job"]


def test_write_tool_cannot_be_executed_directly(db):
    out = agent_tools.execute_tool("create_goal", {"title": "x", "target": 1}, chat_id=1, db_path=db)
    assert out["error"]
    assert "confirmation" in out["message"]


def test_unknown_tool(db):
    out = agent_tools.execute_tool("drop_everything", {}, chat_id=1, db_path=db)
    assert "Unknown tool" in out["message"]


# ---------------------------------------------------------------------------
# logging_flow-backed tools — full build_write_plan validation at preview time
# ---------------------------------------------------------------------------

def test_log_study_session_preview_validation(db):
    # correct > attempted → clarification, not a Confirm card
    bad = agent_tools.prepare_write(
        "log_study_session",
        {"task": "PYQs", "questions_attempted": 10, "questions_correct": 12, "actual_time_min": 30},
        chat_id=1, db_path=db,
    )
    assert not bad["ok"]
    assert bad.get("clarification")
    assert "cannot exceed" in bad["clarification"]


def test_log_doubt_requires_concept(db):
    bad = agent_tools.prepare_write("log_doubt", {}, chat_id=1, db_path=db)
    assert not bad["ok"]
    assert "core_concept" in bad["error"]


def test_log_revision_requires_chapter(db):
    bad = agent_tools.prepare_write("log_revision", {}, chat_id=1, db_path=db)
    assert not bad["ok"]
    assert "chapter_module" in bad["error"]


def test_prepared_run_is_json_serialisable(db):
    """Preview state is persisted to SQLite as JSON — run plans must serialize."""
    import json
    prep = agent_tools.prepare_write(
        "log_study_session",
        {"task": "PYQs kinematics", "questions_attempted": 20, "questions_correct": 15,
         "actual_time_min": 30, "doubts": "relative velocity sign"},
        chat_id=1, db_path=db,
    )
    assert prep["ok"], prep
    json.dumps(prep["run"])
    json.dumps(prep["preview"])


# ---------------------------------------------------------------------------
# Registry sanity
# ---------------------------------------------------------------------------

def test_registry_covers_all_specs():
    spec_names = {s["name"] for s in agent_tools.TOOL_SPECS}
    assert spec_names == agent_tools.WRITE_TOOLS | agent_tools.READ_TOOLS
    # every write tool has both a prep and a run handler
    for name in agent_tools.WRITE_TOOLS:
        assert name in agent_tools._PREP_HANDLERS
        assert name in agent_tools._RUN_HANDLERS


def test_coaching_schedule_read_tool(tmp_path):
    import ntsc_coaching

    db = tmp_path / "coaching.db"
    ntsc_coaching.replace_classes([{
        "classDate": "2026-08-05", "startTime": "15:45", "duration": 120,
        "classType": "Regular Class", "subjects": "Chemistry",
    }], db_path=db)
    out = agent_tools.execute_tool(
        "get_coaching_schedule", {"date": "2026-08-05"}, chat_id=1, db_path=db,
    )
    assert out["date"] == "2026-08-05"
    assert out["classes"][0]["subjects"] == "Chemistry"


def test_relative_coaching_date_resolution(monkeypatch):
    import datetime as dt
    import ntsc_coaching

    now = dt.datetime(2026, 8, 1, 10, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(ntsc_coaching.session_context, "local_now", lambda: now)
    assert ntsc_coaching.resolve_date("is there a class in 4 days?") == "2026-08-05"


# ---------------------------------------------------------------------------
# Coaching read tools (Phase 6/7/8 integration): syllabus, next class, plans
# ---------------------------------------------------------------------------

def _coaching_db(tmp_path):
    import sqlite3
    import ntsc_coaching
    import coaching_syllabus
    import operational_store
    import sync

    path = tmp_path / "coaching-tools.db"
    with sync.connect(path) as conn:
        sync.init_db(conn)
        operational_store.init_db(conn)
        ntsc_coaching.init_db(conn)
    ntsc_coaching.replace_classes([
        {"classDate": "2026-08-05", "startTime": "15:45", "duration": 120,
         "classType": "Regular Class", "subjects": "Chemistry"},
        {"classDate": "2026-08-06", "startTime": "08:00", "duration": 60,
         "classType": "Lecture", "subjects": "Physics"},
    ], db_path=path)
    ntsc_coaching.replace_tests([
        {"id": "t1", "testName": "Weekly Test", "testDateTime": "2026-08-15T09:00:00",
         "courseId": "7", "syllabus": "Physics: Kinematics, Vectors"},
    ], db_path=path)
    coaching_syllabus.replace_syllabi([
        {"id": "t1", "syllabus": "Physics: Kinematics, Vectors"},
    ], db_path=path)
    return path


def test_get_next_class_read_tool(tmp_path, monkeypatch):
    import datetime as dt
    import ntsc_coaching

    db = _coaching_db(tmp_path)
    now = dt.datetime(2026, 8, 5, 10, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(ntsc_coaching.session_context, "local_today_iso", lambda: "2026-08-05")
    out = agent_tools.execute_tool("get_next_class", {}, chat_id=1, db_path=db)
    assert out["classes"][0]["class_date"] == "2026-08-05"
    assert out["classes"][0]["subjects"] == "Chemistry"
    assert out["freshness"]["status"] == "never_synced"


def test_get_upcoming_syllabus_read_tool(tmp_path):
    db = _coaching_db(tmp_path)
    out = agent_tools.execute_tool("get_upcoming_syllabus", {}, chat_id=1, db_path=db)
    tests = out["tests"]
    assert len(tests) == 1
    assert tests[0]["title"] == "Weekly Test"
    assert tests[0]["coverage"]["topic_count"] == 2
    topics = {t["topic"] for t in tests[0]["topics"]}
    assert topics == {"Kinematics", "Vectors"}
    assert all("covered" in t for t in tests[0]["topics"])


def test_get_plan_suggestions_read_tool(tmp_path):
    import sqlite3

    db = _coaching_db(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO op_work_items (id, notion_page_id, archived, title, kind, status, "
            "due_date, estimated_min, created_time, last_edited_time, last_synced_at, raw_json) "
            "VALUES ('hw1','hw1',0,'DPP Sheet','Coaching Homework','Planned','2026-08-05',60,"
            "'2026-07-20T00:00:00+00:00','2026-07-20T00:00:00+00:00','2026-07-20T00:00:00+00:00','{}')"
        )
        conn.commit()
    out = agent_tools.execute_tool(
        "get_plan_suggestions", {"start_date": "2026-08-05"}, chat_id=1, db_path=db,
    )
    assert out["plan_type"] == "daily"
    assert out["start_date"] == "2026-08-05"
    kinds = {b["kind"] for b in out["blocks"]}
    assert "Coaching Class" in kinds
    assert "Coaching Homework" in kinds
    assert out["capacity"]["2026-08-05"]["budget_minutes"] == 600
    assert all(b.get("start") is not None for b in out["blocks"])


def test_get_plan_suggestions_rejects_bad_start_date(db):
    out = agent_tools.execute_tool(
        "get_plan_suggestions", {"start_date": "tomorrow"}, chat_id=1, db_path=db,
    )
    assert out.get("error")
    assert "YYYY-MM-DD" in out["message"]
    out = agent_tools.execute_tool(
        "get_plan_suggestions", {"start_date": "2026-08-05"}, chat_id=1, db_path=db,
    )
    assert not out.get("error"), out


def test_coaching_read_tools_empty_mirror(tmp_path):
    db = tmp_path / "empty.db"
    import sqlite3
    sqlite3.connect(db).close()
    for name, args in (
        ("get_next_class", {}),
        ("get_upcoming_syllabus", {}),
        ("get_plan_suggestions", {}),
        ("get_coaching_schedule", {"date": "2026-08-05"}),
    ):
        out = agent_tools.execute_tool(name, args, chat_id=1, db_path=db)
        assert not out.get("error"), (name, out)
