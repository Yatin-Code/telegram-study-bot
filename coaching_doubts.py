"""Phase 10 — deterministic next-doubt coaching and attempt lifecycle.

Two responsibilities, both fully offline (no LLM, no network):

Selection
---------
``ranked_doubts`` / ``select_next_doubt`` rank every open doubt by a fixed
priority ladder, returning evidence, a human reason and a confidence label:

  1. today's/upcoming class subject          (``coaching_classes``)
  2. nearest-test syllabus topic             (``coaching_syllabus``)
  3. repeated failed attempts                (``op_doubt_attempts``)
  4. marks lost where available              (``op_exam_questions``)
  5. teacher readiness                       (``doubts.teacher_ready``)
  6. then age                                (older first, then subject/concept)

Each doubt lands in the first bucket it satisfies (strict priority), and within
a bucket the remaining signals act as tie-breakers.  ``select_teacher_ready_doubts``
returns the doubts that can be asked of a teacher now, aligned to the nearest
teacher window when a subject matches.

Interaction state
-----------------
``begin_doubt`` → ``record_attempt`` → ``request_hint`` / ``mark_retry`` →
``resolve`` / ``schedule_reattempt`` is persisted in the local SQLite table
``coaching_doubt_interactions``.  The module NEVER invents a hint or a solution:
a hint is stored only when the user/teacher supplies one, a doubt is resolved
only with real resolution evidence, and ``record_attempt`` returns a *write
plan* describing the durable ``study_domain.record_doubt_attempt`` call a later
bot integration should perform (it never writes to Notion/op_* itself).
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

import coaching_syllabus
import ntsc_coaching
import session_context
import study_domain
from config import notion_schema

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "sqlite_mirror.db"

INTERACTION_TABLE = "coaching_doubt_interactions"

# ---------------------------------------------------------------------------
# Interaction lifecycle states
# ---------------------------------------------------------------------------

STATE_SHOWN = "shown"
STATE_ATTEMPTING = "attempting"
STATE_AWAITING_HINT = "awaiting_hint"
STATE_HINT_GIVEN = "hint_given"
STATE_RETRY = "retry"
STATE_RETEST = "retest"
STATE_RESOLVED = "resolved"

ACTIVE_STATES = (
    STATE_SHOWN,
    STATE_ATTEMPTING,
    STATE_AWAITING_HINT,
    STATE_HINT_GIVEN,
    STATE_RETRY,
    STATE_RETEST,
)
STATE_LABELS = {
    STATE_SHOWN: "shown",
    STATE_ATTEMPTING: "attempting",
    STATE_AWAITING_HINT: "awaiting hint",
    STATE_HINT_GIVEN: "hint given",
    STATE_RETRY: "retrying",
    STATE_RETEST: "retest scheduled",
    STATE_RESOLVED: "resolved",
}

ATTEMPT_OUTCOME_OPTIONS = tuple(notion_schema.ATTEMPT_OUTCOME_OPTIONS)
FAILED_OUTCOMES = ("Unsolved", "Hint Used")

# ---------------------------------------------------------------------------
# Selection priority
# ---------------------------------------------------------------------------

BUCKET_CLASS_SUBJECT = 1
BUCKET_SYLLABUS_TOPIC = 2
BUCKET_REPEATED_FAILURE = 3
BUCKET_MARKS_LOST = 4
BUCKET_TEACHER_READY = 5
BUCKET_AGE_ONLY = 6

BUCKET_LABELS = {
    BUCKET_CLASS_SUBJECT: "matches a today/upcoming class subject",
    BUCKET_SYLLABUS_TOPIC: "matches the nearest-test syllabus topic",
    BUCKET_REPEATED_FAILURE: "repeated failed attempts",
    BUCKET_MARKS_LOST: "marks lost on record",
    BUCKET_TEACHER_READY: "teacher-ready",
    BUCKET_AGE_ONLY: "age only (no stronger signal)",
}

UPCOMING_CLASS_DAYS = 3
REPEATED_FAILURE_THRESHOLD = 2

# Within-bucket tie-break weights (only used once the bucket fixes the priority).
W_CLASS_TODAY = 60.0
W_CLASS_UPCOMING = 45.0
W_SYLLABUS_TOPIC = 40.0
W_SYLLABUS_SUBJECT = 10.0
W_FAILURE_PER = 12.0
W_MARKS_PER_POINT = 1.0
MARKS_CAP = 30.0
W_TEACHER_READY = 25.0

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# storage / init
# ---------------------------------------------------------------------------

def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {INTERACTION_TABLE} (
            id TEXT PRIMARY KEY,
            chat_id TEXT NOT NULL,
            doubt_id TEXT NOT NULL,
            doubt_concept TEXT,
            subject TEXT,
            state TEXT NOT NULL DEFAULT '{STATE_SHOWN}',
            shown_at TEXT NOT NULL,
            last_attempt_at TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_attempt_text TEXT,
            last_attempt_outcome TEXT,
            hint_supplied TEXT,
            hint_source TEXT,
            resolution TEXT,
            resolution_source TEXT,
            retest_at TEXT,
            created_time TEXT NOT NULL,
            last_edited_time TEXT NOT NULL
        )
    """)
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{INTERACTION_TABLE}_chat_state "
        f"ON {INTERACTION_TABLE}(chat_id, state)"
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{INTERACTION_TABLE}_doubt "
        f"ON {INTERACTION_TABLE}(doubt_id)"
    )
    conn.commit()


