"""Chapter auto-classification — propose/confirm mastery/revision/hard tags.

C7 in the portal-first onboarding plan. When a ``Current Syllabus`` work item
reaches ``Completed`` and the chapter was first tracked on/after the lifecycle
activation date (no retroactive tagging), the bot aggregates the chapter's
ledger metrics (accuracy + cognitive-yield) and asks the LLM to propose ONE
tag — mastery, revision, or hard — with a one-line reason. The proposal is
stored as a ``proposed`` row and only becomes durable on an explicit Confirm
(callback ``classify:confirm:<chapter_key>``); Dismiss leaves the chapter
untagged. The row is never written with a final tag without confirmation.

This module owns two local tables (registered in
``config.ownership.LOCAL_SQL_TABLES``):

- chapter_classifications — one row per proposed/confirmed/dismissed chapter
- chapter_lifecycle_meta   — singleton activation timestamp written (INSERT OR
                             IGNORE) on the first classification scan run;
                             chapters created before it are never classified

Everything is deterministic SQLite except the single LLM call whose failure
degrades to the fixed ``revision`` default — no exception ever propagates out
of ``propose_chapter_classification``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sqlite3
from pathlib import Path

import coaching_policy
import study_domain
import llm.router as llm_router
from llm.router import LLMRequest

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = PROJECT_ROOT / "sqlite_mirror.db"

CLASSIFICATIONS_TABLE = "chapter_classifications"
LIFECYCLE_TABLE = "chapter_lifecycle_meta"

# The two local tables registered in config.ownership.LOCAL_SQL_TABLES.
LOCAL_TABLES = (CLASSIFICATIONS_TABLE, LIFECYCLE_TABLE)

TAGS = ("mastery", "revision", "hard")
DEFAULT_TAG = "revision"

_SYSTEM_PROMPT = (
    "You are the study coach of a JEE aspirant. The chapter {chapter} ({subject}) "
    "is finished. Based ONLY on these metrics, propose ONE tag: mastery, revision, "
    "or hard. Reply with just the tag and one short reason."
)


def init_db(conn: sqlite3.Connection) -> None:
    """Create the two local classification tables if they don't exist."""
    conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS {CLASSIFICATIONS_TABLE} (
            chapter_key TEXT PRIMARY KEY,
            subject TEXT NOT NULL,
            chapter TEXT NOT NULL,
            tag TEXT NOT NULL CHECK(tag IN ('mastery','revision','hard')),
            accuracy_ratio REAL,
            cognitive_yield REAL,
            evidence_count INTEGER,
            reason TEXT,
            decided_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('proposed','confirmed','dismissed'))
        );
        CREATE TABLE IF NOT EXISTS {LIFECYCLE_TABLE} (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            activated_at TEXT NOT NULL
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


def _utc_iso(value: dt.datetime) -> str:
    """Notion-style UTC ISO string for a datetime (matches ledger created_time)."""
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+00:00")


def chapter_key_for(subject: str, chapter: str) -> str:
    """Stable 16-hex identity for one subject/chapter pair.

    Mirrors ``coaching_progress.progress_key_for``'s hashing style:
    ``sha1("<subject>||<chapter>")`` truncated to 16 hex chars.
    """
    identity = f"{subject}||{chapter}"
    return hashlib.sha1(identity.encode("utf-8")).hexdigest()[:16]


def _activated_at(db_path: str | Path = DEFAULT_DB_PATH) -> str | None:
    """The lifecycle activation timestamp, or None before the first scan run."""
    with _connect(db_path) as conn:
        row = conn.execute(
            f"SELECT activated_at FROM {LIFECYCLE_TABLE} WHERE singleton=1"
        ).fetchone()
    return str(row["activated_at"]) if row is not None else None


def ensure_activated(
    now: dt.datetime, db_path: str | Path = DEFAULT_DB_PATH,
) -> str:
    """INSERT OR IGNORE the activation timestamp (first scan run only).

    Returns the effective activation timestamp (the seeded one on every run
    after the first). Chapters tracked before it are never classified.
    """
    stamp = _utc_iso(now)
    with _connect(db_path) as conn:
        conn.execute(
            f"INSERT OR IGNORE INTO {LIFECYCLE_TABLE} (singleton, activated_at) "
            "VALUES (1, ?)",
            (stamp,),
        )
        conn.commit()
        row = conn.execute(
            f"SELECT activated_at FROM {LIFECYCLE_TABLE} WHERE singleton=1"
        ).fetchone()
    return str(row["activated_at"])


def classify_candidates(
    now: dt.datetime, db_path: str | Path = DEFAULT_DB_PATH,
) -> list[dict]:
    """Eligible completed chapters that need a classification proposal.

    A chapter is eligible iff ALL of:

    * a ``Current Syllabus`` work item reached status ``Completed``;
    * no ``chapter_classifications`` row exists for it yet;
    * its work item was created on/after the lifecycle activation date
      (``created_time >= activated_at`` — "tracked from start");
    * its ``chapter_metrics`` has >= 1 session and the first ledger row is
      on/after the work item's creation date.

    Each candidate is ``{subject, chapter, chapter_key, metrics}``. No LLM is
    involved and no row is written here; with no activation row the result is
    simply [] (the first scan run activates, so nothing qualifies that day).
    """
    activated = _activated_at(db_path)
    if activated is None:
        return []
    candidates: list[dict] = []
    for item in study_domain._rows(
        "work_items",
        "archived=0 AND kind='Current Syllabus' AND status='Completed'",
        db_path=db_path,
    ):
        subject = str(item.get("subject") or "").strip()
        chapter = str(item.get("chapter") or "").strip()
        if not subject or not chapter:
            continue
        created = str(item.get("created_time") or "")[:10]
        if not created or created < str(activated)[:10]:
            continue  # tracked before activation — no retroactive tagging
        key = chapter_key_for(subject, chapter)
        with _connect(db_path) as conn:
            already = conn.execute(
                f"SELECT 1 FROM {CLASSIFICATIONS_TABLE} WHERE chapter_key=?",
                (key,),
            ).fetchone()
        if already is not None:
            continue
        metrics = study_domain.chapter_metrics(
            subject=subject, chapter=chapter, db_path=db_path
        )
        if not metrics:
            continue
        row = metrics[0]
        if int(row.get("sessions") or 0) < 1:
            continue
        first_date = str(row.get("first_date") or "")
        if not first_date or first_date < created:
            continue
        candidates.append({
            "subject": subject,
            "chapter": chapter,
            "chapter_key": key,
            "metrics": row,
        })
    return candidates


def _classify_llm_complete(messages: list[dict[str, str]]) -> str:
    """Thin wrapper over the LLM router. Raises on failure (caller catches)."""
    response = llm_router.complete(LLMRequest(
        messages=messages,
        purpose="domain",
        max_output_tokens=128,
        temperature=0.6,
    ))
    return response.text


def _parse_tag(reply: str) -> tuple[str, str | None]:
    """Extract (tag, reason) from the LLM reply; unknown tag → 'revision'."""
    if not reply or not str(reply).strip():
        return DEFAULT_TAG, None
    text = str(reply).strip()
    lowered = text.lower()
    for tag in TAGS:
        if re.search(rf"\b{re.escape(tag)}\b", lowered):
            reason = re.sub(rf"\b{tag}\b", "", text, flags=re.IGNORECASE)
            reason = reason.strip().strip(":-\u2014\u2013 ").strip()
            return tag, reason or None
    return DEFAULT_TAG, text or None


def _write_proposal(
    candidate: dict, *, tag: str, reason: str | None,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict:
    """Persist a 'proposed' row for the candidate and return the proposal."""
    metrics = candidate.get("metrics") or {}
    key = candidate["chapter_key"]
    decided_at = _utc_iso(dt.datetime.now(dt.timezone.utc))
    with _connect(db_path) as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO {CLASSIFICATIONS_TABLE} "
            "(chapter_key, subject, chapter, tag, accuracy_ratio, cognitive_yield, "
            "evidence_count, reason, decided_at, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                key,
                candidate["subject"],
                candidate["chapter"],
                tag,
                metrics.get("avg_accuracy"),
                metrics.get("avg_cy"),
                int(metrics.get("sessions") or 0),
                reason,
                decided_at,
                "proposed",
            ),
        )
        conn.commit()
    return {
        "chapter_key": key,
        "subject": candidate["subject"],
        "chapter": candidate["chapter"],
        "tag": tag,
        "reason": reason,
        "accuracy_ratio": metrics.get("avg_accuracy"),
        "cognitive_yield": metrics.get("avg_cy"),
        "evidence_count": int(metrics.get("sessions") or 0),
        "status": "proposed",
    }


