"""Phase 10 tests — deterministic next-doubt coaching (coaching_doubts.py).

Temp mirror DBs only; no network, no LLM.  Covers:
  * the priority ladder (class subject → syllabus topic → repeated failures →
    marks lost → teacher readiness → age) with evidence/reason/confidence
  * the show → attempt → hint/retry → resolved/retest interaction lifecycle
    persisted in local SQLite, with write plans for durable attempt writes
  * teacher-ready selection aligned to a teacher window
  * deterministic, read-only selection and never-fabricated hints/solutions

Usage:
    .venv-test/bin/python -m pytest -q test_coaching_doubts.py
"""

from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

import coaching_doubts as cd
import coaching_syllabus
import ntsc_coaching
import operational_store
import sync

UTC = dt.timezone.utc


def _utc(year, month, day, hour=0, minute=0):
    return dt.datetime(year, month, day, hour, minute, tzinfo=UTC)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "coach_doubts.db"
    with sync.connect(path) as conn:
        sync.init_db(conn)
        operational_store.init_db(conn)
        ntsc_coaching.init_db(conn)
        cd.init_db(conn)
    monkeypatch.setattr(cd.session_context, "local_today_iso", lambda: "2026-08-02")
    return path


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
        "last_synced_at": "2026-07-20T00:00:00+00:00",
        "raw_json": "{}",
    }
    if physical.startswith("op_"):
        base.setdefault("id", base["notion_page_id"])
        base.setdefault("created_time", "2026-07-20T00:00:00+00:00")
        base.setdefault("last_edited_time", "2026-07-20T00:00:00+00:00")
    base.update(values)
    with sqlite3.connect(path) as conn:
        if physical.startswith("op_"):
            operational_store.init_db(conn)
        cols = ",".join(f'"{key}"' for key in base)
        marks = ",".join("?" for _ in base)
        conn.execute(
            f'INSERT INTO "{physical}" ({cols}) VALUES ({marks})', tuple(base.values())
        )
        conn.commit()


def add_doubt(db, *, concept, subject=None, page_id=None, status="Unresolved",
              workflow_state=None, teacher_ready=0, created_time="2026-07-20T00:00:00+00:00"):
    values = {
        "core_concept": concept,
        "subject": subject,
        "status": status,
        "workflow_state": workflow_state or ("Eligible for Teacher" if teacher_ready else "New"),
        "teacher_ready": teacher_ready,
        "created_time": created_time,
    }
    insert(db, "doubts", notion_page_id=page_id or f"d-{concept}", **values)


def add_attempt(db, doubt_id, *, outcome="Unsolved", attempt_no=1, valid=1,
                attempted_at="2026-08-01T09:00:00+00:00"):
    insert(
        db, "doubt_attempts", notion_page_id=f"a-{doubt_id}-{attempt_no}",
        title=f"Attempt {attempt_no}", doubt=doubt_id, attempt_no=attempt_no,
        attempted_at=attempted_at, duration_min=20, outcome=outcome,
        approach="tried it", stuck_point="stuck here", valid=valid,
    )


def add_marks(db, *, subject, chapter, marks_lost):
    insert(
        db, "exam_questions", notion_page_id=f"q-{subject}-{chapter}",
        title="Question review", subject=subject, chapter=chapter,
        marks_lost=marks_lost, failure_type="Concept",
    )


def add_class(db, date, start, duration_min=60, class_type="Lecture", subjects="Physics"):
    with sqlite3.connect(db) as conn:
        ntsc_coaching.init_db(conn)
        conn.execute(
            "INSERT INTO coaching_classes "
            "(source_id,class_date,start_time,duration_min,class_type,subjects,source_updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (f"{date}|{start}", date, start, duration_min, class_type, subjects, "2026-07-20"),
        )
        conn.commit()


def add_test(db, source_id, title, test_date, syllabus_records=None):
    with sqlite3.connect(db) as conn:
        ntsc_coaching.init_db(conn)
        conn.execute(
            "INSERT INTO coaching_tests "
            "(source_id,title,test_date,course_id,batch,goal,syllabus,source_updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (source_id, title, test_date, "", "", "", "", "2026-07-20"),
        )
        conn.commit()
    if syllabus_records:
        coaching_syllabus.store_test_syllabus(source_id, syllabus_records, db_path=db)


