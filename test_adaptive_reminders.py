from __future__ import annotations

import datetime as dt

import reminders


IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def test_response_attaches_to_latest_unanswered_delivery(tmp_path):
    db = tmp_path / "adaptive.db"
    reminders.record_delivery(
        "morning:1", chat_id=42, reminder_kind="morning",
        sent_at=dt.datetime(2026, 7, 20, 7, 30, tzinfo=IST), db_path=db,
    )
    reminders.record_delivery(
        "morning:2", chat_id=42, reminder_kind="morning",
        sent_at=dt.datetime(2026, 7, 20, 8, 30, tzinfo=IST), db_path=db,
    )
    matched = reminders.record_response(
        42, responded_at=dt.datetime(2026, 7, 20, 9, 0, tzinfo=IST), db_path=db
    )
    assert matched is not None
    assert matched["event_key"] == "morning:2"
    assert matched["response_latency_min"] == 30


def test_timing_recommendation_blends_toward_stable_response_window(tmp_path, monkeypatch):
    db = tmp_path / "adaptive.db"
    monkeypatch.setattr(reminders.settings, "user_timezone", lambda: "Asia/Kolkata")
    for index, minute_shift in enumerate((0, 5, -5, 10, -10, 0, 5, -5)):
        day = 10 + index
        event = f"planning:{day}"
        reminders.record_delivery(
            event, chat_id=42, reminder_kind="planning", scheduled_local="07:30",
            sent_at=dt.datetime(2026, 7, day, 7, 30, tzinfo=IST), db_path=db,
        )
        reminders.record_response(
            42, event_key=event,
            responded_at=dt.datetime(2026, 7, day, 9, 0, tzinfo=IST)
            + dt.timedelta(minutes=minute_shift),
            db_path=db,
        )
    result = reminders.timing_recommendation(
        42, "planning", "07:30",
        as_of=dt.datetime(2026, 7, 23, 12, tzinfo=IST), db_path=db,
    )
    assert result["samples"] == 8
    assert result["median_response_time"] == "09:00"
    assert result["confidence"] == "medium"
    assert "08:00" <= result["recommended_time"] <= "08:45"
    assert reminders.effective_time(42, "planning", "07:30", db_path=db) == result["recommended_time"]


def test_scattered_or_thin_response_data_keeps_configured_default(tmp_path, monkeypatch):
    db = tmp_path / "adaptive.db"
    monkeypatch.setattr(reminders.settings, "user_timezone", lambda: "Asia/Kolkata")
    for index, hour in enumerate((6, 10, 14, 18)):
        day = 10 + index
        event = f"weekly:{day}"
        reminders.record_delivery(
            event, chat_id=42, reminder_kind="weekly",
            sent_at=dt.datetime(2026, 7, day, 5, 30, tzinfo=IST), db_path=db,
        )
        reminders.record_response(
            42, event_key=event,
            responded_at=dt.datetime(2026, 7, day, hour, 0, tzinfo=IST),
            db_path=db,
        )
    scattered = reminders.timing_recommendation(
        42, "weekly", "09:00",
        as_of=dt.datetime(2026, 7, 23, 12, tzinfo=IST), db_path=db,
    )
    assert scattered["recommended_time"] == "09:00"
    assert scattered["confidence"] == "low"

    thin = reminders.timing_recommendation(
        42, "exam", "08:00",
        as_of=dt.datetime(2026, 7, 23, 12, tzinfo=IST), db_path=db,
    )
    assert thin["recommended_time"] == "08:00"
    assert thin["confidence"] == "insufficient"
