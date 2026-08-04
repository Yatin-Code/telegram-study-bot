"""jee_data_loader: ingestion of synthetic final_data.json into tmp SQLite.

All tests run on isolated tmp databases seeded from a tiny synthetic JSON
fixture — never the real 11MB jee-analysis/raw_data/final_data.json and never
the real sqlite_mirror.db. Covers load() row accounting, idempotency,
corrupt-file rollback, missing-file errors, ratio derivation, Unclassified
exclusion, chapter_evidence matching and refresh_if_stale mtime skipping.
"""

from __future__ import annotations

import json
import os
import sqlite3

import pytest

import jee_data_loader


# ---------------------------------------------------------------------------
# Synthetic final_data.json (tiny, deterministic; shape mirrors the real file)
# ---------------------------------------------------------------------------

def _synthetic_data() -> dict:
    return {
        "metadata": {
            "total_papers": 24,
            "total_questions": 1200,
            "total_classified": 1180,
            "total_patterns": 3,
            "total_chapters": 4,
        },
        "chapter_stats": {
            "physics": {
                "Electric Charges and Fields": {
                    "total_questions": 10,
                    "repeating_questions": 4,
                    "unique_questions": 6,
                    "needs_figure": 1,
                    "by_exam": {"mains": 6, "advanced": 4},
                    "by_difficulty": {"Easy": 3, "Medium": 4, "Hard": 3},
                    "by_year": {"2020": 3, "2021": 4, "2022": 3},
                    "by_question_type": {"MCQ": 7, "Numerical": 3},
                    "sub_topics": ["Coulomb's law", "Gauss law"],
                },
                "Kinematics": {
                    "total_questions": 20,
                    "repeating_questions": 10,
                    "unique_questions": 10,
                    "needs_figure": 0,
                    "by_exam": {"mains": 12, "advanced": 8},
                    "by_difficulty": {"Easy": 10, "Medium": 8, "Hard": 2},
                    "by_year": {"2021": 10, "2022": 10},
                    "by_question_type": {"MCQ": 15, "Numerical": 5},
                    "sub_topics": ["1D motion", "2D motion"],
                },
                "Unclassified": {
                    "total_questions": 8,
                    "repeating_questions": 8,
                    "unique_questions": 0,
                    "needs_figure": 0,
                    "by_exam": {"mains": 8},
                    "by_difficulty": {"Easy": 0, "Medium": 0, "Hard": 8},
                },
            },
            "chemistry": {
                "Chemical Bonding": {
                    "total_questions": 15,
                    "repeating_questions": 6,
                    "unique_questions": 9,
                    "needs_figure": 0,
                    "by_exam": {"mains": 15},
                    "by_difficulty": {"Easy": 5, "Medium": 5, "Hard": 5},
                    "by_year": {"2019": 15},
                    "by_question_type": {"MCQ": 15},
                    "sub_topics": ["VSEPR"],
                },
            },
        },
        "chapter_rankings": [
            {
                "subject": "physics",
                "chapter": "Kinematics",
                "total": 20,
                "roi_score": 0.95,
                "repeat_ratio": 0.5,
                "easy_ratio": 0.5,
            },
        ],
        "patterns": [
            {
                "cluster_id": 1,
                "subject": "physics",
                "chapter": "Electric Charges and Fields",
                "sub_topic": "Coulomb's law",
                "frequency": 8,
                "years": [2020, 2021, 2022],
                "exams": ["mains"],
                "core_concept": "Inverse square law",
                "key_formula": "F = kq1q2/r^2",
                "common_trap": "Sign errors",
                "difficulty": "Easy",
                "question_type": "MCQ",
            },
            {
                "cluster_id": 2,
                "subject": "physics",
                "chapter": "Kinematics",
                "sub_topic": "Projectile motion",
                "frequency": 5,
                "years": [2021, 2022],
                "exams": ["mains", "advanced"],
                "core_concept": "Range equation",
                "key_formula": "R = v^2 sin(2θ)/g",
                "common_trap": "Angle doubling",
                "difficulty": "Medium",
                "question_type": "Numerical",
            },
            {
                "cluster_id": 3,
                "subject": "chemistry",
                "chapter": "Chemical Bonding",
                "sub_topic": "VSEPR geometry",
                "frequency": 3,
                "years": [2019],
                "exams": ["mains"],
                "core_concept": "Geometry prediction",
                "key_formula": "AXE notation",
                "common_trap": "Lone pairs",
                "difficulty": "Hard",
                "question_type": "MCQ",
            },
        ],
        "trends": [
            {
                "subject": "physics",
                "chapter": "Electric Charges and Fields",
                "year_counts": {"2020": 3, "2021": 4, "2022": 3},
            },
            {
                "subject": "chemistry",
                "chapter": "Chemical Bonding",
                "year_counts": {"2019": 15},
            },
        ],
        "questions": [
            {
                "subject": "physics",
                "chapter": "Electric Charges and Fields",
                "sub_topic": "Coulomb's law",
                "difficulty": "Easy",
                "exam_type": "mains",
                "exam": "mains",
                "year": 2020,
                "question_type": "MCQ",
                "cluster_id": 1,
                "cluster_size": 8,
                "cluster_years": [2020, 2021, 2022],
            },
            {
                "subject": "physics",
                "chapter": "Kinematics",
                "sub_topic": "Projectile motion",
                "difficulty": "Medium",
                "exam_type": "advanced",
                "exam": "advanced",
                "year": 2022,
                "question_type": "Numerical",
                "cluster_id": 2,
                "cluster_size": 5,
                "cluster_years": [2021, 2022],
            },
        ],
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def data_file(tmp_path):
    path = tmp_path / "final_data.json"
    path.write_text(json.dumps(_synthetic_data()), encoding="utf-8")
    return path


@pytest.fixture()
def db(tmp_path):
    return tmp_path / "jee_test.db"


def _counts(db) -> dict:
    return jee_data_loader.summary(db_path=db)["counts"]


# ---------------------------------------------------------------------------
# load() — row accounting across all six tables
# ---------------------------------------------------------------------------

def test_load_populates_all_six_tables(db, data_file):
    result = jee_data_loader.load(data_path=data_file, db_path=db)
    counts = result["counts"]
    # chapter_stats: one row per exam in by_exam → 2 + 2 + 1 (physics) + 1 (chem)
    assert counts["op_jee_metadata"] == 1
    assert counts["op_jee_chapter_stats"] == 6
    assert counts["op_jee_patterns"] == 3
    assert counts["op_jee_trends"] == 4  # 3 year_counts + 1
    assert counts["op_jee_questions_meta"] == 2
    assert counts["op_jee_sync_state"] == 1
    assert result["metadata"]["total_papers"] == 24


def test_load_is_idempotent(db, data_file):
    empty = _counts(db)
    assert empty["op_jee_chapter_stats"] == 0
    first = jee_data_loader.load(data_path=data_file, db_path=db)["counts"]
    second = jee_data_loader.load(data_path=data_file, db_path=db)["counts"]
    # Both loads must land on the same stable counts (no growth/duplication).
    assert first == second
    assert second["op_jee_chapter_stats"] == 6
    assert second["op_jee_questions_meta"] == 2
    assert second["op_jee_patterns"] == 3


# ---------------------------------------------------------------------------
# load() — failure paths: corrupt file rollback, missing file
# ---------------------------------------------------------------------------

def test_corrupt_file_raises_and_rolls_back(db, data_file):
    jee_data_loader.load(data_path=data_file, db_path=db)
    before = _counts(db)
    assert before["op_jee_chapter_stats"] == 6
    data_file.write_text("{ this is not valid json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        jee_data_loader.load(data_path=data_file, db_path=db)
    after = _counts(db)
    assert after == before


def test_load_missing_file_raises(db, tmp_path):
    with pytest.raises(FileNotFoundError):
        jee_data_loader.load(data_path=tmp_path / "does_not_exist.json", db_path=db)


# ---------------------------------------------------------------------------
# Ratio derivation (no ranking row → derived from counts)
# ---------------------------------------------------------------------------

def test_ratio_derivation_without_ranking(db, data_file):
    jee_data_loader.load(data_path=data_file, db_path=db)
    # "Electric Charges and Fields" has NO ranking row: T=10, R=4, E=3, M=4, H=3.
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT repeat_ratio, easy_ratio, medium_ratio, hard_ratio "
            "FROM op_jee_chapter_stats WHERE subject='Physics' "
            "AND chapter='Electric Charges and Fields' AND exam_type='mains'"
        ).fetchone()
    assert row["repeat_ratio"] == pytest.approx(4 / 10)
    assert row["easy_ratio"] == pytest.approx(3 / 10)
    assert row["medium_ratio"] == pytest.approx(4 / 10)
    assert row["hard_ratio"] == pytest.approx(3 / 10)


def test_ranking_row_overrides_derived_ratios(db, data_file):
    jee_data_loader.load(data_path=data_file, db_path=db)
    # "Kinematics" HAS a ranking row (repeat_ratio=0.5, easy_ratio=0.5) which
    # must win over the derived values (R/T=0.5 is the same, easy would be 0.5
    # anyway here — but the ranking values are what's stored).
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT repeat_ratio, easy_ratio, importance_score "
            "FROM op_jee_chapter_stats WHERE subject='Physics' "
            "AND chapter='Kinematics' AND exam_type='mains'"
        ).fetchone()
    assert row["repeat_ratio"] == pytest.approx(0.5)
    assert row["easy_ratio"] == pytest.approx(0.5)
    assert row["importance_score"] == pytest.approx(0.95)


