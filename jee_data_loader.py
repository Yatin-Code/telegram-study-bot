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


def _norm(value: Any) -> str:
    """Case-insensitive, whitespace-normalized key for chapter matching.

    ``&`` is mapped to ``and`` so a doubt chapter like "electric charges &
    fields" matches the canonical "Electric Charges and Fields".
    """
    text = str(value or "").strip().lower().replace("&", " and ")
    return " ".join(text.split())


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False) if value is not None else None


def _ratio(numerator: Any, denominator: Any) -> float | None:
    try:
        num = float(numerator or 0)
        den = float(denominator or 0)
    except (TypeError, ValueError):
        return None
    if den <= 0:
        return None
    return round(num / den, 4)


def load(
    data_path: str | Path | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Ingest ``final_data.json`` into the six op_jee_* tables (idempotent).

    Clears and reloads every op_jee_* table inside ONE transaction, so a
    re-run (weekly refresh) never grows or duplicates rows. Returns a summary
    dict of per-table row counts plus the persisted metadata. Raises
    ``FileNotFoundError``/``json.JSONDecodeError`` on a missing/corrupt file
    and leaves prior rows intact (transaction rollback).
    """
    data_path = Path(data_path) if data_path is not None else JEE_DATA_PATH
    db_path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    with open(data_path, "r", encoding="utf-8") as handle:
        data = json.load(handle)

    mtime = data_path.stat().st_mtime
    metadata = data.get("metadata") or {}
    chapter_stats = data.get("chapter_stats") or {}
    rankings = data.get("chapter_rankings") or []
    patterns = data.get("patterns") or []
    trends = data.get("trends") or []
    questions = data.get("questions") or []

    # rankings lookup: (subject, chapter) -> ranking row
    ranking_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for ranking in rankings:
        subject = _canonical_subject(ranking.get("subject"))
        chapter = str(ranking.get("chapter") or "").strip()
        if subject and chapter:
            ranking_by_key[(subject, chapter)] = ranking

    with _connect(db_path) as conn:
        conn.execute("BEGIN")
        try:
            for table in LOCAL_TABLES:
                conn.execute(f"DELETE FROM {table}")
            # Reset AUTOINCREMENT counters so question_id / trend id stay stable
            # across refreshes (row counts are what matter, but stable ids help).
            for seq_table in ("op_jee_questions_meta", "op_jee_trends"):
                conn.execute(
                    "DELETE FROM sqlite_sequence WHERE name=?", (seq_table,)
                )

            # --- metadata ---------------------------------------------------
            conn.execute(
                "INSERT INTO op_jee_metadata (id, total_papers, total_questions, "
                "total_classified, total_patterns, total_chapters, source_updated_at) "
                "VALUES (1, ?, ?, ?, ?, ?, ?)",
                (
                    metadata.get("total_papers"),
                    metadata.get("total_questions"),
                    metadata.get("total_classified"),
                    metadata.get("total_patterns"),
                    metadata.get("total_chapters"),
                    _iso_from_mtime(mtime),
                ),
            )

            # --- chapter_stats (one row per exam in by_exam) ----------------
            for subject_key, chapters in chapter_stats.items():
                subject = _canonical_subject(subject_key)
                for chapter, stats in chapters.items():
                    chapter = str(chapter or "").strip()
                    global_total = stats.get("total_questions") or 0
                    by_difficulty = stats.get("by_difficulty") or {}
                    repeating = stats.get("repeating_questions") or 0
                    unique = stats.get("unique_questions") or 0
                    ranking = ranking_by_key.get((subject, chapter)) or {}
                    repeat_ratio = ranking.get("repeat_ratio")
                    if repeat_ratio is None:
                        repeat_ratio = _ratio(repeating, global_total)
                    easy_ratio = ranking.get("easy_ratio")
                    if easy_ratio is None:
                        easy_ratio = _ratio(by_difficulty.get("Easy"), global_total)
                    medium_ratio = _ratio(by_difficulty.get("Medium"), global_total)
                    hard_ratio = _ratio(by_difficulty.get("Hard"), global_total)
                    importance = ranking.get("roi_score")
                    by_exam = stats.get("by_exam") or {}
                    if by_exam:
                        for exam_type, count in by_exam.items():
                            conn.execute(
                                "INSERT INTO op_jee_chapter_stats (subject, chapter, "
                                "exam_type, total_questions, repeating_questions, "
                                "unique_questions, repeat_ratio, easy_ratio, "
                                "medium_ratio, hard_ratio, importance_score, "
                                "by_year_json, by_difficulty_json, "
                                "by_question_type_json, sub_topics_json, needs_figure) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                (
                                    subject, chapter, str(exam_type), count,
                                    repeating, unique, repeat_ratio, easy_ratio,
                                    medium_ratio, hard_ratio, importance,
                                    _json(stats.get("by_year")),
                                    _json(by_difficulty),
                                    _json(stats.get("by_question_type")),
                                    _json(stats.get("sub_topics")),
                                    1 if stats.get("needs_figure") else 0,
                                ),
                            )
                    else:
                        # Defensive: a ranking chapter with no by_exam row gets a
                        # synthetic 'mains' row. For the current dataset this never
                        # fires (all 64 ranking chapters have by_exam rows).
                        conn.execute(
                            "INSERT INTO op_jee_chapter_stats (subject, chapter, "
                            "exam_type, total_questions, repeating_questions, "
                            "unique_questions, repeat_ratio, easy_ratio, "
                            "medium_ratio, hard_ratio, importance_score, "
                            "by_year_json, by_difficulty_json, "
                            "by_question_type_json, sub_topics_json, needs_figure) "
                            "VALUES (?, ?, 'mains', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                subject, chapter, ranking.get("total") or global_total,
                                repeating, unique, repeat_ratio, easy_ratio,
                                medium_ratio, hard_ratio, importance,
                                _json(stats.get("by_year")),
                                _json(by_difficulty),
                                _json(stats.get("by_question_type")),
                                _json(stats.get("sub_topics")),
                                1 if stats.get("needs_figure") else 0,
                            ),
                        )

            # --- patterns ---------------------------------------------------
            for pattern in patterns:
                pattern_id = pattern.get("cluster_id")
                if pattern_id is None:
                    continue  # AUTOINCREMENT fallback is dead for current data
                conn.execute(
                    "INSERT INTO op_jee_patterns (pattern_id, subject, chapter, "
                    "sub_topic, frequency, years_json, exams_json, core_concept, "
                    "key_formula, common_trap, difficulty, question_type) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        int(pattern_id),
                        _canonical_subject(pattern.get("subject")),
                        str(pattern.get("chapter") or "").strip() or None,
                        str(pattern.get("sub_topic") or "").strip() or None,
                        pattern.get("frequency"),
                        _json(pattern.get("years")),
                        _json(pattern.get("exams")),
                        str(pattern.get("core_concept") or "").strip() or None,
                        str(pattern.get("key_formula") or "").strip() or None,
                        str(pattern.get("common_trap") or "").strip() or None,
                        str(pattern.get("difficulty") or "").strip() or None,
                        str(pattern.get("question_type") or "").strip() or None,
                    ),
                )

            # --- trends (flatten year_counts) --------------------------------
            for trend in trends:
                subject = _canonical_subject(trend.get("subject"))
                chapter = str(trend.get("chapter") or "").strip()
                year_counts = trend.get("year_counts") or {}
                for year, count in year_counts.items():
                    conn.execute(
                        "INSERT INTO op_jee_trends (subject, chapter, year, "
                        "question_count) VALUES (?, ?, ?, ?)",
                        (subject, chapter, str(year), count),
                    )

            # --- questions (metadata only, AUTOINCREMENT rowid) --------------
            for question in questions:
                cluster_years = question.get("cluster_years")
                conn.execute(
                    "INSERT INTO op_jee_questions_meta (subject, chapter, sub_topic, "
                    "difficulty, exam_type, year, question_type, cluster_id, "
                    "cluster_size, cluster_years_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        _canonical_subject(question.get("subject")),
                        str(question.get("chapter") or "").strip() or None,
                        str(question.get("sub_topic") or "").strip() or None,
                        str(question.get("difficulty") or "").strip() or None,
                        str(question.get("exam") or "").strip() or None,
                        str(question.get("year") or "").strip() or None,
                        str(question.get("question_type") or "").strip() or None,
                        question.get("cluster_id"),
                        question.get("cluster_size"),
                        _json(cluster_years),
                    ),
                )

            # --- sync state (reset; mtime recorded by the refresh job) -------
            conn.execute(
                "INSERT INTO op_jee_sync_state (id, last_load_mtime, last_loaded_at) "
                "VALUES (1, NULL, NULL)"
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return summary(db_path=db_path)


def _iso_from_mtime(mtime: float) -> str:
    import datetime as dt

    return dt.datetime.fromtimestamp(mtime, tz=dt.timezone.utc).isoformat()


def summary(db_path: str | Path | None = None) -> dict[str, Any]:
    """Per-table row counts plus the persisted metadata block."""
    db_path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    counts: dict[str, int] = {}
    with _connect(db_path) as conn:
        for table in LOCAL_TABLES:
            row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
            counts[table] = int(row["n"])
        meta = conn.execute(
            "SELECT total_papers, total_questions, total_classified, "
            "total_patterns, total_chapters, source_updated_at "
            "FROM op_jee_metadata WHERE id=1"
        ).fetchone()
    metadata = dict(meta) if meta is not None else {}
    return {"counts": counts, "metadata": metadata}


def chapter_weightage(db_path: str | Path | None = None) -> dict[tuple[str, str], dict[str, Any]]:
    """Per-chapter weightage (SUM of total_questions across exams), ranked.

    Returns a dict keyed by the normalized ``(subject, chapter)`` pair:
    ``{(norm_subject, norm_chapter): {"subject", "chapter", "total_questions",
    "rank", "total_chapters"}}`` sorted by total_questions descending. Empty
    dict when tables are empty.
    """
    db_path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    totals: dict[tuple[str, str], dict[str, Any]] = {}
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT subject, chapter, SUM(total_questions) AS total "
            "FROM op_jee_chapter_stats "
            "WHERE LOWER(COALESCE(chapter,'')) <> 'unclassified' "
            "GROUP BY subject, chapter"
        ).fetchall()
    for row in rows:
        subject = str(row["subject"])
        chapter = str(row["chapter"])
        totals[(subject, chapter)] = {
            "subject": subject,
            "chapter": chapter,
            "total_questions": int(row["total"] or 0),
        }
    ranked = sorted(
        totals.values(), key=lambda item: (-item["total_questions"], item["chapter"])
    )
    total_chapters = len(ranked)
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for index, entry in enumerate(ranked, 1):
        entry["rank"] = index
        entry["total_chapters"] = total_chapters
        result[(_norm(entry["subject"]), _norm(entry["chapter"]))] = entry
    return result


def subject_difficulty(db_path: str | Path | None = None) -> dict[str, float]:
    """Per-subject historical difficulty factor from derived ratio columns.

    ``factor = 0.5 + mean over the subject's chapters (excluding 'Unclassified')
    of (hard_ratio + 0.5 * medium_ratio)`` — lands in [0.5, 1.5] for the current
    dataset. Empty dict when tables are empty.
    """
    db_path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    factors: dict[str, list[float]] = {}
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT subject, chapter, hard_ratio, medium_ratio "
            "FROM op_jee_chapter_stats "
            "WHERE LOWER(COALESCE(chapter,'')) <> 'unclassified'"
        ).fetchall()
    for row in rows:
        subject = str(row["subject"])
        hard = float(row["hard_ratio"] or 0)
        medium = float(row["medium_ratio"] or 0)
        factors.setdefault(subject, []).append(hard + 0.5 * medium)
    return {
        subject: round(0.5 + (sum(values) / len(values)), 3)
        for subject, values in factors.items()
    }


def chapter_evidence(
    subject: str | None, chapter: str | None,
    db_path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Best-effort JEE evidence for one chapter (case/whitespace-insensitive).

    Returns ``{"chapter", "total_questions", "repeat_ratio"}`` or None when the
    chapter does not match any stored row (or tables are empty).
    """
    if not chapter:
        return None
    db_path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    needle = _norm(chapter)
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT subject, chapter, total_questions, repeat_ratio "
            "FROM op_jee_chapter_stats"
        ).fetchall()
    best: dict[str, Any] | None = None
    for row in rows:
        if _norm(row["chapter"]) != needle:
            continue
        if subject and _norm(row["subject"]) != _norm(subject):
            continue
        entry = {
            "chapter": str(row["chapter"]),
            "total_questions": int(row["total_questions"] or 0),
            "repeat_ratio": float(row["repeat_ratio"] or 0),
        }
        if best is None or entry["total_questions"] > best["total_questions"]:
            best = entry
    return best