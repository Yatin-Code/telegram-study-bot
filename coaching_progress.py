"""Phase 9 — deterministic, evidence-aware coaching progress (chapter/topic).

Tracks how much of every upcoming-syllabus chapter/topic the student has
actually completed, along with exercise / MLE / PYQ counts, confidence /
mastery state, when it was last verified, and who verified it.

Design rules:

* stdlib only, fully deterministic, fully offline.  All functions accept an
  explicit ``db_path`` (default the shared ``sqlite_mirror.db``) and never
  touch the portal, Notion, or an LLM.
* The progress row is keyed by a stable ``progress_key`` derived from the
  normalized subject/chapter/topic identity (same normalization the syllabus
  module uses), so re-syncs and case/whitespace drift can never fork rows.
* ``verification_source`` is an ordered ladder::

      unknown < self_reported < partially_evidenced < evidence_backed

  ``merge_progress``/``upsert_progress`` only ever move that ladder upward and
  never let a weaker source overwrite stronger evidence — a self-report can
  never clobber an evidence-backed count, but a later evidence-backed value
  corrects an earlier self-report.
* Every value is validated before storage (counts >= 0, done <= total,
  confidence in [0,100], controlled vocabularies) and nothing is invented: a
  topic with no progress row stays ``verification_source=unknown`` until the
  user actually supplies data.
* Question prompts are generated only for missing high-value data in the
  upcoming-syllabus window, and each prompt is recorded in a history table so
  the same question is not repeated within a cooldown window.

This module is deliberately *not* wired into ``agent_tools.py`` / ``bot.py`` /
``coaching_context.py``.  It exposes safe read helpers and write-ready
``prepare_progress_write`` / ``run_progress_write`` functions so a later phase
can bind them into the agent tool registry without changing this file.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import sqlite3
from pathlib import Path
from typing import Any

import coaching_syllabus
import session_context

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "sqlite_mirror.db"

PROGRESS_TABLE = "coaching_progress"
PROMPT_HISTORY_TABLE = "coaching_progress_prompts"

# Verification-source ladder (weak -> strong).  Higher index = stronger.
EVIDENCE_SOURCES: tuple[str, ...] = (
    "unknown",
    "self_reported",
    "partially_evidenced",
    "evidence_backed",
)
EVIDENCE_ORDER = {source: index for index, source in enumerate(EVIDENCE_SOURCES)}

# Controlled mastery vocabulary (weak -> strong).
MASTERY_STATES: tuple[str, ...] = (
    "unknown",
    "struggling",
    "learning",
    "practiced",
    "mastered",
)
MASTERY_ORDER = {state: index for index, state in enumerate(MASTERY_STATES)}

# Count kinds tracked per progress row.
COUNT_KINDS: tuple[str, ...] = ("exercise", "mle", "pyq")

PROMPT_COOLDOWN_DAYS = 7
MAX_PROMPTS_PER_SCAN = 5

_ISO_DATE_RE = dt.date.fromisoformat


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _today_iso() -> str:
    return session_context.local_today_iso()


def _canonical_subject(value: Any) -> str | None:
    return coaching_syllabus._canonical_subject(str(value or "")) or None


def _normalize_key(value: Any) -> str:
    return coaching_syllabus._normalize_key(value)


def _evidence_strength(source: Any) -> int:
    return EVIDENCE_ORDER.get(str(source or "").strip().lower(), 0)


def _mastery_strength(state: Any) -> int:
    return MASTERY_ORDER.get(str(state or "").strip().lower(), 0)


# ---------------------------------------------------------------------------
# SQLite schema
# ---------------------------------------------------------------------------

def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS {PROGRESS_TABLE} (
            progress_key TEXT PRIMARY KEY,
            subject TEXT NOT NULL,
            chapter TEXT,
            topic TEXT NOT NULL,
            exercise_done INTEGER NOT NULL DEFAULT 0,
            exercise_total INTEGER NOT NULL DEFAULT 0,
            mle_done INTEGER NOT NULL DEFAULT 0,
            mle_total INTEGER NOT NULL DEFAULT 0,
            pyq_done INTEGER NOT NULL DEFAULT 0,
            pyq_total INTEGER NOT NULL DEFAULT 0,
            confidence INTEGER,
            mastery TEXT NOT NULL DEFAULT 'unknown',
            last_verified TEXT,
            verification_source TEXT NOT NULL DEFAULT 'unknown',
            notes TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_coaching_progress_subject
            ON coaching_progress(subject);
        CREATE INDEX IF NOT EXISTS idx_coaching_progress_topic
            ON coaching_progress(topic);
        CREATE TABLE IF NOT EXISTS {PROMPT_HISTORY_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            progress_key TEXT NOT NULL,
            asked_at TEXT NOT NULL,
            question TEXT NOT NULL,
            cooldown_until TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'progress_check'
        );
        CREATE INDEX IF NOT EXISTS idx_coaching_progress_prompts_key
            ON coaching_progress_prompts(progress_key, cooldown_until);
    """)
    conn.commit()