def propose_chapter_classification(
    candidate: dict, db_path: str | Path = DEFAULT_DB_PATH,
) -> dict:
    """LLM proposes ONE tag for a chapter and writes a 'proposed' row.

    The prompt only carries redacted accuracy/CY/sessions/minutes (via
    ``coaching_policy.redact_payload``). On ANY failure — no keys, router
    error, quota, empty/garbage reply — the deterministic default tag
    ``revision`` is used and the row is still written; this never raises.
    """
    tag, reason = DEFAULT_TAG, None
    try:
        metrics = candidate.get("metrics") or {}
        redacted = coaching_policy.redact_payload({
            "metrics": {
                "accuracy_ratio": metrics.get("avg_accuracy"),
                "cognitive_yield": metrics.get("avg_cy"),
                "sessions": metrics.get("sessions"),
                "total_minutes": metrics.get("total_minutes"),
            },
        })
        system = _SYSTEM_PROMPT.format(
            chapter=candidate["chapter"], subject=candidate["subject"],
        )
        system += "\n" + json.dumps(redacted, ensure_ascii=False, sort_keys=True)
        messages = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": (
                    f"Chapter: {candidate['chapter']} ({candidate['subject']}). "
                    "Reply with the tag and one short reason."
                ),
            },
        ]
        tag, reason = _parse_tag(_classify_llm_complete(messages))
    except Exception:
        tag, reason = DEFAULT_TAG, None
    return _write_proposal(candidate, tag=tag, reason=reason, db_path=db_path)


