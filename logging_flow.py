"""
Phase 7 — Structured entry / logging flow with confirm step.

Pure logic layer (no Telegram): turns a validated log_* Intent + the effective
session context into a WritePlan that the bot renders as a confirm preview and,
on confirm, commits to Notion (then triggers an incremental sync).

Responsibilities
----------------
1. Merge parsed fields with session context (subject/chapter/block).
2. Normalise + validate against the REAL Notion option lists (select/status),
   with light fuzzy tolerance ("physics" -> "PHYSICS", "eb1" -> "EB-1").
   Anything that can't be confidently mapped becomes a clarification question
   rather than a bad write.
3. Resolve relation properties (Chapter, Ledger Entry, Logged Errors) by fuzzy
   matching names against the mirrored SQLite tables -> Notion page_id. Never
   ask the user for a raw page id.
4. Render a field-by-field preview using the EXACT Notion property names.
5. commit(): create the page; if a ledger entry carries a doubt, cross-log it
   to the Doubts DB and link it back via the Logged Errors relation; then run an
   incremental sync so the write is queryable immediately.
6. On Notion failure (down / rate-limited) enqueue the write locally and report
   "saved locally, syncing shortly" (Phase 9 edge case).

The confirm/edit/cancel UI and pending-write bookkeeping live in bot.py.
"""

from __future__ import annotations

import datetime as dt
import difflib
import json
import logging
import math
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import notion_client_wrapper as notion
import operational_store
import session_context
import sync
from config import notion_schema

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = PROJECT_ROOT / "sqlite_mirror.db"
WRITE_QUEUE_TABLE = "pending_writes"
MAX_PENDING_ATTEMPTS = 5

# action -> target DB key
ACTION_DB = {
    "log_execution": "ledger",
    "log_doubt": "doubts",
    "log_revision": "revision",
}

# Title (required) property human_name per DB.
TITLE_KEY = {
    "ledger": "task",
    "doubts": "core_concept",
    "revision": "chapter_module",
}

# Which session-context keys map onto a writable property, per DB.
# (On Doubts, subject/chapter are read-only rollups, so context can't be
# written there — it only rides along via the Ledger Entry link.)
CONTEXT_TO_FIELD = {
    "ledger": {"subject": "subject", "chapter": "chapter", "block": "block",
               "exercise": "exercise_type"},
    "doubts": {},
    "revision": {"subject": "subject"},
}

_FUZZY_THRESHOLD = 0.6


class LoggingError(RuntimeError):
    """Unexpected failure building or committing a write plan."""


@dataclass
class WritePlan:
    db_key: str
    action: str
    # human_name -> value, ready for notion.create_page (writable only).
    properties: dict[str, Any] = field(default_factory=dict)
    preview_lines: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    needs_clarification: bool = False
    clarification_question: Optional[str] = None
    # For a ledger entry that also states a doubt: the doubt text to cross-log.
    cross_log_doubt: Optional[str] = None
    # human_name -> display string for relation page_ids (for a readable preview).
    resolved_names: dict[str, str] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        """Serialisable form for stashing a pending write (bot callback data)."""
        return {
            "db_key": self.db_key,
            "action": self.action,
            "properties": self.properties,
            "cross_log_doubt": self.cross_log_doubt,
            "operation_id": uuid.uuid4().hex,
        }


# ---------------------------------------------------------------------------
# Option / value normalisation
# ---------------------------------------------------------------------------

def _norm(s: str) -> str:
    """Loose normalisation for matching: lowercase, collapse non-alnum."""
    return re.sub(r"[^a-z0-9]+", "", str(s).lower())


def normalise_option(value: Any, options: list[str]) -> Optional[str]:
    """Map a loose value onto one of the exact allowed option strings.

    Returns the exact option, or None if no confident match. Tries, in order:
    exact, case-insensitive, normalised (ignoring spaces/punct), then difflib.
    """
    if value is None:
        return None
    sval = str(value).strip()
    if sval in options:
        return sval
    lower_map = {o.lower(): o for o in options}
    if sval.lower() in lower_map:
        return lower_map[sval.lower()]
    norm_map = {_norm(o): o for o in options}
    if _norm(sval) in norm_map:
        return norm_map[_norm(sval)]
    close = difflib.get_close_matches(_norm(sval), list(norm_map.keys()), n=1, cutoff=0.8)
    if close:
        return norm_map[close[0]]
    return None


