"""Local cache and read helpers for the Narayana coaching portal.

The portal is an external source.  This module keeps its useful coaching data
in SQLite so normal LLM requests never need portal credentials or live HTTP.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import coaching_syllabus
import session_context
from config import settings

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "sqlite_mirror.db"

_ISO_DATE_RE = re.compile(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})")
_DMY_DATE_RE = re.compile(r"^(\d{1,2})[-/](\d{1,2})[-/](\d{4})")


def _iso_date(value: Any) -> str | None:
    """Normalize YYYY-MM-DD or DD-MM-YYYY (optionally with a time suffix) to ISO."""
    if value is None:
        return None
    text = str(value).strip()
    match = _ISO_DATE_RE.match(text)
    if match:
        year, month, day = (int(part) for part in match.groups())
    else:
        match = _DMY_DATE_RE.match(text)
        if not match:
            return None
        day, month, year = (int(part) for part in match.groups())
    return f"{year:04d}-{month:02d}-{day:02d}"


def _connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS coaching_profile (
            singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
            student_id TEXT,
            name TEXT,
            goal TEXT,
            current_class TEXT,
            course_name TEXT,
            student_uid TEXT,
            batch TEXT,
            campus TEXT,
            academic_year TEXT,
            source_updated_at TEXT,
            raw_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS coaching_classes (
            source_id TEXT PRIMARY KEY,
            class_date TEXT NOT NULL,
            start_time TEXT NOT NULL,
            duration_min INTEGER,
            class_type TEXT,
            subjects TEXT,
            live_class INTEGER NOT NULL DEFAULT 0,
            source_updated_at TEXT,
            raw_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_coaching_classes_date
            ON coaching_classes(class_date, start_time);
        CREATE TABLE IF NOT EXISTS coaching_tests (
            source_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            test_date TEXT,
            course_id TEXT,
            batch TEXT,
            goal TEXT,
            syllabus TEXT,
            source_updated_at TEXT,
            raw_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_coaching_tests_date
            ON coaching_tests(test_date);
        CREATE TABLE IF NOT EXISTS coaching_results (
            source_id TEXT PRIMARY KEY,
            test_paper_id TEXT,
            title TEXT,
            attempt_date TEXT,
            total_marks REAL,
            maximum_marks REAL,
            rank TEXT,
            batch_rank TEXT,
            percentile REAL,
            correct INTEGER,
            incorrect INTEGER,
            attempted INTEGER,
            unattempted INTEGER,
            raw_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS coaching_subject_results (
            result_id TEXT NOT NULL,
            subject TEXT NOT NULL,
            marks REAL,
            maximum_marks REAL,
            rank TEXT,
            percentile REAL,
            correct INTEGER,
            incorrect INTEGER,
            unattempted INTEGER,
            raw_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY(result_id, subject)
        );
        CREATE TABLE IF NOT EXISTS coaching_sync_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            datasets TEXT NOT NULL DEFAULT '[]',
            error TEXT
        );
    """)
    coaching_syllabus.init_db(conn)
    conn.commit()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def replace_profile(profile: dict[str, Any], *, db_path: str | Path = DEFAULT_DB_PATH) -> None:
    detail = profile.get("data") if isinstance(profile.get("data"), dict) else profile
    with _connect(db_path) as conn:
        conn.execute("""INSERT INTO coaching_profile
            (singleton,student_id,name,goal,current_class,course_name,student_uid,
             batch,campus,academic_year,source_updated_at,raw_json)
            VALUES (1,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(singleton) DO UPDATE SET
              student_id=excluded.student_id,name=excluded.name,goal=excluded.goal,
              current_class=excluded.current_class,course_name=excluded.course_name,
              student_uid=excluded.student_uid,batch=excluded.batch,campus=excluded.campus,
              academic_year=excluded.academic_year,source_updated_at=excluded.source_updated_at,
              raw_json=excluded.raw_json""",
            (str(detail.get("studentId") or ""), detail.get("name"), detail.get("goal"),
             detail.get("currentClass"), detail.get("courseName") or detail.get("course"),
             detail.get("studentUId"), None, None,
             detail.get("academicYearTitle"), _now(),
             json.dumps(detail, ensure_ascii=False, sort_keys=True)))
        conn.commit()