def discard_proposal(
    chapter_key: str, db_path: str | Path = DEFAULT_DB_PATH,
) -> bool:
    """Delete a 'proposed' row so the chapter can be re-proposed.

    Called when the proposal message failed to send: the row was written before
    the Telegram send, so without this the 'proposed' row would permanently
    block re-proposal (``classify_candidates`` skips any chapter that already
    has a classification row). Only 'proposed' rows are ever removed — a
    confirmed or dismissed decision is never discarded.
    """
    with _connect(db_path) as conn:
        cur = conn.execute(
            f"DELETE FROM {CLASSIFICATIONS_TABLE} "
            "WHERE chapter_key=? AND status='proposed'",
            (chapter_key,),
        )
        conn.commit()
        return cur.rowcount == 1


def _set_status(
    chapter_key: str, status: str, db_path: str | Path = DEFAULT_DB_PATH,
) -> dict | None:
    """Flip a row's status (confirmed/dismissed). None when unknown."""
    with _connect(db_path) as conn:
        existing = conn.execute(
            f"SELECT 1 FROM {CLASSIFICATIONS_TABLE} WHERE chapter_key=?",
            (chapter_key,),
        ).fetchone()
        if existing is None:
            return None
        conn.execute(
            f"UPDATE {CLASSIFICATIONS_TABLE} SET status=? WHERE chapter_key=?",
            (status, chapter_key),
        )
        conn.commit()
        row = conn.execute(
            f"SELECT * FROM {CLASSIFICATIONS_TABLE} WHERE chapter_key=?",
            (chapter_key,),
        ).fetchone()
    return dict(row)


def confirm_classification(
    chapter_key: str, db_path: str | Path = DEFAULT_DB_PATH,
) -> dict | None:
    """Confirm a proposed tag (callback ``classify:confirm:<key>``)."""
    return _set_status(chapter_key, "confirmed", db_path=db_path)


def dismiss_classification(
    chapter_key: str, db_path: str | Path = DEFAULT_DB_PATH,
) -> dict | None:
    """Dismiss a proposal, leaving the chapter untagged."""
    return _set_status(chapter_key, "dismissed", db_path=db_path)
