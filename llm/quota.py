"""Durable, concurrency-safe quota accounting and provider health state.

The ledger lives in the main SQLite database so the normal nightly backup also
protects routing history.  Static catalog limits are estimates until a provider
returns authoritative remaining/reset headers; those observations immediately
become the source of truth for the active window.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import email.utils
import re
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any, Optional

from .registry import Quota, Route


DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "sqlite_mirror.db"
_LOCAL = threading.local()


@dataclasses.dataclass(frozen=True)
class Reservation:
    id: str
    route_id: str
    estimated_tokens: int
    advisory: bool = False
    reason: str | None = None


@dataclasses.dataclass(frozen=True)
class Availability:
    usable: bool
    advisory: bool
    reason: str | None = None


def _connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path or DEFAULT_DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS llm_quota_windows (
            route_id TEXT NOT NULL,
            metric TEXT NOT NULL,
            scope TEXT NOT NULL,
            window_start TEXT NOT NULL,
            reset_at TEXT NOT NULL,
            limit_value INTEGER NOT NULL,
            used INTEGER NOT NULL DEFAULT 0,
            reserved INTEGER NOT NULL DEFAULT 0,
            authoritative_remaining INTEGER,
            confidence TEXT NOT NULL DEFAULT 'estimated',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (route_id, metric, scope)
        );
        CREATE TABLE IF NOT EXISTS llm_route_state (
            route_id TEXT PRIMARY KEY,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            cooldown_until TEXT,
            disabled INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            last_success_at TEXT,
            last_route_at TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS llm_requests (
            id TEXT PRIMARY KEY,
            route_id TEXT NOT NULL,
            purpose TEXT NOT NULL,
            status TEXT NOT NULL,
            estimated_tokens INTEGER NOT NULL,
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            reason TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_llm_requests_route_time
            ON llm_requests(route_id, created_at);
        """
    )
    conn.commit()


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat()


def _parse_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
        return parsed.replace(tzinfo=dt.timezone.utc) if parsed.tzinfo is None else parsed
    except ValueError:
        return None


def _window(scope: str, now: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    now = now.astimezone(dt.timezone.utc)
    if scope in ("rpm", "tpm"):
        start = now.replace(second=0, microsecond=0)
        return start, start + dt.timedelta(minutes=1)
    if scope == "rpd":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + dt.timedelta(days=1)
    if scope == "monthly":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1)
        else:
            end = start.replace(month=start.month + 1)
        return start, end
    raise ValueError(f"unsupported quota scope {scope!r}")


