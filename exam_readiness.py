"""Deterministic pre-exam evidence audits.

This module never creates study-plan rows.  It only reads the exam, doubt,
ledger and revision evidence already recorded by the user, plus a small local
table of exam-specific doubt classifications.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable

import session_context
import study_domain


DEFAULT_DB_PATH = Path(__file__).resolve().parent / "sqlite_mirror.db"
ITEMS_TABLE = "exam_readiness_items"
PENDING_TABLE = "pending_readiness_resolution"
RESOLUTION_TTL_MINUTES = 15
MILESTONES = {7: "t7", 3: "t3", 1: "t1", 0: "day"}

_STOPWORDS = {
    "and", "the", "for", "from", "with", "mock", "test",
    "exam", "syllabus", "chapter", "chapters", "unit", "units",
    "jee", "main", "advanced", "part", "full", "class",
}
_SUBJECT_TOKENS = {"physics", "chem", "maths", "math"}


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {ITEMS_TABLE} (
            token TEXT PRIMARY KEY,
            exam_id TEXT NOT NULL,
            doubt_id TEXT NOT NULL,
            decision TEXT,
            updated_at TEXT NOT NULL,
            UNIQUE(exam_id, doubt_id)
        )
    """)
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {PENDING_TABLE} (
            chat_id TEXT PRIMARY KEY,
            item_token TEXT NOT NULL,
            expires_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def _exam_day(exam: dict[str, Any]) -> dt.date | None:
    raw = str(exam.get("exam_date") or "").strip()
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return dt.date.fromisoformat(raw[:10])
        except ValueError:
            return None


def days_until(exam: dict[str, Any], *, now: dt.datetime | None = None) -> int | None:
    day = _exam_day(exam)
    if day is None:
        return None
    now = now or session_context.local_now()
    return (day - now.date()).days


def immediate_phase(exam: dict[str, Any], *, now: dt.datetime | None = None) -> str | None:
    """Phase for a just-confirmed exam, or None outside the seven-day window."""
    days = days_until(exam, now=now)
    return _phase_for_days(days)


def _phase_for_days(days: int | None) -> str | None:
    """Map each day to its current review window, absorbing bot downtime."""
    if days is None or not 0 <= days <= 7:
        return None
    if days >= 4:
        return "t7"
    if days >= 2:
        return "t3"
    if days == 1:
        return "t1"
    return "day"


def event_key(exam: dict[str, Any], phase: str) -> str:
    day = _exam_day(exam)
    return (
        f"exam-readiness:{phase}:{exam.get('notion_page_id') or exam.get('id')}:"
        f"{day.isoformat() if day else 'unknown-date'}"
    )


def scheduled_reviews(
    *, now: dt.datetime | None = None, db_path: str | Path = DEFAULT_DB_PATH,
) -> list[tuple[dict[str, Any], str]]:
    """Return the current T-7/T-3/T-1/day window for upcoming exams.

    The windows absorb a period in which the bot was offline; the durable
    event key prevents duplicate delivery after it catches up.
    """
    now = now or session_context.local_now()
    result: list[tuple[dict[str, Any], str]] = []
    for exam in study_domain._rows(
        "exams", "archived=0 AND status='Planned'", db_path=db_path
    ):
        phase = _phase_for_days(days_until(exam, now=now))
        if phase:
            result.append((exam, phase))
    result.sort(key=lambda item: (_exam_day(item[0]) or dt.date.max, str(item[0].get("title") or "")))
    return result


def select_exam(
    query: str = "", *, now: dt.datetime | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    now = now or session_context.local_now()
    rows = study_domain._rows(
        "exams", "archived=0 AND status='Planned'", db_path=db_path
    )
    if query.strip():
        needle = query.strip().lower()
        matches = [row for row in rows if needle in str(row.get("title") or "").lower()]
        exact = [row for row in matches if str(row.get("title") or "").strip().lower() == needle]
        if len(exact) == 1:
            return exact[0]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise study_domain.DomainError(f"ambiguous exam query {query!r}")
        raise study_domain.DomainError(f"no planned exam matches {query!r}")
    future = [row for row in rows if (days_until(row, now=now) or 0) >= 0 and _exam_day(row)]
    if not future:
        raise study_domain.DomainError("no upcoming planned exam is recorded")
    return min(future, key=lambda row: (_exam_day(row) or dt.date.max, str(row.get("title") or "")))


def exam_by_id(exam_id: str, *, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    rows = study_domain._rows(
        "exams", "notion_page_id=? AND archived=0", (str(exam_id),), db_path=db_path
    )
    if not rows:
        raise study_domain.DomainError("the exam for this readiness action no longer exists")
    return rows[0]


def _tokens(value: Any) -> set[str]:
    text = str(value or "").lower()
    aliases = {
        "chemistry": "chem", "chemical": "chem",
        "mathematics": "maths", "math": "maths",
        "physics": "physics", "phy": "physics",
    }
    return {
        aliases.get(token, token) for token in re.findall(r"[a-z0-9]+", text)
        if len(token) >= 3 and token not in _STOPWORDS
    }


def _row_blob(row: dict[str, Any], fields: Iterable[str]) -> str:
    return " ".join(str(row.get(field) or "") for field in fields)


def _matches_syllabus(row: dict[str, Any], syllabus: str, fields: Iterable[str]) -> bool:
    wanted = _tokens(syllabus)
    if not wanted:
        return True
    blob = _row_blob(row, fields)
    found = _tokens(blob)
    wanted_subjects = wanted & _SUBJECT_TOKENS
    found_subjects = found & _SUBJECT_TOKENS
    if wanted_subjects and found_subjects and wanted_subjects.isdisjoint(found_subjects):
        return False
    topics = wanted - _SUBJECT_TOKENS
    if topics and topics & (found - _SUBJECT_TOKENS):
        return True
    if not topics and wanted & found:
        return True
    # Metadata-free doubts are retained for manual classification instead of
    # being falsely declared out of scope.
    return not str(blob).strip() and "core_concept" in fields


def _doubt_scope(row: dict[str, Any], syllabus: str) -> tuple[bool, bool]:
    """Return (include, uncertain) without silently hiding weak metadata."""
    wanted = _tokens(syllabus)
    if not wanted:
        return True, False
    wanted_subjects = wanted & _SUBJECT_TOKENS
    found_subjects = _tokens(row.get("subject")) & _SUBJECT_TOKENS
    if wanted_subjects and found_subjects and wanted_subjects.isdisjoint(found_subjects):
        return False, False
    topics = wanted - _SUBJECT_TOKENS
    if not topics:
        return True, not bool(found_subjects)
    blob = _row_blob(row, (
        "core_concept", "chapter", "exercise_type",
        "concept_deficit_failure_reason",
    ))
    if topics & (_tokens(blob) - _SUBJECT_TOKENS):
        return True, False
    # Chapter metadata is authoritative enough to exclude a mismatch. If it
    # is absent, keep the doubt visible and explicitly ask the user to decide.
    if str(row.get("chapter") or "").strip():
        return False, False
    return True, True


def _item_token(exam_id: str, doubt_id: str) -> str:
    return hashlib.sha256(f"{exam_id}|{doubt_id}".encode("utf-8")).hexdigest()[:16]


def _decisions(
    exam_id: str, doubts: list[dict[str, Any]], *, db_path: str | Path,
) -> dict[str, dict[str, Any]]:
    now = _utc_now().isoformat()
    with _connect(db_path) as conn:
        for doubt in doubts:
            doubt_id = str(doubt.get("notion_page_id") or "")
            if not doubt_id:
                continue
            token = _item_token(exam_id, doubt_id)
            conn.execute(
                f"INSERT OR IGNORE INTO {ITEMS_TABLE} "
                "(token, exam_id, doubt_id, decision, updated_at) VALUES (?, ?, ?, NULL, ?)",
                (token, exam_id, doubt_id, now),
            )
        conn.commit()
        rows = conn.execute(
            f"SELECT token, doubt_id, decision, updated_at FROM {ITEMS_TABLE} WHERE exam_id=?",
            (exam_id,),
        ).fetchall()
    return {str(row["doubt_id"]): dict(row) for row in rows}


def collect(
    exam: dict[str, Any], *, now: dt.datetime | None = None,
    db_path: str | Path = DEFAULT_DB_PATH, phase: str | None = None,
) -> dict[str, Any]:
    """Collect a deterministic evidence snapshot without creating a plan."""
    now = now or session_context.local_now()
    exam_id = str(exam.get("notion_page_id") or exam.get("id") or "")
    if not exam_id:
        raise study_domain.DomainError("exam has no durable identifier")
    syllabus = str(exam.get("syllabus") or "").strip()
    all_doubts = study_domain.doubt_queue(db_path=db_path)
    decisions = _decisions(exam_id, all_doubts, db_path=db_path)
    relevant: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for row in all_doubts:
        decision = decisions.get(str(row.get("notion_page_id") or ""), {})
        item = {**row, "exam_decision": decision.get("decision"), "readiness_token": decision.get("token")}
        was_classified = bool(decision.get("decision"))
        matches, scope_uncertain = _doubt_scope(row, syllabus)
        item["scope_uncertain"] = scope_uncertain
        if decision.get("decision") == "solved":
            try:
                changed = dt.datetime.fromisoformat(
                    str(row.get("last_edited_time") or "").replace("Z", "+00:00")
                )
                classified = dt.datetime.fromisoformat(str(decision.get("updated_at") or ""))
                reopened = changed > classified
            except (TypeError, ValueError):
                reopened = False
            if not reopened:
                continue
            decision = set_decision(
                str(decision["token"]), "open", db_path=db_path
            )
            item["exam_decision"] = "open"
        if decision.get("decision") == "not_in_exam":
            excluded.append(item)
        elif matches or was_classified:
            relevant.append(item)

    start = (now.date() - dt.timedelta(days=6)).isoformat()
    key_points = study_domain._rows(
        "ledger",
        "archived=0 AND key_points_notes IS NOT NULL "
        "AND TRIM(key_points_notes)<>'' AND substr(COALESCE(date,''),1,10) BETWEEN ? AND ?",
        (start, now.date().isoformat()), db_path=db_path,
    )
    if syllabus:
        key_points = [row for row in key_points if _matches_syllabus(
            row, syllabus,
            ("task", "subject", "chapter", "chapter_text", "key_points_notes", "page_content"),
        )]

    exam_day = _exam_day(exam) or now.date()
    revision = study_domain._rows(
        "revision",
        "archived=0 AND LOWER(COALESCE(status,''))<>'completed'",
        db_path=db_path,
    )
    if syllabus:
        revision = [row for row in revision if _matches_syllabus(
            row, syllabus, ("chapter_module", "subject", "exercises")
        )]
    for row in revision:
        raw = str(row.get("next_execution_date") or "").strip()
        try:
            scheduled = dt.date.fromisoformat(raw[:10]) if raw else None
        except ValueError:
            scheduled = None
        if scheduled is None:
            risk = "unscheduled"
        elif scheduled <= now.date():
            risk = "overdue"
        elif scheduled <= exam_day:
            risk = "due_before_exam"
        else:
            risk = "scheduled_after_exam"
        row["exam_schedule_risk"] = risk

    relevant.sort(key=lambda row: (
        0 if not row.get("exam_decision") else 1,
        {"ready": 0, "attempting": 1, "new": 2}.get(str(row.get("readiness")), 9),
        str(row.get("subject") or ""), str(row.get("core_concept") or ""),
    ))
    key_points.sort(key=lambda row: str(row.get("date") or ""), reverse=True)
    risk_order = {
        "overdue": 0, "unscheduled": 1,
        "due_before_exam": 2, "scheduled_after_exam": 3,
    }
    revision.sort(key=lambda row: (
        risk_order.get(str(row.get("exam_schedule_risk")), 9),
        str(row.get("next_execution_date") or "9999"),
        str(row.get("chapter_module") or ""),
    ))
    zero_attempt = sum(1 for row in relevant if int(row.get("valid_attempts") or 0) == 0)
    teacher_ready = sum(1 for row in relevant if row.get("readiness") == "ready")
    return {
        "exam": exam, "exam_id": exam_id, "exam_day": exam_day,
        "days_until": days_until(exam, now=now), "phase": phase or "manual",
        "syllabus": syllabus, "syllabus_known": bool(syllabus),
        "doubts": relevant, "excluded_doubts": excluded,
        "key_points": key_points, "revision": revision,
        "zero_attempt_count": zero_attempt, "teacher_ready_count": teacher_ready,
        "scope_uncertain_count": sum(
            1 for row in relevant if row.get("scope_uncertain")
        ),
        "collected_at": now.isoformat(), "created_plan_rows": 0,
    }


def set_decision(
    token: str, decision: str, *, db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    if decision not in ("open", "not_in_exam"):
        raise ValueError("invalid readiness decision")
    with _connect(db_path) as conn:
        cur = conn.execute(
            f"UPDATE {ITEMS_TABLE} SET decision=?, updated_at=? WHERE token=?",
            (decision, _utc_now().isoformat(), token),
        )
        if cur.rowcount != 1:
            raise study_domain.DomainError("this readiness action expired")
        row = conn.execute(
            f"SELECT * FROM {ITEMS_TABLE} WHERE token=?", (token,)
        ).fetchone()
        conn.commit()
    return dict(row)


def item_for_token(token: str, *, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    with _connect(db_path) as conn:
        row = conn.execute(
            f"SELECT * FROM {ITEMS_TABLE} WHERE token=?", (token,)
        ).fetchone()
    if row is None:
        raise study_domain.DomainError("this readiness action expired")
    return dict(row)


def start_resolution(
    chat_id: int | str, token: str, *, ttl_minutes: int = RESOLUTION_TTL_MINUTES,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    item = item_for_token(token, db_path=db_path)
    open_row = study_domain._row(
        "doubts",
        "notion_page_id=? AND archived=0 AND "
        "LOWER(COALESCE(status,'')) NOT IN ('resolved','dismissed')",
        (str(item["doubt_id"]),), db_path=db_path,
    )
    if open_row is None:
        raise study_domain.DomainError("that doubt is no longer open")
    expires = _utc_now() + dt.timedelta(minutes=ttl_minutes)
    with _connect(db_path) as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO {PENDING_TABLE} (chat_id, item_token, expires_at) VALUES (?, ?, ?)",
            (str(chat_id), token, expires.isoformat()),
        )
        conn.commit()
    return item


def pending_resolution(
    chat_id: int | str, *, db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any] | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            f"SELECT p.item_token, p.expires_at, i.* FROM {PENDING_TABLE} p "
            f"JOIN {ITEMS_TABLE} i ON i.token=p.item_token WHERE p.chat_id=?",
            (str(chat_id),),
        ).fetchone()
        if row is None:
            return None
        if str(row["expires_at"]) <= _utc_now().isoformat():
            conn.execute(f"DELETE FROM {PENDING_TABLE} WHERE chat_id=?", (str(chat_id),))
            conn.commit()
            return None
    return dict(row)


def cancel_resolution(chat_id: int | str, *, db_path: str | Path = DEFAULT_DB_PATH) -> None:
    with _connect(db_path) as conn:
        conn.execute(f"DELETE FROM {PENDING_TABLE} WHERE chat_id=?", (str(chat_id),))
        conn.commit()


def complete_resolution(
    chat_id: int | str, evidence: str, *, db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    evidence = str(evidence or "").strip()
    vague = re.sub(r"[^a-z0-9 ]+", "", evidence.lower()).strip()
    if len(evidence) < 5 or vague in {
        "done", "solved", "solved now", "got it", "i got it",
        "understood", "i understand", "fixed", "clear now",
    }:
        raise study_domain.DomainError("write the corrected idea or method, not only 'done'")
    pending = pending_resolution(chat_id, db_path=db_path)
    if pending is None:
        raise study_domain.DomainError("the solved-doubt prompt expired; open /readiness again")
    result = study_domain.resolve_doubt_id(
        str(pending["doubt_id"]), evidence, db_path=db_path
    )
    with _connect(db_path) as conn:
        conn.execute(
            f"UPDATE {ITEMS_TABLE} SET decision='solved', updated_at=? WHERE token=?",
            (_utc_now().isoformat(), pending["item_token"]),
        )
        conn.execute(f"DELETE FROM {PENDING_TABLE} WHERE chat_id=?", (str(chat_id),))
        conn.commit()
    return {**result, "exam_id": pending["exam_id"]}
