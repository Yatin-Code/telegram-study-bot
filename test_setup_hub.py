"""Setup-hub rendering (bot.py) — portal-first rows and section pre-fills.

Todo 4 of portal-first-onboarding: the hub renders a "Portal sync" status row
and the chapters / target_exam section prompts consume portal pre-fill data
(suggested topics / course hint). Tests render the views directly against a
tmp mirror (base-path output: personalized_prompt is stubbed to "", so the
base prompt with any appended portal hint is what gets rendered).
"""

from __future__ import annotations

import datetime as dt

import pytest

import bot
import coaching_syllabus
import ntsc_coaching
import onboarding
import session_context
import sync


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "hub.db"
    with sync.connect(path) as conn:
        sync.init_db(conn)
    return path


@pytest.fixture()
def route_status(db, monkeypatch):
    """Force onboarding.status() onto the tmp mirror (bot calls take no db_path)."""
    real_status = onboarding.status

    def _status(**kw):
        kw["db_path"] = db
        return real_status(**kw)

    monkeypatch.setattr(bot.onboarding, "status", _status)
    # Deterministic base path: no LLM rewrite, no exception fallback.
    monkeypatch.setattr(bot.onboarding, "personalized_prompt", lambda section_id: "")
    return db


def _seed_success_portal(db):
    """Seed the NTSC mirror: success sync run + classes + tests + parsed syllabus."""
    stamp = dt.datetime.now(dt.timezone.utc).isoformat()
    with ntsc_coaching._connect(db) as conn:
        conn.execute(
            "INSERT INTO coaching_sync_runs (started_at, finished_at, status, datasets, error) "
            "VALUES (?,?,?,?,?)",
            (stamp, stamp, "success", '["classes"]', None))
    today = session_context.local_today_iso()
    ntsc_coaching.replace_classes([
        {"classDate": today, "startTime": "08:00", "duration": 60,
         "classType": "Class", "subjects": "Physics"},
        {"classDate": today, "startTime": "10:00", "duration": 60,
         "classType": "Class", "subjects": "Maths"},
    ], db_path=db)
    ntsc_coaching.replace_tests([
        {"id": "t1", "testName": "Major Test 1", "testDateTime": f"{today}T09:00:00"},
    ], db_path=db)
    coaching_syllabus.replace_syllabi([
        {"id": "t1", "syllabus": "Physics: Kinematics, Rotational Motion\n"
         "Chem: Mole Concept\nMaths: Limits"},
    ], db_path=db)
    return today


# ---------------------------------------------------------------------------
# New behavior — portal-first hub + pre-filled section prompts.
# ---------------------------------------------------------------------------

def test_hub_shows_portal_sync_row_seeded(route_status):
    _seed_success_portal(route_status)
    text, _ = bot._setup_hub_view()
    assert "Portal sync" in text
    assert "✅ *Portal sync*" in text
    assert "2 classes" in text


def test_hub_shows_portal_sync_row_never_synced(route_status):
    text, _ = bot._setup_hub_view()
    assert "⚠️ *Portal sync*" in text
    assert "never synced" in text


def test_chapters_prompt_suggests_topics(route_status):
    _seed_success_portal(route_status)
    text, _ = bot._setup_section_view("chapters")
    assert "suggests" in text
    assert "Kinematics" in text
    assert "send the one you're on" in text


def test_chapters_prompt_no_suggests_when_no_syllabus(route_status):
    text, _ = bot._setup_section_view("chapters")
    assert "suggests" not in text


def test_target_exam_prompt_course_hint(route_status):
    _seed_success_portal(route_status)
    ntsc_coaching.replace_profile({"courseName": "JEE Main 2028"}, db_path=route_status)
    text, _ = bot._setup_section_view("target_exam")
    assert "Portal shows your course as" in text
    assert "JEE Main 2028" in text


def test_target_exam_prompt_no_hint_without_course(route_status):
    _seed_success_portal(route_status)
    text, _ = bot._setup_section_view("target_exam")
    assert "Portal shows your course as" not in text


def test_finish_summary_warns_not_synced(route_status):
    text = bot._setup_finish_summary()
    assert "not synced" in text


def test_finish_summary_silent_when_synced(route_status):
    _seed_success_portal(route_status)
    text = bot._setup_finish_summary()
    assert "not synced" not in text