def estimate_tokens(messages: list[dict[str, str]], max_output_tokens: int) -> int:
    """Conservative dependency-free estimate used before provider usage exists."""
    chars = sum(len(str(m.get("content", ""))) for m in messages)
    prompt = max(1, (chars + 2) // 3)  # deliberately safer than chars/4
    return prompt + max(0, int(max_output_tokens))


def _threshold(quota: Quota) -> int:
    raw = quota.switch_at
    return max(1, int(quota.limit * raw)) if raw <= 1 else int(raw)


def _amount(quota: Quota, estimated_tokens: int) -> int:
    return estimated_tokens if quota.metric == "tokens" else 1


def _state(conn: sqlite3.Connection, route_id: str, now: dt.datetime) -> Availability:
    row = conn.execute(
        "SELECT * FROM llm_route_state WHERE route_id=?", (route_id,)
    ).fetchone()
    if not row:
        return Availability(True, False)
    if row["disabled"]:
        return Availability(False, False, row["last_error"] or "disabled")
    cooldown = _parse_time(row["cooldown_until"])
    if cooldown and cooldown > now:
        return Availability(False, False, f"cooldown_until={_iso(cooldown)}")
    return Availability(True, False)


def availability(
    route: Route, estimated_tokens: int, db_path: str | Path | None = None,
    *, now: dt.datetime | None = None,
) -> Availability:
    now = now or _utcnow()
    with _connect(db_path) as conn:
        state = _state(conn, route.id, now)
        if not state.usable:
            return state
        advisory_reasons: list[str] = []
        for q in route.quotas:
            start, reset = _window(q.scope, now)
            row = conn.execute(
                "SELECT * FROM llm_quota_windows WHERE route_id=? AND metric=? AND scope=?",
                (route.id, q.metric, q.scope),
            ).fetchone()
            used = reserved = 0
            remaining: int | None = None
            limit_value = q.limit
            if row and _parse_time(row["reset_at"]) and _parse_time(row["reset_at"]) > now:
                used, reserved = int(row["used"]), int(row["reserved"])
                remaining = row["authoritative_remaining"]
                limit_value = int(row["limit_value"])
            amount = _amount(q, estimated_tokens)
            if remaining is not None and amount > int(remaining) - reserved:
                return Availability(False, False, f"{q.scope}_authoritative_exhausted")
            observed_used = max(used, limit_value - int(remaining)) if remaining is not None else used
            threshold = max(1, int(limit_value * q.switch_at)) if q.switch_at <= 1 else int(q.switch_at)
            if observed_used + reserved + amount > threshold:
                advisory_reasons.append(f"{q.scope}_reserve")
        return Availability(True, bool(advisory_reasons), ",".join(advisory_reasons) or None)


def reserve(
    route: Route, purpose: str, estimated_tokens: int,
    db_path: str | Path | None = None, *, now: dt.datetime | None = None,
) -> Reservation | None:
    """Atomically reserve all route quota dimensions; None means hard blocked."""
    now = now or _utcnow()
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        stale_before = _iso(now - dt.timedelta(minutes=5))
        stale = conn.execute(
            "SELECT id,estimated_tokens FROM llm_requests WHERE route_id=? AND status='reserved' AND created_at<?",
            (route.id, stale_before),
        ).fetchall()
        if stale:
            for q in route.quotas:
                released = sum(_amount(q, int(row["estimated_tokens"])) for row in stale)
                conn.execute(
                    """UPDATE llm_quota_windows SET reserved=MAX(0,reserved-?),updated_at=?
                       WHERE route_id=? AND metric=? AND scope=?""",
                    (released, _iso(now), route.id, q.metric, q.scope),
                )
            conn.executemany(
                "UPDATE llm_requests SET status='abandoned',reason='stale_reservation',completed_at=? WHERE id=?",
                [(_iso(now), row["id"]) for row in stale],
            )
        state = _state(conn, route.id, now)
        if not state.usable:
            conn.rollback()
            return None
        advisory: list[str] = []
        rows: list[tuple[Quota, dt.datetime, dt.datetime, int, int, int | None, str, int]] = []
        for q in route.quotas:
            start, reset = _window(q.scope, now)
            old = conn.execute(
                "SELECT * FROM llm_quota_windows WHERE route_id=? AND metric=? AND scope=?",
                (route.id, q.metric, q.scope),
            ).fetchone()
            used = reserved_count = 0
            remaining: int | None = None
            confidence = q.confidence
            if old and _parse_time(old["reset_at"]) and _parse_time(old["reset_at"]) > now:
                used = int(old["used"])
                reserved_count = int(old["reserved"])
                remaining = old["authoritative_remaining"]
                confidence = str(old["confidence"])
            amount = _amount(q, estimated_tokens)
            if remaining is not None and amount > int(remaining) - reserved_count:
                conn.rollback()
                return None
            limit_value = int(old["limit_value"]) if old and _parse_time(old["reset_at"]) and _parse_time(old["reset_at"]) > now else q.limit
            observed_used = max(used, limit_value - int(remaining)) if remaining is not None else used
            threshold = max(1, int(limit_value * q.switch_at)) if q.switch_at <= 1 else int(q.switch_at)
            if observed_used + reserved_count + amount > threshold:
                advisory.append(f"{q.scope}_reserve")
            rows.append((q, start, reset, used, reserved_count, remaining, confidence, limit_value))

        rid = uuid.uuid4().hex
        stamp = _iso(now)
        for q, start, reset, used, reserved_count, remaining, confidence, limit_value in rows:
            amount = _amount(q, estimated_tokens)
            conn.execute(
                """INSERT INTO llm_quota_windows
                   (route_id,metric,scope,window_start,reset_at,limit_value,used,reserved,
                    authoritative_remaining,confidence,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(route_id,metric,scope) DO UPDATE SET
                    window_start=excluded.window_start, reset_at=excluded.reset_at,
                    limit_value=excluded.limit_value, used=excluded.used,
                    reserved=excluded.reserved, authoritative_remaining=excluded.authoritative_remaining,
                    confidence=excluded.confidence, updated_at=excluded.updated_at""",
                (route.id, q.metric, q.scope, _iso(start), _iso(reset), limit_value,
                 used, reserved_count + amount, remaining, confidence, stamp),
            )
        conn.execute(
            "INSERT INTO llm_requests(id,route_id,purpose,status,estimated_tokens,created_at) VALUES(?,?,?,?,?,?)",
            (rid, route.id, purpose, "reserved", estimated_tokens, stamp),
        )
        conn.commit()
        return Reservation(rid, route.id, estimated_tokens, bool(advisory), ",".join(advisory) or None)
    finally:
        conn.close()


def _header(headers: dict[str, str], name: str | None) -> str | None:
    if not name:
        return None
    lower = {str(k).lower(): str(v) for k, v in headers.items()}
    return lower.get(name.lower())


def _reset_from_header(raw: str | None, now: dt.datetime) -> dt.datetime | None:
    if not raw:
        return None
    raw = raw.strip()
    try:
        number = float(raw)
        if number > 10_000_000_000:  # milliseconds epoch
            number /= 1000
        if number > 1_000_000_000:  # seconds epoch
            return dt.datetime.fromtimestamp(number, dt.timezone.utc)
        return now + dt.timedelta(seconds=max(0, number))
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
        if parsed is not None:
            return parsed.replace(tzinfo=dt.timezone.utc) if parsed.tzinfo is None else parsed
    except (TypeError, ValueError, OverflowError):
        pass
    total = 0.0
    for value, unit in re.findall(r"([0-9]+(?:\.[0-9]+)?)(ms|[dhms])", raw.lower()):
        factor = {"d": 86400, "h": 3600, "m": 60, "s": 1, "ms": .001}[unit]
        total += float(value) * factor
    return now + dt.timedelta(seconds=total) if total else None


def reconcile(
    reservation: Reservation, route: Route, *, success: bool,
    prompt_tokens: int | None = None, completion_tokens: int | None = None,
    rate_headers: dict[str, str] | None = None, reason: str | None = None,
    db_path: str | Path | None = None, now: dt.datetime | None = None,
) -> None:
    now = now or _utcnow()
    actual_tokens = (prompt_tokens or 0) + (completion_tokens or 0)
    if not actual_tokens:
        actual_tokens = reservation.estimated_tokens
    headers = rate_headers or {}
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for q in route.quotas:
            amount_reserved = _amount(q, reservation.estimated_tokens)
            amount_used = _amount(q, actual_tokens)
            row = conn.execute(
                "SELECT * FROM llm_quota_windows WHERE route_id=? AND metric=? AND scope=?",
                (route.id, q.metric, q.scope),
            ).fetchone()
            if not row:
                continue
            used = int(row["used"]) + amount_used
            reserved_count = max(0, int(row["reserved"]) - amount_reserved)
            remaining_raw = _header(headers, q.header_remaining)
            limit_raw = _header(headers, q.header_limit)
            reset_raw = _header(headers, q.header_reset)
            remaining = row["authoritative_remaining"]
            limit_value = int(row["limit_value"])
            confidence = str(row["confidence"])
            reset_at = str(row["reset_at"])
            try:
                if remaining_raw is not None:
                    remaining = max(0, int(float(remaining_raw)))
                    confidence = "exact"
                if limit_raw is not None:
                    limit_value = max(1, int(float(limit_raw)))
                    confidence = "exact"
            except ValueError:
                pass
            parsed_reset = _reset_from_header(reset_raw, now)
            if parsed_reset:
                reset_at = _iso(parsed_reset)
                confidence = "exact"
            conn.execute(
                """UPDATE llm_quota_windows SET used=?,reserved=?,limit_value=?,
                   authoritative_remaining=?,confidence=?,reset_at=?,updated_at=?
                   WHERE route_id=? AND metric=? AND scope=?""",
                (used, reserved_count, limit_value, remaining, confidence, reset_at, _iso(now),
                 route.id, q.metric, q.scope),
            )
        conn.execute(
            """UPDATE llm_requests SET status=?,prompt_tokens=?,completion_tokens=?,reason=?,completed_at=?
               WHERE id=?""",
            ("success" if success else "failed", prompt_tokens, completion_tokens,
             reason, _iso(now), reservation.id),
        )
        conn.commit()
    finally:
        conn.close()


def record_route_result(
    route_id: str, *, success: bool, reason: str | None = None,
    retry_after: str | None = None, db_path: str | Path | None = None,
    now: dt.datetime | None = None,
) -> None:
    now = now or _utcnow()
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        old = conn.execute(
            "SELECT consecutive_failures FROM llm_route_state WHERE route_id=?", (route_id,)
        ).fetchone()
        failures = 0 if success else (int(old[0]) if old else 0) + 1
        disabled = int(not success and bool(reason and reason.startswith(("auth_401", "auth_402", "auth_403"))))
        cooldown: dt.datetime | None = None
        if not success and reason == "429":
            cooldown = _reset_from_header(retry_after, now) or now + dt.timedelta(minutes=1)
        elif not success and failures >= 2:
            cooldown = now + dt.timedelta(seconds=min(900, 30 * (2 ** min(failures - 2, 5))))
        conn.execute(
            """INSERT INTO llm_route_state
               (route_id,consecutive_failures,cooldown_until,disabled,last_error,last_success_at,last_route_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(route_id) DO UPDATE SET
                consecutive_failures=excluded.consecutive_failures,
                cooldown_until=excluded.cooldown_until, disabled=excluded.disabled,
                last_error=excluded.last_error,
                last_success_at=COALESCE(excluded.last_success_at,llm_route_state.last_success_at),
                last_route_at=excluded.last_route_at, updated_at=excluded.updated_at""",
            (route_id, failures, _iso(cooldown) if cooldown else None, disabled,
             None if success else reason, _iso(now) if success else None, _iso(now), _iso(now)),
        )
        conn.commit()


def record_unmetered_request(
    route_id: str, purpose: str, estimated_tokens: int, *, success: bool,
    reason: str | None = None, db_path: str | Path | None = None,
    now: dt.datetime | None = None,
) -> None:
    """Record activity for a route without a mappable provider quota."""
    now = now or _utcnow()
    stamp = _iso(now)
    with _connect(db_path) as conn:
        conn.execute(
            """INSERT INTO llm_requests
               (id,route_id,purpose,status,estimated_tokens,reason,created_at,completed_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (uuid.uuid4().hex, route_id, purpose, "success" if success else "failed",
             estimated_tokens, reason, stamp, stamp),
        )
        conn.commit()


def health(db_path: str | Path | None = None) -> dict[str, Any]:
    now = _utcnow()
    with _connect(db_path) as conn:
        states = {r["route_id"]: dict(r) for r in conn.execute("SELECT * FROM llm_route_state")}
        windows = [dict(r) for r in conn.execute(
            "SELECT * FROM llm_quota_windows ORDER BY route_id,metric,scope"
        )]
        last = conn.execute(
            "SELECT route_id,status,reason,completed_at,created_at FROM llm_requests ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    for row in windows:
        remaining = row["authoritative_remaining"]
        if remaining is None:
            remaining = max(0, int(row["limit_value"]) - int(row["used"]) - int(row["reserved"]))
        else:
            remaining = max(0, int(remaining) - int(row["reserved"]))
        row["remaining"] = remaining
    for state in states.values():
        cooldown = _parse_time(state.get("cooldown_until"))
        state["available"] = not bool(state.get("disabled")) and not bool(cooldown and cooldown > now)
    active = None
    if last:
        active = {"route_id": last["route_id"], "status": last["status"],
                  "reason": last["reason"], "at": last["completed_at"] or last["created_at"]}
    return {"active": active, "states": states, "windows": windows, "now": _iso(now)}


def reset_route(route_id: str, db_path: str | Path | None = None) -> None:
    """Administrative recovery after a key/subscription has been corrected."""
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM llm_route_state WHERE route_id=?", (route_id,))
        conn.commit()