# ---------------------------------------------------------------------------
# subject_difficulty — Unclassified chapter excluded
# ---------------------------------------------------------------------------

def test_subject_difficulty_excludes_unclassified(db, data_file):
    jee_data_loader.load(data_path=data_file, db_path=db)
    factors = jee_data_loader.subject_difficulty(db_path=db)
    # Physics rows: ECAF (hard .3 + .5*.4 = .5) + Kinematics (.1 + .5*.4 = .3);
    # Unclassified (1.0) must NOT be in the mean.
    assert factors["Physics"] == pytest.approx(0.5 + (0.5 + 0.3) / 2)
    # If Unclassified were included the mean would be higher than 1.0.
    assert factors["Physics"] < 1.0
    assert "Chemistry" in factors


# ---------------------------------------------------------------------------
# chapter_evidence — case/whitespace-insensitive matching
# ---------------------------------------------------------------------------

def test_chapter_evidence_matches_case_whitespace_insensitively(db, data_file):
    jee_data_loader.load(data_path=data_file, db_path=db)
    evidence = jee_data_loader.chapter_evidence(
        "PHYSICS", "electric  charges and fields", db_path=db
    )
    assert evidence is not None
    assert evidence["chapter"] == "Electric Charges and Fields"
    # Best (highest) per-exam row: mains carries 6 of the chapter's 10.
    assert evidence["total_questions"] == 6
    assert evidence["repeat_ratio"] == pytest.approx(0.4)


