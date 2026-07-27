"""Evidence-backed learner profile and durable nightly insights.

The profile is derived deterministically from SQLite.  The optional LLM step is
allowed to phrase one observation using named evidence keys, but it cannot add
facts or mutate study data.  Insights are deduplicated by stable key.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import bot_identity
import commitments
import session_context
import study_domain
from config import settings
from intent_parser import _extract_json


DEFAULT_DB_PATH = Path(__file__).resolve().parent / "sqlite_mirror.db"
PROFILE_TABLE = "learner_profiles"
INSIGHTS_TABLE = "learner_insights"
PROFILE_VERSION = 1
PROFILE_WINDOW_DAYS = 28
MIN_SUBJECT_ATTEMPTS = 20
MIN_WINDOW_BLOCKS = 3
INSIGHT_CATEGORIES = {
    "rhythm", "strength", "weakness", "adherence", "preference", "workload",
}


def _connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {PROFILE_TABLE} (
            chat_id TEXT PRIMARY KEY,
            profile_json TEXT NOT NULL,
            source_hash TEXT NOT NULL,
            derived_at TEXT NOT NULL,
            as_of_date TEXT NOT NULL,
            profile_version INTEGER NOT NULL
        )
    """)
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {INSIGHTS_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            insight_key TEXT NOT NULL,
            category TEXT NOT NULL,
            text TEXT NOT NULL,
            confidence TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            source TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            UNIQUE(chat_id, insight_key)
        )
    """)
    conn.commit()
    return conn


def _day(value: str | None) -> dt.date:
    return dt.date.fromisoformat((value or session_context.local_today_iso())[:10])


def _subject_metrics(
    start: str, end: str, *, db_path: str | Path
) -> list[dict[str, Any]]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT subject, COUNT(*) blocks,
                   COALESCE(SUM(questions_attempted),0) attempted,
                   COALESCE(SUM(questions_correct),0) correct,
                   COALESCE(SUM(cognitive_yield),0) cognitive_yield,
                   COALESCE(SUM(actual_time_min),0) minutes
            FROM ledger
            WHERE archived=0
              AND substr(COALESCE(date,''),1,10) BETWEEN ? AND ?
              AND subject IS NOT NULL AND TRIM(subject)<>''
            GROUP BY subject
            """,
            (start, end),
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        attempted = int(row["attempted"] or 0)
        correct = int(row["correct"] or 0)
        blocks = int(row["blocks"] or 0)
        result.append({
            "subject": row["subject"],
            "blocks": blocks,
            "attempted": attempted,
            "correct": correct,
            "accuracy_pct": round(correct * 100 / attempted) if attempted else None,
            "cognitive_yield": float(row["cognitive_yield"] or 0),
            "cy_per_block": round(float(row["cognitive_yield"] or 0) / blocks, 1)
            if blocks else None,
            "minutes": float(row["minutes"] or 0),
        })
    return sorted(result, key=lambda item: str(item["subject"]))


def _parse_timestamp(value: Any, timezone: ZoneInfo) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text or "T" not in text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    return parsed.astimezone(timezone)


def _window_name(hour: int) -> str:
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 22:
        return "evening"
    return "night"