def concepts(ranked):
    return [r["concept"] for r in ranked]


# ---------------------------------------------------------------------------
# Priority ladder
# ---------------------------------------------------------------------------

def test_today_class_subject_ranks_first(db):
    add_class(db, "2026-08-01", "09:00", subjects="Physics")
    add_doubt(db, concept="sign of relative velocity", subject="Physics", page_id="d1")
    add_doubt(db, concept="mole fraction", subject="Chemistry", page_id="d2")

    ranked = cd.ranked_doubts(now=_utc(2026, 8, 1, 8, 0), db_path=db)
    assert concepts(ranked) == ["sign of relative velocity", "mole fraction"]
    top = ranked[0]
    assert top["bucket"] == cd.BUCKET_CLASS_SUBJECT
    assert top["evidence"]["class_today"] is True
    assert top["confidence"] == "high"
    assert "class subject" in top["reason"]

    selected = cd.select_next_doubt(now=_utc(2026, 8, 1, 8, 0), db_path=db)
    assert selected["doubt_id"] == "d1"


def test_upcoming_class_subject_counts_within_window(db):
    add_class(db, "2026-08-03", "09:00", subjects="Maths")
    add_doubt(db, concept="derivative chain rule", subject="Maths", page_id="d1")
    add_doubt(db, concept="mole concept", subject="Chemistry", page_id="d2")

    ranked = cd.ranked_doubts(now=_utc(2026, 8, 1, 8, 0), db_path=db)
    assert concepts(ranked) == ["derivative chain rule", "mole concept"]
    top = ranked[0]
    assert top["bucket"] == cd.BUCKET_CLASS_SUBJECT
    assert top["evidence"]["class_upcoming"] is True
    assert top["evidence"]["class_today"] is False


def test_nearest_test_syllabus_topic_ranks_second(db):
    add_test(db, "t1", "Weekly Test", "2026-08-05", syllabus_records=[
        {"ordinal": 0, "subject": "Physics", "chapter": None,
         "topic": "Kinematics", "normalized_text": "Kinematics", "raw_text": "Kinematics"},
    ])
    add_doubt(db, concept="kinematics of relative motion", subject="Physics", page_id="d1")
    add_doubt(db, concept="mole fraction", subject="Chemistry", page_id="d2")

    ranked = cd.ranked_doubts(now=_utc(2026, 8, 1, 8, 0), db_path=db)
    assert concepts(ranked) == ["kinematics of relative motion", "mole fraction"]
    top = ranked[0]
    assert top["bucket"] == cd.BUCKET_SYLLABUS_TOPIC
    assert top["evidence"]["syllabus_topic"] is True
    assert top["evidence"]["nearest_test"]["test_date"] == "2026-08-05"
    assert top["confidence"] == "high"
    assert "nearest test 'Weekly Test' on 2026-08-05" in top["reason"]


def test_repeated_failed_attempts_ranked_before_noise(db):
    add_doubt(db, concept="projectile range", subject="Physics", page_id="d1")
    add_attempt(db, "d1", outcome="Unsolved", attempt_no=1)
    add_attempt(db, "d1", outcome="Unsolved", attempt_no=2)
    add_doubt(db, concept="fresh unrelated doubt", subject="Biology", page_id="d2")

    ranked = cd.ranked_doubts(now=_utc(2026, 8, 1, 8, 0), db_path=db)
    assert concepts(ranked) == ["projectile range", "fresh unrelated doubt"]
    assert ranked[0]["bucket"] == cd.BUCKET_REPEATED_FAILURE
    assert ranked[0]["evidence"]["failed_attempts"] == 2
    assert "2 repeated failed attempts" in ranked[0]["reason"]
    assert ranked[0]["confidence"] == "high"


def test_marks_lost_ranked_before_noise(db):
    add_doubt(db, concept="electrostatics problem", subject="Physics", page_id="d1")
    add_marks(db, subject="Physics", chapter="Electrostatics", marks_lost=5)
    add_doubt(db, concept="fresh unrelated doubt", subject="Biology", page_id="d2")

    ranked = cd.ranked_doubts(now=_utc(2026, 8, 1, 8, 0), db_path=db)
    assert concepts(ranked) == ["electrostatics problem", "fresh unrelated doubt"]
    assert ranked[0]["bucket"] == cd.BUCKET_MARKS_LOST
    assert ranked[0]["evidence"]["marks_lost"] == 5.0
    assert ranked[0]["confidence"] == "medium"
    assert "5 marks lost" in ranked[0]["reason"]


