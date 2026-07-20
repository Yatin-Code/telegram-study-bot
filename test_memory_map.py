"""/memory backend (memory_map.py). Temp mirror, no Telegram/LLM."""

from __future__ import annotations

import sqlite3

import pytest

import commitments
import draft_store
import memory_map
import study_domain as sd
import sync
from config import settings


@pytest.fixture(autouse=True)
def _isolated_overrides(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "_OVERRIDES_PATH", str(tmp_path / "settings.json"))


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "memmap.db"
    with sync.connect(path) as conn:
        sync.init_db(conn)
    return path


def insert(path, table, **values):
    base = {
        "notion_page_id": values.pop("notion_page_id", f"{table}-id"),
        "archived": 0,
        "last_synced_at": "2026-07-21T00:00:00+00:00",
        "raw_json": "{}",
    }
    if table.startswith("op_"):
        base["created_time"] = "2026-07-21T00:00:00+00:00"
        base["last_edited_time"] = "2026-07-21T00:00:00+00:00"
    base.update(values)
    with sqlite3.connect(path) as conn:
        cols = ",".join(f'"{key}"' for key in base)
        marks = ",".join("?" for _ in base)
        conn.execute(f'INSERT INTO "{table}" ({cols}) VALUES ({marks})', tuple(base.values()))
        conn.commit()


def make_goal(db, **overrides):
    data = {
        "title": "Daily PYQs", "goal_type": "Coverage", "metric": "sessions",
        "target": 1, "period": "Daily",
    }
    data.update(overrides)
    return sd.create_goal(data, db_path=db)


CHAT = 7


def test_report_counts_and_sections(db):
    insert(db, "ledger", notion_page_id="l1", date="2026-07-20", exercise_type="PYQs")
    insert(db, "ledger", notion_page_id="l2", date="2026-07-20", exercise_type="Theory")
    make_goal(db)
    commitments.add_pref(CHAT, "prefers maths in the morning", db_path=db)
    draft_store.record_qa(CHAT, "how much today?", "You logged 2 sessions.", db_path=db)
    rep = memory_map.report(CHAT, db_path=db)
    assert rep["on_demand"]["ledger"] == 2
    assert rep["on_demand"]["goals"] == 1
    assert len(rep["commitments"]) == 1 and rep["commitments"][0]["title"] == "Daily PYQs"
    assert len(rep["prefs"]) == 1
    assert rep["qa_pairs"] == 1 and rep["qa_chars"] > 0
    assert rep["sizes"]["persistent_chars"] > 0
    assert rep["sizes"]["over_budget"] is False
    assert rep["internal"]["adherence checks"] == 0


def test_budget_flip_and_warning(db, monkeypatch):
    assert memory_map.budget_warning(CHAT, db_path=db) is None
    for i in range(8):
        commitments.add_pref(CHAT, f"preference number {i} " + "x" * 90, db_path=db)
    monkeypatch.setenv("MEMORY_CONTEXT_BUDGET_CHARS", "500")
    rep = memory_map.report(CHAT, db_path=db)
    assert rep["sizes"]["budget"] == 500
    assert rep["sizes"]["over_budget"] is True
    warn = memory_map.budget_warning(CHAT, db_path=db)
    assert warn and "/memory" in warn
    assert "⚠️" in memory_map.render(rep)


def test_render_contents(db):
    make_goal(db)
    commitments.add_pref(CHAT, "prefers maths in the morning", db_path=db)
    text = memory_map.render(memory_map.report(CHAT, db_path=db))
    assert "always in the model's context" in text.lower()
    assert "Daily PYQs" in text
    assert "#" in text and "maths in the morning" in text
    assert "coaching schedule" in text
    assert "NOT in context" in text
    assert "⚠️" in text  # study-data gaps: no exam date, empty timetable


def test_study_data_gaps_vs_facts(db):
    rep = memory_map.report(CHAT, db_path=db)
    gaps = " ".join(rep["study_data"]["gaps"])
    assert "/exam" in gaps and "timetable" in gaps
    insert(db, "op_exams", notion_page_id="e1", title="JEE Main", status="Planned",
           exam_date="2027-01-24")
    insert(db, "op_timetable", notion_page_id="t1", title="Physics class", weekday="Mon")
    rep = memory_map.report(CHAT, db_path=db)
    facts = " ".join(rep["study_data"]["facts"])
    assert "JEE Main" in facts and "phase" in facts
    assert "timetable" not in " ".join(rep["study_data"]["gaps"])


def test_render_bounded_with_many_prefs(db):
    for i in range(30):
        commitments.add_pref(CHAT, f"preference number {i} with some padding text", db_path=db)
    text = memory_map.render(memory_map.report(CHAT, db_path=db))
    assert len(text) < 4096
    assert "+15 more" in text


def test_render_raw_matches_prompt_block(db):
    make_goal(db)
    commitments.add_pref(CHAT, "prefers maths in the morning", db_path=db)
    raw = memory_map.render_raw(CHAT, db_path=db)
    assert "Daily PYQs" in raw and "maths in the morning" in raw
    assert "USER COMMITMENTS" in raw


def test_keyboard_shape_and_limits(db):
    goal = make_goal(db, title="A very long commitment title that should be truncated nicely")
    pid = commitments.add_pref(CHAT, "p" * 100, db_path=db)
    rows = memory_map.keyboard(memory_map.report(CHAT, db_path=db))
    flat = [btn for row in rows for btn in row]
    data_values = [data for _, data in flat]
    assert f"memory:delpref:{pid}" in data_values
    assert f"memory:pausegoal:{goal['notion_page_id']}" in data_values
    assert all(len(data.encode()) <= 64 for _, data in flat)
    assert all(len(label) <= 32 for label, _ in flat)
    assert ("🔄 Refresh", "memory:refresh") in rows[-1]


def test_reactivate_pref_roundtrip(db):
    pid = commitments.add_pref(CHAT, "temp pref", db_path=db)
    assert commitments.deactivate_pref(CHAT, pid, db_path=db)
    assert commitments.active_prefs(CHAT, db_path=db) == []
    assert commitments.reactivate_pref(CHAT, pid, db_path=db) is True
    assert [p["id"] for p in commitments.active_prefs(CHAT, db_path=db)] == [pid]
    assert commitments.reactivate_pref(CHAT + 1, pid, db_path=db) is False
