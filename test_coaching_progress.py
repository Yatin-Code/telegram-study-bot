"""Phase 9 tests — deterministic, evidence-aware coaching progress.

Offline temp-mirror tests for ``coaching_progress``:
  * schema / identity / validation
  * the merge invariant (weaker evidence can never override stronger)
  * upsert + safe read APIs
  * completion / coverage summaries joined to the upcoming syllabus
  * cooldown-aware missing-data question prompts
  * write-ready prep (prepare -> run) for later tool integration

Usage:
    python test_coaching_progress.py
    .venv-test/bin/python -m pytest -q test_coaching_progress.py
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

import pytest

import coaching_progress as cp
import coaching_syllabus as cs
import ntsc_coaching


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "progress.db"
    with ntsc_coaching._connect(path) as conn:
        ntsc_coaching.init_db(conn)
    return path


def seed_test(db: Path, *, source_id="t1", title="Weekly Test 1",
              date="2026-08-15", syllabus="Physics: Kinematics, Laws of Motion, Vectors"):
    ntsc_coaching.replace_tests([
        {"id": source_id, "testName": title, "testDateTime": f"{date}T09:00:00",
         "courseId": "7", "batch": "B1", "goal": "Test", "syllabus": syllabus},
    ], db_path=db)
    cs.replace_syllabi([
        {"id": source_id, "syllabus": syllabus},
    ], db_path=db)


def insert_progress(db: Path, **record):
    base = {"subject": "Physics", "topic": "Kinematics",
            "verification_source": "self_reported"}
    base.update(record)
    result = cp.upsert_progress(base, db_path=db)
    assert result["ok"], result.get("errors")
    return result["record"]


# ---------------------------------------------------------------------------
# Schema / identity
# ---------------------------------------------------------------------------

def test_init_db_idempotent(tmp_path):
    db = tmp_path / "schema.db"
    conn = cp._connect(db)
    cp.init_db(conn)
    cp.init_db(conn)
    tables = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert cp.PROGRESS_TABLE in tables
    assert cp.PROMPT_HISTORY_TABLE in tables
    conn.close()


def test_ntsc_coaching_init_does_not_create_progress_tables(tmp_path):
    db = tmp_path / "coaching.db"
    conn = sqlite3.connect(str(db))
    ntsc_coaching.init_db(conn)
    tables = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    # Phase 9 owns its schema; existing coaching init must not conflict.
    assert cp.PROGRESS_TABLE not in tables
    conn.close()


def test_progress_key_deterministic_and_case_insensitive():
    a = cp.progress_key_for("Physics", "Kinematics", "kinematics")
    b = cp.progress_key_for(" physics ", "KINEMATICS", "KINEMATICS")
    assert a == b
    c = cp.progress_key_for("Physics", "Kinematics", "Vectors")
    assert a != c


def test_progress_key_stable_across_syllabus_normalization():
    # Same identity after whitespace drift.
    assert cp.progress_key_for("Physics", " Kinematics ", "Kinematics ") == \
        cp.progress_key_for("Physics", "Kinematics", "Kinematics")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_validate_requires_subject_and_topic():
    errors, cleaned = cp.validate_progress({"subject": "Physics"})
    assert "topic is required" in errors
    errors, cleaned = cp.validate_progress({"topic": "Kinematics"})
    assert "subject is required" in errors
    errors, cleaned = cp.validate_progress({"subject": "Physics", "topic": "Kinematics"})
    assert errors == []


def test_validate_rejects_negative_and_done_above_total():
    errors, _ = cp.validate_progress({"subject": "P", "topic": "T", "mle_done": -1})
    assert any("mle_done" in e and "non-negative" in e for e in errors)
    errors, _ = cp.validate_progress(
        {"subject": "P", "topic": "T", "mle_done": 9, "mle_total": 5}
    )
    assert any("cannot exceed" in e for e in errors)


def test_validate_rejects_bad_confidence_mastery_source_and_date():
    errors, _ = cp.validate_progress(
        {"subject": "P", "topic": "T", "confidence": 150}
    )
    assert any("confidence" in e for e in errors)
    errors, _ = cp.validate_progress({"subject": "P", "topic": "T", "mastery": "done"})
    assert any("mastery" in e for e in errors)
    errors, _ = cp.validate_progress(
        {"subject": "P", "topic": "T", "verification_source": "guessed"}
    )
    assert any("verification_source" in e for e in errors)
    errors, _ = cp.validate_progress(
        {"subject": "P", "topic": "T", "last_verified": "15-08-2026"}
    )
    assert any("last_verified" in e for e in errors)


def test_validate_coerces_numeric_strings_and_ignores_unknown_keys():
    errors, cleaned = cp.validate_progress({
        "subject": "Physics", "topic": "Kinematics", "exercise_done": "5",
        "exercise_total": "8", "confidence": "70", "future_field": 1,
    })
    assert errors == []
    assert cleaned["exercise_done"] == 5
    assert cleaned["exercise_total"] == 8
    assert cleaned["confidence"] == 70
    assert "future_field" not in cleaned


def test_validate_rejects_boolean_counts():
    errors, _ = cp.validate_progress({"subject": "P", "topic": "T", "pyq_done": True})
    assert any("boolean" in e for e in errors)


def test_validate_missing_fields_are_none_not_zero():
    _, cleaned = cp.validate_progress({"subject": "P", "topic": "T"})
    assert cleaned["mle_done"] is None
    assert cleaned["mle_total"] is None


# ---------------------------------------------------------------------------
# Merge: weaker evidence never overrides stronger
# ---------------------------------------------------------------------------

def test_merge_self_report_cannot_override_evidence():
    stored = {"exercise_done": 10, "exercise_total": 10, "mle_done": 8,
              "mle_total": 10, "pyq_done": 5, "pyq_total": 5,
              "verification_source": "evidence_backed", "mastery": "mastered",
              "confidence": 90, "last_verified": "2026-08-01", "notes": "ev"}
    incoming = {"exercise_done": 2, "verification_source": "self_reported",
                "mastery": "learning", "confidence": 30, "notes": "weaker"}
    merged = cp.merge_progress(stored, incoming)
    assert merged["exercise_done"] == 10
    assert merged["mle_done"] == 8
    assert merged["confidence"] == 90
    assert merged["mastery"] == "mastered"
    assert merged["verification_source"] == "evidence_backed"
    assert merged["last_verified"] == "2026-08-01"
    assert merged["notes"] == "ev"


def test_merge_stronger_evidence_corrects_self_report():
    stored = {"exercise_done": 10, "exercise_total": 10, "mle_done": 8,
              "mle_total": 10, "verification_source": "self_reported",
              "confidence": 90, "notes": "old"}
    incoming = {"exercise_done": 5, "exercise_total": 6,
                "verification_source": "evidence_backed", "confidence": 60,
                "last_verified": "2026-08-02", "notes": "checked"}
    merged = cp.merge_progress(stored, incoming)
    assert merged["exercise_done"] == 5
    assert merged["exercise_total"] == 6
    assert merged["confidence"] == 60
    assert merged["verification_source"] == "evidence_backed"
    assert merged["last_verified"] == "2026-08-02"
    assert merged["notes"] == "checked"


def test_merge_equal_strength_is_monotonic_and_never_decreases():
    stored = {"exercise_done": 6, "exercise_total": 8, "mle_done": 4, "mle_total": 4,
              "verification_source": "self_reported", "confidence": 50, "mastery": "learning"}
    incoming = {"exercise_done": 3, "exercise_total": 6,
                "verification_source": "self_reported", "confidence": 70, "mastery": "learning"}
    merged = cp.merge_progress(stored, incoming)
    assert merged["exercise_done"] == 6
    assert merged["exercise_total"] == 8
    assert merged["mle_done"] == 4
    assert merged["confidence"] == 70
    assert merged["verification_source"] == "self_reported"


def test_merge_totals_grow_to_stay_consistent_with_done():
    stored = {"exercise_done": 5, "exercise_total": 5, "verification_source": "self_reported"}
    incoming = {"exercise_done": 9, "verification_source": "self_reported"}
    merged = cp.merge_progress(stored, incoming)
    assert merged["exercise_done"] == 9
    assert merged["exercise_total"] == 9


def test_merge_unknown_fields_do_not_reset_existing():
    stored = {"mle_done": 7, "mle_total": 10, "pyq_done": 1, "pyq_total": 3,
              "verification_source": "self_reported"}
    incoming = {"exercise_done": 2, "verification_source": "evidence_backed"}
    merged = cp.merge_progress(stored, incoming)
    assert merged["mle_done"] == 7
    assert merged["mle_total"] == 10
    assert merged["pyq_done"] == 1
    assert merged["pyq_total"] == 3


def test_merge_equal_strength_appends_distinct_notes():
    stored = {"notes": "first", "verification_source": "self_reported"}
    incoming = {"notes": "second", "verification_source": "self_reported"}
    merged = cp.merge_progress(stored, incoming)
    assert merged["notes"] == "first\nsecond"
    # same note is a no-op
    merged = cp.merge_progress(stored, {"notes": "first", "verification_source": "self_reported"})
    assert merged["notes"] == "first"


def test_merge_ladder_only_rises():
    for weaker, stronger in (
        ("unknown", "self_reported"),
        ("self_reported", "partially_evidenced"),
        ("partially_evidenced", "evidence_backed"),
    ):
        merged = cp.merge_progress(
            {"verification_source": weaker}, {"verification_source": stronger}
        )
        assert merged["verification_source"] == stronger
        merged = cp.merge_progress(
            {"verification_source": stronger}, {"verification_source": weaker}
        )
        assert merged["verification_source"] == stronger


# ---------------------------------------------------------------------------
# Upsert + reads
# ---------------------------------------------------------------------------

def test_upsert_creates_and_reads_back(db):
    result = cp.upsert_progress({
        "subject": "Physics", "chapter": "Kinematics", "topic": "Kinematics",
        "exercise_done": 5, "exercise_total": 8, "mle_done": 3,
        "verification_source": "self_reported", "confidence": 60,
        "mastery": "practiced", "notes": "did the ex",
    }, db_path=db)
    assert result["ok"] is True
    assert result["changed"] is True
    row = cp.get_progress_by_key(result["progress_key"], db_path=db)
    assert row["exercise_done"] == 5
    assert row["verification_source"] == "self_reported"
    assert row["mastery"] == "practiced"
    assert row["notes"] == "did the ex"
    rows = cp.get_progress(subject="Physics", topic="Kinematics", db_path=db)
    assert len(rows) == 1
    assert len(cp.all_progress(db_path=db)) == 1


def test_upsert_updates_in_place_no_duplicates(db):
    cp.upsert_progress({"subject": "Physics", "topic": "Kinematics",
                        "exercise_done": 2, "verification_source": "self_reported"}, db_path=db)
    cp.upsert_progress({"subject": "Physics", "topic": "Kinematics",
                        "exercise_done": 4, "verification_source": "self_reported"}, db_path=db)
    rows = cp.all_progress(db_path=db)
    assert len(rows) == 1
    assert rows[0]["exercise_done"] == 4


def test_upsert_rejects_invalid(db):
    result = cp.upsert_progress({"subject": "Physics", "topic": "Kinematics",
                                 "mle_done": -1}, db_path=db)
    assert result["ok"] is False
    assert result["errors"]
    assert cp.all_progress(db_path=db) == []


def test_upsert_self_report_noop_does_not_touch_evidence(db):
    first = cp.upsert_progress({"subject": "Physics", "topic": "Kinematics",
                                "exercise_done": 10, "exercise_total": 10,
                                "verification_source": "evidence_backed",
                                "last_verified": "2026-08-01"}, db_path=db)
    assert first["changed"] is True
    second = cp.upsert_progress({"subject": "Physics", "topic": "Kinematics",
                                 "exercise_done": 3,
                                 "verification_source": "self_reported"}, db_path=db)
    assert second["changed"] is False
    row = cp.get_progress_by_key(first["progress_key"], db_path=db)
    assert row["exercise_done"] == 10
    assert row["verification_source"] == "evidence_backed"


def test_bulk_upsert_summary(db):
    result = cp.bulk_upsert([
        {"subject": "Physics", "topic": "Kinematics", "exercise_done": 1,
         "verification_source": "self_reported"},
        {"subject": "Physics", "topic": "Vectors", "exercise_done": 2,
         "verification_source": "self_reported"},
        {"subject": "Physics", "topic": "", "exercise_done": 1},
    ], db_path=db)
    assert result["saved"] == 2
    assert len(result["rejected"]) == 1
    assert len(cp.all_progress(db_path=db)) == 2


def test_get_progress_safe_when_table_missing(tmp_path):
    db = tmp_path / "empty.db"
    assert cp.all_progress(db_path=db) == []


# ---------------------------------------------------------------------------
# Completion / coverage summaries joined to upcoming syllabus
# ---------------------------------------------------------------------------

def test_completion_summary_joins_syllabus_and_progress(db):
    seed_test(db)
    insert_progress(db, topic="Kinematics", exercise_done=5, exercise_total=8,
                    verification_source="self_reported")
    insert_progress(db, topic="Vectors", exercise_done=8, exercise_total=8,
                    mle_done=10, mle_total=10, pyq_done=5, pyq_total=5,
                    mastery="mastered", verification_source="evidence_backed",
                    last_verified="2026-08-01")

    summary = cp.completion_summary(today="2026-08-01", db_path=db)
    assert len(summary) == 1
    test = summary[0]
    assert test["source_id"] == "t1"
    assert test["counts"]["topic_count"] == 3
    assert test["counts"]["with_progress"] == 2
    assert test["counts"]["evidenced"] == 1
    assert test["counts"]["complete"] == 1
    assert test["counts"]["missing"] == 1
    by_topic = {r["topic"]: r for r in test["records"]}
    assert by_topic["Kinematics"]["status"] == "progress"
    assert by_topic["Vectors"]["status"] == "complete"
    assert by_topic["Laws of Motion"]["status"] == "no_progress"
    assert by_topic["Laws of Motion"]["progress"] is None
    assert test["totals"]["exercise_done"] == 13
    assert test["totals"]["exercise_total"] == 16


def test_completion_summary_empty_without_tests(tmp_path):
    db = tmp_path / "no_tests.db"
    assert cp.completion_summary(today="2026-08-01", db_path=db) == []


def test_coverage_summary_aggregates_by_subject(db):
    seed_test(db, syllabus="Physics: Kinematics, Vectors\nChemistry: Mole Concept")
    insert_progress(db, subject="Physics", topic="Kinematics", mle_done=5, mle_total=5,
                    verification_source="evidence_backed")
    insert_progress(db, subject="Physics", topic="Vectors", verification_source="self_reported")

    summary = cp.coverage_summary(today="2026-08-01", db_path=db)
    subjects = {s["subject"]: s for s in summary["subjects"]}
    assert set(subjects) == {"Physics", "Chemistry"}
    physics = subjects["Physics"]
    assert physics["topic_count"] == 2
    assert physics["with_progress"] == 2
    assert physics["evidenced"] == 1
    assert physics["missing"] == 0
    chemistry = subjects["Chemistry"]
    assert chemistry["missing"] == 1
    assert summary["tests"][0]["with_progress"] == 2


# ---------------------------------------------------------------------------
# Question prompts: missing data only + cooldown
# ---------------------------------------------------------------------------

def test_missing_data_questions_cover_only_missing(db):
    seed_test(db)
    insert_progress(db, topic="Kinematics", exercise_done=8, exercise_total=8,
                    mle_done=10, mle_total=10, pyq_done=5, pyq_total=5,
                    mastery="mastered", verification_source="evidence_backed",
                    last_verified="2026-08-01")
    insert_progress(db, topic="Vectors", exercise_done=2, exercise_total=8,
                    verification_source="self_reported")

    questions = cp.missing_data_questions(today="2026-08-01", db_path=db)
    topics = {q["topic"] for q in questions}
    # fully done+evidenced topic is never asked; Vectors (self-report only) and
    # Laws of Motion (no progress) are.
    assert "Kinematics" not in topics
    assert "Laws of Motion" in topics
    assert "Vectors" in topics
    # highest-priority (missing entirely) comes first
    assert questions[0]["topic"] == "Laws of Motion"
    assert questions[0]["priority"] == 1
    vectors = next(q for q in questions if q["topic"] == "Vectors")
    assert vectors["priority"] == 3
    assert "self-reported" in vectors["reason"]


def test_missing_data_questions_priority_partially_evidenced(db):
    seed_test(db)
    insert_progress(db, topic="Kinematics", mle_done=4, mle_total=10,
                    verification_source="partially_evidenced", last_verified="2026-08-01")
    questions = cp.missing_data_questions(today="2026-08-01", db_path=db)
    kin = next(q for q in questions if q["topic"] == "Kinematics")
    assert kin["priority"] == 4
    assert "mle" in kin["reason"]
    assert "mle" in kin["question"].lower()


def test_questions_respect_cooldown(db):
    seed_test(db, source_id="t2", title="Later Test", date="2026-09-25",
              syllabus="Physics: Kinematics")
    questions = cp.missing_data_questions(today="2026-09-01", db_path=db)
    assert any(q["topic"] == "Kinematics" for q in questions)

    key = cp.progress_key_for("Physics", None, "Kinematics")
    cp.record_prompt_asked(key, "did you cover it?",
                           db_path=db, now="2026-09-10T00:00:00+00:00")

    # within cooldown -> skipped
    questions = cp.missing_data_questions(today="2026-09-01", db_path=db)
    assert not any(q["progress_key"] == key for q in questions)
    assert cp.recently_asked(key, db_path=db, today="2026-09-01") is True

    # after cooldown expires (cooldown_until 2026-09-17) -> eligible again
    assert cp.recently_asked(key, db_path=db, today="2026-09-18") is False
    questions = cp.missing_data_questions(today="2026-09-18", db_path=db)
    assert any(q["progress_key"] == key for q in questions)


def test_questions_capped_and_deterministic(db):
    seed_test(db)
    first = cp.missing_data_questions(today="2026-08-01", db_path=db,
                                      max_prompts=2)
    assert len(first) == 2
    second = cp.missing_data_questions(today="2026-08-01", db_path=db,
                                       max_prompts=2)
    assert [q["progress_key"] for q in first] == [q["progress_key"] for q in second]


# ---------------------------------------------------------------------------
# Write-ready prep (prepare -> run), no shared-file changes
# ---------------------------------------------------------------------------

def test_prepare_progress_write_previews_without_writing(db):
    prep = cp.prepare_progress_write({
        "subject": "Physics", "topic": "Kinematics", "exercise_done": 5,
        "exercise_total": 8, "verification_source": "self_reported",
    }, db_path=db)
    assert prep["ok"] is True
    assert "Kinematics" in prep["preview"]
    assert "self_reported" in prep["preview"]
    assert prep["run"]["record"]["exercise_done"] == 5
    assert cp.all_progress(db_path=db) == []


def test_prepare_progress_write_warns_on_weaker_than_stored(db):
    insert_progress(db, topic="Kinematics", exercise_done=10, exercise_total=10,
                    verification_source="evidence_backed")
    prep = cp.prepare_progress_write({
        "subject": "Physics", "topic": "Kinematics", "exercise_done": 3,
        "verification_source": "self_reported",
    }, db_path=db)
    assert prep["ok"] is True
    assert "stronger" in prep["preview"]
    assert "counts are kept" in prep["preview"]
    # merged shows the preserved evidence-backed values
    assert prep["merged"]["exercise_done"] == 10


def test_prepare_progress_write_warns_on_evidence_upgrade(db):
    insert_progress(db, topic="Kinematics", exercise_done=5,
                    verification_source="self_reported")
    upgrade = cp.prepare_progress_write({
        "subject": "Physics", "topic": "Kinematics", "exercise_done": 5,
        "verification_source": "evidence_backed", "last_verified": "2026-08-02",
    }, db_path=db)
    assert "Raising verification" in upgrade["preview"]


def test_prepare_progress_write_rejects_invalid(db):
    prep = cp.prepare_progress_write({"subject": "Physics", "topic": "",
                                      "mle_done": -1}, db_path=db)
    assert prep["ok"] is False
    assert prep["errors"]


def test_run_progress_write_applies_prepared_write(db):
    prep = cp.prepare_progress_write({
        "subject": "Physics", "topic": "Vectors", "exercise_done": 7,
        "exercise_total": 10, "verification_source": "self_reported",
    }, db_path=db)
    result = cp.run_progress_write(prep["run"], db_path=db)
    assert result["ok"] is True
    rows = cp.all_progress(db_path=db)
    assert len(rows) == 1
    assert rows[0]["topic"] == "Vectors"
    assert rows[0]["exercise_done"] == 7


def test_run_progress_write_survives_missing_run(db):
    result = cp.run_progress_write({}, db_path=db)
    assert result["ok"] is False
    assert result["errors"]
