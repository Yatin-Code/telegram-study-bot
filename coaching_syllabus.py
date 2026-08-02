"""Deterministic normalization of portal test syllabi into a structured graph.

The Narayana portal ships each test with a ``syllabus`` field that is usually
HTML (a rich-text description) but sometimes plain text or a structured JSON
list.  This module parses that source into subject/chapter/topic records and
stores them in SQLite next to the coaching cache, so upcoming-syllabus and
coverage questions never need portal credentials or live HTTP.

Design rules:

* stdlib only (``html.parser``, ``re``) and fully deterministic.
* We never fabricate a chapter: if the source names only topics, ``chapter``
  stays NULL and the exact normalized line is preserved as evidence.
* Every record carries ``normalized_text`` (cleaned line) and ``raw_text``
  (verbatim source line) so the evidence survives normalization.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import re
import sqlite3
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import session_context

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "sqlite_mirror.db"

SYLLABUS_TABLE = "coaching_syllabus"
SYLLABUS_META_TABLE = "coaching_syllabus_meta"

# Canonical subject map (portal spellings -> our normalized label).
SUBJECT_CANONICAL: dict[str, str] = {
    "physics": "Physics",
    "phy": "Physics",
    "phys": "Physics",
    "chemistry": "Chemistry",
    "chem": "Chemistry",
    "mathematics": "Mathematics",
    "maths": "Mathematics",
    "math": "Mathematics",
    "biology": "Biology",
    "bio": "Biology",
    "zoology": "Zoology",
    "botany": "Botany",
    "english": "English",
    "general knowledge": "General Knowledge",
    "gk": "General Knowledge",
    "logical reasoning": "Logical Reasoning",
}

_HTML_LOOKS_RE = re.compile(r"<[a-zA-Z!][^>]*>")
_CHAPTER_RE = re.compile(
    r"^(?P<kind>chapter|chap\.?|ch\.?|unit|module|section|part)\s*"
    r"[#.]?\s*(?P<num>[0-9]+|[ivxlc]+)\b"
    r"\s*(?::|\u2013|\u2014|-|\.)\s*(?P<title>.*)$",
    re.IGNORECASE,
)
_TOPIC_SPLIT_RE = re.compile(r"[,;\u2022\u00b7|]")
_NUMBERED_SPLIT_RE = re.compile(r"\s+\d+[.)]\s+")
_LIST_PREFIX_RE = re.compile(r"^(?:\d+[.)]|[a-zA-Z][.)]|[ivxlc]+[.)]|[-*\u2022\u00b7])\s+")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _canonical_subject(text: str) -> str | None:
    """Map a raw subject name (possibly with padding) to its canonical label."""
    key = re.sub(r"\s+", " ", text.strip().strip(":").strip()).lower()
    return SUBJECT_CANONICAL.get(key)


def _extract_subject(line: str) -> tuple[str | None, str]:
    """Return (canonical subject, rest-of-line) when a line leads with a subject.

    Handles `Physics`, `Subject: Physics`, `Chemistry :` and the like.  When no
    subject is present the original line is returned unchanged as `rest`.
    """
    candidate = re.sub(
        r"^(?:subject|subject name)\s*[:.\-]?\s*", "", line.strip(),
        flags=re.IGNORECASE,
    )
    lower = candidate.lower()
    for name in sorted(SUBJECT_CANONICAL, key=len, reverse=True):
        if lower == name:
            return SUBJECT_CANONICAL[name], ""
        if lower.startswith(name):
            boundary = lower[len(name):len(name) + 1]
            if boundary in ("", " ", ":", "-", "\u2013", "\u2014"):
                suffix = candidate[len(name):]
                suffix = re.sub(r"^\s*(?::|\u2013|\u2014|-)\s*", "", suffix)
                return SUBJECT_CANONICAL[name], suffix.strip()
    return None, line


def _extract_chapter(text: str) -> tuple[str | None, str]:
    """Return (chapter title, rest) for an explicit `Chapter N: title` line."""
    match = _CHAPTER_RE.match(text.strip())
    if not match:
        return None, text
    title = match.group("title").strip().strip(".").strip()
    return title or None, ""


def _strip_list_prefix(text: str) -> str:
    previous = None
    while text != previous:
        previous = text
        text = _LIST_PREFIX_RE.sub("", text, count=1).strip()
    return text


def _split_topics(text: str) -> list[str]:
    parts = []
    for piece in _TOPIC_SPLIT_RE.split(text):
        for numbered in _NUMBERED_SPLIT_RE.split(piece):
            numbered = _strip_list_prefix(numbered.strip())
            if numbered:
                parts.append(numbered)
    return parts


def _clean_line(line: str) -> str:
    line = html.unescape(line)
    line = re.sub(r"\s+", " ", line).strip()
    return _strip_list_prefix(line)


class _SyllabusHTMLParser(HTMLParser):
    """Collect text while breaking lines at block / list boundaries."""

    _BREAK_TAGS = {
        "p", "div", "li", "ul", "ol", "br", "tr", "h1", "h2", "h3", "h4",
        "h5", "h6", "table", "section", "td", "th",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []

    def _newline(self) -> None:
        if self._chunks and not self._chunks[-1].endswith("\n"):
            self._chunks.append("\n")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._BREAK_TAGS:
            self._newline()

    def handle_endtag(self, tag: str) -> None:
        if tag in self._BREAK_TAGS:
            self._newline()

    def handle_data(self, data: str) -> None:
        self._chunks.append(data)


def _plain_lines(text: str) -> list[str]:
    text = html.unescape(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return [line for line in (ln.strip() for ln in text.split("\n")) if line]


def _html_to_lines(text: str) -> list[str]:
    parser = _SyllabusHTMLParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        return _plain_lines(text)
    body = "".join(parser._chunks)
    return _plain_lines(body)


def _to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _append_record(
    records: list[dict[str, Any]], ordinal: int, subject: str | None,
    chapter: str | None, topic: str | None, normalized_text: str,
    raw_text: str,
) -> int:
    records.append({
        "ordinal": ordinal,
        "subject": subject,
        "chapter": chapter,
        "topic": topic,
        "normalized_text": normalized_text,
        "raw_text": raw_text,
    })
    return ordinal + 1


def _parse_structured(value: Any) -> list[dict[str, Any]]:
    """Parse a structured syllabus: [{subject, topics:[...]}, ...]."""
    if isinstance(value, dict):
        entries = [value]
    elif isinstance(value, list):
        entries = value
    else:
        return []
    records: list[dict[str, Any]] = []
    ordinal = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        subject = _canonical_subject(str(entry.get("subject") or entry.get("name") or ""))
        topics = entry.get("topics") or entry.get("topicsList") or entry.get("syllabus")
        if isinstance(topics, str):
            topics = _split_topics(topics)
        elif isinstance(topics, dict):
            topics = list(topics.values())
        elif not isinstance(topics, list):
            topics = [topics] if topics else []
        for topic in topics:
            if isinstance(topic, dict):
                topic_text = str(topic.get("name") or topic.get("topic") or "")
                chapter = str(topic.get("chapter") or "").strip() or None
            else:
                topic_text = str(topic)
                chapter = None
            if not topic_text.strip():
                continue
            ordinal = _append_record(
                records, ordinal, subject, chapter, topic_text.strip(),
                topic_text.strip(), topic_text.strip(),
            )
    return records


def _looks_html(text: str) -> bool:
    return bool(_HTML_LOOKS_RE.search(text))


def parse_syllabus(
    value: Any, *, subject: str | None = None, chapter: str | None = None,
) -> list[dict[str, Any]]:
    """Parse a portal test syllabus into normalized subject/chapter/topic records.

    Accepts HTML, plain text, or a structured list/dict.  Chapters are only
    recorded when the source explicitly names one; bare topic lists stay topic
    records with ``chapter`` unset so we never invent a chapter name.
    """
    if isinstance(value, (dict, list)):
        structured = _parse_structured(value)
        if structured:
            return structured

    text = _to_text(value)
    lines = _html_to_lines(text) if _looks_html(text) else _plain_lines(text)

    records: list[dict[str, Any]] = []
    ordinal = 0
    current_subject = _canonical_subject(subject) if subject else None
    current_chapter = chapter

    for raw_line in lines:
        line = _clean_line(raw_line)
        if not line:
            continue
        subj, rest = _extract_subject(line)
        if subj:
            current_subject = subj
            current_chapter = None
            if not rest:
                continue
            line = rest
        chapter_title, rest = _extract_chapter(line)
        if chapter_title:
            topics = _split_topics(chapter_title)
            if len(topics) == 1:
                current_chapter = chapter_title
                ordinal = _append_record(
                    records, ordinal, current_subject, chapter_title, None,
                    line, raw_line,
                )
                continue
            line = chapter_title
        topics = _split_topics(line) or [line]
        for topic in topics:
            ordinal = _append_record(
                records, ordinal, current_subject, current_chapter, topic,
                line, raw_line,
            )
    return records


# ---------------------------------------------------------------------------
# SQLite storage
# ---------------------------------------------------------------------------

def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS {SYLLABUS_TABLE} (
            source_test_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            subject TEXT,
            chapter TEXT,
            topic TEXT,
            normalized_text TEXT NOT NULL,
            raw_text TEXT,
            source_updated_at TEXT,
            PRIMARY KEY(source_test_id, ordinal)
        );
        CREATE INDEX IF NOT EXISTS idx_coaching_syllabus_subject
            ON coaching_syllabus(subject);
        CREATE INDEX IF NOT EXISTS idx_coaching_syllabus_topic
            ON coaching_syllabus(topic);
        CREATE TABLE IF NOT EXISTS {SYLLABUS_META_TABLE} (
            source_test_id TEXT PRIMARY KEY,
            parsed_at TEXT NOT NULL,
            record_count INTEGER NOT NULL DEFAULT 0,
            raw_hash TEXT
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


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _raw_hash(records: list[dict[str, Any]]) -> str:
    evidence = "\n".join(str(record.get("raw_text") or "") for record in records)
    return hashlib.sha256(evidence.encode("utf-8")).hexdigest()[:16]


def store_test_syllabus(
    source_test_id: str, records: list[dict[str, Any]],
    *, db_path: str | Path = DEFAULT_DB_PATH, parsed_at: str | None = None,
) -> int:
    """Replace the stored syllabus for one test; returns stored record count."""
    parsed_at = parsed_at or _now()
    rows = []
    for index, record in enumerate(records):
        rows.append((
            str(source_test_id),
            int(record.get("ordinal", index)),
            record.get("subject"),
            record.get("chapter"),
            record.get("topic"),
            record.get("normalized_text") or "",
            record.get("raw_text"),
            parsed_at,
        ))
    with _connect(db_path) as conn:
        conn.execute(
            f"DELETE FROM {SYLLABUS_TABLE} WHERE source_test_id=?",
            (str(source_test_id),),
        )
        conn.executemany(
            f"""INSERT INTO {SYLLABUS_TABLE}
                (source_test_id, ordinal, subject, chapter, topic,
                 normalized_text, raw_text, source_updated_at)
                VALUES (?,?,?,?,?,?,?,?)""",
            rows,
        )
        conn.execute(
            f"""INSERT INTO {SYLLABUS_META_TABLE}
                (source_test_id, parsed_at, record_count, raw_hash)
                VALUES (?,?,?,?)
                ON CONFLICT(source_test_id) DO UPDATE SET
                  parsed_at=excluded.parsed_at,
                  record_count=excluded.record_count,
                  raw_hash=excluded.raw_hash""",
            (str(source_test_id), parsed_at, len(rows), _raw_hash(records)),
        )
        conn.commit()
    return len(rows)


def replace_syllabi(
    tests: list[dict[str, Any]], *, db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, int]:
    """Parse and store syllabus records for every fetched test row."""
    parsed = 0
    stored = 0
    for row in tests or []:
        if not isinstance(row, dict):
            continue
        source_id = str(row.get("id") or row.get("testPaperId") or "")
        syllabus = row.get("syllabus")
        if not source_id or not syllabus:
            continue
        records = parse_syllabus(syllabus)
        if not records:
            continue
        parsed += 1
        stored += store_test_syllabus(source_id, records, db_path=db_path)
    return {"tests_parsed": parsed, "records_stored": stored}


def syllabus_for_test(
    source_test_id: str, *, db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"""SELECT ordinal, subject, chapter, topic, normalized_text,
                       raw_text, source_updated_at
                FROM {SYLLABUS_TABLE} WHERE source_test_id=? ORDER BY ordinal""",
            (str(source_test_id),),
        ).fetchall()
        meta = conn.execute(
            f"SELECT parsed_at, record_count, raw_hash FROM {SYLLABUS_META_TABLE} "
            "WHERE source_test_id=?",
            (str(source_test_id),),
        ).fetchone()
    return {
        "source_test_id": str(source_test_id),
        "records": [dict(row) for row in rows],
        "meta": dict(meta) if meta else None,
    }


def upcoming_syllabus(
    *, today: str | None = None, limit: int = 5,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    """Upcoming portal tests joined with their normalized syllabus records."""
    today = (today or session_context.local_today_iso())[:10]
    with _connect(db_path) as conn:
        if not _table_exists(conn, "coaching_tests"):
            return []
        tests = conn.execute(
            """SELECT source_id, title, test_date, course_id, batch, goal,
                      source_updated_at
               FROM coaching_tests
               WHERE substr(COALESCE(test_date,''),1,10)>=?
               ORDER BY test_date LIMIT ?""",
            (today, limit),
        ).fetchall()
        result = []
        for test in tests:
            entry = dict(test)
            records = conn.execute(
                f"""SELECT ordinal, subject, chapter, topic, normalized_text,
                           raw_text, source_updated_at
                    FROM {SYLLABUS_TABLE} WHERE source_test_id=? ORDER BY ordinal""",
                (str(test["source_id"]),),
            ).fetchall()
            meta = conn.execute(
                f"SELECT parsed_at, record_count, raw_hash FROM {SYLLABUS_META_TABLE} "
                "WHERE source_test_id=?",
                (str(test["source_id"]),),
            ).fetchone()
            entry["syllabus_records"] = [dict(row) for row in records]
            entry["syllabus_meta"] = dict(meta) if meta else None
            entry["syllabus_count"] = len(records)
            result.append(entry)
    return result


# ---------------------------------------------------------------------------
# Coverage / progress representation
# ---------------------------------------------------------------------------

def _normalize_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text.strip(" .,;:-\u2013\u2014()[]{}")


def _record_keys(record: dict[str, Any]) -> list[str]:
    keys = []
    for value in (record.get("topic"), record.get("chapter")):
        key = _normalize_key(value)
        if key:
            keys.append(key)
    return keys


def _match_ledger(rows: list[dict[str, Any]], record: dict[str, Any]) -> bool:
    keys = _record_keys(record)
    if not keys:
        return False
    subject = record.get("subject")
    for row in rows:
        if subject and not _subject_matches(row.get("subject"), subject):
            continue
        for field in ("chapter_text", "task", "exercise_type", "page_content"):
            hay = _normalize_key(row.get(field))
            if not hay:
                continue
            for key in keys:
                if key in hay or hay in key:
                    return True
    return False


def _match_doubts(rows: list[dict[str, Any]], record: dict[str, Any]) -> bool:
    keys = _record_keys(record)
    if not keys:
        return False
    subject = record.get("subject")
    for row in rows:
        if subject and not _subject_matches(row.get("subject"), subject):
            continue
        for field in ("core_concept", "page_content"):
            hay = _normalize_key(row.get(field))
            if not hay:
                continue
            for key in keys:
                if key in hay or hay in key:
                    return True
    return False


def _subject_matches(value: Any, target: str | None) -> bool:
    if not target:
        return True
    return _canonical_subject(str(value or "")) == target


def coverage_snapshot(
    *, today: str | None = None, limit: int = 5,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    """Upcoming tests with per-topic covered / has_doubt flags from the mirror."""
    tests = upcoming_syllabus(today=today, limit=limit, db_path=db_path)
    with _connect(db_path) as conn:
        ledger_rows = (
            conn.execute(
                "SELECT subject, task, chapter_text, exercise_type, page_content "
                "FROM ledger WHERE archived=0"
            ).fetchall()
            if _table_exists(conn, "ledger") else []
        )
        doubt_rows = (
            conn.execute(
                "SELECT subject, core_concept, page_content FROM doubts WHERE archived=0"
            ).fetchall()
            if _table_exists(conn, "doubts") else []
        )
    ledger = [dict(row) for row in ledger_rows]
    doubts = [dict(row) for row in doubt_rows]
    for test in tests:
        records = test.get("syllabus_records") or []
        covered = 0
        for record in records:
            record["covered"] = _match_ledger(ledger, record)
            record["has_doubt"] = _match_doubts(doubts, record)
            if record["covered"]:
                covered += 1
        test["coverage"] = {
            "topic_count": len(records),
            "covered_count": covered,
            "uncovered_count": len(records) - covered,
            "covered_fraction": round(covered / len(records), 3) if records else None,
            "known": bool(records),
        }
    return tests


def progress_snapshot(
    *, today: str | None = None, limit: int = 5,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Subject-level coverage/progress across the upcoming syllabus window."""
    tests = coverage_snapshot(today=today, limit=limit, db_path=db_path)
    by_subject: dict[str, dict[str, Any]] = {}
    for test in tests:
        for record in test.get("syllabus_records") or []:
            subject = record.get("subject") or "(uncategorised)"
            bucket = by_subject.setdefault(subject, {
                "subject": subject, "topic_count": 0, "covered_count": 0,
                "uncovered_count": 0, "doubt_count": 0, "tests": set(),
            })
            bucket["topic_count"] += 1
            bucket["tests"].add(test.get("source_id"))
            if record.get("covered"):
                bucket["covered_count"] += 1
            if record.get("has_doubt"):
                bucket["doubt_count"] += 1
    subjects = []
    for bucket in by_subject.values():
        bucket["uncovered_count"] = bucket["topic_count"] - bucket["covered_count"]
        bucket["covered_fraction"] = (
            round(bucket["covered_count"] / bucket["topic_count"], 3)
            if bucket["topic_count"] else None
        )
        bucket["tests"] = sorted(bucket["tests"])
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
                "topic_count": len(test.get("syllabus_records") or []),
                "covered_count": (test.get("coverage") or {}).get("covered_count", 0),
                "uncovered_count": (test.get("coverage") or {}).get("uncovered_count", 0),
            }
            for test in tests
        ],
        "subjects": subjects,
    }