def _resolve_date(value: Any) -> Optional[str]:
    """Resolve a date value to an ISO date string in the user's timezone.

    Accepts "today", ISO date/datetime, or "in N days". Returns None if it
    can't be parsed (caller decides whether that's fatal).
    """
    if value is None:
        return None
    if isinstance(value, str):
        v = value.strip().lower()
        if v == "today":
            return session_context.local_today_iso()
        if v in ("tomorrow",):
            return (session_context.local_now().date() + dt.timedelta(days=1)).isoformat()
        m = re.match(r"in\s+(\d+)\s+days?", v)
        if m:
            return (session_context.local_now().date() + dt.timedelta(days=int(m.group(1)))).isoformat()
        # ISO date or datetime?
        try:
            dt.date.fromisoformat(value[:10])
            return value[:10]
        except ValueError:
            return None
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    return None


# ---------------------------------------------------------------------------
# Relation resolution against the SQLite mirror
# ---------------------------------------------------------------------------

def resolve_relation(
    target_db_key: str,
    name: str,
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> tuple[Optional[str], list[str]]:
    """Fuzzy-match a name against a related DB's titles in the mirror.

    Returns (page_id or None, candidate_titles). A None page_id with candidates
    means "ambiguous / not found" — caller should ask the user.
    """
    title_col = TITLE_KEY[target_db_key]
    table = sync.DB_TABLES[target_db_key]
    # Drafts keep already-resolved Notion IDs. Accept an active exact ID before
    # applying title fuzzy matching so editing an unrelated field does not
    # invalidate existing relations.
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        by_id = conn.execute(
            f'SELECT notion_page_id FROM "{table}" WHERE notion_page_id = ? AND archived = 0',
            (str(name),),
        ).fetchone()
    if by_id is not None:
        return str(by_id["notion_page_id"]), []
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f'SELECT notion_page_id, "{title_col}" AS title '
            f'FROM "{table}" WHERE archived = 0'
        ).fetchall()

    titles = [(r["notion_page_id"], (r["title"] or "").strip()) for r in rows if r["title"]]
    if not titles:
        return None, []

    nquery = _norm(name)
    # 1. exact normalised match
    for pid, title in titles:
        if _norm(title) == nquery:
            return pid, [title]
    # 2. substring (query in title or title in query)
    subs = [(pid, title) for pid, title in titles if nquery and (nquery in _norm(title) or _norm(title) in nquery)]
    if len(subs) == 1:
        return subs[0][0], [subs[0][1]]
    if len(subs) > 1:
        return None, [t for _, t in subs]
    # 3. difflib best match
    norm_titles = {_norm(t): (pid, t) for pid, t in titles}
    close = difflib.get_close_matches(nquery, list(norm_titles.keys()), n=3, cutoff=_FUZZY_THRESHOLD)
    if len(close) == 1:
        return norm_titles[close[0]][0], [norm_titles[close[0]][1]]
    if close:
        return None, [norm_titles[c][1] for c in close]
    return None, []


# ---------------------------------------------------------------------------
# Build the write plan
# ---------------------------------------------------------------------------

