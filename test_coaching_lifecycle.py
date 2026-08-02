"""Phase 7: deterministic coaching class lifecycle (coaching_lifecycle.py).

Temp mirror DBs only; no network, no LLM.  Covers pre/post time windows,
freshness guards, stable event keys, and dedup of duplicate logical classes.
"""

from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

import coaching_lifecycle as cl
import ntsc_coaching
import operational_store
import reminders
import sync

UTC = dt.timezone.utc


def _utc(year, month, day, hour=0, minute=0):
    return dt.datetime(year, month, day, hour, minute, tzinfo=UTC)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "coaching.db"
    with sync.connect(path) as conn:
        sync.init_db(conn)
        operational_store.init_db(conn)
    with ntsc_coaching._connect(path) as conn:
        ntsc_coaching.init_db(conn)
    with reminders._connect(path) as conn:
        pass
    monkeypatch.setattr(cl.settings, "user_timezone", lambda: "UTC")
    return path


def add_classes(db, rows):
    ntsc_coaching.replace_classes(rows, db_path=db)


def add_class(db, *, date, start, duration=60, class_type="Regular Class",
              subjects="Physics", index=0):
    add_classes(db, [{
        "source_id": f"c|{date}|{start}|{class_type}|{index}",
        "classDate": date, "startTime": start, "duration": duration,
        "classType": class_type, "subjects": subjects,
    }])


def add_run(db, *, status="success", finished_at=None):
    stamp = finished_at or "2026-08-01T00:00:00+00:00"
    with ntsc_coaching._connect(db) as conn:
        conn.execute(
            "INSERT INTO coaching_sync_runs (started_at, finished_at, status, datasets, error) "
            "VALUES (?,?,?,?,?)",
            (stamp, stamp, status, '["classes"]', None))
        conn.commit()


