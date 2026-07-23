from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

import exam_readiness
import message_templates
import operational_store
import reminders
import study_domain as sd
import sync


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "readiness.db"
    with sync.connect(path) as conn:
        sync.init_db(conn)
        operational_store.init_db(conn)
    return path


def insert_mirror(path, table, **values):
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


def create_exam(path, *, title="Rotation mock", day="2026-07-29", syllabus="Physics: Rotation"):
    return sd.create_exam({
        "title": title, "exam_date": day, "kind": "JEE Main Mock",
        "date_confidence": "Official", "syllabus": syllabus,
        "operation_id": f"exam-{title}-{day}",
    }, db_path=path)


def test_full_audit_filters_syllabus_and_uses_exact_seven_day_evidence(db):
    exam = create_exam(db)
    insert_mirror(
        db, "doubts", notion_page_id="d-rotation",
        core_concept="angular momentum in rolling", subject="Physics",
        chapter="Rotation", status="Unresolved", workflow_state="Attempting",
    )
    insert_mirror(
        db, "doubts", notion_page_id="d-mole", core_concept="mole fraction sign",
        subject="Chem", chapter="Mole concept", status="Unresolved",
        workflow_state="New",
    )
    # Metadata-free legacy doubts stay visible so filtering cannot silently
    # hide something that may be in the mock.
    insert_mirror(
        db, "doubts", notion_page_id="d-unknown", core_concept="diagram ambiguity",
        status="Unresolved", workflow_state="New",
    )
    for number, when in enumerate(("2026-07-20T10:00:00+00:00", "2026-07-21T10:00:00+00:00"), 1):
        operational_store.create("doubt_attempts", {
            "title": f"Rotation attempt {number}", "doubt": "d-rotation",
            "attempt_no": number, "attempted_at": when, "duration_min": 15,
            "approach": f"approach {number}", "stuck_point": "sign",
            "outcome": "Unsolved", "valid": True,
            "operation_id": f"rotation-a{number}",
        }, db_path=db)

    insert_mirror(
        db, "ledger", notion_page_id="kp-recent", task="Rotation PYQs",
        date="2026-07-16", subject="Physics", chapter_text="Rotation",
        key_points_notes="Torque about contact point avoids unknown friction.",
    )
    insert_mirror(
        db, "ledger", notion_page_id="kp-too-old", task="Rotation notes",
        date="2026-07-15", subject="Physics", chapter_text="Rotation",
        key_points_notes="This is outside the inclusive seven-day window.",
    )
    insert_mirror(
        db, "ledger", notion_page_id="kp-off-topic", task="Mole PYQs",
        date="2026-07-22", subject="Chem", chapter_text="Mole concept",
        key_points_notes="Use limiting reagent first.",
    )
    insert_mirror(
        db, "revision", notion_page_id="r-rotation", chapter_module="Rotation",
        subject="Physics", status="Pending", mastery="In progress",
        next_execution_date="2026-07-24",
    )
    insert_mirror(
        db, "revision", notion_page_id="r-mole", chapter_module="Mole concept",
        subject="Chem", status="Pending", mastery="In progress",
        next_execution_date="2026-07-24",
    )

    with sqlite3.connect(db) as conn:
        plans_before = conn.execute("SELECT COUNT(*) FROM daily_plan").fetchone()[0]
    now = dt.datetime(2026, 7, 22, 12, tzinfo=dt.timezone.utc)
    snapshot = exam_readiness.collect(exam, now=now, db_path=db, phase="t7")
    with sqlite3.connect(db) as conn:
        plans_after = conn.execute("SELECT COUNT(*) FROM daily_plan").fetchone()[0]

    assert {row["notion_page_id"] for row in snapshot["doubts"]} == {
        "d-rotation", "d-unknown",
    }
    assert snapshot["teacher_ready_count"] == 1
    assert snapshot["zero_attempt_count"] == 1
    assert [row["notion_page_id"] for row in snapshot["key_points"]] == ["kp-recent"]
    assert [row["notion_page_id"] for row in snapshot["revision"]] == ["r-rotation"]
    assert snapshot["created_plan_rows"] == 0
    assert plans_after == plans_before == 0

    card = message_templates.exam_readiness(snapshot)
    assert "T−7" in card
    assert "teacher-ready" in card and "0/2 attempts" in card
    assert "Evidence audit only — no Daily Plan rows were created" in card


