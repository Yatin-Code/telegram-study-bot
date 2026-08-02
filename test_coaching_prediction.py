"""Phase 11 tests — deterministic coaching score-range projection (offline).

Covers:
  * no-evidence behaviour (no upcoming test / no historical scores)
  * normalization across different maximum marks
  * improving / declining subject trends
  * bounded output ranges (percent + marks) with guaranteed ordering
  * confidence levels (high / medium / low / unavailable)
  * idempotent local snapshot storage keyed by (as_of, test_id)
  * a full-evidence integration (coverage + revision + plan capacity) that
    stays read-only and yields bounded actions and honest risks
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import coaching_prediction as cp
import coaching_syllabus as cs
import ntsc_coaching
import operational_store
import sync


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "coach.db"
    with sync.connect(path) as conn:
        sync.init_db(conn)
        operational_store.init_db(conn)
        ntsc_coaching.init_db(conn)
    return path


def _conn(path, *, row_factory=None):
    conn = sqlite3.connect(str(path))
    if row_factory is not None:
        conn.row_factory = row_factory
    return conn


def add_test(path, test_id, title, test_date, syllabus=""):
    with _conn(path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO coaching_tests "
            "(source_id,title,test_date,course_id,batch,goal,syllabus,source_updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (test_id, title, test_date, "", "", "", syllabus, "2026-07-20"),
        )
        conn.commit()
    if syllabus:
        cs.store_test_syllabus(test_id, cs.parse_syllabus(syllabus), db_path=path)


def add_exam(path, title, exam_date, max_marks):
    operational_store.create("exams", {
        "operation_id": f"exam-{title}",
        "title": title,
        "kind": "Coaching Test",
        "status": "Planned",
        "exam_date": exam_date,
        "max_marks": max_marks,
    }, db_path=path)


def add_result(path, source_id, attempt_date, total_marks, maximum_marks, percentile=None):
    with _conn(path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO coaching_results "
            "(source_id,title,attempt_date,total_marks,maximum_marks,percentile,"
            " correct,incorrect,attempted,unattempted) "
            "VALUES (?,?,?,?,?,?,0,0,0,0)",
            (source_id, f"Test {source_id}", attempt_date, total_marks,
             maximum_marks, percentile),
        )
        conn.commit()


def add_subject_result(path, result_id, subject, marks, maximum_marks):
    with _conn(path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO coaching_subject_results "
            "(result_id,subject,marks,maximum_marks,percentile,correct,incorrect,unattempted) "
            "VALUES (?,?,?,?,?,0,0,0)",
            (result_id, subject, marks, maximum_marks, None),
        )
        conn.commit()


def add_revision(path, chapter_module, subject, next_execution_date, status="Pending"):
    with _conn(path) as conn:
        conn.execute(
            "INSERT INTO revision (notion_page_id,last_synced_at,archived,raw_json,"
            "chapter_module,subject,next_execution_date,status) "
            "VALUES (?,?,0,'{}',?,?,?,?)",
            (f"rev-{chapter_module}", "2026-07-20", chapter_module, subject,
             next_execution_date, status),
        )
        conn.commit()


def add_ledger(path, notion_id, subject, task):
    with _conn(path) as conn:
        conn.execute(
            "INSERT INTO ledger (notion_page_id,last_synced_at,archived,raw_json,"
            "subject,task) VALUES (?,?,0,'{}',?,?)",
            (notion_id, "2026-07-20", subject, task),
        )
        conn.commit()


def add_doubt(path, notion_id, subject, core_concept):
    with _conn(path) as conn:
        conn.execute(
            "INSERT INTO doubts (notion_page_id,last_synced_at,archived,raw_json,"
            "subject,core_concept,status) VALUES (?,?,0,'{}',?,?,'Unresolved')",
            (notion_id, "2026-07-20", subject, core_concept),
        )
        conn.commit()


def assert_ordered_bounded(bands, maximum=None):
    low = bands["conservative"]
    likely_low = bands["likely_low"]
    likely_high = bands["likely_high"]
    stretch = bands["stretch"]
    assert 0.0 <= low <= likely_low <= likely_high <= stretch
    if maximum is not None:
        assert all(0.0 <= value <= maximum for value in bands.values())
    else:
        assert stretch <= 100.0


# ---------------------------------------------------------------------------
# No evidence
# ---------------------------------------------------------------------------

def test_empty_db_returns_unavailable_no_upcoming_test(db):
    snap = cp.project_coaching_score(today="2026-08-02", db_path=db)
    assert snap["status"] == "unavailable"
    assert snap["confidence"] == "unavailable"
    assert snap["total"] is None
    assert "upcoming_test" in snap["missing"]
    assert snap["evidence_count"] == 0


def test_upcoming_test_without_scores_returns_unavailable(db):
    add_test(db, "T1", "Weekly Test", "2026-08-15", "Physics: Kinematics, Vectors")
    snap = cp.project_coaching_score(today="2026-08-02", db_path=db)
    assert snap["status"] == "unavailable"
    assert snap["confidence"] == "unavailable"
    assert snap["total"] is None
    assert "historical_scores" in snap["missing"]
    assert snap["test_id"] == "T1"
    assert any("no historical scores" in risk for risk in snap["risks"])


# ---------------------------------------------------------------------------
# Normalized mixed max marks
# ---------------------------------------------------------------------------

def test_normalized_mixed_max_marks(db):
    add_test(db, "T1", "Weekly Test", "2026-08-15")
    add_result(db, "r1", "2026-07-01", 36, 60)     # 60%
    add_result(db, "r2", "2026-07-15", 84, 120)    # 70%
    add_result(db, "r3", "2026-08-01", 144, 180)   # 80%
    add_subject_result(db, "r1", "Physics", 36, 60)
    add_subject_result(db, "r2", "Physics", 84, 120)
    add_subject_result(db, "r3", "Physics", 144, 180)

    snap = cp.project_coaching_score(today="2026-08-02", db_path=db)

    assert snap["status"] == "ok"
    hist = snap["factors"]["historical"]
    assert hist["samples"] == 3
    # percentages, not raw marks (144 is the largest raw mark but 80%).
    assert hist["mean_pct"] == 70.0
    assert hist["trend_direction"] == "improving"

    physics = next(s for s in snap["subjects"] if s["subject"] == "Physics")
    assert physics["status"] == "projected"
    assert physics["mean_pct"] == 70.0
    assert physics["samples"] == 3
    assert_ordered_bounded(physics["pct"])

    assert_ordered_bounded(snap["total"]["pct"])
    # portal tests do not record maximum marks, so marks stay None (honest).
    assert snap["total"]["marks"] is None
    assert any("maximum marks are not recorded" in risk for risk in snap["risks"])


# ---------------------------------------------------------------------------
# Trends
# ---------------------------------------------------------------------------

def test_improving_trend(db):
    add_test(db, "T1", "Weekly Test", "2026-08-15")
    for i, pct in enumerate((50, 60, 70, 80)):
        add_result(db, f"r{i}", f"2026-07-0{i+1}", pct, 100)
    snap = cp.project_coaching_score(today="2026-08-02", db_path=db)
    hist = snap["factors"]["historical"]
    assert hist["trend_direction"] == "improving"
    assert hist["trend_pct_per_attempt"] > 0
    assert snap["total"]["center_pct"] > hist["mean_pct"]
    assert snap["total"]["pct"]["stretch"] > snap["total"]["pct"]["conservative"]


def test_declining_trend(db):
    add_test(db, "T1", "Weekly Test", "2026-08-15")
    for i, pct in enumerate((80, 70, 60, 50)):
        add_result(db, f"r{i}", f"2026-07-0{i+1}", pct, 100)
    snap = cp.project_coaching_score(today="2026-08-02", db_path=db)
    hist = snap["factors"]["historical"]
    assert hist["trend_direction"] == "declining"
    assert hist["trend_pct_per_attempt"] < 0
    assert snap["total"]["center_pct"] < hist["mean_pct"]
    assert any("declining" in risk for risk in snap["risks"])


# ---------------------------------------------------------------------------
# Bounded ranges
# ---------------------------------------------------------------------------

def test_ranges_are_bounded_and_ordered_with_full_coverage(db):
    add_test(
        db, "T1", "Weekly Test", "2026-08-05",
        "Physics: Kinematics, Vectors, Laws of Motion",
    )
    add_ledger(db, "l1", "Physics", "Kinematics")
    add_ledger(db, "l2", "Physics", "Vectors")
    add_ledger(db, "l3", "Physics", "Laws of Motion")
    add_result(db, "r1", "2026-07-20", 95, 100)
    add_subject_result(db, "r1", "Physics", 95, 100)

    snap = cp.project_coaching_score(today="2026-08-02", db_path=db)
    assert snap["status"] == "ok"
    assert snap["factors"]["coverage"]["covered_fraction"] == 1.0
    assert_ordered_bounded(snap["total"]["pct"])
    physics = next(s for s in snap["subjects"] if s["subject"] == "Physics")
    assert_ordered_bounded(physics["pct"])
    assert snap["total"]["pct"]["stretch"] <= 100.0
    assert snap["total"]["pct"]["conservative"] <= snap["total"]["pct"]["likely_low"]


def test_marks_ranges_bounded_by_maximum_marks(db):
    add_exam(db, "Mock 2026-08", "2026-08-05", 180)
    add_result(db, "r1", "2026-07-20", 95, 100)

    snap = cp.project_coaching_score(today="2026-08-02", db_path=db)
    assert snap["status"] == "ok"
    assert snap["maximum_marks"] == 180
    marks = snap["total"]["marks"]
    assert marks is not None
    assert_ordered_bounded(marks, maximum=180)
    assert marks["stretch"] <= 180
    # marks mirrors the pct ordering and stays inside [0, max].
    assert marks["likely_low"] <= marks["likely_high"]


def test_marks_bands_unit_bound():
    bands = {"conservative": 0.0, "likely_low": 10.0, "likely_high": 90.0, "stretch": 100.0}
    marks = cp._marks_bands(bands, 360)
    assert marks == {
        "conservative": 0.0, "likely_low": 36.0, "likely_high": 324.0, "stretch": 360.0,
    }
    assert cp._marks_bands(bands, None) is None
    assert cp._marks_bands(bands, 0) is None


# ---------------------------------------------------------------------------
# Confidence
# ---------------------------------------------------------------------------

def test_confidence_unavailable_without_scores(db):
    add_test(db, "T1", "Weekly Test", "2026-08-15")
    snap = cp.project_coaching_score(today="2026-08-02", db_path=db)
    assert snap["confidence"] == "unavailable"


def test_confidence_low_with_single_sample(db):
    add_test(db, "T1", "Weekly Test", "2026-08-15")
    add_result(db, "r1", "2026-07-20", 60, 100)
    snap = cp.project_coaching_score(today="2026-08-02", db_path=db)
    assert snap["status"] == "ok"
    assert snap["confidence"] == "low"
    assert any("only 1 historical result" in risk for risk in snap["risks"])


def test_confidence_high_with_scores_subjects_and_coverage(db):
    add_test(
        db, "T1", "Weekly Test", "2026-08-15",
        "Physics: Kinematics, Vectors; Chemistry: Mole Concept",
    )
    add_ledger(db, "l1", "Physics", "Kinematics")
    add_ledger(db, "l2", "Physics", "Vectors")
    for i, pct in enumerate((50, 60, 70, 80)):
        add_result(db, f"r{i}", f"2026-07-0{i+1}", pct, 100)
        add_subject_result(db, f"r{i}", "Physics", pct, 100)
        add_subject_result(db, f"r{i}", "Chemistry", pct, 100)

    snap = cp.project_coaching_score(today="2026-08-02", db_path=db)
    assert snap["status"] == "ok"
    assert snap["confidence"] == "high"
    assert snap["evidence_count"] >= 8
    projected = [s for s in snap["subjects"] if s["status"] == "projected"]
    assert len(projected) == 2


# ---------------------------------------------------------------------------
# Storage (idempotent)
# ---------------------------------------------------------------------------

def test_snapshot_storage_is_idempotent(db):
    add_test(db, "T1", "Weekly Test", "2026-08-15")
    add_result(db, "r1", "2026-07-20", 60, 100)

    first = cp.project_coaching_score(today="2026-08-02", db_path=db, store=True)
    second = cp.project_coaching_score(today="2026-08-02", db_path=db, store=True)
    assert first == second

    rows = cp.list_predictions(test_id="T1", db_path=db)
    assert len(rows) == 1
    assert rows[0]["as_of"] == "2026-08-02"
    assert rows[0]["test_id"] == "T1"
    assert rows[0]["status"] == "ok"
    assert rows[0]["confidence"] == "low"

    loaded = cp.load_prediction("2026-08-02", "T1", db_path=db)
    assert loaded == first
    assert loaded["factors"]["historical"]["samples"] == 1

    # another as_of day → a second, independent row
    cp.project_coaching_score(today="2026-08-03", db_path=db, store=True)
    assert len(cp.list_predictions(test_id="T1", db_path=db)) == 2
    assert len(cp.list_predictions(db_path=db)) == 2

    # standalone save/load round-trips and upserts in place
    cp.save_prediction(first, db_path=db)
    assert len(cp.list_predictions(test_id="T1", db_path=db)) == 2
    assert cp.load_prediction("2026-08-02", "T1", db_path=db) == first


def test_snapshot_storage_stores_unavailable_rows(db):
    snap = cp.project_coaching_score(today="2026-08-02", db_path=db, store=True)
    assert snap["status"] == "unavailable"
    # no target test → nothing to key a snapshot on; nothing is persisted
    assert cp.list_predictions(db_path=db) == []


def test_snapshot_storage_persists_unavailable_with_test(db):
    add_test(db, "T1", "Weekly Test", "2026-08-15")
    snap = cp.project_coaching_score(today="2026-08-02", db_path=db, store=True)
    assert snap["status"] == "unavailable"
    rows = cp.list_predictions(db_path=db)
    assert len(rows) == 1
    assert rows[0]["test_id"] == "T1"
    assert rows[0]["status"] == "unavailable"


# ---------------------------------------------------------------------------
# Full evidence integration
# ---------------------------------------------------------------------------

def test_full_evidence_integration_is_read_only_and_bounded(db):
    add_test(
        db, "T1", "Weekly Test", "2026-08-15",
        "Physics: Kinematics, Vectors, Laws of Motion\nChemistry: Mole Concept",
    )
    add_ledger(db, "l1", "Physics", "Kinematics")
    add_ledger(db, "l2", "Physics", "Vectors")
    add_doubt(db, "d1", "Physics", "Laws of Motion sign convention")
    add_revision(db, "Thermodynamics", "Physics", "2026-08-01", status="Pending")
    for i, pct in enumerate((55, 62, 70, 78)):
        add_result(db, f"r{i}", f"2026-07-0{i+1}", pct, 100)
        add_subject_result(db, f"r{i}", "Physics", pct, 100)

    with _conn(db) as conn:
        before = conn.execute("SELECT COUNT(*) FROM op_daily_plan").fetchone()[0]

    snap = cp.project_coaching_score(today="2026-08-02", db_path=db)

    with _conn(db) as conn:
        after = conn.execute("SELECT COUNT(*) FROM op_daily_plan").fetchone()[0]
    assert before == after == 0

    assert snap["status"] == "ok"
    assert snap["confidence"] in ("high", "medium")
    assert snap["evidence_count"] > 0

    coverage = snap["factors"]["coverage"]
    assert coverage["syllabus_known"] is True
    assert coverage["covered_fraction"] < 1.0
    assert coverage["topic_count"] == 4

    revision = snap["factors"]["revision"]
    assert revision["available"] is True
    assert revision["overdue"] == 1

    physics = next(s for s in snap["subjects"] if s["subject"] == "Physics")
    assert physics["status"] == "projected"
    assert_ordered_bounded(physics["pct"])
    chemistry = next(s for s in snap["subjects"] if s["subject"] == "Chemistry")
    assert chemistry["status"] == "no_scores"
    assert chemistry["pct"] is None

    # every action is bounded with a capped max gain
    for action in snap["actions"]:
        assert action["max_gain_pct"] > 0
        assert action["max_gain_pct"] <= 6.0
        assert action["bound"].endswith(" pp")

    assert any("overdue revision" in risk for risk in snap["risks"])
    assert any("doubt(s) in not-fully-covered" in risk for risk in snap["risks"])


def test_project_is_deterministic(db):
    add_test(db, "T1", "Weekly Test", "2026-08-15")
    add_result(db, "r1", "2026-07-01", 36, 60)
    add_result(db, "r2", "2026-07-15", 84, 120)

    first = cp.project_coaching_score(today="2026-08-02", db_path=db)
    second = cp.project_coaching_score(today="2026-08-02", db_path=db)
    assert first == second
    assert "rank_statement" in first
    assert "No rank or AIR is claimed" in first["rank_statement"]


def test_unknown_subject_names_are_preserved(db):
    add_test(db, "T1", "Weekly Test", "2026-08-15")
    add_result(db, "r1", "2026-07-01", 30, 50)
    add_subject_result(db, "r1", "General Aptitude", 30, 50)
    snap = cp.project_coaching_score(today="2026-08-02", db_path=db)
    subjects = {s["subject"]: s for s in snap["subjects"]}
    assert subjects["General Aptitude"]["status"] == "projected"


def main() -> int:
    import sys

    import pytest

    return pytest.main([__file__, "-q"])


if __name__ == "__main__":
    sys.exit(main())