def test_chapter_evidence_missing_chapter_returns_none(db, data_file):
    jee_data_loader.load(data_path=data_file, db_path=db)
    assert (
        jee_data_loader.chapter_evidence("Physics", "Quantum Mechanics", db_path=db)
        is None
    )
    assert jee_data_loader.chapter_evidence("Physics", None, db_path=db) is None


# ---------------------------------------------------------------------------
# chapter_weightage — public API sanity (ranked, Unclassified excluded)
# ---------------------------------------------------------------------------

def test_chapter_weightage_ranks_chapters(db, data_file):
    jee_data_loader.load(data_path=data_file, db_path=db)
    weightage = jee_data_loader.chapter_weightage(db_path=db)
    assert ("physics", "kinematics") in weightage
    assert weightage[("physics", "kinematics")]["total_questions"] == 20
    assert weightage[("physics", "kinematics")]["rank"] == 1
    assert weightage[("physics", "kinematics")]["total_chapters"] == 3
    assert all("unclassified" not in key[1] for key in weightage)


# ---------------------------------------------------------------------------
# refresh_if_stale — missing file, first load, mtime skip, mtime bump reload
# ---------------------------------------------------------------------------

def test_refresh_if_stale_missing_file(tmp_path):
    db = tmp_path / "refresh.db"
    result = jee_data_loader.refresh_if_stale(
        data_path=tmp_path / "missing.json", db_path=db
    )
    assert result["skipped"] == "missing"


def test_refresh_if_stale_mtime_skip_and_reload(tmp_path):
    db = tmp_path / "refresh.db"
    data = tmp_path / "final_data.json"
    data.write_text(json.dumps(_synthetic_data()), encoding="utf-8")

    # (b) first call: never loaded (NULL mtime) → always loads.
    first = jee_data_loader.refresh_if_stale(data_path=data, db_path=db)
    assert first["loaded"] is True
    assert first["counts"]["op_jee_patterns"] == 3

    # last_load_mtime is persisted after the load.
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT last_load_mtime FROM op_jee_sync_state WHERE id=1"
        ).fetchone()
    assert row[0] is not None

    # (c) same mtime → skipped as current.
    second = jee_data_loader.refresh_if_stale(data_path=data, db_path=db)
    assert second["skipped"] == "current"
    assert "mtime" in second

    # (d) bumped mtime → reloads.
    old = data.stat().st_mtime
    os.utime(data, (old + 1000, old + 1000))
    third = jee_data_loader.refresh_if_stale(data_path=data, db_path=db)
    assert third["loaded"] is True
    assert third["mtime"] == pytest.approx(old + 1000)
    assert third["counts"]["op_jee_patterns"] == 3
