"""Phase 12 + 14 core — backlog escalation policy, privacy, freshness, notifications.

This module is a **read-only decision core**. It never sends a message and never
writes a plan. It owns four deterministic concern areas:

1. **Privacy policy** (Phase 14) — names and redacts sensitive fields and values
   (Aadhaar/Uidai numbers, parent/guardian phone numbers, addresses, passwords,
   tokens, API keys, raw sensitive JSON blobs) before any dict/tool-result/text
   reaches an LLM. ``redact_row`` / ``redact_rows`` / ``redact_value`` /
   ``redact_text`` are safe on any payload; ``privacy_policy_block`` renders the
   rule set for a system prompt.

2. **Freshness classification** (Phase 14) — per-dataset status from the
   Notion mirror ``sync_meta``, the coaching ``coaching_sync_runs`` history, and
   the locally-owned ``op_*`` tables. Every dataset is classified explicitly as
   ``fresh`` | ``stale`` | ``failed`` | ``never_synced`` (never guessed).

3. **Notification policy** (Phase 14) — a durable decision/audit core: quiet
   hours, per-kind cooldowns, data-freshness relevance gating, a max per-day
   budget, and a ``notification_decisions`` audit table. ``decide_notification``
   returns a decision + human-readable reasons; ``record_decision`` persists it.
   Nothing is ever sent here.

4. **Bounded backlog escalation** (Phase 12) — deterministic backlog metrics
   (count, age, estimated hours, 3/7-day net growth where evidence exists),
   plan adherence/capacity and coverage risk, and a four-level verdict
   (``normal``/``growing``/``critical``/``impossible``). Any suggested minute
   increase is bounded by ``settings.max_daily_committed_minutes``, the
   day-window (``DAY_START``..``DAY_END``), and never auto-writes a plan.

Everything is offline SQL + pure functions; no LLM is involved.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import statistics
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import ntsc_coaching
import operational_store
import session_context
import study_domain
import sync
from config import settings

import coaching_planner

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "sqlite_mirror.db"
REDACTED = "[REDACTED]"
DECISIONS_TABLE = "notification_decisions"

REDACT_MAX_DEPTH = 6

# ---------------------------------------------------------------------------
# environment-backed policy knobs (settings.json → .env → default)
# ---------------------------------------------------------------------------


def _policy_env(name: str, default: str) -> str:
    override = settings.get_override(name)
    if override and str(override).strip():
        return str(override).strip()
    raw = os.environ.get(name, "").strip()
    return raw or default


def _policy_env_int(name: str, default: int) -> int:
    try:
        return int(_policy_env(name, str(default)))
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# privacy policy (Phase 14)
# ---------------------------------------------------------------------------

# Field names whose value is redacted wholesale when found in a row/dict.
_SENSITIVE_EXACT = {
    "password", "passwd", "pwd", "secret", "otp",
    "apikey", "accesstoken", "authtoken", "refreshtoken", "devicetoken",
    "authorization", "bearer",
    "aadhaar", "aadhar", "uidai",
}
_SENSITIVE_SUBSTRINGS = (
    "token",       # auth/readiness tokens are never needed by an LLM
    "phone",       # parent_phone, mobile, telephone, ...
    "mobile",      # parent_mobile, mobile_no
    "contact",     # contact_no, contact_number
    "address",     # home_address, residential_address
    "street",
    "pincode",
    "guardian",
    "aadhaar",
    "aadhar",
    "uidai",
)
_ADDRESS_KEYWORDS = ("address", "street", "lane", "colony", "sector",
                     "residential", "flat", "house no")
_STUDY_CONTEXT_WORDS = ("chapter", "doubt", "syllabus", "topic", "exercise",
                        "physics", "maths", "chem", "accuracy", "marks", "rank")

_AADHAAR_RE = re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}\b")
_PHONE_RE = re.compile(
    r"\b(?:\+?91[\s-]?)?"
    r"(?:[6-9]\d{9}|[6-9]\d{4}[\s-]?\d{5}|[6-9]\d{2}[\s-]?\d{3}[\s-]?\d{4})\b"
)
_PINCODE_RE = re.compile(r"\b[1-9]\d{5}\b")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_BEARER_RE = re.compile(
    r"(?i)\b(bearer|token|password|passwd|pwd|secret|api[-_]?key)"
    r"\s*[=:]\s*\S+"
)
_SECRET_KEY_RE = re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{8,}\b")

RAW_JSON_KEYS = {"raw_json", "raw", "page_content"}


def _normalise_name(name: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def is_sensitive_field(name: Any) -> bool:
    """True when a field *name* denotes sensitive data (phone/address/secret)."""
    normalised = _normalise_name(name)
    if not normalised:
        return False
    if normalised in _SENSITIVE_EXACT:
        return True
    return any(substring in normalised for substring in _SENSITIVE_SUBSTRINGS)


def _looks_like_full_address(text: str) -> bool:
    low = str(text).lower()
    if not any(keyword in low for keyword in _ADDRESS_KEYWORDS):
        return False
    if not _PINCODE_RE.search(str(text)):
        return False
    words = len(str(text).split())
    if not (2 <= words <= 300):
        return False
    if any(keyword in low for keyword in _STUDY_CONTEXT_WORDS):
        return False
    return True


def _contains_sensitive_pattern(text: str) -> bool:
    return bool(
        _AADHAAR_RE.search(text) or _PHONE_RE.search(text)
        or _PINCODE_RE.search(text) or _EMAIL_RE.search(text)
        or _BEARER_RE.search(text) or _SECRET_KEY_RE.search(text)
        or _looks_like_full_address(text)
    )


def redact_text(text: str) -> str:
    """Redact sensitive *values* inside a free-text string.

    Conservative on purpose: study numbers (marks, page ids, sequences) are
    left untouched; only unambiguous personal data is replaced.
    """
    if not isinstance(text, str) or not text:
        return text
    if _looks_like_full_address(text):
        return REDACTED
    redacted = _AADHAAR_RE.sub(REDACTED, text)
    redacted = _PHONE_RE.sub(REDACTED, redacted)
    redacted = _PINCODE_RE.sub(REDACTED, redacted)
    redacted = _EMAIL_RE.sub(REDACTED, redacted)
    redacted = _SECRET_KEY_RE.sub(REDACTED, redacted)
    redacted = _BEARER_RE.sub(lambda m: f"{m.group(1)}={REDACTED}", redacted)
    return redacted


def _redact_json_string(text: Any) -> str:
    if text is None:
        return text
    if isinstance(text, str):
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return redact_text(text) if _contains_sensitive_pattern(text) else text
        return json.dumps(redact_value(parsed), ensure_ascii=False, sort_keys=True)
    return redact_value(text)


def redact_value(value: Any, *, _depth: int = 0) -> Any:
    """Recursively redact a nested value (dict/list/str). Never mutates input."""
    if _depth > REDACT_MAX_DEPTH:
        return value
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if is_sensitive_field(key):
                out[str(key)] = REDACTED
            elif _normalise_name(key) in RAW_JSON_KEYS:
                out[str(key)] = _redact_json_string(item)
            else:
                out[str(key)] = redact_value(item, _depth=_depth + 1)
        return out
    if isinstance(value, list):
        return [redact_value(item, _depth=_depth + 1) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item, _depth=_depth + 1) for item in value)
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_row(row: dict[str, Any]) -> dict[str, Any]:
    """Redact one row dict (LLM tool result) without mutating the original."""
    return redact_value(row) if isinstance(row, dict) else redact_value(row)


def redact_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Redact a list of row dicts, preserving order."""
    return [redact_row(row) for row in rows]