def _connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    init_db(conn)
    return conn


def _now_iso() -> str:
    return session_context.local_now().isoformat()


# ---------------------------------------------------------------------------
# text / relation normalization helpers
# ---------------------------------------------------------------------------

def _as_text(value: Any) -> str:
    """Coerce a stored cell to text, unwrapping JSON-wrapped rollup/relation."""
    if value is None:
        return ""
    if isinstance(value, str):
        text = value.strip()
        if text.startswith(("[", '"')):
            try:
                decoded = json.loads(text)
            except ValueError:
                return text
            if isinstance(decoded, str):
                return decoded.strip()
            if isinstance(decoded, list):
                return ", ".join(str(item) for item in decoded if item)
            if isinstance(decoded, dict):
                return _as_text(decoded.get("name") or decoded.get("id"))
            return str(decoded).strip()
        return text
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value if item)
    return str(value).strip()


def _normalize_text(value: Any) -> str:
    text = _as_text(value).lower()
    text = re.sub(r"\s+", " ", text)
    return text.strip(" .,;:-\u2013\u2014()[]{}")


def _canonical_subject(value: Any) -> str | None:
    """Canonical subject label, resolving portal/Notion aliases."""
    text = _normalize_text(value)
    if not text:
        return None
    mapping = getattr(coaching_syllabus, "SUBJECT_CANONICAL", None)
    if isinstance(mapping, dict):
        if text in mapping:
            return mapping[text]
        for alias, label in mapping.items():
            if text == alias or text.startswith(alias + " "):
                return label
    return text.title() if text else None


def _subject_tokens(text: Any) -> list[str]:
    return [part.strip() for part in _as_text(text).split(",") if part.strip()]