def test_teacher_ready_ranked_before_noise(db):
    add_doubt(db, concept="double circled doubt", subject="Chemistry", page_id="d1",
              workflow_state="Eligible for Teacher", teacher_ready=1)
    add_attempt(db, "d1", outcome="Solved Independently", attempt_no=1)
    add_attempt(db, "d1", outcome="Solved Independently", attempt_no=2)
    add_doubt(db, concept="brand new doubt", subject="Biology", page_id="d2")

    ranked = cd.ranked_doubts(now=_utc(2026, 8, 1, 8, 0), db_path=db)
    assert concepts(ranked) == ["double circled doubt", "brand new doubt"]
    assert ranked[0]["bucket"] == cd.BUCKET_TEACHER_READY
    assert ranked[0]["evidence"]["teacher_ready"] is True
    assert ranked[0]["confidence"] == "medium"
    assert "teacher-ready" in ranked[0]["reason"]


def test_age_tiebreak_prefers_older_within_same_bucket(db):
    add_doubt(db, concept="older doubt", subject="Biology", page_id="d1",
              created_time="2026-07-01T00:00:00+00:00")
    add_doubt(db, concept="newer doubt", subject="Biology", page_id="d2",
              created_time="2026-07-20T00:00:00+00:00")

    ranked = cd.ranked_doubts(now=_utc(2026, 8, 1, 8, 0), db_path=db)
    assert concepts(ranked) == ["older doubt", "newer doubt"]
    assert ranked[0]["bucket"] == cd.BUCKET_AGE_ONLY
    assert ranked[0]["evidence"]["age_days"] > ranked[1]["evidence"]["age_days"]
    assert ranked[0]["confidence"] == "low"


def test_within_bucket_secondary_signals_break_ties(db):
    add_class(db, "2026-08-01", "09:00", subjects="Physics")
    add_doubt(db, concept="rich physics doubt", subject="Physics", page_id="d1")
    add_attempt(db, "d1", outcome="Unsolved", attempt_no=1)
    add_attempt(db, "d1", outcome="Unsolved", attempt_no=2)
    add_marks(db, subject="Physics", chapter="Kinematics", marks_lost=10)
    add_doubt(db, concept="plain physics doubt", subject="Physics", page_id="d2")

    ranked = cd.ranked_doubts(now=_utc(2026, 8, 1, 8, 0), db_path=db)
    assert concepts(ranked) == ["rich physics doubt", "plain physics doubt"]
    assert all(r["bucket"] == cd.BUCKET_CLASS_SUBJECT for r in ranked)
    assert ranked[0]["within_score"] > ranked[1]["within_score"]


def test_syllabus_subject_only_is_weaker_than_topic_match(db):
    add_test(db, "t1", "Weekly Test", "2026-08-05", syllabus_records=[
        {"ordinal": 0, "subject": "Chemistry", "chapter": None,
         "topic": "Mole Concept", "normalized_text": "Mole Concept", "raw_text": "Mole Concept"},
    ])
    add_doubt(db, concept="mole concept of gases", subject="Chemistry", page_id="d1")
    add_doubt(db, concept="another chemistry doubt", subject="Chemistry", page_id="d2")

    ranked = cd.ranked_doubts(now=_utc(2026, 8, 1, 8, 0), db_path=db)
    assert concepts(ranked) == ["mole concept of gases", "another chemistry doubt"]
    assert ranked[0]["bucket"] == cd.BUCKET_SYLLABUS_TOPIC
    assert ranked[1]["bucket"] == cd.BUCKET_AGE_ONLY
    assert ranked[1]["evidence"]["syllabus_subject"] is True


def test_resolved_and_dismissed_doubts_are_excluded(db):
    add_class(db, "2026-08-01", "09:00", subjects="Physics")
    add_doubt(db, concept="open doubt", subject="Physics", page_id="d1")
    add_doubt(db, concept="resolved doubt", subject="Physics", page_id="d2", status="Resolved")
    add_doubt(db, concept="dismissed doubt", subject="Physics", page_id="d3", status="Resolved",
              workflow_state="Dismissed")

    ranked = cd.ranked_doubts(now=_utc(2026, 8, 1, 8, 0), db_path=db)
    assert concepts(ranked) == ["open doubt"]


