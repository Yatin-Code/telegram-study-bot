"""Focused tests for Phase 6 syllabus normalization/graph (offline).

Covers the deterministic portal-syllabus parser, SQLite storage/freshness,
upcoming-syllabus helpers, coverage/progress representation, and the sync
integration that populates normalized records from fetched tests.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import ntsc_coaching
import ntsc_sync
import coaching_syllabus as cs

HTML_SYLLABUS = """<div>
  <p><b>Physics:</b> Kinematics, Laws of Motion, Vectors</p>
  <p><b>Chemistry :</b> Mole Concept, Stoichiometry</p>
  <p><b>Mathematics</b></p>
  <ul>
    <li>Quadratic Equations</li>
    <li>Sequences and Series</li>
  </ul>
  <p>Chapter 1 - Units and Measurements</p>
</div>"""

PLAIN_SYLLABUS = """Physics:
Kinematics
Laws of Motion
Chemistry: Chemical Bonding
1. Redox Reactions 2. Electrochemistry
"""


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def test_parse_html_syllabus():
    records = cs.parse_syllabus(HTML_SYLLABUS)
    assert len(records) == 8
    subjects = [r["subject"] for r in records]
    assert subjects[0:3] == ["Physics", "Physics", "Physics"]
    assert subjects[3:5] == ["Chemistry", "Chemistry"]
    assert subjects[5:7] == ["Mathematics", "Mathematics"]
    assert [r["topic"] for r in records[0:3]] == ["Kinematics", "Laws of Motion", "Vectors"]
    assert [r["topic"] for r in records[3:5]] == ["Mole Concept", "Stoichiometry"]
    assert [r["topic"] for r in records[5:7]] == ["Quadratic Equations", "Sequences and Series"]


def test_parse_plain_syllabus_and_numbered_topics():
    records = cs.parse_syllabus(PLAIN_SYLLABUS)
    assert [r["topic"] for r in records[0:2]] == ["Kinematics", "Laws of Motion"]
    assert records[2]["subject"] == "Chemistry"
    assert records[2]["topic"] == "Chemical Bonding"
    assert [r["topic"] for r in records[3:5]] == ["Redox Reactions", "Electrochemistry"]


def test_parse_never_invents_chapter_for_bare_topics():
    records = cs.parse_syllabus("Kinematics, Laws of Motion")
    assert len(records) == 2
    assert all(r["chapter"] is None for r in records)
    assert records[0]["topic"] == "Kinematics"


def test_parse_chapter_header_recorded_only_when_named():
    records = cs.parse_syllabus("Chapter 1 - Units and Measurements")
    assert len(records) == 1
    assert records[0]["chapter"] == "Units and Measurements"
    assert records[0]["topic"] is None
    # a chapter marker followed by a list stays topic records (no fabricated name)
    mixed = cs.parse_syllabus("Chapter 1: Kinematics, Vectors")
    assert len(mixed) == 2
    assert [r["topic"] for r in mixed] == ["Kinematics", "Vectors"]
    assert all(r["chapter"] is None for r in mixed)


def test_parse_normalizes_subject_aliases():
    assert cs._canonical_subject("Chem") == "Chemistry"
    assert cs._canonical_subject("Maths") == "Mathematics"
    assert cs._canonical_subject("PHYSICS") == "Physics"
    assert cs._canonical_subject("nothing here") is None
    records = cs.parse_syllabus("Maths: Calculus")
    assert records[0]["subject"] == "Mathematics"


def test_parse_structured_input():
    structured = [
        {"subject": "Physics", "topics": ["Kinematics", "Laws of Motion"]},
        {"subject": "Chem", "topics": [{"name": "Mole Concept", "chapter": "Basics"}]},
    ]
    records = cs.parse_syllabus(structured)
    assert len(records) == 3
    assert records[0]["subject"] == "Physics"
    assert records[2]["topic"] == "Mole Concept"
    assert records[2]["chapter"] == "Basics"


def test_parse_preserves_evidence():
    records = cs.parse_syllabus("<p>Kinematics, Laws of Motion</p>")
    assert records[0]["raw_text"] == "Kinematics, Laws of Motion"
    assert records[0]["normalized_text"] == "Kinematics, Laws of Motion"
    assert records[1]["normalized_text"] == "Kinematics, Laws of Motion"
    assert records[0]["topic"] == "Kinematics"


def test_parse_empty_and_none():
    assert cs.parse_syllabus(None) == []
    assert cs.parse_syllabus("") == []
    assert cs.parse_syllabus("<p></p>") == []


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _fresh_db(tmp_path: Path) -> Path:
    return tmp_path / "syllabus.db"


def test_init_db_idempotent(tmp_path):
    db = _fresh_db(tmp_path)
    conn = cs._connect(db)
    cs.init_db(conn)
    cs.init_db(conn)
    tables = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert cs.SYLLABUS_TABLE in tables
    assert cs.SYLLABUS_META_TABLE in tables
    conn.close()


def test_ntsc_coaching_init_creates_syllabus_tables(tmp_path):
    db = _fresh_db(tmp_path)
    conn = sqlite3.connect(str(db))
    ntsc_coaching.init_db(conn)
    tables = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert cs.SYLLABUS_TABLE in tables
    assert cs.SYLLABUS_META_TABLE in tables
    conn.close()


def test_store_and_read_roundtrip(tmp_path):
    db = _fresh_db(tmp_path)
    records = cs.parse_syllabus("Physics: Kinematics, Vectors")
    count = cs.store_test_syllabus("t1", records, db_path=db)
    assert count == 2
    stored = cs.syllabus_for_test("t1", db_path=db)
    assert stored["source_test_id"] == "t1"
    assert len(stored["records"]) == 2
    assert stored["records"][0]["subject"] == "Physics"
    assert stored["records"][0]["topic"] == "Kinematics"
    assert stored["meta"]["record_count"] == 2
    assert stored["meta"]["parsed_at"]
    assert stored["meta"]["raw_hash"]
    assert stored["records"][0]["source_updated_at"] == stored["meta"]["parsed_at"]


def test_store_replaces_previous(tmp_path):
    db = _fresh_db(tmp_path)
    cs.store_test_syllabus("t1", cs.parse_syllabus("Physics: A, B, C"), db_path=db)
    cs.store_test_syllabus("t1", cs.parse_syllabus("Chemistry: D"), db_path=db)
    stored = cs.syllabus_for_test("t1", db_path=db)
    assert len(stored["records"]) == 1
    assert stored["records"][0]["subject"] == "Chemistry"
    assert stored["records"][0]["topic"] == "D"


def test_replace_syllabi_and_upcoming(tmp_path):
    db = _fresh_db(tmp_path)
    ntsc_coaching.replace_tests([
        {
            "id": "t1", "testName": "Weekly Test 1", "testDateTime": "2026-08-15T09:00:00",
            "courseId": "7", "batch": "B1", "goal": "Test",
            "syllabus": "<p>Physics: Kinematics, Vectors</p>",
        },
        {
            "id": "t2", "testName": "Weekly Test 2", "testDateTime": "2026-08-22T09:00:00",
            "courseId": "7", "batch": "B1", "goal": "Test",
            "syllabus": "Chemistry: Mole Concept",
        },
        {
            "id": "t3", "testName": "No Syllabus", "testDateTime": "2026-09-01T09:00:00",
            "courseId": "7", "batch": "B1", "goal": "Test", "syllabus": "",
        },
    ], db_path=db)
    result = cs.replace_syllabi([
        {"id": "t1", "syllabus": "<p>Physics: Kinematics, Vectors</p>"},
        {"id": "t2", "syllabus": "Chemistry: Mole Concept"},
        {"id": "t3", "syllabus": ""},
    ], db_path=db)
    assert result["tests_parsed"] == 2
    assert result["records_stored"] == 3

    upcoming = cs.upcoming_syllabus(today="2026-08-01", db_path=db)
    assert [t["source_id"] for t in upcoming] == ["t1", "t2", "t3"]
    t1 = upcoming[0]
    assert t1["syllabus_count"] == 2
    assert [r["topic"] for r in t1["syllabus_records"]] == ["Kinematics", "Vectors"]
    assert t1["syllabus_records"][0]["subject"] == "Physics"
    # test with no syllabus has no records and meta
    assert upcoming[2]["syllabus_count"] == 0
    assert upcoming[2]["syllabus_meta"] is None


def test_upcoming_syllabus_past_tests_excluded(tmp_path):
    db = _fresh_db(tmp_path)
    ntsc_coaching.replace_tests([
        {"id": "old", "testName": "Past", "testDateTime": "2026-01-01T09:00:00",
         "courseId": "7", "syllabus": "Physics: Kinematics"},
    ], db_path=db)
    upcoming = cs.upcoming_syllabus(today="2026-08-01", db_path=db)
    assert upcoming == []


# ---------------------------------------------------------------------------
# Coverage / progress
# ---------------------------------------------------------------------------

def _seed_ledger(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE ledger (notion_page_id TEXT PRIMARY KEY, archived INTEGER DEFAULT 0, "
        "subject TEXT, task TEXT, chapter_text TEXT, exercise_type TEXT, page_content TEXT)"
    )
    conn.executemany(
        "INSERT INTO ledger (notion_page_id, archived, subject, task, chapter_text, page_content) "
        "VALUES (?,0,?,?,?,?)",
        [
            ("l1", "Physics", "solved kinematics ex", "Kinematics", "EB-1 kinematics"),
            ("l2", "Physics", "vectors practice", "Vectors", ""),
        ],
    )
    conn.commit()


def _seed_doubts(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE doubts (notion_page_id TEXT PRIMARY KEY, archived INTEGER DEFAULT 0, "
        "subject TEXT, core_concept TEXT, page_content TEXT)"
    )
    conn.execute(
        "INSERT INTO doubts (notion_page_id, archived, subject, core_concept, page_content) "
        "VALUES ('d1',0,'Physics','Laws of Motion sign flips','')"
    )
    conn.commit()


def _seed_test(db: Path) -> None:
    ntsc_coaching.replace_tests([
        {"id": "t1", "testName": "Weekly Test", "testDateTime": "2026-08-15T09:00:00",
         "courseId": "7", "syllabus": "Physics: Kinematics, Laws of Motion, Vectors"},
    ], db_path=db)
    cs.replace_syllabi([
        {"id": "t1", "syllabus": "Physics: Kinematics, Laws of Motion, Vectors"},
    ], db_path=db)


def test_coverage_snapshot(tmp_path):
    db = _fresh_db(tmp_path)
    _seed_test(db)
    conn = sqlite3.connect(str(db))
    _seed_ledger(conn)
    _seed_doubts(conn)
    conn.close()

    tests = cs.coverage_snapshot(today="2026-08-01", db_path=db)
    assert len(tests) == 1
    records = {r["topic"]: r for r in tests[0]["syllabus_records"]}
    assert records["Kinematics"]["covered"] is True
    assert records["Vectors"]["covered"] is True
    assert records["Laws of Motion"]["covered"] is False
    assert records["Laws of Motion"]["has_doubt"] is True
    cov = tests[0]["coverage"]
    assert cov["topic_count"] == 3
    assert cov["covered_count"] == 2
    assert cov["uncovered_count"] == 1
    assert cov["covered_fraction"] == 0.667
    assert cov["known"] is True


def test_coverage_snapshot_without_mirror_tables(tmp_path):
    db = _fresh_db(tmp_path)
    _seed_test(db)
    tests = cs.coverage_snapshot(today="2026-08-01", db_path=db)
    records = {r["topic"]: r for r in tests[0]["syllabus_records"]}
    assert all(records[t]["covered"] is False for t in records)
    assert all(records[t]["has_doubt"] is False for t in records)
    assert tests[0]["coverage"]["covered_count"] == 0


def test_progress_snapshot(tmp_path):
    db = _fresh_db(tmp_path)
    _seed_test(db)
    conn = sqlite3.connect(str(db))
    _seed_ledger(conn)
    conn.close()

    progress = cs.progress_snapshot(today="2026-08-01", db_path=db)
    assert progress["tests"][0]["source_id"] == "t1"
    assert progress["tests"][0]["covered_count"] == 2
    assert len(progress["subjects"]) == 1
    subject = progress["subjects"][0]
    assert subject["subject"] == "Physics"
    assert subject["topic_count"] == 3
    assert subject["covered_count"] == 2
    assert subject["uncovered_count"] == 1
    assert subject["covered_fraction"] == 0.667
    assert subject["tests"] == ["t1"]


# ---------------------------------------------------------------------------
# Sync integration
# ---------------------------------------------------------------------------

class _FakeClient:
    def login(self):
        return {"data": {"academicYear": 2026, "academicYearTitle": "2025-26"}}

    def profile(self):
        return {"data": {"studentId": "s1", "name": "Test Student"}}

    def batches(self, academic_year):
        return {"data": [{"title": "B1", "campusName": "Campus", "courseName": "Course"}]}

    def tests(self):
        return {"data": {"result": [
            {"id": 101, "courseId": 7, "isExamCourse": 0, "testName": "Weekly Test",
             "testDateTime": "2026-08-15T09:00:00",
             "syllabus": "<p>Physics: Kinematics, Vectors</p>"},
        ]}}

    def scheduled_exams(self, course_id):
        return {"data": {"examPaper": []}}

    def course_results(self, course_id):
        return {"data": {"result": []}}

    def appeared_results(self, result_id):
        return {"data": {"result": []}}

    def result_analysis(self, exam_id):
        return {"result": {}}

    def classes(self, start_date, end_date):
        return {"data": {"data": {"timeTable": []}}}


def test_sync_once_populates_syllabus(tmp_path):
    db = _fresh_db(tmp_path)
    result = ntsc_sync.sync_once(client=_FakeClient(), db_path=db)
    assert result["status"] == "success"
    assert "syllabus" in result["datasets"]
    assert "tests" in result["datasets"]

    stored = cs.syllabus_for_test("101", db_path=db)
    assert stored["meta"]["record_count"] == 2
    assert [r["topic"] for r in stored["records"]] == ["Kinematics", "Vectors"]

    upcoming = cs.upcoming_syllabus(today="2026-08-01", db_path=db)
    assert len(upcoming) == 1
    assert upcoming[0]["syllabus_count"] == 2
