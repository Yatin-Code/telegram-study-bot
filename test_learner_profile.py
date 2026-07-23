from __future__ import annotations

import datetime as dt
import sqlite3

import advisor
import commitments
import learner_profile
import operational_store
import planner
import reminders
import sync


def insert(path, table, **values):
    base = {
        "notion_page_id": values.pop("notion_page_id", f"{table}-id"),
        "archived": 0,
        "last_synced_at": "2026-07-22T00:00:00+00:00",
        "raw_json": "{}",
    }
    base.update(values)
    with sqlite3.connect(path) as conn:
        columns = ",".join(f'"{key}"' for key in base)
        marks = ",".join("?" for _ in base)
        conn.execute(
            f'INSERT INTO "{table}" ({columns}) VALUES ({marks})',
            tuple(base.values()),
        )
        conn.commit()


def seeded_profile_db(tmp_path):
    db = tmp_path / "learner.db"
    with sync.connect(db) as conn:
        sync.init_db(conn)
        operational_store.init_db(conn)
    for index in range(3):
        insert(
            db, "ledger", notion_page_id=f"physics-{index}",
            date=f"2026-07-{18 + index:02d}T06:30:00+05:30",
            subject="Physics", questions_attempted=20, questions_correct=10,
            cognitive_yield=40, actual_time_min=60,
        )
        insert(
            db, "ledger", notion_page_id=f"chem-{index}",
            date=f"2026-07-{18 + index:02d}T18:30:00+05:30",
            subject="Chem", questions_attempted=20, questions_correct=18,
            cognitive_yield=80, actual_time_min=60,
        )
    commitments.add_pref(42, "I prefer concise explanations", db_path=db)
    operational_store.create(
        "work_items",
        {"title": "Old mechanics", "status": "Backlog", "kind": "Backlog"},
        db_path=db,
    )
    insert(
        db, "revision", notion_page_id="rev-1", chapter_module="Rotation",
        status="Pending", next_execution_date="2026-07-21",
    )
    insert(
        db, "doubts", notion_page_id="doubt-1", core_concept="rolling sign",
        status="Unresolved",
    )
    return db


def test_profile_derives_only_evidence_backed_comparisons(tmp_path):
    db = seeded_profile_db(tmp_path)
    profile = learner_profile.derive(42, as_of="2026-07-22", db_path=db)
    assert profile["weakest_subject"]["subject"] == "Physics"
    assert profile["weakest_subject"]["accuracy_pct"] == 50
    assert profile["strongest_subject"]["subject"] == "Chem"
    assert profile["rhythm"]["best_window"] == "evening"
    assert profile["preferences"] == ["I prefer concise explanations"]
    assert profile["workload"] == {
        "backlog_count": 1,
        "overdue_revision_count": 1,
        "unresolved_doubt_count": 1,
    }


def test_profile_save_is_stable_when_only_derived_timestamp_changes(tmp_path):
    db = seeded_profile_db(tmp_path)
    first = learner_profile.derive(42, as_of="2026-07-22", db_path=db)
    second = learner_profile.derive(42, as_of="2026-07-22", db_path=db)
    assert learner_profile.save(first, db_path=db) is True
    assert learner_profile.save(second, db_path=db) is False
    assert learner_profile.latest(42, db_path=db)["as_of_date"] == "2026-07-22"


def test_nightly_insights_are_new_deduplicated_and_evidence_linked(tmp_path):
    db = seeded_profile_db(tmp_path)
    first = learner_profile.nightly_insight(
        42, as_of="2026-07-22", use_llm=False, db_path=db
    )
    second = learner_profile.nightly_insight(
        42, as_of="2026-07-22", use_llm=False, db_path=db
    )
    assert first is not None and first["created"] is True
    assert first["key"] == "weak-subject:physics"
    assert set(first["evidence"]) == {
        "subject.Physics.attempted", "subject.Physics.accuracy_pct"
    }
    assert second is not None and second["key"] == "best-window:evening"
    assert len(learner_profile.list_insights(42, db_path=db)) == 2


def test_profile_is_available_to_advisor_prompt_context(tmp_path):
    db = seeded_profile_db(tmp_path)
    learner_profile.refresh(42, as_of="2026-07-22", db_path=db)
    block = advisor.memory_prompt_block(42, db_path=db)
    assert "LEARNER PROFILE" in block
    assert "Physics — 50% across 60 attempts" in block
    assert "Best evidenced study window: evening" in block


def test_profile_personalizes_planner_and_planning_reminder(tmp_path):
    db = seeded_profile_db(tmp_path)
    learner_profile.refresh(42, as_of="2026-07-22", db_path=db)
    analysis = planner.analyze("2026-07-22", chat_id=42, db_path=db)
    revision = next(
        item for item in analysis["suggestions"]
        if item["source_signal"] == "overdue_revision_unplanned"
    )
    assert revision["priority"] == 85
    assert "revision pressure" in revision["personalization"][0]

    message = reminders.planning_message(
        now=dt.datetime(2026, 7, 22, 21, 0),
        chat_id=42,
        db_path=db,
    )
    assert "Personalized focus" in message
    assert "Physics" in message