def test_selection_shape_determinism_and_no_writes(db):
    add_class(db, "2026-08-01", "09:00", subjects="Physics")
    add_doubt(db, concept="selection doubt", subject="Physics", page_id="d1")
    with sqlite3.connect(db) as conn:
        before = conn.execute("SELECT COUNT(*) FROM coaching_doubt_interactions").fetchone()[0]

    first = cd.ranked_doubts(now=_utc(2026, 8, 1, 8, 0), db_path=db)
    second = cd.ranked_doubts(now=_utc(2026, 8, 1, 8, 0), db_path=db)
    assert first == second

    with sqlite3.connect(db) as conn:
        after = conn.execute("SELECT COUNT(*) FROM coaching_doubt_interactions").fetchone()[0]
    assert before == after == 0

    top = first[0]
    assert top["llm_involved"] is False
    assert top["generated_with"] == "deterministic"
    assert {"bucket", "bucket_label", "score", "within_score", "reason",
            "confidence", "evidence"} <= set(top)
    assert top["score"] == top["bucket"] * 10000 + int(top["within_score"])


def test_select_next_doubt_returns_none_when_no_doubts(db):
    assert cd.select_next_doubt(now=_utc(2026, 8, 1, 8, 0), db_path=db) is None
    assert cd.ranked_doubts(now=_utc(2026, 8, 1, 8, 0), db_path=db) == []


# ---------------------------------------------------------------------------
# Teacher-ready selection
# ---------------------------------------------------------------------------

def test_select_teacher_ready_only_ready_doubts(db):
    add_doubt(db, concept="ready physics doubt", subject="Physics", page_id="d1")
    add_attempt(db, "d1", outcome="Unsolved", attempt_no=1)
    add_attempt(db, "d1", outcome="Unsolved", attempt_no=2)
    add_doubt(db, concept="still attempting doubt", subject="Physics", page_id="d2")
    add_attempt(db, "d2", outcome="Unsolved", attempt_no=1)

    result = cd.select_teacher_ready_doubts(now=_utc(2026, 8, 1, 12, 0), db_path=db)
    assert result["count"] == 1
    assert result["doubts"][0]["doubt_id"] == "d1"
    assert result["doubts"][0]["confidence"] == "high"
    assert "teacher-ready" in result["doubts"][0]["reason"]


def test_select_teacher_ready_prefers_window_subject_match(db):
    insert(
        db, "timetable", notion_page_id="window-1", title="Teacher doubts",
        weekday="Saturday", start_time="15:00", end_time="16:00",
        kind="Doubt Window", subject="Physics", active=1, questions_allowed=1,
    )
    add_doubt(db, concept="physics ready doubt", subject="Physics", page_id="d1")
    add_attempt(db, "d1", outcome="Unsolved", attempt_no=1)
    add_attempt(db, "d1", outcome="Unsolved", attempt_no=2)
    add_doubt(db, concept="chem ready doubt", subject="Chemistry", page_id="d2")
    add_attempt(db, "d2", outcome="Unsolved", attempt_no=1)
    add_attempt(db, "d2", outcome="Unsolved", attempt_no=2)

    result = cd.select_teacher_ready_doubts(now=_utc(2026, 8, 1, 12, 0), db_path=db)
    assert result["window"] is not None
    assert result["window"]["subject"] == "Physics"
    ordered = [d["doubt_id"] for d in result["doubts"]]
    assert ordered == ["d1", "d2"]
    assert result["doubts"][0]["window_match"] is True
    assert result["doubts"][1]["window_match"] is False
    assert result["llm_involved"] is False


# ---------------------------------------------------------------------------
# Interaction lifecycle
# ---------------------------------------------------------------------------

def test_begin_doubt_creates_shown_session(db):
    add_doubt(db, concept="begin doubt", subject="Physics", page_id="d1")
    result = cd.begin_doubt(42, "d1", db_path=db)
    assert result["state"] == cd.STATE_SHOWN
    assert result["attempt_count"] == 0
    assert result["doubt_id"] == "d1"
    assert result["already_active"] is False
    assert "begin doubt" in result["message"]
    assert "Attempt it now" in result["message"]
    assert "hint" not in result["message"].lower() or "record a hint" in result["message"]