def build_write_plan(
    intent: Any,
    chat_id: int | str,
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    first_round: bool = True,
) -> WritePlan:
    """Turn a validated log_* Intent into a WritePlan (no writes performed)."""
    action = intent.action
    if action not in ACTION_DB:
        raise LoggingError(f"build_write_plan called with non-log action {action!r}")
    db_key = ACTION_DB[action]
    schema = notion_schema.PROPERTIES_BY_DB[db_key]
    plan = WritePlan(db_key=db_key, action=action)

    # 1. Start from parsed fields (already human names), keep only known writable.
    raw: dict[str, Any] = dict(getattr(intent, "fields", {}) or {})

    # 2. Inject session context onto the writable fields for this DB.
    effective_ctx = session_context.merge_into_intent(intent, chat_id)
    for ctx_key, field_name in CONTEXT_TO_FIELD[db_key].items():
        if not raw.get(field_name) and effective_ctx.get(ctx_key):
            raw[field_name] = effective_ctx[ctx_key]

    # 3. Pull a doubt off a ledger entry for cross-logging, before validation.
    if db_key == "ledger":
        doubt_text = raw.get("doubts")
        if doubt_text:
            plan.cross_log_doubt = str(doubt_text)

    # 4. Defaults.
    if db_key == "ledger" and not raw.get("date"):
        raw["date"] = "today"

    # 4a. Auto-fill session time from the context timer when the user didn't
    # state it. Bounded to 1..480 min so a stale timer can't produce nonsense.
    if db_key == "ledger" and not raw.get("actual_time_min"):
        elapsed = session_context.elapsed_minutes(chat_id)
        if elapsed is not None and 1 <= elapsed <= 480:
            raw["actual_time_min"] = round(elapsed)
            plan.warnings.append(
                f"time auto-filled from session timer ({round(elapsed)} min) "
                "— tap Edit if wrong"
            )

    # 4b. On Doubts, subject/chapter/exercise are read-only rollups fed by the
    # Ledger Entry relation — without a link the doubt is unfindable by
    # subject. Pin it to the ledger session it most likely came from.
    if db_key == "doubts" and not raw.get("ledger_entry"):
        outcome, value = _suggest_ledger_link(raw, effective_ctx, db_path=db_path)
        if outcome == "link":
            raw["ledger_entry"] = value
        elif outcome == "ambiguous":
            plan.needs_clarification = True
            plan.clarification_question = (
                "Ledger Entry: which session did this doubt come from? "
                f"Did you mean: {', '.join(value)}?"
            )
            return plan
        else:
            plan.warnings.append(
                "no ledger entry linked — subject/chapter stay empty "
                "until you link it in Notion"
            )

    # 5. Validate/normalise each field against the schema.
    props: dict[str, Any] = {}
    for human_name, value in raw.items():
        prop = schema.get(human_name)
        if prop is None:
            plan.warnings.append(f"ignored unknown field {human_name!r}")
            continue
        if prop["read_only"]:
            plan.warnings.append(f"ignored read-only field {human_name!r}")
            continue
        if value is None or value == "":
            continue
        t = prop["type"]

        if t in ("select", "status"):
            matched = normalise_option(value, prop["options"])
            if matched is None:
                plan.needs_clarification = True
                plan.clarification_question = (
                    f"{prop['notion_name']}: {value!r} isn't a valid option. "
                    f"Choose one of: {', '.join(prop['options'])}."
                )
                return plan
            props[human_name] = matched

        elif t == "relation":
            target = prop.get("relates_to")
            pid, candidates = resolve_relation(target, str(value), db_path=db_path)
            if pid is None:
                plan.needs_clarification = True
                if candidates:
                    plan.clarification_question = (
                        f"{prop['notion_name']}: couldn't pin {value!r}. "
                        f"Did you mean: {', '.join(candidates)}?"
                    )
                else:
                    plan.clarification_question = (
                        f"{prop['notion_name']}: no {target} entry matches {value!r}. "
                        f"Create it in Notion first, or rephrase."
                    )
                return plan
            props[human_name] = pid
            if candidates:
                plan.resolved_names[human_name] = candidates[0]

        elif t == "date":
            iso = _resolve_date(value)
            if iso is None:
                plan.warnings.append(f"dropped unparseable date {human_name}={value!r}")
                continue
            props[human_name] = iso

        elif t == "number":
            try:
                if isinstance(value, bool):
                    raise ValueError("boolean is not a number")
                num = float(value)
                if not math.isfinite(num):
                    plan.needs_clarification = True
                    plan.clarification_question = (
                        f"{prop['notion_name']} must be a finite number. What is the correct value?"
                    )
                    return plan
                props[human_name] = int(num) if num.is_integer() else num
            except (TypeError, ValueError):
                plan.warnings.append(f"dropped non-numeric {human_name}={value!r}")
                continue

        elif t == "checkbox":
            props[human_name] = bool(value)

        else:  # title, rich_text
            props[human_name] = str(value)

    # Impossible numeric states corrupt every downstream metric. Clarify rather
    # than silently capping, coercing or recording a misleading zero.
    if db_key == "ledger":
        attempted = props.get("questions_attempted")
        correct = props.get("questions_correct")
        actual_time = props.get("actual_time_min")
        fractional = next(
            (name for name, value in (
                ("Questions Attempted", attempted),
                ("Questions Correct", correct),
            ) if value is not None and not float(value).is_integer()),
            None,
        )
        if fractional:
            plan.needs_clarification = True
            plan.clarification_question = f"{fractional} must be a whole number. What is the correct count?"
            return plan
        invalid = next(
            (name for name, value in (
                ("Questions Attempted", attempted),
                ("Questions Correct", correct),
                ("Actual Time Spent", actual_time),
            ) if value is not None and value < 0),
            None,
        )
        if invalid:
            plan.needs_clarification = True
            plan.clarification_question = f"{invalid} cannot be negative. What is the correct value?"
            return plan
        if attempted is not None and correct is not None and correct > attempted:
            plan.needs_clarification = True
            plan.clarification_question = (
                f"Correct answers ({correct:g}) cannot exceed attempted ({attempted:g}). "
                "What are the correct counts?"
            )
            return plan
        if actual_time == 0 and (attempted or props.get("exercise_type") == "Theory"):
            plan.needs_clarification = True
            plan.clarification_question = "Actual time must be greater than zero. How many minutes did the block take?"
            return plan

    # 5b. Guided completion: an execution log without results is almost always
    # an omission, not intent — ask once (first round only, so a "none" reply
    # or an explicit skip can't loop forever).
    if action == "log_execution" and first_round:
        attempted = props.get("questions_attempted")
        if attempted is None and props.get("exercise_type") != "Theory":
            plan.needs_clarification = True
            plan.clarification_question = (
                "How many questions did you attempt and how many were correct? "
                "(reply 'none' if this was theory-only)"
            )
            return plan
        if attempted and props.get("questions_correct") is None:
            plan.needs_clarification = True
            plan.clarification_question = (
                f"How many of the {attempted} were correct?"
            )
            return plan

    # 6. Ensure the required title exists; synthesise a sensible one if missing.
    title_key = TITLE_KEY[db_key]
    if not props.get(title_key):
        synth = _synthesise_title(db_key, props, plan)
        if synth is None:
            plan.needs_clarification = True
            plan.clarification_question = (
                f"What should the {schema[title_key]['notion_name']} be?"
            )
            return plan
        props[title_key] = synth

    # Commitment advisor (trigger a): late in the day, an execution log that
    # ignores a still-unmet daily commitment gets a visible warning in the
    # preview — the user confirms with eyes open. Advisory only: any failure
    # here must never block logging.
    if db_key == "ledger":
        try:
            import advisor
            plan.warnings.extend(advisor.log_warnings(props, db_path=db_path))
        except Exception:
            logger.debug("commitment advisor check failed", exc_info=True)

    plan.properties = props
    plan.preview_lines = _render_preview(db_key, props, plan.cross_log_doubt, plan.resolved_names)
    return plan