def _connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    init_db(conn)
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


# ---------------------------------------------------------------------------
# Identity + validation (deterministic)
# ---------------------------------------------------------------------------

def progress_key_for(subject: Any, chapter: Any, topic: Any) -> str:
    """Stable identity for one subject/chapter/topic progress row."""
    identity = "||".join((
        _normalize_key(subject) or "",
        _normalize_key(chapter),
        _normalize_key(topic) or "",
    ))
    return hashlib.sha1(identity.encode("utf-8")).hexdigest()[:16]


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _to_count(value: Any, label: str, errors: list[str]) -> int | None:
    """Coerce a count value; appends an error and returns None on failure."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        errors.append(f"{label} must be an integer, not a boolean")
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        errors.append(f"{label} must be a non-negative integer, got {value!r}")
        return None
    if number < 0:
        errors.append(f"{label} must be non-negative, got {number}")
        return None
    return number


def _to_confidence(value: Any, errors: list[str]) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        errors.append(f"confidence must be an integer in [0,100], got {value!r}")
        return None
    if not 0 <= number <= 100:
        errors.append(f"confidence must be in [0,100], got {number}")
        return None
    return number


def _to_verified(value: Any, errors: list[str]) -> str | None:
    if value is None or value == "":
        return None
    text = str(value).strip()[:10]
    try:
        _ISO_DATE_RE(text)
    except ValueError:
        errors.append(f"last_verified must be an ISO date (YYYY-MM-DD), got {value!r}")
        return None
    return text


def validate_progress(record: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    """Validate an incoming progress record.

    Returns ``(errors, cleaned)``.  When ``errors`` is non-empty the record is
    rejected; otherwise ``cleaned`` contains only the known fields, with counts
    coerced to non-negative integers and vocabularies validated.  Fields the
    caller did not provide are kept as ``None`` so the merge can distinguish
    "no update" from an explicit zero.
    """
    if not isinstance(record, dict):
        return ["progress record must be a dict"], {}
    errors: list[str] = []
    cleaned: dict[str, Any] = {}

    subject = _clean_text(record.get("subject"))
    topic = _clean_text(record.get("topic"))
    chapter = _clean_text(record.get("chapter"))
    if not subject:
        errors.append("subject is required")
    if not topic:
        errors.append("topic is required")
    cleaned["subject"] = subject
    cleaned["topic"] = topic
    cleaned["chapter"] = chapter

    for kind in COUNT_KINDS:
        done = _to_count(record.get(f"{kind}_done"), f"{kind}_done", errors)
        total = _to_count(record.get(f"{kind}_total"), f"{kind}_total", errors)
        cleaned[f"{kind}_done"] = done
        cleaned[f"{kind}_total"] = total
        if done is not None and total is not None and done > total:
            errors.append(
                f"{kind}_done ({done}) cannot exceed {kind}_total ({total})"
            )

    cleaned["confidence"] = _to_confidence(record.get("confidence"), errors)

    mastery = str(record.get("mastery") or "").strip().lower() or None
    if mastery is not None and mastery not in MASTERY_ORDER:
        errors.append(
            f"mastery must be one of {MASTERY_STATES}, got {record.get('mastery')!r}"
        )
    cleaned["mastery"] = mastery

    source = str(record.get("verification_source") or "").strip().lower() or None
    if source is not None and source not in EVIDENCE_ORDER:
        errors.append(
            f"verification_source must be one of {EVIDENCE_SOURCES}, "
            f"got {record.get('verification_source')!r}"
        )
    cleaned["verification_source"] = source

    cleaned["last_verified"] = _to_verified(record.get("last_verified"), errors)
    cleaned["notes"] = _clean_text(record.get("notes"))
    return errors, cleaned


# ---------------------------------------------------------------------------
# Merge (never lets weaker evidence override stronger)
# ---------------------------------------------------------------------------

def _merge_counts(
    stored: dict[str, Any], incoming: dict[str, Any],
    incoming_strength: int, stored_strength: int,
) -> dict[str, int]:
    merged: dict[str, int] = {}
    for kind in COUNT_KINDS:
        stored_done = stored.get(f"{kind}_done") or 0
        stored_total = stored.get(f"{kind}_total") or 0
        incoming_done = incoming.get(f"{kind}_done")
        incoming_total = incoming.get(f"{kind}_total")
        if incoming_strength < stored_strength:
            done, total = stored_done, stored_total
        elif incoming_strength > stored_strength:
            done = stored_done if incoming_done is None else incoming_done
            total = stored_total if incoming_total is None else incoming_total
        else:
            # Same evidence tier: progress is monotonic (never decreases), and
            # totals grow to stay consistent with the highest done count.
            done = max(stored_done, incoming_done or 0)
            total = max(stored_total, incoming_total or 0)
        if done > total:
            total = done
        merged[f"{kind}_done"] = done
        merged[f"{kind}_total"] = total
    return merged


def merge_progress(
    stored: dict[str, Any], incoming: dict[str, Any],
    *, now: str | None = None,
) -> dict[str, Any]:
    """Merge an incoming (validated) record into stored progress.

    Pure, deterministic, and safe to unit-test without a database.  The merge
    invariant: an incoming record whose ``verification_source`` is weaker than
    the stored one is applied to *nothing* (counts, confidence, mastery, notes
    all keep the stored value).  A stronger source replaces weaker values; an
    equal source keeps progress monotonic (max) and never erases data.
    """
    now = now or _now()
    stored = dict(stored or {})
    incoming_strength = _evidence_strength(incoming.get("verification_source") or "unknown")
    stored_strength = _evidence_strength(stored.get("verification_source") or "unknown")

    merged: dict[str, Any] = {
        "progress_key": progress_key_for(
            incoming.get("subject"), incoming.get("chapter"), incoming.get("topic")
        ),
        "subject": incoming.get("subject"),
        "chapter": incoming.get("chapter"),
        "topic": incoming.get("topic"),
    }

    merged.update(_merge_counts(stored, incoming, incoming_strength, stored_strength))

    # confidence / mastery: weaker cannot override stronger; equal keeps max.
    stored_conf = stored.get("confidence")
    incoming_conf = incoming.get("confidence")
    if incoming_strength < stored_strength:
        merged["confidence"] = stored_conf
    elif incoming_conf is None:
        merged["confidence"] = stored_conf
    elif incoming_strength > stored_strength:
        merged["confidence"] = incoming_conf
    else:
        merged["confidence"] = max(stored_conf or 0, incoming_conf)

    stored_mastery = stored.get("mastery") or "unknown"
    incoming_mastery = incoming.get("mastery") or "unknown"
    if incoming_strength < stored_strength:
        merged["mastery"] = stored_mastery
    elif incoming_strength > stored_strength:
        merged["mastery"] = incoming_mastery
    else:
        merged["mastery"] = (
            stored_mastery
            if _mastery_strength(stored_mastery) >= _mastery_strength(incoming_mastery)
            else incoming_mastery
        )

    # verification ladder can only rise; equal tier keeps the (same) source.
    if incoming_strength >= stored_strength:
        merged["verification_source"] = incoming.get("verification_source") or stored.get(
            "verification_source") or "unknown"
    else:
        merged["verification_source"] = stored.get("verification_source") or "unknown"

    # last_verified: stronger tier wins; equal tier takes the most recent date.
    incoming_verified = incoming.get("last_verified")
    stored_verified = stored.get("last_verified")
    if incoming_strength < stored_strength:
        merged["last_verified"] = stored_verified
    elif incoming_strength > stored_strength:
        merged["last_verified"] = incoming_verified or stored_verified
    elif incoming_verified and stored_verified:
        merged["last_verified"] = max(incoming_verified, stored_verified)
    else:
        merged["last_verified"] = incoming_verified or stored_verified

    # notes: weaker never overwrites; stronger replaces; equal appends a line.
    stored_notes = stored.get("notes") or ""
    incoming_notes = incoming.get("notes") or ""
    if incoming_strength < stored_strength:
        merged["notes"] = stored_notes
    elif incoming_strength > stored_strength:
        merged["notes"] = incoming_notes or stored_notes
    elif incoming_notes and incoming_notes != stored_notes:
        merged["notes"] = f"{stored_notes}\n{incoming_notes}".strip() if stored_notes else incoming_notes
    else:
        merged["notes"] = stored_notes

    merged["updated_at"] = now
    return merged


# ---------------------------------------------------------------------------
# Read APIs (safe for later tool integration)
# ---------------------------------------------------------------------------

def get_progress(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    progress_key: str | None = None,
    subject: str | None = None,
    chapter: str | None = None,
    topic: str | None = None,
) -> list[dict[str, Any]]:
    """Read progress rows, optionally filtered by identity fields.

    Read-only and never raises on missing tables (returns [] when the mirror
    has no progress table yet).  All filters are exact on the stored identity.
    """
    filters: list[str] = []
    params: list[Any] = []
    if progress_key is not None:
        filters.append("progress_key=?")
        params.append(progress_key)
    if subject is not None:
        filters.append("subject=?")
        params.append(subject)
    if chapter is not None:
        filters.append("chapter=?")
        params.append(chapter)
    if topic is not None:
        filters.append("topic=?")
        params.append(topic)
    where = " AND ".join(filters) if filters else "1=1"
    with _connect(db_path) as conn:
        if not _table_exists(conn, PROGRESS_TABLE):
            return []
        rows = conn.execute(
            f"SELECT * FROM {PROGRESS_TABLE} WHERE {where} ORDER BY subject, topic",
            tuple(params),
        ).fetchall()
    return [dict(row) for row in rows]


def get_progress_by_key(
    progress_key: str, *, db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any] | None:
    rows = get_progress(progress_key=progress_key, db_path=db_path)
    return rows[0] if rows else None


def all_progress(*, db_path: str | Path = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    return get_progress(db_path=db_path)


# ---------------------------------------------------------------------------
# Upsert APIs (deterministic, evidence-aware)
# ---------------------------------------------------------------------------

def upsert_progress(
    record: dict[str, Any],
    *, db_path: str | Path = DEFAULT_DB_PATH, now: str | None = None,
) -> dict[str, Any]:
    """Validate + merge + store one progress record.  Never raises.

    Returns ``{"ok": True, "record": <stored row>, "changed": bool,
    "progress_key": ...}`` or ``{"ok": False, "errors": [...]}``.
    """
    errors, cleaned = validate_progress(record)
    if errors:
        return {"ok": False, "errors": errors}
    key = progress_key_for(cleaned["subject"], cleaned["chapter"], cleaned["topic"])
    with _connect(db_path) as conn:
        stored_row = conn.execute(
            f"SELECT * FROM {PROGRESS_TABLE} WHERE progress_key=?", (key,)
        ).fetchone()
        stored = dict(stored_row) if stored_row else {}
        merged = merge_progress(stored, cleaned, now=now)
        changed = _changed(stored, merged)
        if changed or not stored_row:
            conn.execute(
                f"""INSERT INTO {PROGRESS_TABLE}
                    (progress_key, subject, chapter, topic,
                     exercise_done, exercise_total, mle_done, mle_total,
                     pyq_done, pyq_total, confidence, mastery,
                     last_verified, verification_source, notes, updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(progress_key) DO UPDATE SET
                      subject=excluded.subject, chapter=excluded.chapter,
                      topic=excluded.topic,
                      exercise_done=excluded.exercise_done,
                      exercise_total=excluded.exercise_total,
                      mle_done=excluded.mle_done, mle_total=excluded.mle_total,
                      pyq_done=excluded.pyq_done, pyq_total=excluded.pyq_total,
                      confidence=excluded.confidence, mastery=excluded.mastery,
                      last_verified=excluded.last_verified,
                      verification_source=excluded.verification_source,
                      notes=excluded.notes, updated_at=excluded.updated_at""",
                (
                    key, merged["subject"], merged["chapter"], merged["topic"],
                    merged["exercise_done"], merged["exercise_total"],
                    merged["mle_done"], merged["mle_total"],
                    merged["pyq_done"], merged["pyq_total"],
                    merged["confidence"], merged["mastery"],
                    merged["last_verified"], merged["verification_source"],
                    merged["notes"], merged["updated_at"],
                ),
            )
            conn.commit()
        stored = dict(conn.execute(
            f"SELECT * FROM {PROGRESS_TABLE} WHERE progress_key=?", (key,)
        ).fetchone())
    return {
        "ok": True,
        "progress_key": key,
        "record": stored,
        "changed": changed or not stored_row,
    }


def _changed(stored: dict[str, Any], merged: dict[str, Any]) -> bool:
    if not stored:
        return True
    for field in (
        "subject", "chapter", "topic", "exercise_done", "exercise_total",
        "mle_done", "mle_total", "pyq_done", "pyq_total", "confidence",
        "mastery", "last_verified", "verification_source", "notes",
    ):
        if stored.get(field) != merged.get(field):
            return True
    return False


def bulk_upsert(
    records: list[dict[str, Any]],
    *, db_path: str | Path = DEFAULT_DB_PATH, now: str | None = None,
) -> dict[str, Any]:
    saved = 0
    rejected: list[dict[str, Any]] = []
    stored: list[dict[str, Any]] = []
    for record in records or []:
        result = upsert_progress(record, db_path=db_path, now=now)
        if result.get("ok"):
            saved += 1
            stored.append(result["record"])
        else:
            rejected.append({"record": record, "errors": result.get("errors", [])})
    return {"ok": saved > 0 or not records, "saved": saved, "rejected": rejected, "records": stored}


# ---------------------------------------------------------------------------
# Write-ready prep (mirrors agent_tools.prepare_write / run_prepared_write)
# ---------------------------------------------------------------------------

def _describe_kind(kind: str, row: dict[str, Any]) -> str:
    done, total = row.get(f"{kind}_done"), row.get(f"{kind}_total")
    return f"{kind}: {done}/{total}" if total is not None else f"{kind}: {done}"


def preview_progress(record: dict[str, Any]) -> str:
    """Human-readable preview line for a validated/merged progress record."""
    subject = record.get("subject") or "(no subject)"
    chapter = record.get("chapter")
    topic = record.get("topic")
    identity = f"{subject} · {chapter} · {topic}" if chapter else f"{subject} · {topic}"
    lines = [
        f"📈 Coaching progress: {identity}",
        "• " + " · ".join(_describe_kind(kind, record) for kind in COUNT_KINDS),
        f"• confidence {record.get('confidence')} · mastery {record.get('mastery')}",
        f"• verification: {record.get('verification_source')} "
        f"(last verified {record.get('last_verified') or 'never'})",
    ]
    if record.get("notes"):
        lines.append(f"• notes: {record['notes']}")
    return "\n".join(lines)


def prepare_progress_write(
    record: dict[str, Any], *, db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Validate + preview + merge a progress write WITHOUT storing it.

    Returns ``{"ok": True, "preview": str, "run": {"record": cleaned},
    "merged": <would-be stored row>}`` or ``{"ok": False, "errors": [...]}``.
    Read-only, deterministic, and safe for an agent to call before confirming.
    """
    errors, cleaned = validate_progress(record)
    if errors:
        return {"ok": False, "errors": errors}
    stored = get_progress_by_key(
        progress_key_for(cleaned["subject"], cleaned["chapter"], cleaned["topic"]),
        db_path=db_path,
    )
    merged = merge_progress(stored or {}, cleaned)
    preview = preview_progress(merged)
    if stored and (
        _evidence_strength(merged["verification_source"])
        > _evidence_strength(stored.get("verification_source") or "unknown")
    ):
        preview += (
            f"\n⚠ Raising verification from "
            f"{stored.get('verification_source')} to {merged['verification_source']}"
        )
    elif stored and (
        _evidence_strength(cleaned.get("verification_source") or "unknown")
        < _evidence_strength(stored.get("verification_source") or "unknown")
    ):
        preview += (
            f"\nℹ Stored evidence ({stored.get('verification_source')}) is stronger "
            f"than this {cleaned.get('verification_source')} report — counts are kept."
        )
    return {"ok": True, "preview": preview, "run": {"record": cleaned}, "merged": merged}


def run_progress_write(
    run: dict[str, Any], *, db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Execute a previously prepared (user-confirmed) progress write.

    Never raises: returns the upsert result (``ok`` / ``errors``).
    """
    record = (run or {}).get("record")
    if not isinstance(record, dict):
        return {"ok": False, "errors": ["prepared run is missing a record"]}
    return upsert_progress(record, db_path=db_path)


# ---------------------------------------------------------------------------
# Completion / coverage summaries joined to the upcoming syllabus
# ---------------------------------------------------------------------------

def _match_progress(
    rows: list[dict[str, Any]], record: dict[str, Any],
) -> dict[str, Any] | None:
    """First progress row matching a syllabus record (subject+chapter+topic)."""
    subject = record.get("subject")
    keys = [
        key for key in (
            _normalize_key(record.get("topic")),
            _normalize_key(record.get("chapter")),
        ) if key
    ]
    if not keys:
        return None
    for row in rows:
        if subject and (
            _canonical_subject(row.get("subject")) != _canonical_subject(subject)
        ):
            continue
        row_topic = _normalize_key(row.get("topic"))
        row_chapter = _normalize_key(row.get("chapter"))
        if row_topic in keys or row_chapter in keys:
            return row
        for key in keys:
            if row_topic and (key in row_topic or row_topic in key):
                return row
    return None


def _row_complete(row: dict[str, Any]) -> bool:
    if row.get("mastery") == "mastered":
        return True
    for kind in COUNT_KINDS:
        total = row.get(f"{kind}_total") or 0
        done = row.get(f"{kind}_done") or 0
        if total > 0 and done < total:
            return False
        if total == 0 and done == 0:
            return False
    return True


def _row_evidenced(row: dict[str, Any]) -> bool:
    return _evidence_strength(row.get("verification_source")) >= _evidence_strength(
        "partially_evidenced"
    )


def completion_summary(
    *, today: str | None = None, limit: int = 5,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    """Upcoming tests joined to their per-topic progress rows.

    Each syllabus record gets a ``progress`` (matched row or None) and a
    ``status`` of ``no_progress`` / ``progress`` / ``evidenced`` / ``complete``.
    Per-test ``counts`` and summed ``totals`` make the shape of remaining work
    obvious without any inference.
    """
    tests = coaching_syllabus.upcoming_syllabus(today=today, limit=limit, db_path=db_path)
    rows = all_progress(db_path=db_path)
    result = []
    for test in tests:
        records = test.get("syllabus_records") or []
        totals = {f"{kind}_done": 0 for kind in COUNT_KINDS}
        totals.update({f"{kind}_total": 0 for kind in COUNT_KINDS})
        counts = {
            "topic_count": len(records), "with_progress": 0, "evidenced": 0,
            "complete": 0, "missing": 0,
        }
        for record in records:
            progress = _match_progress(rows, record)
            record["progress"] = progress
            if progress is None:
                record["status"] = "no_progress"
                counts["missing"] += 1
            else:
                counts["with_progress"] += 1
                if _row_evidenced(progress):
                    counts["evidenced"] += 1
                if _row_complete(progress):
                    counts["complete"] += 1
                record["status"] = "complete" if _row_complete(progress) else (
                    "evidenced" if _row_evidenced(progress) else "progress"
                )
                for kind in COUNT_KINDS:
                    totals[f"{kind}_done"] += progress.get(f"{kind}_done") or 0
                    totals[f"{kind}_total"] += progress.get(f"{kind}_total") or 0
        result.append({
            "source_id": test.get("source_id"),
            "title": test.get("title"),
            "test_date": test.get("test_date"),
            "counts": counts,
            "totals": totals,
            "records": records,
        })
    return result


def coverage_summary(
    *, today: str | None = None, limit: int = 5,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Subject-level evidence-aware coverage across the upcoming window."""
    tests = completion_summary(today=today, limit=limit, db_path=db_path)
    by_subject: dict[str, dict[str, Any]] = {}
    for test in tests:
        for record in test.get("records") or []:
            subject = record.get("subject") or "(uncategorised)"
            bucket = by_subject.setdefault(subject, {
                "subject": subject, "topic_count": 0, "with_progress": 0,
                "evidenced": 0, "complete": 0, "missing": 0,
                "confidence_values": [], "tests": set(),
            })
            bucket["topic_count"] += 1
            bucket["tests"].add(test.get("source_id"))
            progress = record.get("progress")
            if progress is None:
                bucket["missing"] += 1
            else:
                bucket["with_progress"] += 1
                if _row_evidenced(progress):
                    bucket["evidenced"] += 1
                if _row_complete(progress):
                    bucket["complete"] += 1
                if progress.get("confidence") is not None:
                    bucket["confidence_values"].append(progress["confidence"])
    subjects = []
    for bucket in by_subject.values():
        values = bucket.pop("confidence_values")
        bucket["avg_confidence"] = (
            round(sum(values) / len(values), 1) if values else None
        )
        bucket["tests"] = sorted(bucket["tests"])
        bucket["covered_fraction"] = (
            round(bucket["with_progress"] / bucket["topic_count"], 3)
            if bucket["topic_count"] else None
        )
        subjects.append(bucket)
    subjects.sort(key=lambda item: (
        item["covered_fraction"] is None,
        -(item["covered_fraction"] or 0),
        item["subject"],
    ))
    return {
        "generated_at": _now(),
        "tests": [
            {
                "source_id": test.get("source_id"),
                "title": test.get("title"),
                "test_date": test.get("test_date"),
                **test.get("counts", {}),
            }
            for test in tests
        ],
        "subjects": subjects,
    }


# ---------------------------------------------------------------------------
# Question prompts (only missing high-value data; cooldown-aware)
# ---------------------------------------------------------------------------

def _question_for_record(
    record: dict[str, Any], progress: dict[str, Any] | None,
) -> tuple[int, str, str] | None:
    """Return (priority, reason, question) for one syllabus record or None."""
    topic = record.get("topic")
    chapter = record.get("chapter")
    subject = record.get("subject")
    if not topic and not chapter:
        return None
    label = topic or chapter
    if progress is None:
        return (
            1,
            "no progress recorded",
            f"Did you cover “{label}”? Reply with how many exercises, "
            f"MLE questions and PYQs you completed (or 'done').",
        )
    missing = [
        kind for kind in COUNT_KINDS
        if (progress.get(f"{kind}_total") or 0) > 0
        and (progress.get(f"{kind}_done") or 0) < (progress.get(f"{kind}_total") or 0)
    ]
    source = progress.get("verification_source") or "unknown"
    if source == "unknown":
        return (
            2,
            "progress unverified",
            f"Your progress on “{label}” isn't verified yet. Confirm the "
            f"exercise / MLE / PYQ counts so it counts as evidence.",
        )
    if source == "self_reported":
        return (
            3,
            "only self-reported",
            f"“{label}” is only self-reported. Practice and tell me how many "
            f"questions you solved to back it with evidence.",
        )
    if missing:
        kinds = ", ".join(missing)
        return (
            4,
            f"missing {kinds}",
            f"On “{label}”, you still owe {kinds}. Reply with how many you did.",
        )
    return None


def record_prompt_asked(
    progress_key: str, question: str,
    *, db_path: str | Path = DEFAULT_DB_PATH,
    now: str | None = None, cooldown_days: int = PROMPT_COOLDOWN_DAYS,
) -> None:
    """Record that a prompt was asked so it won't repeat within the cooldown."""
    now = now or _now()
    try:
        asked_dt = dt.datetime.fromisoformat(now)
    except ValueError:
        asked_dt = dt.datetime.now(dt.timezone.utc)
    cooldown_until = (asked_dt + dt.timedelta(days=cooldown_days)).date().isoformat()
    with _connect(db_path) as conn:
        conn.execute(
            f"""INSERT INTO {PROMPT_HISTORY_TABLE}
                (progress_key, asked_at, question, cooldown_until, source)
                VALUES (?,?,?,?,?)""",
            (progress_key, now, question, cooldown_until, "progress_check"),
        )
        conn.commit()


def recently_asked(
    progress_key: str, *, db_path: str | Path = DEFAULT_DB_PATH,
    today: str | None = None, cooldown_days: int = PROMPT_COOLDOWN_DAYS,
) -> bool:
    """True when this key has an unexpired prompt (asked within the cooldown)."""
    today = today or _today_iso()
    with _connect(db_path) as conn:
        if not _table_exists(conn, PROMPT_HISTORY_TABLE):
            return False
        row = conn.execute(
            f"""SELECT 1 FROM {PROMPT_HISTORY_TABLE}
                WHERE progress_key=? AND cooldown_until>=? LIMIT 1""",
            (progress_key, today),
        ).fetchone()
    return row is not None


def _last_asked(
    progress_key: str, *, db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any] | None:
    with _connect(db_path) as conn:
        if not _table_exists(conn, PROMPT_HISTORY_TABLE):
            return None
        row = conn.execute(
            f"""SELECT asked_at, question, cooldown_until FROM {PROMPT_HISTORY_TABLE}
                WHERE progress_key=? ORDER BY id DESC LIMIT 1""",
            (progress_key,),
        ).fetchone()
    return dict(row) if row else None


def missing_data_questions(
    *, today: str | None = None, limit: int = 5,
    db_path: str | Path = DEFAULT_DB_PATH,
    cooldown_days: int = PROMPT_COOLDOWN_DAYS,
    max_prompts: int = MAX_PROMPTS_PER_SCAN,
) -> list[dict[str, Any]]:
    """Eligible question prompts for missing high-value progress data.

    Only records in the upcoming-syllabus window are considered, only genuinely
    missing data produces a question, and any key already asked inside the
    cooldown window is skipped.  Output is deterministic: sorted by priority
    then subject/topic, capped at ``max_prompts``.
    """
    today = today or _today_iso()
    tests = coaching_syllabus.upcoming_syllabus(today=today, limit=limit, db_path=db_path)
    rows = all_progress(db_path=db_path)
    candidates: list[dict[str, Any]] = []
    for test in tests:
        for record in test.get("syllabus_records") or []:
            progress = _match_progress(rows, record)
            question = _question_for_record(record, progress)
            if question is None:
                continue
            priority, reason, text = question
            key = (
                progress["progress_key"] if progress is not None else progress_key_for(
                    record.get("subject"), record.get("chapter"), record.get("topic")
                )
            )
            if recently_asked(
                key, db_path=db_path, today=today, cooldown_days=cooldown_days
            ):
                continue
            entry = {
                "progress_key": key,
                "subject": record.get("subject"),
                "chapter": record.get("chapter"),
                "topic": record.get("topic"),
                "priority": priority,
                "reason": reason,
                "question": text,
            }
            last = _last_asked(key, db_path=db_path)
            if last:
                entry["last_asked"] = last["asked_at"]
                entry["cooldown_until"] = last["cooldown_until"]
            candidates.append(entry)
    candidates.sort(key=lambda item: (
        item["priority"],
        item.get("subject") or "",
        item.get("topic") or "",
    ))
    return candidates[:max_prompts]