def test_exam_specific_classification_never_closes_global_doubt(db):
    exam = create_exam(db, syllabus="")
    insert_mirror(
        db, "doubts", notion_page_id="d1", core_concept="relative velocity",
        subject="Physics", status="Unresolved", workflow_state="New",
    )
    first = exam_readiness.collect(exam, db_path=db)
    token = first["doubts"][0]["readiness_token"]

    exam_readiness.set_decision(token, "not_in_exam", db_path=db)
    excluded = exam_readiness.collect(exam, db_path=db)
    assert excluded["doubts"] == []
    assert excluded["excluded_doubts"][0]["notion_page_id"] == "d1"
    assert sd.doubt_queue(db_path=db)[0]["readiness"] == "new"

    exam_readiness.set_decision(token, "open", db_path=db)
    restored = exam_readiness.collect(exam, db_path=db)
    assert restored["doubts"][0]["exam_decision"] == "open"


def test_t7_t3_t1_and_exam_day_are_distinct_and_deduplicated(db):
    exam = create_exam(db, day="2026-07-29")
    expected = {7: "t7", 3: "t3", 1: "t1", 0: "day"}
    keys = set()
    for days, phase in expected.items():
        now = dt.datetime.combine(
            dt.date(2026, 7, 29) - dt.timedelta(days=days),
            dt.time(8), tzinfo=dt.timezone.utc,
        )
        reviews = exam_readiness.scheduled_reviews(now=now, db_path=db)
        assert [(row["notion_page_id"], got) for row, got in reviews] == [
            (exam["notion_page_id"], phase)
        ]
        key = exam_readiness.event_key(exam, phase)
        keys.add(key)
        assert reminders.claim(key, db_path=db) is True
        assert reminders.claim(key, db_path=db) is False
    assert len(keys) == 4
    # Day 2 is still in the T-3 window.  If the bot was offline on exact T-3,
    # the scan catches up; the already-claimed phase key prevents a duplicate.
    catch_up = exam_readiness.scheduled_reviews(
        now=dt.datetime(2026, 7, 27, 8, tzinfo=dt.timezone.utc), db_path=db
    )
    assert [(row["notion_page_id"], phase) for row, phase in catch_up] == [
        (exam["notion_page_id"], "t3")
    ]
    assert reminders.claim(
        exam_readiness.event_key(exam, "t3"), db_path=db
    ) is False


def test_solved_button_requires_typed_resolution_evidence(db, monkeypatch):
    exam = create_exam(db, syllabus="")
    insert_mirror(
        db, "doubts", notion_page_id="d1", core_concept="relative velocity",
        subject="Physics", status="Unresolved", workflow_state="New",
    )
    snapshot = exam_readiness.collect(exam, db_path=db)
    token = snapshot["doubts"][0]["readiness_token"]
    exam_readiness.start_resolution(99, token, db_path=db)
    with pytest.raises(sd.DomainError, match="corrected idea"):
        exam_readiness.complete_resolution(99, "done", db_path=db)
    assert exam_readiness.pending_resolution(99, db_path=db) is not None

    writes = []
    monkeypatch.setattr(sd.notion, "update_page", lambda page_id, props: writes.append((page_id, props)) or {})
    monkeypatch.setattr(sd, "_sync", lambda *args, **kwargs: None)
    result = exam_readiness.complete_resolution(
        99, "The relative velocity sign follows the chosen frame direction.",
        db_path=db,
    )
    assert result["doubt_id"] == "d1"
    assert writes[0][0] == "d1"
    assert writes[0][1]["status"] == "Resolved"
    assert exam_readiness.pending_resolution(99, db_path=db) is None


