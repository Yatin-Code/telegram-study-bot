"""Synchronize useful Narayana coaching data into the local SQLite cache."""

from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any

import coaching_syllabus
import ntsc_coaching
import session_context
from ntsc_client import Client

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "sqlite_mirror.db"
logger = logging.getLogger(__name__)
_lock = threading.Lock()


def _data(payload: dict[str, Any]) -> Any:
    return payload.get("data") if isinstance(payload, dict) else None


def _list_at(payload: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
    value: Any = _data(payload)
    for key in keys:
        value = value.get(key) if isinstance(value, dict) else None
    return value if isinstance(value, list) else []


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _record_run(
    started: str, status: str, datasets: list[str], error: str | None,
    *, db_path: str | Path,
) -> None:
    with ntsc_coaching._connect(db_path) as conn:
        conn.execute("""INSERT INTO coaching_sync_runs
            (started_at,finished_at,status,datasets,error) VALUES (?,?,?,?,?)""",
            (started, dt.datetime.now(dt.timezone.utc).isoformat(), status,
             json.dumps(datasets), error))
        conn.commit()


def sync_once(
    *, client: Client | None = None, db_path: str | Path = DEFAULT_DB_PATH,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    if not _lock.acquire(blocking=False):
        return {"status": "already_running", "datasets": []}
    started = dt.datetime.now(dt.timezone.utc).isoformat()
    datasets: list[str] = []
    try:
        client = client or Client.from_settings()
        login = client.login()
        login_data = _data(login) or {}
        profile = client.profile()
        ntsc_coaching.replace_profile(profile, db_path=db_path)
        datasets.append("profile")

        academic_year = _to_int(login_data.get("academicYear")) or session_context.local_now().year
        batch_rows = _data(client.batches(academic_year)) or []
        batch = batch_rows[0] if batch_rows else {}

        tests_payload = client.tests()
        test_rows = _list_at(tests_payload, "result")
        course_ids = [
            cid for row in test_rows
            if (cid := _to_int(row.get("courseId"))) is not None and not row.get("isExamCourse")
        ]
        course_id = course_ids[0] if course_ids else None
        if course_id:
            scheduled_payload = client.scheduled_exams(course_id)
            scheduled_rows = _list_at(scheduled_payload, "examPaper")
            by_id = {str(row.get("id")): row for row in scheduled_rows}
            combined_tests = []
            for row in test_rows:
                merged = dict(row)
                scheduled = by_id.get(str(row.get("id")))
                if scheduled:
                    merged.update({k: v for k, v in scheduled.items() if v not in (None, "")})
                combined_tests.append(merged)
            ntsc_coaching.replace_tests(combined_tests, db_path=db_path)
            coaching_syllabus.replace_syllabi(combined_tests, db_path=db_path)
            datasets.append("tests")
            datasets.append("syllabus")

            result_list = _list_at(client.course_results(course_id), "result")
            analyses: list[dict[str, Any]] = []
            for result_row in result_list:
                if result_row.get("isGroup"):
                    continue
                result_row_id = _to_int(result_row.get("id"))
                if result_row_id is None:
                    continue
                appeared = _list_at(
                    client.appeared_results(result_row_id), "result"
                )
                for attempt in appeared:
                    exam_id = _to_int(attempt.get("examId"))
                    if exam_id is None:
                        continue
                    analysis = _data(client.result_analysis(exam_id)) or {}
                    summary = dict(analysis.get("result") or {})
                    summary.update({
                        "id": str(exam_id),
                        "testPaperId": analysis.get("testPaperId"),
                        "testName": analysis.get("testName"),
                        "attemptDate": analysis.get("attemptDate"),
                        "rank": analysis.get("rank"),
                        "batchRank": analysis.get("batchRank"),
                        "percentile": analysis.get("percentile"),
                    })
                    analyses.append(summary)
            ntsc_coaching.replace_results(analyses, db_path=db_path)
            datasets.append("results")

        ntsc_coaching.update_profile_context(
            batch=batch.get("title"), campus=batch.get("campusName"),
            course_name=batch.get("courseName") or batch.get("course"),
            academic_year=login_data.get("academicYearTitle"), db_path=db_path,
        )

        local_now = now or session_context.local_now()
        start = (local_now.date() - dt.timedelta(days=7)).isoformat()
        end = (local_now.date() + dt.timedelta(days=45)).isoformat()
        class_payload = client.classes(start, end)
        class_rows = _list_at(class_payload, "data", "timeTable")
        if not class_rows:
            data = _data(class_payload)
            class_rows = ((data or {}).get("data") or {}).get("timeTable") or []
        ntsc_coaching.replace_classes(class_rows, db_path=db_path)
        datasets.append("classes")

        # Invalidate cached day-type resolutions so they re-resolve against the
        # freshly-synced coaching cache. Without this a day resolved as
        # non_coaching during a stale/empty-cache window stays wrong forever.
        try:
            with ntsc_coaching._connect(db_path) as conn:
                conn.execute("DELETE FROM execution_day_types")
                conn.commit()
        except Exception:
            logger.warning("day-type cache invalidation failed", exc_info=True)

        _record_run(started, "success", datasets, None, db_path=db_path)
        return {"status": "success", "datasets": datasets, "course_id": course_id,
                "classes": len(class_rows)}
    except Exception as exc:
        logger.exception("NTSC coaching sync failed")
        _record_run(started, "failed", datasets, str(exc)[:500], db_path=db_path)
        return {"status": "failed", "datasets": datasets, "error": str(exc)}
    finally:
        _lock.release()