def _suggest_ledger_link(
    raw: dict[str, Any],
    effective_ctx: dict[str, Any],
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> tuple[str, Any]:
    """Pick the ledger entry a new doubt most likely came from.

    Scores recent active ledger rows on subject match (+2), exercise-type
    match (+2, token extracted from the doubt text), and today's date (+1).
    Returns ("link", task_title) for a unique winner, ("ambiguous", [titles])
    for a tie, or ("none", None) when nothing scores.
    """
    subject_hint = effective_ctx.get("subject")

    options = notion_schema.PROPERTIES_BY_DB["ledger"]["exercise_type"]["options"]
    exercise_hint = None
    for token in re.findall(r"[A-Za-z0-9]+", str(raw.get("core_concept") or "")):
        matched = normalise_option(token, options)
        if matched:
            exercise_hint = matched
            break

    today = session_context.local_today_iso()
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT task, date, subject, exercise_type FROM ledger "
            "WHERE archived = 0 AND task IS NOT NULL AND task <> '' "
            "ORDER BY date DESC LIMIT 15"
        ).fetchall()

    scored: list[tuple[int, str]] = []
    for r in rows:
        score = 0
        if (
            subject_hint
            and r["subject"]
            and str(r["subject"]).lower() == str(subject_hint).lower()
        ):
            score += 2
        if exercise_hint and r["exercise_type"] == exercise_hint:
            score += 2
        if r["date"] and str(r["date"])[:10] == today:
            score += 1
        if score > 0:
            scored.append((score, r["task"]))

    if not scored:
        return "none", None
    top = max(s for s, _ in scored)
    winners = [t for s, t in scored if s == top]
    if len(winners) == 1:
        return "link", winners[0]
    return "ambiguous", winners[:5]