def test_revision_after_exam_and_without_date_are_reported_as_risks(db):
    exam = create_exam(db, day="2026-07-29")
    insert_mirror(
        db, "revision", notion_page_id="r-late", chapter_module="Rotation",
        subject="Physics", status="Pending", mastery="In progress",
        next_execution_date="2026-08-02",
    )
    insert_mirror(
        db, "revision", notion_page_id="r-unscheduled",
        chapter_module="Rotation error log", subject="Physics",
        status="Pending", mastery="Not started", next_execution_date=None,
    )
    insert_mirror(
        db, "revision", notion_page_id="r-done",
        chapter_module="Rotation completed", subject="Physics",
        status="Completed", mastery="Done", next_execution_date="2026-07-23",
    )
    snapshot = exam_readiness.collect(
        exam, now=dt.datetime(2026, 7, 22, 12, tzinfo=dt.timezone.utc),
        db_path=db, phase="t7",
    )
    assert {
        row["notion_page_id"]: row["exam_schedule_risk"]
        for row in snapshot["revision"]
    } == {
        "r-late": "scheduled_after_exam",
        "r-unscheduled": "unscheduled",
    }
    card = message_templates.exam_readiness(snapshot)
    assert "after exam" in card and "no date" in card


def test_strategy_ranks_chapters_by_mistakes_doubts_revision_and_time(db):
    exam = create_exam(db, day="2026-07-29", syllabus="Physics: Rotation")
    insert_mirror(
        db, "doubts", notion_page_id="d-rotation",
        core_concept="rolling torque sign", subject="Physics", chapter="Rotation",
        status="Unresolved", workflow_state="New",
    )
    insert_mirror(
        db, "revision", notion_page_id="r-rotation", chapter_module="Rotation",
        subject="Physics", status="Pending", next_execution_date="2026-08-02",
    )
    for index in range(2):
        operational_store.create("exam_questions", {
            "title": f"Old mock Q{index + 1}", "exam": "old-mock",
            "question_no": str(index + 1), "subject": "Physics",
            "chapter": "Rotation", "failure_type": "Concept",
            "marks_lost": 8, "operation_id": f"strategy-q-{index}",
        }, db_path=db)
        insert_mirror(
            db, "ledger", notion_page_id=f"rotation-block-{index}",
            task="Rotation timed drill", date=f"2026-07-{20 + index}",
            subject="Physics", chapter_text="Rotation", questions_attempted=20,
            questions_correct=10, accuracy_ratio=0.5, cognitive_yield=30,
        )

    t7 = exam_readiness.collect(
        exam, now=dt.datetime(2026, 7, 22, 12, tzinfo=dt.timezone.utc),
        db_path=db, phase="t7",
    )
    top = t7["strategy_priorities"][0]
    assert top["chapter"] == "Rotation"
    assert top["zero_attempt_doubts"] == 1
    assert top["marks_lost"] == 16
    assert "scheduled_after_exam" in top["revision_risks"]
    assert top["avg_accuracy"] == 50
    assert top["weightage_proxy"]["basis"].startswith("recorded past-paper")
    assert t7["strategy"]["mode"] == "prioritize_and_rehearse"

    t1 = exam_readiness.collect(
        exam, now=dt.datetime(2026, 7, 28, 12, tzinfo=dt.timezone.utc),
        db_path=db, phase="t1",
    )
    assert t1["strategy"]["mode"] == "protect_known_marks"
    assert t1["strategy_priorities"][0]["priority_score"] > top["priority_score"]


def test_readiness_card_stays_below_telegram_limit():
    very_long = "x" * 1000
    snapshot = {
        "exam": {"title": very_long}, "exam_id": "exam",
        "days_until": 7, "phase": "t7", "syllabus_known": True,
        "syllabus": very_long, "zero_attempt_count": 10,
        "teacher_ready_count": 0, "scope_uncertain_count": 10,
        "excluded_doubts": [],
        "doubts": [{
            "core_concept": very_long, "subject": "Physics",
            "readiness": "new", "valid_attempts": 0,
            "scope_uncertain": True,
        } for _ in range(10)],
        "revision": [{
            "chapter_module": very_long, "exam_schedule_risk": "unscheduled",
            "next_execution_date": None,
        } for _ in range(10)],
        "key_points": [{
            "date": "2026-07-22", "task": very_long,
            "key_points_notes": very_long,
        } for _ in range(10)],
    }
    card = message_templates.exam_readiness(snapshot)
    assert len(card) <= 4000
    assert card.endswith("Evidence audit only — no Daily Plan rows were created.")