def _relation_ids(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    if isinstance(value, dict):
        return [str(value["id"])] if value.get("id") else []
    text = str(value).strip()
    if not text:
        return []
    if text.startswith(("[", '"')):
        try:
            decoded = json.loads(text)
        except ValueError:
            return [text]
        if isinstance(decoded, list):
            return [str(item) for item in decoded if str(item)]
        if isinstance(decoded, dict):
            return [str(decoded["id"])] if decoded.get("id") else []
        return [str(decoded)] if decoded else []
    return [text]


def _parse_date(value: Any) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        try:
            return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
        except ValueError:
            return None


def _num(value: Any) -> float:
    try:
        number = float(value or 0)
        return number if number > 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _valid_date(value: Any) -> str:
    text = str(value or "").strip()
    if not _ISO_DATE_RE.match(text):
        raise ValueError(f"date must be YYYY-MM-DD, got {value!r}")
    return text


# ---------------------------------------------------------------------------
# selection data gathering
# ---------------------------------------------------------------------------

def _nearest_test(today: str, *, db_path: str | Path) -> dict[str, Any] | None:
    try:
        tests = coaching_syllabus.upcoming_syllabus(today=today, limit=10, db_path=db_path)
    except Exception:
        return None
    future = [
        test for test in tests
        if str(test.get("test_date") or "")[:10] >= today
    ]
    if not future:
        return None
    future.sort(key=lambda test: (str(test.get("test_date") or ""), str(test.get("source_id") or "")))
    top = future[0]
    return {
        "source_id": top.get("source_id"),
        "title": _as_text(top.get("title")) or "Coaching test",
        "test_date": str(top.get("test_date") or "")[:10],
        "syllabus_records": top.get("syllabus_records") or [],
    }


def _attempts_by_doubt(*, db_path: str | Path) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    try:
        rows = study_domain._rows("doubt_attempts", "archived=0 AND valid=1", db_path=db_path)
    except Exception:
        return {}
    for row in rows:
        for doubt_id in _relation_ids(row.get("doubt")):
            result[doubt_id].append(row)
    return dict(result)


def _exam_questions(*, db_path: str | Path) -> list[dict[str, Any]]:
    try:
        return study_domain._rows("exam_questions", "archived=0", db_path=db_path)
    except Exception:
        return []


def _subject_hits(doubt_subject: str | None, doubt_concept: str, subjects_text: Any) -> int:
    """How many of a class's subject tokens relate to this doubt."""
    hits = 0
    for token in _subject_tokens(subjects_text):
        tok = _normalize_text(token)
        if not tok:
            continue
        canon = _canonical_subject(token)
        if doubt_subject and (canon == doubt_subject or doubt_subject in tok or tok in doubt_subject):
            hits += 1
        elif doubt_concept and (tok in doubt_concept or doubt_concept in tok):
            hits += 1
    return hits


def _record_keys(record: dict[str, Any]) -> list[str]:
    keys = []
    for field in ("topic", "chapter", "normalized_text"):
        key = _normalize_text(record.get(field))
        if key:
            keys.append(key)
    return keys


def _flags_for(
    doubt: dict[str, Any], *,
    classes_today: list[dict[str, Any]], classes_upcoming: list[dict[str, Any]],
    nearest_test: dict[str, Any] | None,
    attempts_by_doubt: dict[str, list[dict[str, Any]]],
    exam_questions: list[dict[str, Any]], today: dt.date,
) -> dict[str, Any]:
    subject = _canonical_subject(doubt.get("subject"))
    concept = _normalize_text(doubt.get("core_concept"))

    class_today = _subject_hits(subject, concept, " / ".join(
        str(row.get("subjects") or "") for row in classes_today
    )) > 0
    class_upcoming = (not class_today) and _subject_hits(subject, concept, " / ".join(
        str(row.get("subjects") or "") for row in classes_upcoming
    )) > 0

    syllabus_topic, syllabus_subject, _matched_record = False, False, None
    if nearest_test is not None:
        for record in nearest_test.get("syllabus_records") or []:
            rec_subject = _canonical_subject(record.get("subject"))
            if subject and rec_subject and rec_subject == subject:
                syllabus_subject = True
            keys = _record_keys(record)
            if concept and any(concept in key or key in concept for key in keys):
                syllabus_topic = True
                break

    attempt_rows = attempts_by_doubt.get(doubt["notion_page_id"], [])
    failed_attempts = sum(
        1 for row in attempt_rows
        if str(row.get("outcome") or "") in FAILED_OUTCOMES
    )

    marks_lost = 0.0
    for question in exam_questions:
        question_subject = _canonical_subject(question.get("subject"))
        if subject and question_subject and question_subject != subject:
            continue
        chapter = _normalize_text(question.get("chapter"))
        if chapter and concept and not (concept in chapter or chapter in concept):
            continue
        marks_lost += _num(question.get("marks_lost"))

    teacher_ready = bool(doubt.get("teacher_ready")) or (
        str(doubt.get("readiness") or "") in ("ready", "expedited")
    )
    age_days = 0
    created = _parse_date(doubt.get("created_time")) or _parse_date(doubt.get("last_edited_time"))
    if created is not None:
        age_days = max(0, (today - created).days)

    return {
        "class_today": bool(class_today),
        "class_upcoming": bool(class_upcoming),
        "class_subject": subject,
        "syllabus_topic": bool(syllabus_topic),
        "syllabus_subject": bool(syllabus_subject),
        "nearest_test": nearest_test,
        "valid_attempts": int(doubt.get("valid_attempts") or 0),
        "failed_attempts": failed_attempts,
        "marks_lost": round(marks_lost, 2),
        "teacher_ready": teacher_ready,
        "age_days": age_days,
    }


def _bucket_for(flags: dict[str, Any]) -> int:
    if flags["class_today"] or flags["class_upcoming"]:
        return BUCKET_CLASS_SUBJECT
    if flags["syllabus_topic"]:
        return BUCKET_SYLLABUS_TOPIC
    if flags["failed_attempts"] >= REPEATED_FAILURE_THRESHOLD:
        return BUCKET_REPEATED_FAILURE
    if flags["marks_lost"] > 0:
        return BUCKET_MARKS_LOST
    if flags["teacher_ready"]:
        return BUCKET_TEACHER_READY
    return BUCKET_AGE_ONLY


def _within_score(flags: dict[str, Any], bucket: int) -> float:
    total = 0.0
    if bucket != BUCKET_CLASS_SUBJECT:
        total += (W_CLASS_TODAY if flags["class_today"] else 0.0)
        total += (W_CLASS_UPCOMING if flags["class_upcoming"] else 0.0)
    if bucket != BUCKET_SYLLABUS_TOPIC:
        total += (W_SYLLABUS_TOPIC if flags["syllabus_topic"] else 0.0)
        total += (W_SYLLABUS_SUBJECT if flags["syllabus_subject"] else 0.0)
    if bucket != BUCKET_REPEATED_FAILURE:
        total += min(flags["failed_attempts"], 3) * W_FAILURE_PER
    if bucket != BUCKET_MARKS_LOST:
        total += min(flags["marks_lost"], MARKS_CAP) * W_MARKS_PER_POINT
    if bucket != BUCKET_TEACHER_READY:
        total += W_TEACHER_READY if flags["teacher_ready"] else 0.0
    return round(total, 2)


def _reason(flags: dict[str, Any], bucket: int) -> str:
    parts: list[str] = []
    if flags["class_today"]:
        parts.append("matches today's class subject")
    elif flags["class_upcoming"]:
        parts.append("matches an upcoming class subject")
    nearest = flags["nearest_test"]
    if flags["syllabus_topic"] and nearest:
        parts.append(f"topic in nearest test '{nearest['title']}' on {nearest['test_date']}")
    elif flags["syllabus_subject"] and nearest:
        parts.append(f"subject in nearest test '{nearest['title']}' on {nearest['test_date']}")
    if flags["failed_attempts"] >= REPEATED_FAILURE_THRESHOLD:
        parts.append(f"{flags['failed_attempts']} repeated failed attempts")
    elif flags["failed_attempts"]:
        parts.append(f"{flags['failed_attempts']} failed attempt(s)")
    if flags["marks_lost"] > 0:
        parts.append(f"{flags['marks_lost']:g} marks lost")
    if flags["teacher_ready"]:
        parts.append("teacher-ready")
    if flags["age_days"]:
        parts.append(f"{flags['age_days']} day(s) old")
    if not parts:
        parts.append("no stronger signals — newest open doubt")
    return "; ".join(parts)


def _confidence(bucket: int) -> str:
    if bucket <= BUCKET_REPEATED_FAILURE:
        return "high"
    if bucket in (BUCKET_MARKS_LOST, BUCKET_TEACHER_READY):
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# selection entry points
# ---------------------------------------------------------------------------

def ranked_doubts(
    *, now: dt.datetime | None = None, db_path: str | Path = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    """All open doubts ordered by the deterministic priority ladder."""
    today = now.date() if now is not None else dt.date.fromisoformat(session_context.local_today_iso())
    today_text = today.isoformat()
    candidates = study_domain.doubt_queue(db_path=db_path)
    if not candidates:
        return []

    classes_today = ntsc_coaching.classes_for_date(today_text, db_path=db_path)
    limit_date = (today + dt.timedelta(days=UPCOMING_CLASS_DAYS)).isoformat()
    classes_upcoming = [
        row for row in ntsc_coaching.next_classes(today=today_text, limit=50, db_path=db_path)
        if today_text < str(row.get("class_date") or "")[:10] <= limit_date
    ]
    nearest_test = _nearest_test(today_text, db_path=db_path)
    attempts_by_doubt = _attempts_by_doubt(db_path=db_path)
    exam_questions = _exam_questions(db_path=db_path)

    ranked: list[dict[str, Any]] = []
    for doubt in candidates:
        flags = _flags_for(
            doubt,
            classes_today=classes_today, classes_upcoming=classes_upcoming,
            nearest_test=nearest_test,
            attempts_by_doubt=attempts_by_doubt,
            exam_questions=exam_questions, today=today,
        )
        bucket = _bucket_for(flags)
        within = _within_score(flags, bucket)
        ranked.append({
            "doubt": doubt,
            "doubt_id": doubt["notion_page_id"],
            "concept": _as_text(doubt.get("core_concept")),
            "subject": _as_text(doubt.get("subject")),
            "bucket": bucket,
            "bucket_label": BUCKET_LABELS[bucket],
            "score": bucket * 10000 + int(within),
            "within_score": within,
            "reason": _reason(flags, bucket),
            "confidence": _confidence(bucket),
            "evidence": flags,
            "llm_involved": False,
            "generated_with": "deterministic",
        })

    ranked.sort(key=lambda item: (
        item["bucket"],
        -item["within_score"],
        -item["evidence"]["age_days"],
        _normalize_text(item["subject"]),
        _normalize_text(item["concept"]),
    ))
    return ranked


def select_next_doubt(
    *, now: dt.datetime | None = None, db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any] | None:
    """The single highest-priority open doubt, or None when nothing is open."""
    ranked = ranked_doubts(now=now, db_path=db_path)
    return ranked[0] if ranked else None


def select_teacher_ready_doubts(
    *, now: dt.datetime | None = None, days: int = 7,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Doubts that can be asked of a teacher now, aligned to a teacher window."""
    now = now or session_context.local_now()
    doubts = study_domain.doubt_queue(db_path=db_path)
    ready = [doubt for doubt in doubts if doubt.get("readiness") in ("ready", "expedited")]
    try:
        windows = study_domain.upcoming_teacher_windows(now=now, days=days, db_path=db_path)
    except Exception:
        windows = []
    window = windows[0] if windows else None
    window_subject = _canonical_subject(window.get("subject")) if window else None

    matched: list[dict[str, Any]] = []
    for doubt in ready:
        subject = _canonical_subject(doubt.get("subject"))
        window_match = bool(
            window_subject and subject and window_subject == subject
        )
        matched.append({
            **doubt,
            "doubt_id": doubt["notion_page_id"],
            "concept": _as_text(doubt.get("core_concept")),
            "reason": f"teacher-ready with {doubt.get('valid_attempts')} valid attempt(s)",
            "confidence": "high",
            "window_match": window_match,
            "evidence": {
                "valid_attempts": doubt.get("valid_attempts"),
                "readiness": doubt.get("readiness"),
                "serious_attempt": doubt.get("serious_attempt"),
                "window_subject": window.get("subject") if window else None,
                "window_starts_at": window["starts_at"].isoformat() if window else None,
            },
        })
    matched.sort(key=lambda item: (
        not item["window_match"],
        -int(item.get("valid_attempts") or 0),
        _normalize_text(item.get("subject")),
        _normalize_text(item.get("core_concept")),
    ))
    return {
        "window": window,
        "doubts": matched,
        "count": len(matched),
        "llm_involved": False,
        "generated_with": "deterministic",
    }


# ---------------------------------------------------------------------------
# prompt / message helpers (known facts only, never invented content)
# ---------------------------------------------------------------------------

def no_doubts_message() -> str:
    return "No open doubts to work on right now. Log one with /doubt."


def show_prompt(doubt: dict[str, Any], session: dict[str, Any] | None = None) -> str:
    concept = _as_text(doubt.get("core_concept")) or "Untitled doubt"
    subject = _as_text(doubt.get("subject")) or "unspecified subject"
    attempts = int(doubt.get("valid_attempts") or 0)
    lines = [f"🧠 Doubt: {concept}", f"Subject: {subject}"]
    if attempts:
        lines.append(f"{attempts} valid attempt(s) so far.")
    lines += [
        "",
        "Attempt it now. Write what you try and exactly where you get stuck.",
        "/done when it resolves · /hint to record a hint · /retry to try again",
    ]
    return "\n".join(lines)


def attempt_ack(session: dict[str, Any], doubt: dict[str, Any] | None = None) -> str:
    lines = [f"Attempt {session['attempt_count']} recorded."]
    outcome = session.get("last_attempt_outcome")
    if outcome:
        lines.append(f"Outcome marked: {outcome}.")
    lines += [
        "",
        "Did it resolve? Reply /done with the corrected method, or /hint /retry to continue.",
    ]
    return "\n".join(lines)


def request_hint_prompt() -> str:
    return (
        "No hint is stored for this doubt — I don't invent hints.\n"
        "Write the hint yourself (or paste what the teacher said) and I'll record it:\n"
        "/hint <the hint>\n"
        "Otherwise, describe what you tried so I can log the attempt."
    )


def hint_prompt(hint: str, source: str = "user") -> str:
    label = "Teacher supplied" if source == "teacher" else "You supplied"
    return (
        f"{label} hint:\n{hint}\n\n"
        "Retry the doubt with this hint. /done when it resolves, "
        "/hint to record another, or /retry to attempt again."
    )


def retry_prompt(session: dict[str, Any], doubt: dict[str, Any] | None = None) -> str:
    return (
        "Take another shot at this doubt. Write what you try and where you're stuck "
        "so it can be logged."
    )


def resolve_prompt(
    session: dict[str, Any], doubt: dict[str, Any] | None = None, *,
    retest_at: str | None = None,
) -> str:
    concept = session.get("doubt_concept") or (
        _as_text(doubt.get("core_concept")) if doubt else ""
    )
    lines = [f"Resolved: {concept}" if concept else "Doubt resolved."]
    if session.get("resolution"):
        source = session.get("resolution_source") or "user"
        lines.append(f"Resolution recorded ({source}): {session['resolution']}")
    if retest_at:
        lines.append(f"Reattempt scheduled for {retest_at}.")
    return "\n".join(lines)


def retest_prompt(session: dict[str, Any], doubt: dict[str, Any] | None = None) -> str:
    concept = session.get("doubt_concept") or (
        _as_text(doubt.get("core_concept")) if doubt else "this doubt"
    )
    date = session.get("retest_at") or "scheduled"
    return (
        f"Reattempt due: {concept} (scheduled {date}).\n"
        "Try it again now, or reply /done to keep it resolved."
    )


def status_line(session: dict[str, Any], doubt: dict[str, Any] | None = None) -> str:
    concept = session.get("doubt_concept") or (
        _as_text(doubt.get("core_concept")) if doubt else "doubt"
    )
    state = STATE_LABELS.get(session.get("state") or "", session.get("state") or "?")
    return f"• {concept} — {state} — {session.get('attempt_count') or 0} attempt(s)"


# ---------------------------------------------------------------------------
# interaction lifecycle
# ---------------------------------------------------------------------------

def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _fetch_session(session_id: str, *, db_path: str | Path) -> dict[str, Any] | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            f"SELECT * FROM {INTERACTION_TABLE} WHERE id=?", (session_id,)
        ).fetchone()
    return _row_to_dict(row)


def _active_session(
    chat_id: int | str, doubt_id: str, *, db_path: str | Path,
) -> dict[str, Any] | None:
    placeholders = ", ".join("?" for _ in ACTIVE_STATES)
    with _connect(db_path) as conn:
        row = conn.execute(
            f"SELECT * FROM {INTERACTION_TABLE} "
            f"WHERE chat_id=? AND doubt_id=? AND state IN ({placeholders}) "
            "ORDER BY created_time DESC LIMIT 1",
            (str(chat_id), str(doubt_id), *ACTIVE_STATES),
        ).fetchone()
    return _row_to_dict(row)


def _require_session(
    chat_id: int | str, session_id: str, *, db_path: str | Path,
    active: bool = False,
) -> dict[str, Any]:
    session = _fetch_session(session_id, db_path=db_path)
    if session is None or session.get("chat_id") != str(chat_id):
        raise ValueError(f"no interaction session {session_id!r} for this chat")
    if active and session.get("state") not in ACTIVE_STATES:
        raise ValueError("this doubt interaction is already resolved; start a new one")
    return session


def _update_session(session_id: str, fields: dict[str, Any], *, db_path: str | Path) -> dict[str, Any]:
    fields = dict(fields)
    fields["last_edited_time"] = _now_iso()
    assignments = ", ".join(f"{name}=?" for name in fields)
    with _connect(db_path) as conn:
        conn.execute(
            f"UPDATE {INTERACTION_TABLE} SET {assignments} WHERE id=?",
            (*fields.values(), session_id),
        )
        conn.commit()
    return _fetch_session(session_id, db_path=db_path) or {}


def begin_doubt(
    chat_id: int | str, doubt_id: str, *, db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Open (or resume) an attempt interaction for one open doubt."""
    doubt = study_domain._row(
        "doubts", "notion_page_id=? AND archived=0", (str(doubt_id),), db_path=db_path,
    )
    if not doubt:
        raise ValueError(f"no open doubt with id {doubt_id!r}")
    status = str(doubt.get("status") or "").strip().lower()
    if status in ("resolved", "dismissed"):
        raise ValueError("this doubt is already resolved/dismissed")

    existing = _active_session(str(chat_id), str(doubt_id), db_path=db_path)
    if existing is not None:
        return {**existing, "message": show_prompt(doubt, existing), "already_active": True}

    now = _now_iso()
    session_id = uuid.uuid4().hex
    row = {
        "id": session_id,
        "chat_id": str(chat_id),
        "doubt_id": str(doubt_id),
        "doubt_concept": _as_text(doubt.get("core_concept")),
        "subject": _as_text(doubt.get("subject")),
        "state": STATE_SHOWN,
        "shown_at": now,
        "attempt_count": 0,
        "created_time": now,
        "last_edited_time": now,
    }
    with _connect(db_path) as conn:
        conn.execute(
            f"INSERT INTO {INTERACTION_TABLE} ({', '.join(row)}) "
            f"VALUES ({', '.join('?' for _ in row)})",
            tuple(row.values()),
        )
        conn.commit()
    session = _fetch_session(session_id, db_path=db_path) or row
    return {**session, "message": show_prompt(doubt, session), "already_active": False}


def _attempt_write_plan(
    session: dict[str, Any], *, attempt_text: str, outcome: str,
    duration_min: Any, approach: str | None, stuck_point: str | None,
) -> dict[str, Any]:
    return {
        "kind": "doubt_attempt",
        "delegate": "study_domain.record_doubt_attempt",
        "persisted": "local_interaction_only",
        "llm_involved": False,
        "params": {
            "doubt": session.get("doubt_concept") or "",
            "duration_min": duration_min,
            "approach": (approach or attempt_text).strip(),
            "stuck_point": (stuck_point or "").strip(),
            "outcome": outcome,
        },
        "requires": (
            "Call study_domain.record_doubt_attempt(params) for the durable "
            "op_doubt_attempts row. approach and stuck_point must both be "
            "non-empty and duration_min>=1 or it raises DomainError; fill them "
            "from attempt_text via a future user-facing parser. Never invent them."
        ),
    }


def record_attempt(
    chat_id: int | str, session_id: str,
    *, attempt_text: str, outcome: str = "Unsolved",
    duration_min: Any = None, approach: str | None = None, stuck_point: str | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Record a user's own attempt and return the durable write plan."""
    session = _require_session(chat_id, session_id, db_path=db_path, active=True)
    if outcome not in ATTEMPT_OUTCOME_OPTIONS:
        raise ValueError(f"invalid attempt outcome: {outcome!r}")
    text = str(attempt_text or "").strip()
    if not text and not str(approach or "").strip() and not str(stuck_point or "").strip():
        raise ValueError("record what you tried before logging an attempt")

    now = _now_iso()
    new_count = int(session.get("attempt_count") or 0) + 1
    updated = _update_session(session_id, {
        "state": STATE_ATTEMPTING,
        "attempt_count": new_count,
        "last_attempt_at": now,
        "last_attempt_text": text,
        "last_attempt_outcome": outcome,
    }, db_path=db_path)
    doubt = study_domain._row(
        "doubts", "notion_page_id=? AND archived=0",
        (session["doubt_id"],), db_path=db_path,
    )
    write_plan = _attempt_write_plan(
        session, attempt_text=text, outcome=outcome,
        duration_min=duration_min, approach=approach, stuck_point=stuck_point,
    )
    return {
        **updated,
        "attempt_count": new_count,
        "state": STATE_ATTEMPTING,
        "last_attempt_at": now,
        "last_attempt_text": text,
        "last_attempt_outcome": outcome,
        "message": attempt_ack(updated, doubt),
        "write_plan": write_plan,
    }


def request_hint(
    chat_id: int | str, session_id: str,
    *, hint: str | None = None, source: str = "user",
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Store a user/teacher-supplied hint, or prompt for one without inventing."""
    session = _require_session(chat_id, session_id, db_path=db_path, active=True)
    supplied = str(hint or "").strip()
    if supplied:
        updated = _update_session(session_id, {
            "hint_supplied": supplied,
            "hint_source": source,
            "state": STATE_HINT_GIVEN,
        }, db_path=db_path)
        message = hint_prompt(supplied, source)
    else:
        state = STATE_AWAITING_HINT if session.get("state") != STATE_HINT_GIVEN else STATE_HINT_GIVEN
        updated = _update_session(session_id, {"state": state}, db_path=db_path)
        message = request_hint_prompt()
    return {**updated, "message": message}


def mark_retry(
    chat_id: int | str, session_id: str, *, db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    session = _require_session(chat_id, session_id, db_path=db_path, active=True)
    updated = _update_session(session_id, {"state": STATE_RETRY}, db_path=db_path)
    return {**updated, "message": retry_prompt(updated)}


def _resolve_write_plan(
    session: dict[str, Any], resolution: str, teacher: bool,
) -> dict[str, Any]:
    return {
        "kind": "doubt_resolve",
        "delegate": "study_domain.resolve_doubt",
        "persisted": "local_interaction_only",
        "llm_involved": False,
        "params": {
            "query": session.get("doubt_concept") or "",
            "resolution": resolution,
            "teacher_asked": teacher,
        },
        "requires": (
            "Call study_domain.resolve_doubt(params) for the durable Notion "
            "doubt state. teacher=True requires >=2 valid attempts or it raises "
            "DomainError. Resolution must be the corrected method, never invented."
        ),
    }


def resolve(
    chat_id: int | str, session_id: str,
    *, resolution: str, teacher: bool = False, retest_at: str | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Resolve with real evidence, optionally scheduling a retest."""
    session = _require_session(chat_id, session_id, db_path=db_path)
    resolution = str(resolution or "").strip()
    if not resolution:
        raise ValueError("resolution evidence is required — a doubt resolves only with the corrected method")
    if retest_at is not None:
        retest_at = _valid_date(retest_at)
    source = "teacher" if teacher else "user"
    state = STATE_RETEST if retest_at is not None else STATE_RESOLVED
    fields: dict[str, Any] = {
        "resolution": resolution,
        "resolution_source": source,
        "state": state,
        "retest_at": retest_at,
    }
    updated = _update_session(session_id, fields, db_path=db_path)
    write_plan = _resolve_write_plan(session, resolution, teacher)
    return {
        **updated,
        "state": state,
        "retest_at": retest_at,
        "message": resolve_prompt(updated, retest_at=retest_at),
        "write_plan": write_plan,
    }


def schedule_reattempt(
    chat_id: int | str, session_id: str, date: str,
    *, db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Schedule a retest/reattempt for an existing interaction."""
    session = _require_session(chat_id, session_id, db_path=db_path)
    day = _valid_date(date)
    updated = _update_session(session_id, {"retest_at": day, "state": STATE_RETEST}, db_path=db_path)
    return {
        **updated,
        "state": STATE_RETEST,
        "retest_at": day,
        "message": retest_prompt(updated),
    }


def get_session(
    chat_id: int | str, session_id: str, *, db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any] | None:
    return _require_session(chat_id, session_id, db_path=db_path)


def active_interactions(
    chat_id: int | str | None = None, *, db_path: str | Path = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    """Active (not resolved) interactions, newest first."""
    placeholders = ", ".join("?" for _ in ACTIVE_STATES)
    params: list[Any] = [*ACTIVE_STATES]
    where = f"state IN ({placeholders})"
    if chat_id is not None:
        where += " AND chat_id=?"
        params.append(str(chat_id))
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM {INTERACTION_TABLE} WHERE {where} "
            "ORDER BY last_edited_time DESC",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def due_reattempts(
    chat_id: int | str | None = None, *, today: str | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    """Retest-scheduled interactions whose retest date has arrived."""
    today = (today or session_context.local_today_iso())[:10]
    where = "state=? AND substr(COALESCE(retest_at,''),1,10)<=?"
    params: list[Any] = [STATE_RETEST, today]
    if chat_id is not None:
        where += " AND chat_id=?"
        params.append(str(chat_id))
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM {INTERACTION_TABLE} WHERE {where} "
            "ORDER BY COALESCE(retest_at,''), created_time",
            params,
        ).fetchall()
    sessions = [dict(row) for row in rows]
    return [{**session, "message": retest_prompt(session)} for session in sessions]


def start_next_doubt(
    chat_id: int | str, *, now: dt.datetime | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Select the highest-priority open doubt and open an interaction for it."""
    selection = select_next_doubt(now=now, db_path=db_path)
    if selection is None:
        return {"selection": None, "session": None, "message": no_doubts_message()}
    session = begin_doubt(chat_id, selection["doubt_id"], db_path=db_path)
    return {"selection": selection, "session": session, "message": session["message"]}