def _synthesise_title(db_key: str, props: dict[str, Any], plan: WritePlan) -> Optional[str]:
    """Build a reasonable title when the user didn't state one explicitly."""
    if db_key == "ledger":
        bits = []
        if props.get("exercise_type"):
            bits.append(props["exercise_type"])
        if plan.cross_log_doubt:
            bits.append("(+doubt)")
        base = " ".join(bits).strip()
        return base or "Study session"
    if db_key == "doubts":
        return None  # a doubt with no statement is meaningless — ask.
    if db_key == "revision":
        return None  # revision must name its chapter/module — ask.
    return None


def _render_preview(
    db_key: str,
    props: dict[str, Any],
    cross_log_doubt: Optional[str],
    resolved_names: Optional[dict[str, str]] = None,
) -> list[str]:
    resolved_names = resolved_names or {}
    schema = notion_schema.PROPERTIES_BY_DB[db_key]
    db_title = notion_schema.DATABASES[db_key]["title"]
    lines = [db_title]
    for human_name, value in props.items():
        prop = schema.get(human_name, {})
        label = prop.get("notion_name", human_name)
        shown = value
        if prop.get("type") == "relation":
            shown = str(resolved_names.get(human_name, value))
        lines.append(f"• {label}: {shown}")
    if cross_log_doubt:
        lines.append(f"• ↳ cross-log doubt: {cross_log_doubt}")
    return lines


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------

def commit_write(
    payload: dict[str, Any],
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    do_sync: bool = True,
) -> dict[str, Any]:
    """Create the page(s) in Notion, cross-log, and incremental-sync.

    Returns {"status": "saved"|"queued", "url": ..., "detail": ...}.
    On a Notion outage/rate-limit the write is queued locally and retried later.
    """
    db_key = payload["db_key"]
    properties = dict(payload["properties"])
    operation_id = payload.get("operation_id") or uuid.uuid4().hex
    properties["operation_id"] = operation_id
    cross_log_doubt = payload.get("cross_log_doubt")

    try:
        page = notion.create_page(db_key, properties)
        page_id = page.get("id")
        url = page.get("url")
        local_work_item_id = payload.get("local_work_item_id")
        if db_key == "ledger" and local_work_item_id and page_id:
            operational_store.link_execution(
                str(local_work_item_id), str(page_id), db_path=db_path
            )

        cross_id = None
        cross_url = None
        if cross_log_doubt and db_key == "ledger":
            cross_id, cross_url = _cross_log_doubt(
                page_id, cross_log_doubt, operation_id=f"{operation_id}-doubt"
            )

        if do_sync:
            keys = ["ledger"] if db_key == "ledger" else [db_key]
            if cross_url:
                keys.append("doubts")
            try:
                sync.sync_once_locked_sync(
                    db_path=db_path, db_keys=tuple(dict.fromkeys(keys))
                )
            except Exception:  # sync is best-effort; the write already succeeded
                logger.exception("post-write sync failed (write already saved)")

        return {"status": "saved", "url": url, "page_id": page_id,
                "cross_url": cross_url, "cross_page_id": cross_id}

    except Exception as exc:
        # Notion down / rate-limited / transient: queue locally, retry later.
        logger.warning("Notion write failed, queuing locally: %r", exc)
        enqueue_pending(payload, db_path=db_path)
        return {"status": "queued", "detail": str(exc)}