def redact_payload(payload: Any) -> Any:
    """Redact any structured payload (dict / list / text)."""
    return redact_value(payload)


def privacy_policy_block() -> str:
    """Prompt block describing the redaction rules applied before the model."""
    return (
        "## Privacy redaction policy (applied to every tool result and context)\n"
        "- Sensitive fields are removed or replaced with [REDACTED] before this "
        "prompt is assembled: Aadhaar/Uidai numbers, parent/guardian phone "
        "numbers, addresses, passwords, tokens, API keys, and raw sensitive "
        "JSON blobs.\n"
        "- Values that clearly look like a phone (10 digits starting 6-9, "
        "optional +91), Aadhaar (12 digits), a 6-digit pincode, an email, a "
        "bearer token, or an sk-/pk-/rk- API key are redacted even inside free "
        "text.\n"
        "- Never try to reconstruct a redacted value from context; ask the "
        "user directly if identity data is genuinely required."
    )


# ---------------------------------------------------------------------------
# freshness classification (Phase 14)
# ---------------------------------------------------------------------------

FRESH_NOTION_MINUTES = _policy_env_int("POLICY_FRESH_NOTION_MIN", 15)
FRESH_COACHING_MINUTES = _policy_env_int("POLICY_FRESH_COACHING_MIN", 60)
FRESH_OPERATIONAL_MINUTES = _policy_env_int("POLICY_FRESH_OPERATIONAL_MIN", 60)

FRESH_LABELS = {
    "fresh": "fresh",
    "stale": "stale",
    "failed": "failed",
    "never_synced": "never_synced",
}

NOTION_DATASETS = ("ledger", "doubts", "revision")
ALL_DATASETS = (*NOTION_DATASETS, "coaching", "operational")