def test_begin_doubt_resumes_active_session(db):
    add_doubt(db, concept="begin doubt", subject="Physics", page_id="d1")
    first = cd.begin_doubt(42, "d1", db_path=db)
    second = cd.begin_doubt(42, "d1", db_path=db)
    assert first["id"] == second["id"]
    assert second["already_active"] is True
    assert len(cd.active_interactions(42, db_path=db)) == 1


def test_begin_doubt_rejects_closed_doubt(db):
    add_doubt(db, concept="closed doubt", subject="Physics", page_id="d1", status="Resolved")
    with pytest.raises(ValueError):
        cd.begin_doubt(42, "d1", db_path=db)


def test_begin_doubt_rejects_unknown_doubt(db):
    with pytest.raises(ValueError):
        cd.begin_doubt(42, "does-not-exist", db_path=db)


def test_record_attempt_stores_text_and_returns_write_plan(db):
    add_doubt(db, concept="attempt doubt", subject="Physics", page_id="d1")
    session = cd.begin_doubt(42, "d1", db_path=db)

    result = cd.record_attempt(
        42, session["id"], attempt_text="tried v=u+at but sign is wrong",
        duration_min=15, db_path=db,
    )
    assert result["state"] == cd.STATE_ATTEMPTING
    assert result["attempt_count"] == 1
    assert result["last_attempt_text"] == "tried v=u+at but sign is wrong"
    assert result["last_attempt_outcome"] == "Unsolved"
    assert "Attempt 1 recorded" in result["message"]

    plan = result["write_plan"]
    assert plan["kind"] == "doubt_attempt"
    assert plan["delegate"] == "study_domain.record_doubt_attempt"
    assert plan["llm_involved"] is False
    assert plan["params"]["doubt"] == "attempt doubt"
    assert plan["params"]["approach"] == "tried v=u+at but sign is wrong"
    assert plan["params"]["duration_min"] == 15
    assert plan["params"]["outcome"] == "Unsolved"
    assert "Never invent" in plan["requires"]


def test_record_attempt_requires_user_text(db):
    add_doubt(db, concept="attempt doubt", subject="Physics", page_id="d1")
    session = cd.begin_doubt(42, "d1", db_path=db)
    with pytest.raises(ValueError, match="record what you tried"):
        cd.record_attempt(42, session["id"], attempt_text="", db_path=db)


def test_record_attempt_rejects_invalid_outcome(db):
    add_doubt(db, concept="attempt doubt", subject="Physics", page_id="d1")
    session = cd.begin_doubt(42, "d1", db_path=db)
    with pytest.raises(ValueError, match="invalid attempt outcome"):
        cd.record_attempt(42, session["id"], attempt_text="tried it", outcome="Bogus", db_path=db)


def test_request_hint_stores_only_supplied_hint(db):
    add_doubt(db, concept="hint doubt", subject="Physics", page_id="d1")
    session = cd.begin_doubt(42, "d1", db_path=db)
    result = cd.request_hint(42, session["id"], hint="Try the sign convention", db_path=db)
    assert result["state"] == cd.STATE_HINT_GIVEN
    assert result["hint_supplied"] == "Try the sign convention"
    assert result["hint_source"] == "user"
    assert "Try the sign convention" in result["message"]


def test_request_hint_never_fabricates(db):
    add_doubt(db, concept="hint doubt", subject="Physics", page_id="d1")
    session = cd.begin_doubt(42, "d1", db_path=db)
    result = cd.request_hint(42, session["id"], db_path=db)
    assert result["state"] == cd.STATE_AWAITING_HINT
    assert result["hint_supplied"] is None
    assert "don't invent hints" in result["message"]
    stored = cd.get_session(42, session["id"], db_path=db)
    assert stored["hint_supplied"] is None


def test_mark_retry_transition(db):
    add_doubt(db, concept="retry doubt", subject="Physics", page_id="d1")
    session = cd.begin_doubt(42, "d1", db_path=db)
    result = cd.mark_retry(42, session["id"], db_path=db)
    assert result["state"] == cd.STATE_RETRY
    assert "another shot" in result["message"]