def _rhythm_metrics(
    start: str, end: str, *, db_path: str | Path
) -> dict[str, Any]:
    try:
        timezone = ZoneInfo(settings.user_timezone())
    except Exception:
        timezone = ZoneInfo("UTC")
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT date, created_time, questions_attempted, questions_correct,
                   cognitive_yield
            FROM ledger
            WHERE archived=0
              AND substr(COALESCE(date,''),1,10) BETWEEN ? AND ?
            """,
            (start, end),
        ).fetchall()
    buckets: dict[str, dict[str, float]] = {}
    for row in rows:
        stamp = _parse_timestamp(row["date"], timezone) or _parse_timestamp(
            row["created_time"], timezone
        )
        if stamp is None:
            continue
        name = _window_name(stamp.hour)
        bucket = buckets.setdefault(name, {
            "blocks": 0, "attempted": 0, "correct": 0, "cognitive_yield": 0,
        })
        bucket["blocks"] += 1
        bucket["attempted"] += float(row["questions_attempted"] or 0)
        bucket["correct"] += float(row["questions_correct"] or 0)
        bucket["cognitive_yield"] += float(row["cognitive_yield"] or 0)
    windows: list[dict[str, Any]] = []
    for name, values in buckets.items():
        blocks = int(values["blocks"])
        attempted = int(values["attempted"])
        windows.append({
            "window": name,
            "blocks": blocks,
            "accuracy_pct": round(values["correct"] * 100 / attempted)
            if attempted else None,
            "cy_per_block": round(values["cognitive_yield"] / blocks, 1)
            if blocks else None,
        })
    windows.sort(key=lambda item: (-item["blocks"], item["window"]))
    eligible = [item for item in windows if item["blocks"] >= MIN_WINDOW_BLOCKS]
    eligible.sort(key=lambda item: (
        -(item["cy_per_block"] or 0),
        -(item["accuracy_pct"] or 0),
        -item["blocks"],
    ))
    has_comparison = len(eligible) >= 2
    return {
        "best_window": eligible[0]["window"] if has_comparison else None,
        "best_window_evidence_blocks": eligible[0]["blocks"] if has_comparison else 0,
        "windows": windows,
    }


def _commitment_metrics(as_of: str, *, db_path: str | Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for goal in commitments.active_daily_goals(db_path=db_path):
        goal_id = goal.get("notion_page_id")
        if not goal_id:
            continue
        stats = commitments.adherence(goal_id, as_of=as_of, db_path=db_path)
        result.append({
            "goal": goal.get("title"),
            "met": stats["met"],
            "verified_days": stats["total"],
            "adherence_pct": stats["pct"],
            "streak": commitments.streak(goal_id, as_of=as_of, db_path=db_path),
        })
    return result


def _rank_subjects(subjects: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    eligible = [row for row in subjects if row["attempted"] >= MIN_SUBJECT_ATTEMPTS]
    if len(eligible) < 2:
        return None, None
    weakest = min(eligible, key=lambda row: (
        row["accuracy_pct"] if row["accuracy_pct"] is not None else 101,
        row["cy_per_block"] if row["cy_per_block"] is not None else float("inf"),
    ))
    strongest = max(eligible, key=lambda row: (
        row["accuracy_pct"] if row["accuracy_pct"] is not None else -1,
        row["cy_per_block"] if row["cy_per_block"] is not None else -1,
    ))
    return weakest, strongest


def derive(
    chat_id: int | str,
    *,
    as_of: str | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    end_day = _day(as_of)
    start_day = end_day - dt.timedelta(days=PROFILE_WINDOW_DAYS - 1)
    start, end = start_day.isoformat(), end_day.isoformat()
    subjects = _subject_metrics(start, end, db_path=db_path)
    weakest, strongest = _rank_subjects(subjects)
    rhythm = _rhythm_metrics(start, end, db_path=db_path)
    prefs = commitments.active_prefs(int(chat_id), db_path=db_path)
    adherence = _commitment_metrics(end, db_path=db_path)
    backlog = study_domain._rows(
        "work_items", "archived=0 AND status IN ('Backlog','Inbox')", db_path=db_path
    )
    overdue_revision = study_domain._rows(
        "revision",
        "archived=0 AND next_execution_date IS NOT NULL "
        "AND substr(next_execution_date,1,10) <= ? "
        "AND LOWER(COALESCE(status,''))<>'completed'",
        (end,), db_path=db_path,
    )
    unresolved_doubts = study_domain._rows(
        "doubts",
        "archived=0 AND LOWER(COALESCE(status,'')) "
        "NOT IN ('resolved','dismissed')",
        db_path=db_path,
    )
    focus: list[dict[str, Any]] = []
    if weakest:
        focus.append({
            "code": "weak_subject",
            "subject": weakest["subject"],
            "reason": f"{weakest['accuracy_pct']}% accuracy across {weakest['attempted']} attempts",
        })
    low_goals = [
        item for item in adherence
        if item["verified_days"] >= 4
        and item["adherence_pct"] is not None
        and item["adherence_pct"] < 60
    ]
    if low_goals:
        focus.append({
            "code": "low_adherence",
            "goal": low_goals[0]["goal"],
            "reason": f"{low_goals[0]['adherence_pct']}% adherence across {low_goals[0]['verified_days']} verified days",
        })
    if overdue_revision:
        focus.append({
            "code": "overdue_revision",
            "count": len(overdue_revision),
            "reason": "revision items are due and still incomplete",
        })
    profile = {
        "profile_version": PROFILE_VERSION,
        "chat_id": str(chat_id),
        "as_of_date": end,
        "window": {"start": start, "end": end, "days": PROFILE_WINDOW_DAYS},
        "preferences": [item["text"] for item in prefs],
        "rhythm": rhythm,
        "subjects": subjects,
        "weakest_subject": weakest,
        "strongest_subject": strongest,
        "commitments": adherence,
        "workload": {
            "backlog_count": len(backlog),
            "overdue_revision_count": len(overdue_revision),
            "unresolved_doubt_count": len(unresolved_doubts),
        },
        "coaching_focus": focus,
        "derived_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    return profile


def save(profile: dict[str, Any], *, db_path: str | Path = DEFAULT_DB_PATH) -> bool:
    encoded = json.dumps(profile, ensure_ascii=False, sort_keys=True)
    stable_profile = dict(profile)
    stable_profile.pop("derived_at", None)
    source_hash = hashlib.sha256(
        json.dumps(stable_profile, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    chat_id = str(profile["chat_id"])
    with _connect(db_path) as conn:
        existing = conn.execute(
            f"SELECT source_hash FROM {PROFILE_TABLE} WHERE chat_id=?", (chat_id,)
        ).fetchone()
        changed = existing is None or existing["source_hash"] != source_hash
        conn.execute(
            f"""
            INSERT INTO {PROFILE_TABLE}
                (chat_id,profile_json,source_hash,derived_at,as_of_date,profile_version)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(chat_id) DO UPDATE SET
                profile_json=excluded.profile_json,
                source_hash=excluded.source_hash,
                derived_at=excluded.derived_at,
                as_of_date=excluded.as_of_date,
                profile_version=excluded.profile_version
            """,
            (
                chat_id, encoded, source_hash, profile["derived_at"],
                profile["as_of_date"], PROFILE_VERSION,
            ),
        )
        conn.commit()
    return changed


def refresh(
    chat_id: int | str, *, as_of: str | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    profile = derive(chat_id, as_of=as_of, db_path=db_path)
    save(profile, db_path=db_path)
    return profile


def latest(
    chat_id: int | str, *, db_path: str | Path = DEFAULT_DB_PATH
) -> dict[str, Any] | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            f"SELECT profile_json FROM {PROFILE_TABLE} WHERE chat_id=?",
            (str(chat_id),),
        ).fetchone()
    return json.loads(row["profile_json"]) if row else None


def evidence_map(profile: dict[str, Any]) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "rhythm.best_window": profile.get("rhythm", {}).get("best_window"),
        "rhythm.best_window_evidence_blocks": profile.get("rhythm", {}).get(
            "best_window_evidence_blocks"
        ),
        "workload.backlog_count": profile.get("workload", {}).get("backlog_count"),
        "workload.overdue_revision_count": profile.get("workload", {}).get(
            "overdue_revision_count"
        ),
        "workload.unresolved_doubt_count": profile.get("workload", {}).get(
            "unresolved_doubt_count"
        ),
    }
    for subject in profile.get("subjects", []):
        name = str(subject.get("subject") or "unknown")
        for field in ("blocks", "attempted", "accuracy_pct", "cy_per_block"):
            evidence[f"subject.{name}.{field}"] = subject.get(field)
    for index, goal in enumerate(profile.get("commitments", [])):
        for field in ("goal", "verified_days", "adherence_pct", "streak"):
            evidence[f"commitment.{index}.{field}"] = goal.get(field)
    return {key: value for key, value in evidence.items() if value is not None}


def list_insights(
    chat_id: int | str, *, active_only: bool = True,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    where = "WHERE chat_id=?" + (" AND active=1" if active_only else "")
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM {INSIGHTS_TABLE} {where} ORDER BY id", (str(chat_id),)
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["evidence"] = json.loads(item.pop("evidence_json"))
        result.append(item)
    return result


def _deterministic_insight(
    profile: dict[str, Any], existing_keys: set[str]
) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    weakest = profile.get("weakest_subject")
    if weakest:
        candidates.append({
            "key": f"weak-subject:{str(weakest['subject']).lower()}",
            "category": "weakness",
            "text": f"{weakest['subject']} currently needs the strongest accuracy push.",
            "confidence": "high" if weakest["attempted"] >= 50 else "medium",
            "evidence_keys": [
                f"subject.{weakest['subject']}.attempted",
                f"subject.{weakest['subject']}.accuracy_pct",
            ],
        })
    window = profile.get("rhythm", {}).get("best_window")
    if window:
        candidates.append({
            "key": f"best-window:{window}", "category": "rhythm",
            "text": f"The strongest evidenced study window is currently {window}.",
            "confidence": "medium",
            "evidence_keys": [
                "rhythm.best_window", "rhythm.best_window_evidence_blocks"
            ],
        })
    for index, goal in enumerate(profile.get("commitments", [])):
        if (
            goal.get("verified_days", 0) >= 4
            and goal.get("adherence_pct") is not None
            and goal["adherence_pct"] < 60
        ):
            candidates.append({
                "key": f"low-adherence:{index}", "category": "adherence",
                "text": f"{goal['goal']} needs a simpler recovery loop.",
                "confidence": "high",
                "evidence_keys": [
                    f"commitment.{index}.verified_days",
                    f"commitment.{index}.adherence_pct",
                ],
            })
    for candidate in candidates:
        if candidate["key"] not in existing_keys:
            return candidate
    return None


def _call_insight_model(
    profile: dict[str, Any], existing_keys: set[str]
) -> dict[str, Any]:
    from llm import router

    evidence = evidence_map(profile)
    allowed_keys = set(evidence)

    def validate(text: str) -> dict[str, Any]:
        data = _extract_json(text)
        key = str(data.get("key") or "").strip()
        category = str(data.get("category") or "").strip().lower()
        insight_text = str(data.get("text") or "").strip()
        confidence = str(data.get("confidence") or "").strip().lower()
        evidence_keys = data.get("evidence_keys")
        if not re.fullmatch(r"[a-z0-9][a-z0-9:_-]{2,80}", key):
            raise ValueError("invalid insight key")
        if key in existing_keys:
            raise ValueError("insight key already exists")
        if category not in INSIGHT_CATEGORIES:
            raise ValueError("invalid insight category")
        if not 10 <= len(insight_text) <= 240:
            raise ValueError("insight text length is invalid")
        if confidence not in {"low", "medium", "high"}:
            raise ValueError("invalid confidence")
        if not isinstance(evidence_keys, list) or not evidence_keys:
            raise ValueError("evidence_keys are required")
        clean_keys = [str(item) for item in evidence_keys]
        if any(item not in allowed_keys for item in clean_keys):
            raise ValueError("insight cited unknown evidence")
        return {
            "key": key, "category": category, "text": insight_text,
            "confidence": confidence, "evidence_keys": clean_keys,
        }

    import actions
    prompt = f"""{actions.identity_with_actions(role="nightly learner insight extractor", context="any")}

