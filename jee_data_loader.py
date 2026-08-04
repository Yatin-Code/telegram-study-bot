"""Ingest jee-analysis analytics into the bot's local SQLite store.

The jee-analysis pipeline (run manually, outside the bot) produces a single
``final_data.json`` with mined JEE exam analytics (2016-2026). This module
ingests that file into six ``op_jee_*`` local tables so the agent tools and
bot commands can answer data-backed questions about chapter frequency,
weightage and repeating patterns.

Design rules (see .omo/plans/jee-analysis-integration.md):
  * Fully local SQLite — no Notion, no new dependencies (stdlib only).
  * Idempotent: ``load()`` clears and reloads all six tables in ONE
    transaction, so re-running (weekly refresh) never grows or duplicates rows.
  * No raw question text is stored — only metadata and pattern summaries.
  * All read paths thread ``db_path`` from callers; never read the module-level
    default inside a read helper.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = PROJECT_ROOT / "sqlite_mirror.db"
JEE_DATA_PATH = PROJECT_ROOT / "jee-analysis" / "raw_data" / "final_data.json"

# The six local tables registered in config.ownership.LOCAL_SQL_TABLES.
LOCAL_TABLES = (
    "op_jee_metadata",
    "op_jee_chapter_stats",
    "op_jee_patterns",
    "op_jee_trends",
    "op_jee_questions_meta",
    "op_jee_sync_state",
)

# Canonical subject labels (source JSON uses lowercase keys).
_SUBJECT_CANONICAL = {
    "physics": "Physics",
    "chemistry": "Chemistry",
    "mathematics": "Mathematics",
}


def _canonical_subject(value: Any) -> str:
    return _SUBJECT_CANONICAL.get(str(value or "").strip().lower(), str(value or "").strip())


def init_db(conn: sqlite3.Connection) -> None:
    """Create the six op_jee_* tables if they don't exist.

    ``op_jee_chapter_stats`` stores ONE row per exam present in a chapter's
    ``by_exam`` map; the ``*_json`` columns carry the chapter-GLOBAL data
    (by_year / by_difficulty / by_question_type / sub_topics) which is the same
    on every exam row. Ratio columns are derived at load time.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS op_jee_metadata (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            total_papers INTEGER,
            total_questions INTEGER,
            total_classified INTEGER,
            total_patterns INTEGER,
            total_chapters INTEGER,
            source_updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS op_jee_chapter_stats (
            subject TEXT NOT NULL,
            chapter TEXT NOT NULL,
            exam_type TEXT NOT NULL,
            total_questions INTEGER,
            repeating_questions INTEGER,
            unique_questions INTEGER,
            repeat_ratio REAL,
            easy_ratio REAL,
            medium_ratio REAL,
            hard_ratio REAL,
            importance_score REAL,
            by_year_json TEXT,
            by_difficulty_json TEXT,
            by_question_type_json TEXT,
            sub_topics_json TEXT,
            needs_figure INTEGER,
            PRIMARY KEY (subject, chapter, exam_type)
        );
        CREATE INDEX IF NOT EXISTS idx_jee_chapter_stats_subject
            ON op_jee_chapter_stats(subject, chapter);
        CREATE TABLE IF NOT EXISTS op_jee_patterns (
            pattern_id INTEGER PRIMARY KEY,
            subject TEXT NOT NULL,
            chapter TEXT,
            sub_topic TEXT,
            frequency INTEGER,
            years_json TEXT,
            exams_json TEXT,
            core_concept TEXT,
            key_formula TEXT,
            common_trap TEXT,
            difficulty TEXT,
            question_type TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_jee_patterns_subject
            ON op_jee_patterns(subject, chapter);
        CREATE TABLE IF NOT EXISTS op_jee_trends (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT,
            chapter TEXT,
            year TEXT,
            question_count INTEGER,
            UNIQUE (subject, chapter, year)
        );
        CREATE TABLE IF NOT EXISTS op_jee_questions_meta (
            question_id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT,
            chapter TEXT,
            sub_topic TEXT,
            difficulty TEXT,
            exam_type TEXT,
            year TEXT,
            question_type TEXT,
            cluster_id INTEGER,
            cluster_size INTEGER,
            cluster_years_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_jee_questions_subject
            ON op_jee_questions_meta(subject, chapter);
        CREATE TABLE IF NOT EXISTS op_jee_sync_state (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            last_load_mtime REAL,
            last_loaded_at TEXT
        );
    """)
    conn.commit()


def _connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    init_db(conn)
    return conn