def update_profile_context(
    *, batch: str | None = None, campus: str | None = None,
    course_name: str | None = None, academic_year: str | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> None:
    with _connect(db_path) as conn:
        conn.execute("""UPDATE coaching_profile SET
            batch=COALESCE(?,batch), campus=COALESCE(?,campus),
            course_name=COALESCE(?,course_name), academic_year=COALESCE(?,academic_year),
            source_updated_at=? WHERE singleton=1""",
            (batch, campus, course_name, academic_year, _now()))
        conn.commit()


def replace_classes(rows: list[dict[str, Any]], *, db_path: str | Path = DEFAULT_DB_PATH) -> None:
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM coaching_classes")
        for index, row in enumerate(rows):
            class_date = _iso_date(row.get("classDate"))
            if not class_date:
                continue
            key = f"{class_date}|{row.get('startTime')}|{row.get('classType')}|{index}"
            live_class = 1 if (row.get("isLive") or row.get("liveClass")) else 0
            conn.execute("""INSERT OR REPLACE INTO coaching_classes
                (source_id,class_date,start_time,duration_min,class_type,subjects,
                 live_class,source_updated_at,raw_json) VALUES (?,?,?,?,?,?,?,?,?)""",
                (key, class_date, str(row.get("startTime") or ""), row.get("duration"),
                 row.get("classType"), row.get("subjects"), live_class, _now(),
                 json.dumps(row, ensure_ascii=False, sort_keys=True)))
        conn.commit()


def replace_tests(rows: list[dict[str, Any]], *, db_path: str | Path = DEFAULT_DB_PATH) -> None:
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM coaching_tests")
        for row in rows:
            source_id = str(row.get("id") or row.get("testPaperId") or "")
            if not source_id:
                continue
            conn.execute("""INSERT OR REPLACE INTO coaching_tests
                (source_id,title,test_date,course_id,batch,goal,syllabus,source_updated_at,raw_json)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (source_id, row.get("testName") or row.get("test_name") or "",
                 _iso_date(row.get("testDateTime") or row.get("dateTime")),
                 str(row.get("courseId") or ""),
                 row.get("batch"), row.get("goal"), row.get("syllabus"), _now(),
                 json.dumps(row, ensure_ascii=False, sort_keys=True)))
        conn.commit()


def replace_results(rows: list[dict[str, Any]], *, db_path: str | Path = DEFAULT_DB_PATH) -> None:
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM coaching_subject_results")
        conn.execute("DELETE FROM coaching_results")
        for result in rows:
            result_id = str(result.get("id") or result.get("exam_id") or "")
            if not result_id:
                continue
            conn.execute("""INSERT OR REPLACE INTO coaching_results
                (source_id,test_paper_id,title,attempt_date,total_marks,maximum_marks,rank,
                 batch_rank,percentile,correct,incorrect,attempted,unattempted,raw_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (result_id, str(result.get("testPaperId") or ""), result.get("testName"),
                 _iso_date(result.get("attemptDate")), result.get("totalMarks"), result.get("totalSubjectMarks"),
                 result.get("rank"), result.get("batchRank"), result.get("percentile"),
                 result.get("totalCorrect"), result.get("totalInCorrect"), result.get("totalAttempted"),
                 result.get("totalUnAttempted"), json.dumps(result, ensure_ascii=False, sort_keys=True)))
            for subject in result.get("subjectData") or []:
                conn.execute("""INSERT OR REPLACE INTO coaching_subject_results
                    (result_id,subject,marks,maximum_marks,rank,percentile,correct,incorrect,unattempted,raw_json)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (result_id, subject.get("subjectName") or "", subject.get("totalMarks"),
                     subject.get("totalSubjectMarks"), subject.get("rank"), subject.get("percentile"),
                     subject.get("totalCorrect"), subject.get("totalIncorrect"),
                     subject.get("totalUnattempted"), json.dumps(subject, ensure_ascii=False, sort_keys=True)))
        conn.commit()


def classes_for_date(date: str, *, db_path: str | Path = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    with _connect(db_path) as conn:
        rows = conn.execute("""SELECT class_date,start_time,duration_min,class_type,subjects,
            live_class,source_updated_at FROM coaching_classes WHERE class_date=?
            ORDER BY start_time""", (date[:10],)).fetchall()
    return [dict(row) for row in rows]


def next_classes(
    *, today: str | None = None, limit: int = 5, db_path: str | Path = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    """Next upcoming cached classes at/after today (local), ordered by time."""
    today = (today or session_context.local_today_iso())[:10]
    with _connect(db_path) as conn:
        rows = conn.execute(
            """SELECT source_id,class_date,start_time,duration_min,class_type,subjects,
               live_class,source_updated_at FROM coaching_classes
               WHERE class_date>=? ORDER BY class_date,start_time LIMIT ?""",
            (today, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def next_tests(*, today: str | None = None, limit: int = 5, db_path: str | Path = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    today = (today or session_context.local_today_iso())[:10]
    with _connect(db_path) as conn:
        rows = conn.execute("""SELECT source_id,title,test_date,course_id,batch,goal,syllabus,source_updated_at
            FROM coaching_tests WHERE substr(COALESCE(test_date,''),1,10)>=?
            ORDER BY test_date LIMIT ?""", (today, limit)).fetchall()
    return [dict(row) for row in rows]


def resolve_date(text: str, *, now: dt.datetime | None = None) -> str | None:
    """Resolve simple relative date phrases for schedule reads."""
    now = now or session_context.local_now()
    value = text.lower()
    if "today" in value:
        return now.date().isoformat()
    if "tomorrow" in value:
        return (now.date() + dt.timedelta(days=1)).isoformat()
    match = re.search(r"(?:in|after)\s+(\d+)\s+days?", value)
    if match:
        return (now.date() + dt.timedelta(days=int(match.group(1)))).isoformat()
    return None


def schedule_answer(text: str, *, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, Any] | None:
    date = resolve_date(text)
    if date is None:
        return None
    return {"date": date, "classes": classes_for_date(date, db_path=db_path)}


def freshness(*, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    with _connect(db_path) as conn:
        row = conn.execute("""SELECT started_at,finished_at,status,error,datasets
            FROM coaching_sync_runs ORDER BY id DESC LIMIT 1""").fetchone()
    return dict(row) if row else {"status": "never_synced"}


def context_snapshot(*, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    today = session_context.local_today_iso()
    tomorrow = (dt.date.fromisoformat(today) + dt.timedelta(days=1)).isoformat()
    with _connect(db_path) as conn:
        profile_row = conn.execute("SELECT * FROM coaching_profile WHERE singleton=1").fetchone()
        latest = conn.execute("""SELECT title,attempt_date,total_marks,maximum_marks,rank,
            batch_rank,percentile FROM coaching_results ORDER BY substr(attempt_date,1,10) DESC
            LIMIT 1""").fetchone()
        result_count = conn.execute("SELECT COUNT(*) FROM coaching_results").fetchone()[0]
    return {
        "profile": dict(profile_row) if profile_row else None,
        "today": {"date": today, "classes": classes_for_date(today, db_path=db_path)},
        "tomorrow": {"date": tomorrow, "classes": classes_for_date(tomorrow, db_path=db_path)},
        "next_tests": next_tests(today=today, limit=2, db_path=db_path),
        "latest_result": dict(latest) if latest else None,
        "result_count": result_count,
        "freshness": freshness(db_path=db_path),
    }
