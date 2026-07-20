"""AI escape hatch for /setup — offline plumbing tests (no LLM, no Telegram)."""

from __future__ import annotations

import pytest

import commitments
import onboarding
import study_domain as sd
import sync
from config import settings

CHAT = 21


@pytest.fixture(autouse=True)
def _isolated_overrides(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "_OVERRIDES_PATH", str(tmp_path / "settings.json"))


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "setupai.db"
    with sync.connect(path) as conn:
        sync.init_db(conn)
    return path


def test_validate_whitelist_and_secrets_blocked():
    actions, errors = onboarding.validate_ai_actions([
        {"type": "remember_preference", "text": "schedule changes weekly"},
        {"type": "set_setting", "key": "TIMETABLE_REMINDER_WEEKDAY", "value": "6"},
        {"type": "set_setting", "key": "LLM_API_KEY", "value": "stolen"},
        {"type": "set_setting", "key": "NOTION_TOKEN", "value": "stolen"},
        {"type": "skip_section"},
        {"type": "drop_tables"},
        "not-an-object",
    ])
    assert [a["type"] for a in actions] == [
        "remember_preference", "set_setting", "skip_section"
    ]
    assert actions[1]["key"] == "TIMETABLE_REMINDER_WEEKDAY"
    assert len(errors) == 4


def test_validate_field_rules():
    actions, errors = onboarding.validate_ai_actions([
        {"type": "create_timetable_entry", "subject": "physics", "weekday": "mon",
         "start": "17:00", "end": "19:00", "teacher": "Ramesh"},
        {"type": "create_timetable_entry", "subject": "history", "weekday": "mon",
         "start": "17:00", "end": "19:00"},
        {"type": "create_exam", "title": "Major test", "kind": "coaching test",
         "exam_date": "2026-08-10"},
        {"type": "create_exam", "title": "Bad", "kind": "Coaching Test",
         "exam_date": "not-a-date"},
        {"type": "create_work_item", "title": "Add mock dates", "kind": "other",
         "due_date": "2026-08-21"},
        {"type": "remember_preference", "text": "   "},
    ])
    kinds = [a["type"] for a in actions]
    assert kinds == ["create_timetable_entry", "create_exam", "create_work_item"]
    assert actions[0]["subject"] == "Physics" and actions[0]["weekday"] == "Monday"
    assert actions[1]["kind"] == "Coaching Test"
    assert len(errors) == 3


def test_apply_actions_land_in_stores(db):
    actions, errors = onboarding.validate_ai_actions([
        {"type": "remember_preference", "text": "coaching schedule changes weekly"},
        {"type": "set_setting", "key": "TIMETABLE_REMINDER_WEEKDAY", "value": "6"},
        {"type": "create_work_item", "title": "Add mock dates", "kind": "Other",
         "due_date": "2026-08-21"},
        {"type": "create_exam", "title": "Major test", "kind": "Coaching Test",
         "exam_date": "2026-08-10"},
        {"type": "create_timetable_entry", "subject": "Physics", "weekday": "Monday",
         "start": "17:00", "end": "19:00", "teacher": "Ramesh"},
        {"type": "skip_section"},
    ])
    assert not errors
    results, skip = onboarding.apply_ai_actions(CHAT, actions, db_path=db)
    assert skip is True
    assert len(results) == 6 and not any(r.startswith("⚠️") for r in results)
    assert commitments.active_prefs(CHAT, db_path=db)[0]["text"].startswith("coaching schedule")
    assert settings.get_override("TIMETABLE_REMINDER_WEEKDAY") == "6"
    assert sd._rows("work_items", "archived=0", db_path=db)[0]["title"] == "Add mock dates"
    assert sd._rows("exams", "archived=0", db_path=db)[0]["kind"] == "Coaching Test"
    assert sd._rows("timetable", "archived=0", db_path=db)[0]["weekday"] == "Monday"


def test_apply_survives_partial_failure(db):
    actions = [
        # Passes shape validation but fails in the domain layer (end < start).
        {"type": "create_timetable_entry", "subject": "Physics", "weekday": "Monday",
         "start": "17:00", "end": "16:00", "teacher": None},
        {"type": "remember_preference", "text": "still saved"},
    ]
    results, skip = onboarding.apply_ai_actions(CHAT, actions, db_path=db)
    assert any(r.startswith("⚠️") for r in results)
    assert commitments.active_prefs(CHAT, db_path=db)[0]["text"] == "still saved"
    assert skip is False


def test_describe_lines():
    actions, _ = onboarding.validate_ai_actions([
        {"type": "remember_preference", "text": "schedule changes weekly"},
        {"type": "set_setting", "key": "TIMETABLE_REMINDER_WEEKDAY", "value": "6"},
        {"type": "skip_section"},
    ])
    lines = onboarding.describe_ai_actions(actions)
    assert lines[0].startswith("🧠") and "schedule changes weekly" in lines[0]
    assert "Sun" in lines[1]
    assert lines[2].startswith("▸")