def _parse_ts(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        stamp = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return stamp.astimezone(dt.timezone.utc)


def _age_minutes(now: dt.datetime, stamp: dt.datetime) -> float:
    now_utc = now.astimezone(dt.timezone.utc) if now.tzinfo else now
    return max(0.0, (now_utc - stamp).total_seconds() / 60)


def _notion_freshness(
    now: dt.datetime, db_path: str | Path,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    with sync.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT db_key, last_completed_at, last_error FROM sync_meta"
        ).fetchall()
    meta = {str(row["db_key"]): row for row in rows}
    for dataset in NOTION_DATASETS:
        row = meta.get(dataset)
        detail: list[str] = []
        if row is None or not row["last_completed_at"]:
            if row is not None and row["last_error"]:
                result[dataset] = {
                    "status": "failed", "label": "failed",
                    "last_synced_at": None, "age_minutes": None,
                    "detail": f"last attempt failed: {str(row['last_error'])[:120]}",
                }
            else:
                result[dataset] = {
                    "status": "never_synced", "label": "never_synced",
                    "last_synced_at": None, "age_minutes": None,
                    "detail": "no successful sync recorded",
                }
            continue
        completed = _parse_ts(row["last_completed_at"])
        if completed is None:
            result[dataset] = {
                "status": "stale", "label": "stale",
                "last_synced_at": str(row["last_completed_at"]),
                "age_minutes": None,
                "detail": "last completed time is unparseable",
            }
            continue
        age = _age_minutes(now, completed)
        error = row["last_error"]
        if error:
            detail.append(f"latest attempt failed: {str(error)[:120]}")
        if age <= FRESH_NOTION_MINUTES:
            status = "fresh"
        elif error:
            status = "failed"
        else:
            status = "stale"
        result[dataset] = {
            "status": status, "label": FRESH_LABELS[status],
            "last_synced_at": completed.isoformat(),
            "age_minutes": round(age, 1),
            "detail": "; ".join(detail) or f"{round(age)}m old",
        }
    return result


def _coaching_freshness(
    now: dt.datetime, db_path: str | Path,
) -> dict[str, Any]:
    with ntsc_coaching._connect(db_path) as conn:
        row = conn.execute(
            "SELECT status, finished_at, datasets, error "
            "FROM coaching_sync_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return {
            "status": "never_synced", "label": "never_synced",
            "last_synced_at": None, "age_minutes": None,
            "detail": "portal has never been synced",
        }
    ok = str(row["status"]) == "success"
    finished = _parse_ts(row["finished_at"])
    detail = f"datasets: {row['datasets'] or '[]'}"
    if row["error"]:
        detail += f"; error: {str(row['error'])[:120]}"
    if not ok:
        return {
            "status": "failed", "label": "failed",
            "last_synced_at": finished.isoformat() if finished else None,
            "age_minutes": _age_minutes(now, finished) if finished else None,
            "detail": detail,
        }
    if finished is None:
        return {
            "status": "stale", "label": "stale",
            "last_synced_at": str(row["finished_at"]),
            "age_minutes": None,
            "detail": "successful run with an unparseable finish time",
        }
    age = _age_minutes(now, finished)
    status = "fresh" if age <= FRESH_COACHING_MINUTES else "stale"
    return {
        "status": status, "label": FRESH_LABELS[status],
        "last_synced_at": finished.isoformat(),
        "age_minutes": round(age, 1),
        "detail": detail,
    }


def _operational_freshness(
    now: dt.datetime, db_path: str | Path,
) -> dict[str, Any]:
    total_rows = 0
    oldest_synced: dt.datetime | None = None
    with operational_store.connect(db_path) as conn:
        operational_store.init_db(conn)
        for key, table in operational_store.OP_TABLES.items():
            try:
                count = conn.execute(
                    f'SELECT COUNT(*) FROM "{table}" WHERE archived=0'
                ).fetchone()[0]
            except sqlite3.OperationalError:
                continue
            total_rows += int(count)
            stamp = conn.execute(
                f'SELECT MAX(last_synced_at) FROM "{table}"'
            ).fetchone()[0]
            parsed = _parse_ts(stamp)
            if parsed and (oldest_synced is None or parsed > oldest_synced):
                oldest_synced = parsed
    if total_rows == 0:
        return {
            "status": "never_synced", "label": "never_synced",
            "last_synced_at": None, "age_minutes": None,
            "detail": "no local operational records yet",
        }
    if oldest_synced is None:
        return {
            "status": "stale", "label": "stale",
            "last_synced_at": None, "age_minutes": None,
            "detail": f"{total_rows} records but no sync timestamps",
        }
    age = _age_minutes(now, oldest_synced)
    status = "fresh" if age <= FRESH_OPERATIONAL_MINUTES else "stale"
    return {
        "status": status, "label": FRESH_LABELS[status],
        "last_synced_at": oldest_synced.isoformat(),
        "age_minutes": round(age, 1),
        "detail": f"{total_rows} local operational records",
    }


def classify_freshness(
    *, now: dt.datetime | None = None, db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, dict[str, Any]]:
    """Per-dataset freshness with explicit fresh/stale/failed/never_synced."""
    now = now or session_context.local_now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    result = _notion_freshness(now, db_path)
    result["coaching"] = _coaching_freshness(now, db_path)
    result["operational"] = _operational_freshness(now, db_path)
    return result


def freshness_block(
    *, now: dt.datetime | None = None, db_path: str | Path = DEFAULT_DB_PATH,
) -> str:
    """Compact per-dataset freshness text block for an LLM system prompt."""
    data = classify_freshness(now=now, db_path=db_path)
    lines = ["## Data freshness (last successful sync)"]
    for dataset in ALL_DATASETS:
        item = data[dataset]
        stamp = item["last_synced_at"] or "never"
        age = item["age_minutes"]
        suffix = f" · {round(age)}m ago" if age is not None else ""
        lines.append(
            f"- {dataset}: {item['status']} (last synced {stamp}{suffix})"
        )
    lines.append(
        "Do not state a figure as current if its dataset is stale or failed."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# durable notification decision/audit store (Phase 14)
# ---------------------------------------------------------------------------


def _connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = study_domain._connect(db_path)
    ntsc_coaching.init_db(conn)
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {DECISIONS_TABLE} (
            decision_key TEXT PRIMARY KEY,
            chat_id TEXT,
            kind TEXT NOT NULL,
            allow INTEGER NOT NULL,
            local_date TEXT NOT NULL,
            decided_at TEXT NOT NULL,
            reasons TEXT NOT NULL DEFAULT '[]',
            blocked_by TEXT NOT NULL DEFAULT '[]',
            relevance TEXT NOT NULL DEFAULT '{{}}',
            metadata TEXT NOT NULL DEFAULT '{{}}'
        )
    """)
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{DECISIONS_TABLE}_date_kind "
        f"ON {DECISIONS_TABLE}(local_date, kind)"
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{DECISIONS_TABLE}_kind_sent "
        f"ON {DECISIONS_TABLE}(kind, decided_at)"
    )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# notification policy core (Phase 14)
# ---------------------------------------------------------------------------

QUIET_HOURS_DEFAULT = (_policy_env("POLICY_QUIET_START", "22:00"),
                       _policy_env("POLICY_QUIET_END", "08:00"))
NOTIFICATIONS_MAX_PER_DAY = _policy_env_int("POLICY_NOTIFICATIONS_PER_DAY", 12)
DEFAULT_COOLDOWN_MIN = _policy_env_int("POLICY_COOLDOWN_MIN", 30)

# Kinds that may bypass quiet hours + the daily budget (never the cooldown).
URGENT_KINDS = {"system_alert"}
URGENT_PRIORITIES = {"urgent", "critical"}

# Discipline kinds may skip the quiet-hours gate: the execution-discipline scan
# only emits them while inside a declared study block, so they can fire during
# the late scheduled blocks (e.g. 22:15-01:00) without disturbing sleep/breaks.
QUIET_BYPASS_KINDS = {
    "discipline_start", "discipline_push", "discipline_shame", "discipline_checkin",
}

# Dataset a kind depends on (used for relevance gating).
KIND_DATASETS: dict[str, tuple[str, ...]] = {
    "planning": ("operational",),
    "exam": ("coaching",),
    "teacher": ("doubts",),
    "commitment_check": ("ledger",),
    "commitment_nudge": ("ledger",),
    "weekly_report": ("ledger", "coaching"),
    "insight": ("ledger",),
    "coaching_pre": ("coaching",),
    "coaching_post": ("coaching",),
    "backlog": ("work_items",),
    "system_alert": (),
    # Phase 13 proactive jobs (deterministic, mirror-only).
    "coaching_progress": ("coaching",),
    "doubt_reattempt": ("doubts",),
    "readiness": ("coaching",),
    # Execution-discipline kinds. start/push/shame are NOT data-gated: they
    # depend only on the local template (day_type already gates coaching-ness).
    "discipline_start": (),
    "discipline_push": (),
    "discipline_shame": (),
    # checkin verifies ledger evidence, so it must stay ledger-freshness gated.
    "discipline_checkin": ("ledger",),
}

# Kinds that must not fire while their dataset is stale/failed/never_synced.
DATA_GATED_KINDS = {
    "exam", "teacher", "commitment_check", "commitment_nudge",
    "weekly_report", "insight", "coaching_pre", "coaching_post",
    "coaching_progress", "doubt_reattempt", "readiness",
    # checkin verifies ledger evidence; start/push/shame deliberately NOT here.
    "discipline_checkin",
}

# Per-kind cooldown override (minutes); falls back to DEFAULT_COOLDOWN_MIN.
KIND_COOLDOWN_MIN: dict[str, int] = {
    "planning": 12 * 60,
    "commitment_check": 6 * 60,
    "coaching_pre": 60,
    "coaching_post": 60,
    "exam": 60,
    "teacher": 45,
    "coaching_progress": 24 * 60,
    "doubt_reattempt": 24 * 60,
    "readiness": 12 * 60,
    # Execution-discipline escalation tiers + post-block checkin.
    "discipline_start": 0,
    "discipline_push": 10,
    "discipline_shame": 10,
    "discipline_checkin": 60,
}


def _parse_hhmm(value: str) -> int:
    try:
        hour, minute = (int(part) for part in str(value).split(":", 1))
    except (ValueError, AttributeError):
        raise ValueError(f"time must be HH:MM, got {value!r}")
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"time must be HH:MM, got {value!r}")
    return hour * 60 + minute


def _hhmm(minutes: int) -> str:
    minutes %= 24 * 60
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def is_quiet_hours(
    now: dt.datetime, *, start_hhmm: str | None = None, end_hhmm: str | None = None,
) -> tuple[bool, tuple[str, str]]:
    """True when ``now`` falls inside the quiet window (overnight aware).

    ``start == end`` disables quiet hours. Returns ``(active, (start, end))``.
    """
    start = start_hhmm or QUIET_HOURS_DEFAULT[0]
    end = end_hhmm or QUIET_HOURS_DEFAULT[1]
    minute = now.hour * 60 + now.minute
    start_min = _parse_hhmm(start)
    end_min = _parse_hhmm(end)
    if start_min == end_min:
        return False, (start, end)
    if start_min < end_min:
        active = start_min <= minute < end_min
    else:
        active = minute >= start_min or minute < end_min
    return active, (start, end)


def _decision_key(kind: str, event_key: str | None, local_date: str) -> str:
    raw = f"{kind}|{event_key or ''}|{local_date}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def daily_notification_count(
    *, local_date: str | None = None, kind: str | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> int:
    """Count of allowed notification decisions on a local date."""
    local_date = (local_date or session_context.local_today_iso())[:10]
    with _connect(db_path) as conn:
        if kind:
            row = conn.execute(
                f"SELECT COUNT(*) FROM {DECISIONS_TABLE} "
                "WHERE local_date=? AND kind=? AND allow=1",
                (local_date, str(kind).strip().lower()),
            ).fetchone()
        else:
            row = conn.execute(
                f"SELECT COUNT(*) FROM {DECISIONS_TABLE} "
                "WHERE local_date=? AND allow=1", (local_date,),
            ).fetchone()
    return int(row[0])


def decide_notification(
    *,
    kind: str,
    now: dt.datetime | None = None,
    event_key: str | None = None,
    chat_id: int | str | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
    priority: str = "normal",
    quiet_hours: tuple[str, str] | None = None,
    cooldown_min: int | None = None,
    budget_per_day: int | None = None,
) -> dict[str, Any]:
    """Decide whether a notification of ``kind`` may fire. Nothing is sent.

    Applies, in order: quiet hours → cooldown → data-freshness relevance →
    daily budget. Every verdict carries human-readable ``reasons``, the
    ``blocked_by`` factors, and audit metadata. Callers persist the returned
    decision with :func:`record_decision`.
    """
    kind = str(kind or "").strip().lower()
    if not kind:
        raise ValueError("kind is required")
    now = now or session_context.local_now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=session_context._tz())
    local_date = now.date().isoformat()
    urgent = kind in URGENT_KINDS or str(priority).lower() in URGENT_PRIORITIES
    cooldown = cooldown_min if cooldown_min is not None else KIND_COOLDOWN_MIN.get(
        kind, DEFAULT_COOLDOWN_MIN
    )
    budget = budget_per_day if budget_per_day is not None else NOTIFICATIONS_MAX_PER_DAY

    allow = True
    blocked_by: list[str] = []
    reasons: list[str] = []

    quiet_active, quiet_window = is_quiet_hours(
        now, start_hhmm=quiet_hours[0] if quiet_hours else None,
        end_hhmm=quiet_hours[1] if quiet_hours else None,
    )
    if kind in QUIET_BYPASS_KINDS:
        quiet_active = False  # discipline kinds are scan-gated to study blocks only
    if quiet_active and not urgent:
        allow = False
        blocked_by.append("quiet_hours")
        reasons.append(
            f"inside quiet hours ({quiet_window[0]}-{quiet_window[1]}) "
            f"and {kind} is not urgent"
        )
    else:
        reasons.append(
            "quiet hours" + ("" if not quiet_active else f" ({quiet_window[0]}-{quiet_window[1]})")
        )

    sent_today = daily_notification_count(local_date=local_date, db_path=db_path)
    remaining = max(0, budget - sent_today)
    if not urgent and sent_today >= budget:
        allow = False
        blocked_by.append("budget")
        reasons.append(
            f"daily budget reached ({sent_today}/{budget} notifications today)"
        )
    else:
        reasons.append(
            f"daily budget {remaining} remaining of {budget}"
            if not urgent else "urgent kind is exempt from the daily budget"
        )

    last_allowed: dt.datetime | None = None
    with _connect(db_path) as conn:
        row = conn.execute(
            f"SELECT decided_at FROM {DECISIONS_TABLE} "
            "WHERE kind=? AND allow=1 ORDER BY decided_at DESC LIMIT 1",
            (kind,),
        ).fetchone()
    if row is not None:
        last_allowed = _parse_ts(row["decided_at"])
    cooling = False
    minutes_since_last: float | None = None
    if last_allowed is not None:
        minutes_since_last = _age_minutes(now, last_allowed)
        cooling = minutes_since_last < cooldown
    if cooling:
        allow = False
        blocked_by.append("cooldown")
        reasons.append(
            f"{kind} was allowed {round(minutes_since_last)}m ago, below the "
            f"{cooldown}m cooldown"
        )
    else:
        reasons.append(
            f"cooldown satisfied (last {kind} {round(minutes_since_last) if minutes_since_last is not None else 'never'}m ago)"
        )

    freshness = classify_freshness(now=now, db_path=db_path)
    relevant_datasets = KIND_DATASETS.get(kind, ())
    data_fresh = True
    stale_datasets: list[str] = []
    for dataset in relevant_datasets:
        if dataset == "operational" or operational_store.is_operational(dataset):
            continue
        status = freshness.get(dataset, {}).get("status")
        if status != "fresh":
            data_fresh = False
            stale_datasets.append(f"{dataset}={status}")
    if kind in DATA_GATED_KINDS and not data_fresh:
        allow = False
        blocked_by.append("stale_data")
        reasons.append(
            f"refusing to notify on stale/absent data for {kind}: "
            + ", ".join(stale_datasets)
        )
    else:
        reasons.append(
            "relevance ok — data fresh" if data_fresh
            else f"relevance low — data not fresh for {', '.join(stale_datasets)}"
        )

    relevance_score = (
        "high" if allow and data_fresh and not cooling and not quiet_active
        else "low" if blocked_by else "medium"
    )

    return {
        "kind": kind,
        "decision_key": _decision_key(kind, event_key, local_date),
        "allow": allow,
        "reasons": reasons,
        "blocked_by": blocked_by,
        "quiet_hours": {"start": quiet_window[0], "end": quiet_window[1],
                        "active": quiet_active, "bypassed": urgent},
        "cooldown": {"min_gap_min": cooldown,
                     "last_allowed_min_ago": minutes_since_last,
                     "cooling": cooling},
        "relevance": {"score": relevance_score, "data_fresh": data_fresh,
                      "priority": str(priority),
                      "stale_datasets": stale_datasets,
                      "datasets": list(relevant_datasets)},
        "budget": {"per_day": budget, "sent_today": sent_today,
                   "remaining": remaining},
        "local_date": local_date,
        "decided_at": now.isoformat(),
        "sends_nothing": True,
    }


def record_decision(
    decision: dict[str, Any], *, db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Persist one decision for the durable audit trail (idempotent by key).

    A blocked re-decision of the same ``event_key``/day never overwrites an
    earlier ``allow=1`` record for that key — the first allowed decision stays
    as the cooldown/budget anchor so a retry scan cannot re-fire the same event.
    """
    key = str(decision["decision_key"])
    allow = int(bool(decision["allow"]))
    with _connect(db_path) as conn:
        existing = conn.execute(
            f"SELECT allow FROM {DECISIONS_TABLE} WHERE decision_key=?", (key,)
        ).fetchone()
        if existing is not None and int(existing["allow"]) == 1 and allow != 1:
            row = conn.execute(
                f"SELECT * FROM {DECISIONS_TABLE} WHERE decision_key=?", (key,)
            ).fetchone()
            conn.commit()
            return {**decision, "audit_row": dict(row)}
        conn.execute(
            f"INSERT OR REPLACE INTO {DECISIONS_TABLE} "
            "(decision_key, chat_id, kind, allow, local_date, decided_at, "
            " reasons, blocked_by, relevance, metadata) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                key,
                str(decision.get("chat_id") or ""),
                str(decision["kind"]),
                allow,
                str(decision["local_date"]),
                str(decision["decided_at"]),
                json.dumps(decision.get("reasons") or []),
                json.dumps(decision.get("blocked_by") or []),
                json.dumps(decision.get("relevance") or {}),
                json.dumps({k: v for k, v in decision.items()
                            if k not in ("decision_key", "kind", "allow",
                                         "local_date", "decided_at", "reasons",
                                         "blocked_by", "relevance", "chat_id")}),
            ),
        )
        conn.commit()
        row = conn.execute(
            f"SELECT * FROM {DECISIONS_TABLE} WHERE decision_key=?", (key,)
        ).fetchone()
    return {**decision, "audit_row": dict(row)}


# ---------------------------------------------------------------------------
# bounded backlog escalation (Phase 12)
# ---------------------------------------------------------------------------

DAY_START = coaching_planner.DAY_START
DAY_END = coaching_planner.DAY_END

HORIZON_DAYS = 7
CRITICAL_BACKLOG_COUNT = 12
CRITICAL_BACKLOG_HOURS = 10.0
CRITICAL_GROWTH_7D = 6
GROWING_MIN_COUNT = 6
MIN_ADHERENCE_ITEMS = 3
COVERAGE_HIGH_HOURS = 4.0
LOW_ADHERENCE_PCT = 60

LEVEL_BOOST_MINUTES = {
    "normal": 0,
    "growing": 15,
    "critical": 30,
    "impossible": 0,
}

LEVEL_RECOMMENDATION = {
    "normal": (
        "Backlog is under control; keep the current daily budget. "
        "No escalation needed."
    ),
    "growing": (
        "Backlog is growing. Add the suggested study minutes only if the daily "
        "commitment budget has headroom; otherwise re-prioritise the queue."
    ),
    "critical": (
        "Backlog is critical: the suggested extra minutes are already bounded "
        "by max daily committed minutes and today's day-window. Prefer "
        "re-scoping over piling on more time."
    ),
    "impossible": (
        "No realistic minute increase clears this backlog inside the week. "
        "Re-scope, defer, or discard items; adding time will not fix the queue."
    ),
}

_BACKLOG_STATUSES = ("Backlog", "Inbox")


def _created_day(row: dict[str, Any]) -> dt.date | None:
    stamp = _parse_ts(row.get("created_time"))
    if stamp is None:
        return None
    return stamp.astimezone(session_context._tz()).date()


def _edited_day(row: dict[str, Any]) -> dt.date | None:
    stamp = _parse_ts(row.get("last_edited_time"))
    if stamp is None:
        return None
    return stamp.astimezone(session_context._tz()).date()


def _backlog_rows(*, db_path: str | Path) -> list[dict[str, Any]]:
    return study_domain._rows(
        "work_items",
        "archived=0 AND status IN ('Backlog','Inbox')",
        db_path=db_path,
    )


def _growth_window(
    today: dt.date, window_days: int, rows: list[dict[str, Any]],
    resolved_rows: list[dict[str, Any]], *, db_path: str | Path,
) -> dict[str, int] | None:
    """Net backlog growth over a trailing window where evidence exists."""
    start = today - dt.timedelta(days=window_days - 1)
    added = sum(1 for row in rows
                if (created := _created_day(row)) is not None and start <= created <= today)
    resolved = sum(1 for row in resolved_rows
                   if (edited := _edited_day(row)) is not None and start <= edited <= today)
    evidence = any(_created_day(row) is not None for row in rows) or bool(resolved)
    if not evidence:
        return None
    return {"added": added, "resolved": resolved, "net": added - resolved}


def _committed_duration_minutes(*, db_path: str | Path) -> int:
    goals = study_domain._rows(
        "goals", "archived=0 AND status='Active' AND period='Daily'",
        db_path=db_path,
    )
    return int(sum(
        float(goal.get("target") or 0)
        for goal in goals
        if str(goal.get("goal_type") or "").lower() == "duration"
    ))


def _plan_adherence(
    today: dt.date, *, window_days: int = HORIZON_DAYS, db_path: str | Path,
) -> dict[str, Any]:
    start = (today - dt.timedelta(days=window_days - 1)).isoformat()
    rows = study_domain._rows(
        "daily_plan",
        "archived=0 AND plan_date IS NOT NULL "
        "AND substr(plan_date,1,10) BETWEEN ? AND ?",
        (start, today.isoformat()), db_path=db_path,
    )
    met = 0
    considered = 0
    days_considered: set[str] = set()
    for row in rows:
        status = str(row.get("status") or "").strip().lower()
        if status == "moved":
            continue
        plan_date = str(row.get("plan_date") or "")[:10]
        days_considered.add(plan_date)
        if status == "completed":
            met += 1
        considered += 1
    pct = round(100 * met / considered) if considered >= MIN_ADHERENCE_ITEMS else None
    return {
        "adherence_pct": pct,
        "met": met,
        "considered": considered,
        "verified_days": len(days_considered),
    }


def backlog_escalation(
    *, today: str | None = None, db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Deterministic backlog verdict + bounded escalation. Never writes a plan.

    Metrics, plan adherence/capacity, coverage risk, a four-level verdict, and
    a suggested daily minute increase bounded by max committed minutes, the
    day-window and today's planned load. Pure decision; callers render it.
    """
    today = dt.date.fromisoformat((today or session_context.local_today_iso())[:10])
    rows = _backlog_rows(db_path=db_path)
    resolved_rows = study_domain._rows(
        "work_items",
        "archived=0 AND status IN ('Completed','Dismissed')",
        db_path=db_path,
    )

    count = len(rows)
    estimated_minutes = int(sum(
        int(row.get("estimated_min") or 0)
        for row in rows
        if (row.get("estimated_min") or 0) is not None
        and int(row.get("estimated_min") or 0) > 0
    ))
    estimated_hours = round(estimated_minutes / 60, 1)
    unknown_estimates = sum(1 for row in rows if not (row.get("estimated_min") or 0))

    ages = [
        (today - created).days
        for row in rows
        if (created := _created_day(row)) is not None
    ]
    age_metrics = {
        "oldest_days": max(ages) if ages else None,
        "median_days": round(statistics.median(ages), 1) if ages else None,
        "mean_days": round(statistics.mean(ages), 1) if ages else None,
        "unknown_age": count - len(ages),
    }

    growth_3d = _growth_window(today, 3, rows, resolved_rows, db_path=db_path)
    growth_7d = _growth_window(today, 7, rows, resolved_rows, db_path=db_path)

    adherence = _plan_adherence(today, db_path=db_path)
    facts = study_domain.plan_facts(today.isoformat(), db_path=db_path)
    planned_minutes_today = int(facts.get("planned_minutes") or 0)
    unplanned_backlog = int(facts.get("unplanned_backlog_count") or 0)
    due_revision = int(facts.get("due_revision_count") or 0)
    capacity_headroom_cy = float(facts.get("capacity_headroom_cy") or 0)

    try:
        max_daily = settings.max_daily_committed_minutes()
    except Exception:
        max_daily = 600
    committed_minutes = _committed_duration_minutes(db_path=db_path)
    day_window_minutes = _parse_hhmm(DAY_END) - _parse_hhmm(DAY_START)
    headroom_commitments = max(0, max_daily - committed_minutes)
    headroom_day = max(0, day_window_minutes - planned_minutes_today)

    if unplanned_backlog and estimated_hours >= COVERAGE_HIGH_HOURS and (
        adherence["adherence_pct"] is None
        or adherence["adherence_pct"] < LOW_ADHERENCE_PCT
    ):
        coverage_risk = "high"
    elif unplanned_backlog:
        coverage_risk = "medium"
    else:
        coverage_risk = "low"

    net_3d = growth_3d["net"] if growth_3d else None
    net_7d = growth_7d["net"] if growth_7d else None

    if estimated_minutes > HORIZON_DAYS * max_daily:
        level = "impossible"
    elif (
        count >= CRITICAL_BACKLOG_COUNT
        or estimated_hours >= CRITICAL_BACKLOG_HOURS
        or (net_7d is not None and net_7d >= CRITICAL_GROWTH_7D)
        or (coverage_risk == "high" and headroom_day == 0)
    ):
        level = "critical"
    elif (
        (net_3d is not None and net_3d > 0)
        or (net_7d is not None and net_7d > 0)
        or (
            count >= GROWING_MIN_COUNT and adherence["adherence_pct"] is not None
            and adherence["adherence_pct"] < LOW_ADHERENCE_PCT
        )
    ):
        level = "growing"
    else:
        level = "normal"

    candidate = LEVEL_BOOST_MINUTES[level]
    suggested = min(candidate, headroom_commitments, headroom_day)
    bounded_by: list[str] = []
    if candidate > 0:
        if suggested == headroom_commitments and headroom_commitments < candidate:
            bounded_by.append("max_daily_committed_minutes")
        if suggested == headroom_day and headroom_day < candidate:
            bounded_by.append("day_window")
    if level == "impossible":
        bounded_by.append("unachievable_horizon")
    if not bounded_by and suggested < candidate:
        bounded_by.append("level_boost")
    bounded_by = sorted(set(bounded_by))

    reasons: list[str] = [
        f"{count} backlog item(s), ~{estimated_hours:g}h estimated"
        if estimated_minutes else f"{count} backlog item(s), estimates unknown",
    ]
    if age_metrics["oldest_days"] is not None:
        reasons.append(f"oldest item {age_metrics['oldest_days']} day(s) old")
    if net_3d is not None:
        reasons.append(f"3-day net growth {net_3d:+d}")
    if net_7d is not None:
        reasons.append(f"7-day net growth {net_7d:+d}")
    if adherence["adherence_pct"] is not None:
        reasons.append(
            f"{adherence['adherence_pct']}% plan adherence "
            f"({adherence['met']}/{adherence['considered']} items, "
            f"{adherence['verified_days']} day(s))"
        )
    reasons.append(
        f"coverage risk {coverage_risk} "
        f"({unplanned_backlog} unplanned backlog, {due_revision} due revisions)"
    )
    reasons.append(
        f"headroom {min(headroom_commitments, headroom_day)} min "
        f"(commitments {committed_minutes}/{max_daily}, "
        f"planned today {planned_minutes_today}, "
        f"day window {DAY_START}-{DAY_END})"
    )

    return {
        "policy": "backlog_escalation",
        "as_of": today.isoformat(),
        "level": level,
        "metrics": {
            "count": count,
            "age_days": age_metrics,
            "estimated_minutes": estimated_minutes,
            "estimated_hours": estimated_hours,
            "unknown_estimate_count": unknown_estimates,
            "growth_3d": growth_3d,
            "growth_7d": growth_7d,
        },
        "plan": {
            "adherence_pct": adherence["adherence_pct"],
            "verified_days": adherence["verified_days"],
            "planned_minutes_today": planned_minutes_today,
            "committed_minutes": committed_minutes,
            "max_daily_minutes": max_daily,
            "headroom_minutes": min(headroom_commitments, headroom_day),
            "day_window_minutes": day_window_minutes,
            "capacity_headroom_cy": capacity_headroom_cy,
            "unplanned_backlog_count": unplanned_backlog,
            "due_revision_count": due_revision,
            "coverage_risk": coverage_risk,
        },
        "escalation": {
            "candidate_minutes": candidate,
            "suggested_minutes": suggested,
            "bounded_by": sorted(set(bounded_by)),
            "respects_max_daily_minutes": suggested <= headroom_commitments,
            "respects_day_window": suggested <= headroom_day,
            "no_automatic_plan_write": True,
        },
        "reasons": reasons,
        "recommendation": LEVEL_RECOMMENDATION[level],
        "sends_nothing": True,
    }