def test_resolve_requires_real_evidence(db):
    add_doubt(db, concept="resolve doubt", subject="Physics", page_id="d1")
    session = cd.begin_doubt(42, "d1", db_path=db)
    with pytest.raises(ValueError, match="resolution evidence is required"):
        cd.resolve(42, session["id"], resolution="", db_path=db)


def test_resolve_sets_state_and_write_plan(db):
    add_doubt(db, concept="resolve doubt", subject="Physics", page_id="d1")
    session = cd.begin_doubt(42, "d1", db_path=db)
    result = cd.resolve(
        42, session["id"], resolution="Applied the sign convention; solved it",
        db_path=db,
    )
    assert result["state"] == cd.STATE_RESOLVED
    assert result["resolution_source"] == "user"
    assert "Applied the sign convention" in result["resolution"]
    plan = result["write_plan"]
    assert plan["kind"] == "doubt_resolve"
    assert plan["delegate"] == "study_domain.resolve_doubt"
    assert plan["params"]["resolution"] == "Applied the sign convention; solved it"
    assert plan["params"]["teacher_asked"] is False


def test_resolve_as_teacher(db):
    add_doubt(db, concept="resolve doubt", subject="Physics", page_id="d1")
    session = cd.begin_doubt(42, "d1", db_path=db)
    result = cd.resolve(42, session["id"], resolution="Teacher explained the method", teacher=True, db_path=db)
    assert result["resolution_source"] == "teacher"
    assert result["write_plan"]["params"]["teacher_asked"] is True
    assert ">=2 valid attempts" in result["write_plan"]["requires"]


def test_resolve_with_retest_schedules_and_is_due(db):
    add_doubt(db, concept="retest doubt", subject="Physics", page_id="d1")
    session = cd.begin_doubt(42, "d1", db_path=db)
    result = cd.resolve(42, session["id"], resolution="got it", retest_at="2026-08-05", db_path=db)
    assert result["state"] == cd.STATE_RETEST
    assert result["retest_at"] == "2026-08-05"
    assert "Reattempt scheduled for 2026-08-05" in result["message"]

    assert cd.due_reattempts(42, today="2026-08-04", db_path=db) == []
    due = cd.due_reattempts(42, today="2026-08-05", db_path=db)
    assert len(due) == 1
    assert due[0]["doubt_concept"] == "retest doubt"
    assert "Reattempt due" in due[0]["message"]


def test_schedule_reattempt_requires_valid_date(db):
    add_doubt(db, concept="retest doubt", subject="Physics", page_id="d1")
    session = cd.begin_doubt(42, "d1", db_path=db)
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        cd.schedule_reattempt(42, session["id"], "tomorrow", db_path=db)


def test_schedule_reattempt_then_reattempt_loop(db):
    add_doubt(db, concept="loop doubt", subject="Physics", page_id="d1")
    session = cd.begin_doubt(42, "d1", db_path=db)
    scheduled = cd.schedule_reattempt(42, session["id"], "2026-08-06", db_path=db)
    assert scheduled["state"] == cd.STATE_RETEST

    retried = cd.record_attempt(42, session["id"], attempt_text="second attempt text", db_path=db)
    assert retried["state"] == cd.STATE_ATTEMPTING
    assert retried["attempt_count"] == 1


def test_active_interactions_and_status_line(db):
    add_doubt(db, concept="active doubt", subject="Physics", page_id="d1")
    cd.begin_doubt(42, "d1", db_path=db)
    sessions = cd.active_interactions(42, db_path=db)
    assert len(sessions) == 1
    assert "active doubt" in cd.status_line(sessions[0])


def test_start_next_doubt_combines_selection_and_begin(db):
    add_class(db, "2026-08-01", "09:00", subjects="Physics")
    add_doubt(db, concept="start doubt", subject="Physics", page_id="d1")
    result = cd.start_next_doubt(42, now=_utc(2026, 8, 1, 8, 0), db_path=db)
    assert result["selection"]["doubt_id"] == "d1"
    assert result["session"]["state"] == cd.STATE_SHOWN
    assert "start doubt" in result["message"]


def test_start_next_doubt_without_doubts_returns_message(db):
    result = cd.start_next_doubt(42, now=_utc(2026, 8, 1, 8, 0), db_path=db)
    assert result["selection"] is None
    assert result["session"] is None
    assert result["message"] == cd.no_doubts_message()