def add_session_debrief_doubt(
    ledger_page_id: str, doubt_text: str, *, db_path: str | Path = DEFAULT_DB_PATH
) -> tuple[Optional[str], Optional[str]]:
    """Block-close debrief: create a doubt linked to the just-finished block.

    Reuses the cross-log machinery so the doubt inherits subject/chapter/
    exercise rollups via the ledger relation — that's what makes
    "list doubts from physics MLE" answerable later.
    """
    doubt_id, url = _cross_log_doubt(
        ledger_page_id, doubt_text, operation_id=uuid.uuid4().hex
    )
    if doubt_id:
        try:
            sync.sync_once_locked_sync(
                db_path=db_path, db_keys=("doubts", "ledger")
            )
        except Exception:
            logger.exception("debrief doubt sync failed (doubt already saved)")
    return doubt_id, url


def append_session_notes(
    ledger_page_id: str, text: str, *, db_path: str | Path = DEFAULT_DB_PATH
) -> None:
    """Block-close debrief: save key takeaways onto the ledger entry."""
    notion.update_page(ledger_page_id, {"key_points_notes": text})
    try:
        sync.sync_once_locked_sync(db_path=db_path, db_keys=("ledger",))
    except Exception:
        logger.exception("debrief notes sync failed (notes already saved)")


def _cross_log_doubt(
    ledger_page_id: str, doubt_text: str, *, operation_id: str | None = None
) -> tuple[Optional[str], Optional[str]]:
    """Create a Doubts page for a doubt raised in a ledger entry and link it.

    Links the ledger entry -> doubt via Ledger's `Logged Errors` relation.
    Returns (doubt_page_id, doubt_page_url); (None, None) on failure (non-fatal).
    """
    try:
        existing = notion.query_database(
            "doubts",
            filter={"property": "Operation ID", "rich_text": {"equals": operation_id}},
            max_pages=1,
        ) if operation_id else []
        doubt_page = existing[0] if existing else notion.create_page("doubts", {
                "core_concept": doubt_text,
                "status": "Unresolved",
                "workflow_state": "New",
                "valid_attempts": 0,
                "teacher_ready": False,
                "operation_id": operation_id or uuid.uuid4().hex,
            })
        doubt_id = doubt_page.get("id")
        # Link back from the ledger entry via Logged Errors relation.
        notion.update_page(ledger_page_id, {"logged_errors": doubt_id})
        return doubt_id, doubt_page.get("url")
    except Exception:
        logger.exception("cross-log doubt failed (ledger entry already saved)")
        return None, None


# ---------------------------------------------------------------------------
# Local write queue (Phase 9 edge case: Notion down / rate-limited)
# ---------------------------------------------------------------------------