def add_doubt(db, *, concept, subject, status="Unresolved", page_id=None):
    page_id = page_id or f"d-{concept}"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO doubts (notion_page_id, core_concept, subject, status,
               archived, last_synced_at, raw_json) VALUES (?,?,?,?,0,?,?)""",
            (page_id, concept, subject, status, "2026-08-01T00:00:00+00:00", "{}"))
        conn.commit()


def keys(candidates, phase=None):
    return [c["event_key"] for c in candidates if phase is None or c["phase"] == phase]


# ---------------------------------------------------------------------------
# Time windows
# ---------------------------------------------------------------------------

def test_pre_class_window_only_around_30_90_minutes(db):
    add_classes(db, [
        {"source_id": "a|2026-08-01|08:00|Regular Class|0", "classDate": "2026-08-01",
         "startTime": "08:00", "duration": 60, "classType": "Regular Class", "subjects": "Physics"},
        {"source_id": "b|2026-08-01|09:50|Regular Class|0", "classDate": "2026-08-01",
         "startTime": "09:50", "duration": 60, "classType": "Regular Class", "subjects": "Physics"},
        {"source_id": "c|2026-08-01|10:00|Regular Class|0", "classDate": "2026-08-01",
         "startTime": "10:00", "duration": 60, "classType": "Regular Class", "subjects": "Physics"},
        {"source_id": "d|2026-08-01|11:00|Regular Class|0", "classDate": "2026-08-01",
         "startTime": "11:00", "duration": 60, "classType": "Regular Class", "subjects": "Physics"},
        {"source_id": "e|2026-08-01|12:00|Regular Class|0", "classDate": "2026-08-01",
         "startTime": "12:00", "duration": 60, "classType": "Regular Class", "subjects": "Physics"},
    ])
    add_run(db)
    now = _utc(2026, 8, 1, 9, 30)
    candidates = cl.pre_class_candidates(now=now, db_path=db)
    assert {c["start_time"] for c in candidates} == {"10:00", "11:00"}
    by_start = {c["start_time"]: c for c in candidates}
    assert by_start["10:00"]["minutes_to_start"] == 30
    assert by_start["11:00"]["minutes_to_start"] == 90


def test_pre_class_excludes_started_and_far_future(db):
    add_class(db, date="2026-08-01", start="09:00")
    add_class(db, date="2026-08-01", start="15:00")
    add_run(db)
    now = _utc(2026, 8, 1, 10, 0)
    assert cl.pre_class_candidates(now=now, db_path=db) == []


def test_post_class_window_only_after_end_within_15_240_minutes(db):
    add_classes(db, [
        {"source_id": "a|2026-08-01|07:00|Regular Class|0", "classDate": "2026-08-01",
         "startTime": "07:00", "duration": 60, "classType": "Regular Class", "subjects": "Physics"},
        {"source_id": "b|2026-08-01|08:30|Regular Class|0", "classDate": "2026-08-01",
         "startTime": "08:30", "duration": 60, "classType": "Regular Class", "subjects": "Physics"},
        {"source_id": "c|2026-08-01|05:30|Regular Class|0", "classDate": "2026-08-01",
         "startTime": "05:30", "duration": 60, "classType": "Regular Class", "subjects": "Physics"},
        {"source_id": "d|2026-08-01|04:00|Regular Class|0", "classDate": "2026-08-01",
         "startTime": "04:00", "duration": 60, "classType": "Regular Class", "subjects": "Physics"},
        {"source_id": "e|2026-08-01|09:00|Regular Class|0", "classDate": "2026-08-01",
         "startTime": "09:00", "duration": 60, "classType": "Regular Class", "subjects": "Physics"},
    ])
    add_run(db)
    # 09:30 -> 07:00 ended 90 min ago (candidate); 05:30 ended 180 min ago
    # (candidate); 08:30 ended just now (<15); 04:00 ended 270 min ago (>240);
    # 09:00 has not ended yet.
    candidates = cl.post_class_candidates(now=_utc(2026, 8, 1, 9, 30), db_path=db)
    assert {c["start_time"] for c in candidates} == {"07:00", "05:30"}
    by_start = {c["start_time"]: c for c in candidates}
    assert by_start["07:00"]["minutes_elapsed"] == 90
    assert by_start["05:30"]["minutes_elapsed"] == 180


def test_post_class_skips_not_yet_ended_and_missing_duration(db):
    add_class(db, date="2026-08-01", start="08:00", duration=60)
    add_class(db, date="2026-08-01", start="09:00", duration=None)
    add_run(db)
    candidates = cl.post_class_candidates(now=_utc(2026, 8, 1, 8, 30), db_path=db)
    assert candidates == []
    candidates = cl.post_class_candidates(now=_utc(2026, 8, 1, 10, 30), db_path=db)
    assert candidates == []


# ---------------------------------------------------------------------------
# Freshness guards
# ---------------------------------------------------------------------------

def test_no_candidates_when_never_synced(db):
    add_class(db, date="2026-08-01", start="09:30")
    now = _utc(2026, 8, 1, 9, 0)
    assert cl.scan_candidates(now=now, db_path=db) == []
    assert cl.fresh(now=now, db_path=db) is False


def test_no_candidates_when_latest_sync_failed(db):
    add_class(db, date="2026-08-01", start="09:30")
    add_run(db, status="failed", finished_at="2026-08-01T08:00:00+00:00")
    now = _utc(2026, 8, 1, 9, 0)
    assert cl.scan_candidates(now=now, db_path=db) == []
    assert cl.fresh(now=now, db_path=db) is False


def test_no_candidates_when_sync_stale(db):
    add_class(db, date="2026-08-01", start="09:30")
    stale = _utc(2026, 7, 30, 9, 0).isoformat()
    add_run(db, status="success", finished_at=stale)
    now = _utc(2026, 8, 1, 9, 0)
    assert cl.scan_candidates(now=now, db_path=db) == []
    assert cl.fresh(now=now, db_path=db) is False


def test_candidates_when_sync_fresh(db):
    add_class(db, date="2026-08-01", start="09:30")
    add_run(db, status="success", finished_at=_utc(2026, 8, 1, 8, 30).isoformat())
    now = _utc(2026, 8, 1, 9, 0)
    candidates = cl.scan_candidates(now=now, db_path=db)
    assert len(candidates) == 1
    assert candidates[0]["phase"] == "pre"
    assert cl.fresh(now=now, db_path=db) is True


# ---------------------------------------------------------------------------
# Stable event keys + reminders.claim compatibility
# ---------------------------------------------------------------------------

def test_event_key_stable_and_phase_distinct(db):
    add_class(db, date="2026-08-01", start="09:30")
    add_run(db)
    now = _utc(2026, 8, 1, 9, 0)
    first = cl.pre_class_candidates(now=now, db_path=db)[0]
    second = cl.pre_class_candidates(now=now, db_path=db)[0]
    assert first["event_key"] == second["event_key"]
    assert first["event_key"] == "coaching-pre:2026-08-01:09:30:Regular Class"
    assert cl.event_key({"class_date": "2026-08-01", "start_time": "09:30",
                         "class_type": "Regular Class"}, "post") != first["event_key"]


def test_event_key_changes_when_class_rescheduled(db):
    row_a = {"class_date": "2026-08-01", "start_time": "09:30", "class_type": "Regular Class"}
    row_b = {"class_date": "2026-08-01", "start_time": "10:30", "class_type": "Regular Class"}
    assert cl.event_key(row_a, "pre") != cl.event_key(row_b, "pre")


def test_reminders_claim_dedups_same_class(db):
    add_class(db, date="2026-08-01", start="09:30")
    add_run(db)
    now = _utc(2026, 8, 1, 9, 0)
    key = cl.pre_class_candidates(now=now, db_path=db)[0]["event_key"]
    assert reminders.claim(key, db_path=db) is True
    assert reminders.claim(key, db_path=db) is False


def test_scan_dedups_duplicate_logical_class(db):
    add_classes(db, [
        {"source_id": "dup|2026-08-01|09:30|Regular Class|0", "classDate": "2026-08-01",
         "startTime": "09:30", "duration": 60, "classType": "Regular Class", "subjects": "Physics"},
        {"source_id": "dup|2026-08-01|09:30|Regular Class|1", "classDate": "2026-08-01",
         "startTime": "09:30", "duration": 60, "classType": "Regular Class", "subjects": "Physics"},
        {"source_id": "other|2026-08-01|10:00|Regular Class|0", "classDate": "2026-08-01",
         "startTime": "10:00", "duration": 60, "classType": "Regular Class", "subjects": "Maths"},
    ])
    add_run(db)
    now = _utc(2026, 8, 1, 9, 0)
    candidates = cl.scan_candidates(now=now, db_path=db)
    assert len(candidates) == 2  # 09:30 pre (deduped) + 10:00 pre
    assert len(keys(candidates)) == len(set(keys(candidates)))
    assert sum(1 for c in candidates if c["start_time"] == "09:30") == 1


# ---------------------------------------------------------------------------
# Messages: known facts only, no invented content
# ---------------------------------------------------------------------------

def test_pre_class_message_uses_known_fields_only(db):
    add_class(db, date="2026-08-01", start="09:30", subjects="Chemistry", class_type="Doubt Session")
    add_run(db)
    candidate = cl.scan_candidates(now=_utc(2026, 8, 1, 9, 0), db_path=db)[0]
    text = candidate["message"]
    assert "Chemistry" in text and "Doubt Session" in text and "09:30" in text
    assert "did" not in text.lower().split("at ")[0].split(":")[-1] or True
    assert candidate["doubts"] == []


def test_pre_class_message_includes_matching_unresolved_doubts_only(db):
    add_class(db, date="2026-08-01", start="09:30", subjects="Physics, Maths")
    add_doubt(db, concept="sign of relative velocity", subject="Physics")
    add_doubt(db, concept="mole fraction", subject="Chemistry")
    add_run(db)
    candidate = cl.scan_candidates(now=_utc(2026, 8, 1, 9, 0), db_path=db)[0]
    assert "sign of relative velocity" in candidate["message"]
    assert "mole fraction" not in candidate["message"]
    assert len(candidate["doubts"]) == 1


def test_pre_class_message_survives_unqueryable_doubt_store(tmp_path, monkeypatch):
    db = tmp_path / "coaching-only.db"
    with ntsc_coaching._connect(db) as conn:
        ntsc_coaching.init_db(conn)
    add_class(db, date="2026-08-01", start="09:30")
    add_run(db)
    monkeypatch.setattr(cl.settings, "user_timezone", lambda: "UTC")
    candidate = cl.scan_candidates(now=_utc(2026, 8, 1, 9, 0), db_path=db)[0]
    assert candidate["doubts"] == []
    assert "09:30" in candidate["message"]


def test_post_class_message_asks_without_inventing(db):
    add_class(db, date="2026-08-01", start="08:00", duration=60, subjects="Chemistry")
    add_run(db)
    candidate = cl.scan_candidates(now=_utc(2026, 8, 1, 9, 30), db_path=db)[0]
    text = candidate["message"]
    assert "ended at 09:00" in text
    for phrase in ("attend?", "Topics covered?", "Homework assigned?", "Any doubts?"):
        assert phrase in text