Extract exactly one NEW, useful learner insight from the supplied deterministic
evidence. Cite only evidence keys that appear below. Do not restate an existing
key and do not infer personality, ability, rank, or causation. Return JSON only:
{{"key":"stable-key","category":"rhythm|strength|weakness|adherence|preference|workload",
  "text":"one concise observation","confidence":"low|medium|high",
  "evidence_keys":["exact.key"]}}

Existing keys: {json.dumps(sorted(existing_keys))}
Evidence: {json.dumps(evidence, ensure_ascii=False, sort_keys=True)}"""
    response = router.complete(router.LLMRequest(
        messages=[{"role": "system", "content": prompt}],
        purpose="domain", max_output_tokens=500, validator=validate,
    ))
    return response.value


def _store_insight(
    chat_id: int | str, insight: dict[str, Any], profile: dict[str, Any],
    *, source: str, db_path: str | Path,
) -> dict[str, Any]:
    evidence = evidence_map(profile)
    cited = {key: evidence[key] for key in insight["evidence_keys"] if key in evidence}
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    with _connect(db_path) as conn:
        existing = conn.execute(
            f"SELECT id FROM {INSIGHTS_TABLE} WHERE chat_id=? AND insight_key=?",
            (str(chat_id), insight["key"]),
        ).fetchone()
        conn.execute(
            f"""
            INSERT INTO {INSIGHTS_TABLE}
                (chat_id,insight_key,category,text,confidence,evidence_json,source,
                 first_seen_at,last_seen_at,active)
            VALUES (?,?,?,?,?,?,?,?,?,1)
            ON CONFLICT(chat_id,insight_key) DO UPDATE SET
                category=excluded.category,
                text=excluded.text,
                confidence=excluded.confidence,
                evidence_json=excluded.evidence_json,
                source=excluded.source,
                last_seen_at=excluded.last_seen_at,
                active=1
            """,
            (
                str(chat_id), insight["key"], insight["category"], insight["text"],
                insight["confidence"], json.dumps(cited, ensure_ascii=False, sort_keys=True),
                source, now, now,
            ),
        )
        conn.commit()
    return {**insight, "evidence": cited, "source": source, "created": existing is None}


def nightly_insight(
    chat_id: int | str,
    *,
    as_of: str | None = None,
    use_llm: bool = True,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any] | None:
    profile = refresh(chat_id, as_of=as_of, db_path=db_path)
    existing_keys = {row["insight_key"] for row in list_insights(chat_id, db_path=db_path)}
    insight: dict[str, Any] | None = None
    source = "deterministic"
    if use_llm and evidence_map(profile):
        try:
            insight = _call_insight_model(profile, existing_keys)
            source = "llm"
        except Exception:
            insight = None
    if insight is None:
        insight = _deterministic_insight(profile, existing_keys)
    if insight is None:
        return None
    return _store_insight(
        chat_id, insight, profile, source=source, db_path=db_path
    )


def prompt_block(
    chat_id: int | str, *, db_path: str | Path = DEFAULT_DB_PATH
) -> str:
    profile = latest(chat_id, db_path=db_path) or refresh(chat_id, db_path=db_path)
    details: list[str] = []
    weakest = profile.get("weakest_subject")
    if weakest:
        details.append(
            f"- Current weakest evidenced subject: {weakest['subject']} — "
            f"{weakest['accuracy_pct']}% across {weakest['attempted']} attempts"
        )
    strongest = profile.get("strongest_subject")
    if strongest:
        details.append(
            f"- Current strongest evidenced subject: {strongest['subject']} — "
            f"{strongest['accuracy_pct']}% across {strongest['attempted']} attempts"
        )
    rhythm = profile.get("rhythm", {})
    if rhythm.get("best_window"):
        details.append(
            f"- Best evidenced study window: {rhythm['best_window']} "
            f"({rhythm['best_window_evidence_blocks']} blocks)"
        )
    workload = profile.get("workload", {})
    if any(int(workload.get(key) or 0) for key in (
        "backlog_count", "overdue_revision_count", "unresolved_doubt_count"
    )):
        details.append(
            "- Current load: "
            f"{workload.get('backlog_count', 0)} backlog, "
            f"{workload.get('overdue_revision_count', 0)} overdue revision, "
            f"{workload.get('unresolved_doubt_count', 0)} unresolved doubts"
        )
    insights = list_insights(chat_id, db_path=db_path)[-3:]
    for insight in insights:
        details.append(
            f"- Stored insight ({insight['confidence']} confidence): {insight['text']}"
        )
    if not details:
        return ""
    return "\n".join([
        "LEARNER PROFILE (derived evidence; use for framing, not invented facts):",
        *details,
    ])