def _connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _init_queue(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {WRITE_QUEUE_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payload_json TEXT NOT NULL,
            queued_at TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            exhausted INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({WRITE_QUEUE_TABLE})")}
    if "operation_id" not in cols:
        conn.execute(f"ALTER TABLE {WRITE_QUEUE_TABLE} ADD COLUMN operation_id TEXT")
    if "exhausted" not in cols:
        conn.execute(
            f"ALTER TABLE {WRITE_QUEUE_TABLE} "
            "ADD COLUMN exhausted INTEGER NOT NULL DEFAULT 0"
        )
    conn.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{WRITE_QUEUE_TABLE}_operation_id "
        f"ON {WRITE_QUEUE_TABLE}(operation_id) WHERE operation_id IS NOT NULL"
    )
    conn.commit()


def enqueue_pending(payload: dict[str, Any], *, db_path: str | Path = DEFAULT_DB_PATH) -> None:
    with _connect(db_path) as conn:
        _init_queue(conn)
        conn.execute(
            f"INSERT OR IGNORE INTO {WRITE_QUEUE_TABLE} "
            "(payload_json, queued_at, operation_id) VALUES (?, ?, ?)",
            (json.dumps(payload, ensure_ascii=False), _utc_now(), payload.get("operation_id")),
        )
        conn.commit()


def pending_count(*, db_path: str | Path = DEFAULT_DB_PATH) -> int:
    with _connect(db_path) as conn:
        _init_queue(conn)
        return conn.execute(
            f"SELECT COUNT(*) AS n FROM {WRITE_QUEUE_TABLE}"
        ).fetchone()["n"]


def exhausted_pending_count(*, db_path: str | Path = DEFAULT_DB_PATH) -> int:
    """Queued writes retained for inspection after exhausting automatic retry."""
    with _connect(db_path) as conn:
        _init_queue(conn)
        return conn.execute(
            f"SELECT COUNT(*) AS n FROM {WRITE_QUEUE_TABLE} WHERE exhausted=1"
        ).fetchone()["n"]


def flush_pending(
    *, db_path: str | Path = DEFAULT_DB_PATH, sync_after: bool = True
) -> dict[str, int]:
    """Retry queued writes. Returns {"flushed": n, "remaining": m}."""
    flushed = 0
    with _connect(db_path) as conn:
        _init_queue(conn)
        rows = conn.execute(
            f"SELECT id, payload_json, attempts, operation_id "
            f"FROM {WRITE_QUEUE_TABLE} "
            "WHERE exhausted=0 AND attempts < ? ORDER BY id",
            (MAX_PENDING_ATTEMPTS,),
        ).fetchall()

    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
            if not isinstance(payload, dict):
                raise ValueError("queued payload must be a JSON object")
            operation_id = payload.get("operation_id") or row["operation_id"]
            existing = []
            if operation_id:
                existing = notion.query_database(
                    payload["db_key"],
                    filter={"property": "Operation ID", "rich_text": {"equals": operation_id}},
                    max_pages=1,
                )
            if existing:
                page = existing[0]
            else:
                props = dict(payload["properties"])
                props["operation_id"] = operation_id or uuid.uuid4().hex
                page = notion.create_page(payload["db_key"], props)
            if payload["db_key"] == "ledger" and payload.get("local_work_item_id"):
                operational_store.link_execution(
                    str(payload["local_work_item_id"]), str(page.get("id")),
                    db_path=db_path,
                )
            if payload.get("cross_log_doubt") and payload["db_key"] == "ledger":
                _cross_log_doubt(
                    page.get("id"), payload["cross_log_doubt"],
                    operation_id=f"{operation_id}-doubt" if operation_id else None,
                )
            with _connect(db_path) as conn:
                conn.execute(f"DELETE FROM {WRITE_QUEUE_TABLE} WHERE id = ?", (row["id"],))
                conn.commit()
            flushed += 1
        except Exception as exc:
            next_attempt = int(row["attempts"] or 0) + 1
            exhausted = 1 if next_attempt >= MAX_PENDING_ATTEMPTS else 0
            with _connect(db_path) as conn:
                conn.execute(
                    f"UPDATE {WRITE_QUEUE_TABLE} "
                    "SET attempts = ?, last_error = ?, exhausted = ? WHERE id = ?",
                    (next_attempt, str(exc), exhausted, row["id"]),
                )
                conn.commit()
            if exhausted:
                logger.error(
                    "flush_pending: write %s exhausted after %d attempts: %r",
                    row["id"], next_attempt, exc,
                )
            else:
                logger.warning(
                    "flush_pending: write %s still failing (attempt %d/%d): %r",
                    row["id"], next_attempt, MAX_PENDING_ATTEMPTS, exc,
                )

    if flushed and sync_after:
        try:
            sync.sync_once_locked_sync(db_path=db_path)
        except Exception:
            logger.exception("post-flush sync failed")
    return {"flushed": flushed, "remaining": pending_count(db_path=db_path)}


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()
