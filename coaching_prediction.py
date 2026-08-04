"""Phase 11 — deterministic early score-range projection for coaching tests.

Reads only local evidence and returns a bounded, conservative/likely/stretch
percentage-range projection for the nearest upcoming coaching test:

  * historical scores (``coaching_results``) and per-subject scores
    (``coaching_subject_results``), always normalized to percentages so tests
    with different ``maximum_marks`` are comparable,
  * subject score trends (chronological least-squares slope),
  * normalized syllabus coverage / progress (``coaching_syllabus``),
  * revision evidence (``revision`` mirror table),
  * plan capacity over a bounded near-term window (``coaching_planner.build_plan``).

Design rules:

  * Fully deterministic, offline, no LLM.
  * We never claim a guaranteed rank/AIR. When evidence is missing the module
    returns ``status="unavailable"`` with an explicit ``missing`` list instead
    of inventing a range.
  * Every range is clamped to ``[0, 100]`` (percent) / ``[0, maximum_marks]``
    (marks), and ``conservative <= likely_low <= likely_high <= stretch``
    always holds.
  * Actions that could change the projection are bounded (each carries a
    ``max_gain_pct`` cap).
  * Snapshots are stored locally keyed by ``(as_of, test_id)``, so re-running
    the projection for the same day/test is idempotent.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Any

import ntsc_coaching
import session_context
from coaching_syllabus import _canonical_subject

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "sqlite_mirror.db"
PREDICTIONS_TABLE = "coaching_predictions"
MODEL_VERSION = "coaching-score-v1"

# Near-term planner window. A full multi-week `build_plan` is expensive and the
# near-term capacity snapshot is the actionable signal, so we bound it to a
# short deterministic window and let the long-horizon risk/uncertainty carry
# the rest.
PLAN_HORIZON_DAYS = 2
MAX_UNCERTAINTY = 18.0
RECENT_WINDOW = 3

# Bounded adjustment caps (percentage points).
TREND_SHIFT_CAP = 5.0
COVERAGE_ADJUST_CAP = 6.0
REVISION_DRAG_CAP = 3.0
PLAN_ADJUST_CAP = 1.0
DIFFICULTY_SHIFT_CAP = 5.0

SAMPLE_UNCERTAINTY = {0: 15.0, 1: 14.0, 2: 10.0, 3: 8.0}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if _finite(number) else None


def _finite(number: float) -> bool:
    return number == number and number not in (float("inf"), float("-inf"))


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _pct(marks: Any, maximum_marks: Any) -> float | None:
    """Percentage for one attempt, or None when it cannot be normalized."""
    marks = _num(marks)
    maximum_marks = _num(maximum_marks)
    if marks is None or maximum_marks is None or maximum_marks <= 0:
        return None
    if marks < 0:
        return None
    return round(marks / maximum_marks * 100.0, 2)


def _bands(center: float, uncertainty: float) -> dict[str, float]:
    """conservative / likely_low / likely_high / stretch, bounded to [0, 100]."""
    center = _clamp(round(center, 1), 0.0, 100.0)
    width = max(0.0, uncertainty)
    return {
        "conservative": _clamp(round(center - 1.5 * width, 1), 0.0, 100.0),
        "likely_low": _clamp(round(center - width, 1), 0.0, 100.0),
        "likely_high": _clamp(round(center + width, 1), 0.0, 100.0),
        "stretch": _clamp(round(center + 1.5 * width, 1), 0.0, 100.0),
    }


def _marks_bands(bands: dict[str, float], maximum_marks: Any) -> dict[str, float] | None:
    maximum_marks = _num(maximum_marks)
    if maximum_marks is None or maximum_marks <= 0:
        return None
    return {
        key: round(min(maximum_marks, value / 100.0 * maximum_marks), 1)
        for key, value in bands.items()
    }


def _slope(values: list[float]) -> float:
    """Least-squares slope of a series indexed by position (pp per attempt)."""
    if len(values) < 2:
        return 0.0
    n = len(values)
    mean_x = (n - 1) / 2.0
    mean_y = sum(values) / n
    num = 0.0
    den = 0.0
    for index, value in enumerate(values):
        dx = index - mean_x
        num += dx * (value - mean_y)
        den += dx * dx
    return num / den if den > 0 else 0.0


def _trend_direction(slope: float) -> str:
    if slope > 0.5:
        return "improving"
    if slope < -0.5:
        return "declining"
    return "flat"


def _recent_mean(samples: list[dict[str, Any]]) -> float:
    values = [sample["pct"] for sample in samples]
    if not values:
        return 0.0
    if len(values) <= RECENT_WINDOW:
        return sum(values) / len(values)
    recent = values[-RECENT_WINDOW:]
    return 0.6 * (sum(recent) / len(recent)) + 0.4 * (sum(values) / len(values))


# ---------------------------------------------------------------------------
# SQLite connection / snapshot storage
# ---------------------------------------------------------------------------

def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {PREDICTIONS_TABLE} (
            as_of TEXT NOT NULL,
            test_id TEXT NOT NULL,
            test_date TEXT,
            title TEXT,
            status TEXT NOT NULL,
            confidence TEXT,
            snapshot_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(as_of, test_id)
        )
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


def save_prediction(
    snapshot: dict[str, Any], *, db_path: str | Path = DEFAULT_DB_PATH,
) -> None:
    """Idempotently store one snapshot keyed by (as_of, test_id).

    Re-storing for the same ``as_of`` + ``test_id`` replaces the previous row.
    """
    as_of = str(snapshot.get("as_of") or "")
    test_id = str(snapshot.get("test_id") or "")
    if not as_of or not test_id:
        raise ValueError("snapshot needs as_of and test_id")
    with _connect(db_path) as conn:
        conn.execute(f"""
            INSERT INTO {PREDICTIONS_TABLE}
                (as_of, test_id, test_date, title, status, confidence,
                 snapshot_json, updated_at)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(as_of, test_id) DO UPDATE SET
                test_date=excluded.test_date,
                title=excluded.title,
                status=excluded.status,
                confidence=excluded.confidence,
                snapshot_json=excluded.snapshot_json,
                updated_at=excluded.updated_at
        """, (
            as_of,
            test_id,
            str(snapshot.get("test_date") or "") or None,
            str(snapshot.get("test_title") or ""),
            str(snapshot.get("status") or "unknown"),
            str(snapshot.get("confidence") or "") or None,
            json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
            _now(),
        ))
        conn.commit()


def load_prediction(
    as_of: str, test_id: str, *, db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any] | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            f"SELECT snapshot_json FROM {PREDICTIONS_TABLE} "
            "WHERE as_of=? AND test_id=?",
            (str(as_of), str(test_id)),
        ).fetchone()
    if row is None:
        return None
    return json.loads(row["snapshot_json"])


def list_predictions(
    test_id: str | None = None, *, db_path: str | Path = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    with _connect(db_path) as conn:
        if test_id is None:
            rows = conn.execute(
                f"SELECT as_of, test_id, test_date, title, status, confidence, "
                f"updated_at FROM {PREDICTIONS_TABLE} ORDER BY as_of"
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT as_of, test_id, test_date, title, status, confidence, "
                f"updated_at FROM {PREDICTIONS_TABLE} WHERE test_id=? ORDER BY as_of",
                (str(test_id),),
            ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Evidence gathering
# ---------------------------------------------------------------------------

def _upcoming_tests(
    today: str, *, db_path: str | Path,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for row in ntsc_coaching.next_tests(today=today, limit=50, db_path=db_path):
        test_date = str(row.get("test_date") or "")[:10]
        if not test_date:
            continue
        candidates.append({
            "test_id": str(row.get("source_id")),
            "test_title": str(row.get("title") or "Coaching test").strip(),
            "test_date": test_date,
            "syllabus": str(row.get("syllabus") or ""),
            "maximum_marks": None,
            "source": "portal",
        })
    with ntsc_coaching._connect(db_path) as conn:
        if _table_exists(conn, "op_exams"):
            rows = conn.execute(
                "SELECT id, notion_page_id, title, exam_date, syllabus, max_marks "
                "FROM op_exams WHERE archived=0 AND status='Planned' "
                "AND LOWER(COALESCE(kind,''))='coaching test'"
            ).fetchall()
            for row in rows:
                test_date = str(row["exam_date"] or "")[:10]
                if not test_date:
                    continue
                candidates.append({
                    "test_id": str(row["notion_page_id"] or row["id"]),
                    "test_title": str(row["title"] or "Coaching test").strip(),
                    "test_date": test_date,
                    "syllabus": str(row["syllabus"] or ""),
                    "maximum_marks": _num(row["max_marks"]),
                    "source": "exam",
                })
    return sorted(candidates, key=lambda item: (item["test_date"], item["test_id"]))


def _select_target(
    candidates: list[dict[str, Any]], test_id: str | None,
) -> dict[str, Any] | None:
    if test_id is not None:
        for candidate in candidates:
            if candidate["test_id"] == str(test_id):
                return candidate
        return None
    return candidates[0] if candidates else None


def _historical_samples(*, db_path: str | Path) -> tuple[list[dict[str, Any]], int]:
    samples: list[dict[str, Any]] = []
    skipped = 0
    with ntsc_coaching._connect(db_path) as conn:
        rows = conn.execute(
            "SELECT source_id, title, attempt_date, total_marks, maximum_marks, "
            "percentile FROM coaching_results"
        ).fetchall()
    for row in rows:
        pct = _pct(row["total_marks"], row["maximum_marks"])
        if pct is None:
            skipped += 1
            continue
        samples.append({
            "date": str(row["attempt_date"] or "")[:10] or "unknown",
            "source_id": str(row["source_id"]),
            "pct": pct,
            "total_marks": _num(row["total_marks"]),
            "maximum_marks": _num(row["maximum_marks"]),
            "percentile": _num(row["percentile"]),
        })
    samples.sort(key=lambda item: (item["date"], item["source_id"]))
    return samples, skipped


def _subject_samples(*, db_path: str | Path) -> dict[str, list[dict[str, Any]]]:
    by_subject: dict[str, list[dict[str, Any]]] = {}
    with ntsc_coaching._connect(db_path) as conn:
        rows = conn.execute(
            "SELECT s.subject, s.marks, s.maximum_marks, r.attempt_date "
            "FROM coaching_subject_results s "
            "JOIN coaching_results r ON r.source_id = s.result_id"
        ).fetchall()
    for row in rows:
        pct = _pct(row["marks"], row["maximum_marks"])
        if pct is None:
            continue
        subject = _canonical_subject(str(row["subject"] or "")) or (
            str(row["subject"] or "").strip() or "(uncategorised)"
        )
        by_subject.setdefault(subject, []).append({
            "date": str(row["attempt_date"] or "")[:10] or "unknown",
            "pct": pct,
            "maximum_marks": _num(row["maximum_marks"]),
        })
    for series in by_subject.values():
        series.sort(key=lambda item: (item["date"], item["pct"]))
    return by_subject


def _coverage(
    today: str, target: dict[str, Any], *, db_path: str | Path,
) -> dict[str, Any]:
    try:
        import coaching_syllabus
        tests = coaching_syllabus.coverage_snapshot(
            today=today, limit=50, db_path=db_path,
        )
    except Exception:
        return {
            "syllabus_known": False,
            "coverage_known": False,
            "topic_count": 0,
            "covered_count": 0,
            "covered_fraction": None,
            "subjects": {},
            "coverage_adjust": 0.0,
        }
    for test in tests:
        if test.get("source_id") == target["test_id"]:
            coverage = test.get("coverage") or {}
            subjects: dict[str, dict[str, Any]] = {}
            for record in test.get("syllabus_records") or []:
                subject = _canonical_subject(str(record.get("subject") or "")) or (
                    str(record.get("subject") or "").strip() or "(uncategorised)"
                )
                bucket = subjects.setdefault(subject, {
                    "subject": subject, "topic_count": 0, "covered_count": 0,
                    "uncovered_count": 0, "doubt_count": 0,
                    "covered_fraction": None,
                })
                bucket["topic_count"] += 1
                if record.get("covered"):
                    bucket["covered_count"] += 1
                if record.get("has_doubt"):
                    bucket["doubt_count"] += 1
            for bucket in subjects.values():
                bucket["uncovered_count"] = (
                    bucket["topic_count"] - bucket["covered_count"]
                )
                bucket["covered_fraction"] = round(
                    bucket["covered_count"] / bucket["topic_count"], 3
                ) if bucket["topic_count"] else None
            known = bool(test.get("syllabus_records"))
            fraction = coverage.get("covered_fraction")
            adjust = 0.0
            if known and fraction is not None:
                adjust = _clamp(
                    (float(fraction) - 0.6) * 15.0,
                    -COVERAGE_ADJUST_CAP, COVERAGE_ADJUST_CAP,
                )
            return {
                "syllabus_known": known,
                "coverage_known": known,
                "topic_count": int(coverage.get("topic_count") or 0),
                "covered_count": int(coverage.get("covered_count") or 0),
                "covered_fraction": fraction,
                "subjects": subjects,
                "coverage_adjust": round(adjust, 1),
            }
    return {
        "syllabus_known": False,
        "coverage_known": False,
        "topic_count": 0,
        "covered_count": 0,
        "covered_fraction": None,
        "subjects": {},
        "coverage_adjust": 0.0,
    }


def _revision_evidence(
    today: str, test_date: str, subjects: set[str] | None,
    *, db_path: str | Path,
) -> dict[str, Any]:
    overdue = 0
    due_before_test = 0
    scheduled_after = 0
    relevant = 0
    try:
        with ntsc_coaching._connect(db_path) as conn:
            if not _table_exists(conn, "revision"):
                return {"available": False}
            rows = conn.execute(
                "SELECT subject, next_execution_date, status FROM revision "
                "WHERE archived=0 AND LOWER(COALESCE(status,''))<>'completed'"
            ).fetchall()
    except Exception:
        return {"available": False}
    for row in rows:
        raw = str(row["next_execution_date"] or "")[:10]
        row_subject = _canonical_subject(str(row["subject"] or ""))
        if subjects and row_subject is not None and row_subject not in subjects:
            continue
        relevant += 1
        if not raw:
            scheduled_after += 1
            continue
        if raw <= today:
            overdue += 1
        elif raw <= test_date:
            due_before_test += 1
        else:
            scheduled_after += 1
    return {
        "available": True,
        "overdue": overdue,
        "due_before_test": due_before_test,
        "scheduled_after": scheduled_after,
        "relevant_count": relevant,
    }


def _plan_evidence(
    today: str, days_to_test: int | None, *, db_path: str | Path,
) -> dict[str, Any] | None:
    horizon = 1
    if days_to_test is not None and days_to_test >= 0:
        horizon = max(1, min(PLAN_HORIZON_DAYS, days_to_test)) + 1
    try:
        import coaching_planner
        plan = coaching_planner.build_plan(
            target_date=today, days=horizon, db_path=db_path,
        )
    except Exception:
        return None
    capacity_rows = list((plan.get("capacity") or {}).values())
    headrooms = [float(row.get("minutes_headroom") or 0) for row in capacity_rows]
    avg_headroom = (sum(headrooms) / len(headrooms)) if headrooms else None
    budget = capacity_rows[0].get("budget_minutes") if capacity_rows else None
    unplaced = int(plan.get("totals", {}).get("unplaced_count") or 0)
    adjust = 0.0
    if avg_headroom is not None:
        if avg_headroom >= 60:
            adjust = 1.0
        elif avg_headroom >= 30:
            adjust = 0.0
        else:
            adjust = -1.0
    return {
        "available": True,
        "days_planned": horizon,
        "planned_minutes": round(float(plan.get("totals", {}).get("planned_minutes") or 0), 1),
        "planned_cy": round(float(plan.get("totals", {}).get("planned_cy") or 0), 1),
        "avg_headroom_min": round(avg_headroom, 1) if avg_headroom is not None else None,
        "budget_minutes": budget,
        "unplaced": unplaced,
        "plan_adjust": round(_clamp(adjust, -PLAN_ADJUST_CAP, PLAN_ADJUST_CAP), 1),
    }


def _total_uncertainty(
    n: int, slope: float, days_to_test: int,
    coverage_known: bool,
) -> dict[str, float]:
    sample = SAMPLE_UNCERTAINTY.get(n, 5.0)
    horizon = min(6.0, 0.4 * days_to_test)
    coverage = 3.0 if not coverage_known else 1.5
    trend = 2.0 if abs(slope) > 2.0 else 1.0
    total = _clamp(
        sample + horizon + coverage + trend, 0.0, MAX_UNCERTAINTY,
    )
    return {
        "total_pct": round(total, 1),
        "sample_pct": round(sample, 1),
        "horizon_pct": round(horizon, 1),
        "coverage_pct": round(coverage, 1),
        "trend_pct": round(trend, 1),
    }


def _confidence(
    n: int, coverage_known: bool, projected_subjects: int,
) -> str:
    if n == 0:
        return "unavailable"
    if n >= 4 and coverage_known and projected_subjects >= 2:
        return "high"
    if n >= 2 and (coverage_known or n >= 3):
        return "medium"
    return "low"


def _subject_bands(
    mean: float, slope: float, fraction: float | None,
    days_to_test: int, n: int,
) -> dict[str, float]:
    trend_shift = _clamp(slope * 0.8, -4.0, 4.0)
    coverage_shift = 0.0
    if fraction is not None:
        coverage_shift = _clamp((fraction - 0.6) * 10.0, -4.0, 4.0)
    center = _clamp(mean + trend_shift + coverage_shift, 0.0, 100.0)
    width = (10.0 if n == 1 else (7.0 if n < 4 else 5.0)) + min(
        3.0, 0.25 * days_to_test
    )
    return _bands(center, width)


# ---------------------------------------------------------------------------
# risks and bounded actions
# ---------------------------------------------------------------------------

def _build_risks(
    *, n: int, skipped: int, days_to_test: int, maximum_marks: Any,
    coverage: dict[str, Any], revision: dict[str, Any], plan: dict[str, Any] | None,
    slope: float, projected_subjects: list[dict[str, Any]],
    score_subjects: set[str],
) -> list[str]:
    risks: list[str] = []
    if n < 4:
        risks.append(f"only {n} historical result(s) — wide spread")
    if skipped:
        risks.append(
            f"{skipped} historical result(s) had unusable maximum marks and were excluded"
        )
    if not coverage.get("syllabus_known"):
        risks.append("no normalized syllabus is stored for this test")
    if not coverage.get("coverage_known"):
        risks.append("no topic coverage evidence")
    if revision.get("available") and revision["overdue"]:
        risks.append(f"{revision['overdue']} overdue revision item(s)")
    if plan is not None and plan["unplaced"]:
        risks.append(f"plan could not place {plan['unplaced']} item(s)")
    if days_to_test > 14:
        risks.append(f"{days_to_test} days until the test — long-horizon uncertainty")
    if _num(maximum_marks) is None:
        risks.append("target test maximum marks are not recorded; marks ranges unavailable")
    if slope < -0.5:
        risks.append("overall score trend is declining")
    for subject in sorted(projected_subjects, key=lambda item: item["subject"]):
        if subject["trend_direction"] == "declining":
            risks.append(f"{subject['subject']} score trend is declining")
    for bucket in coverage.get("subjects", {}).values():
        if bucket["doubt_count"] and bucket["covered_fraction"] is not None and bucket["covered_fraction"] < 1.0:
            risks.append(
                f"{bucket['subject']}: {bucket['doubt_count']} doubt(s) in not-fully-covered syllabus"
            )
    missing_subjects = sorted(
        subject for subject in coverage.get("subjects", {})
        if subject not in score_subjects
    )
    if missing_subjects:
        risks.append(
            "no historical subject scores for: " + ", ".join(missing_subjects[:3])
        )
    return risks


def _build_actions(
    *, coverage: dict[str, Any], revision: dict[str, Any],
    plan: dict[str, Any] | None,
    projected_subjects: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    fraction = coverage.get("covered_fraction")
    if coverage.get("coverage_known") and fraction is not None and fraction < 1.0:
        uncovered = int(coverage.get("topic_count") or 0) - int(
            coverage.get("covered_count") or 0
        )
        gain = round(min(6.0, (1.0 - fraction) * 12.0), 1)
        actions.append({
            "action": f"Complete the remaining {uncovered} uncovered syllabus topic(s)",
            "bound": f"+{gain} pp",
            "max_gain_pct": gain,
        })
    uncovered_doubts = sum(
        bucket["doubt_count"] for bucket in coverage.get("subjects", {}).values()
        if bucket["doubt_count"] and (
            bucket["covered_fraction"] is None or bucket["covered_fraction"] < 1.0
        )
    )
    if uncovered_doubts:
        gain = round(min(4.0, uncovered_doubts * 2.0), 1)
        actions.append({
            "action": f"Resolve {uncovered_doubts} doubt(s) in uncovered syllabus topics",
            "bound": f"+{gain} pp",
            "max_gain_pct": gain,
        })
    for subject in projected_subjects:
        if subject["trend_direction"] == "declining":
            gain = round(min(5.0, abs(subject["trend_pct_per_attempt"]) * 1.5), 1)
            actions.append({
                "action": (
                    f"Run timed subject practice for {subject['subject']} to "
                    "reverse the declining trend"
                ),
                "bound": f"+{gain} pp",
                "max_gain_pct": gain,
            })
    if revision.get("available") and revision["overdue"]:
        gain = round(min(4.0, float(revision["overdue"]) * 1.0), 1)
        actions.append({
            "action": f"Clear {revision['overdue']} overdue revision item(s) before the test",
            "bound": f"+{gain} pp",
            "max_gain_pct": gain,
        })
    if plan is not None and plan["available"] and (
        plan["avg_headroom_min"] is None or plan["avg_headroom_min"] < 30
    ):
        actions.append({
            "action": "Protect a fixed daily test-prep block until the test",
            "bound": "+3 pp",
            "max_gain_pct": 3.0,
        })
    return actions


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------

def _difficulty_evidence(
    subjects: set[str] | None, *, db_path: str | Path,
) -> dict[str, Any]:
    """Historical JEE difficulty factor for the test's subjects.

    ``factor = clamp(mean over matched subjects of (0.5 + mean(hard_ratio +
    0.5 * medium_ratio)), 0.5, 1.5)`` from the derived ratio columns; the
    'Unclassified' chapter rows are excluded by ``subject_difficulty``. Falls
    back to the mean across ALL subjects when none of the test's subjects have
    JEE evidence. Degrades to ``factor 1.0, jee_evidence False`` when the
    ``op_jee_*`` tables are empty or unreadable.
    """
    try:
        import jee_data_loader
        factors = jee_data_loader.subject_difficulty(db_path=db_path)
    except Exception:
        return {"difficulty_factor": 1.0, "jee_evidence": False}
    if not factors:
        return {"difficulty_factor": 1.0, "jee_evidence": False}
    matched = [
        factor for subject, factor in factors.items()
        if subjects is None or _canonical_subject(subject) in subjects
    ]
    if not matched:
        matched = list(factors.values())
    factor = _clamp(round(sum(matched) / len(matched), 3), 0.5, 1.5)
    return {"difficulty_factor": factor, "jee_evidence": True}


def project_coaching_score(
    *, test_id: str | None = None, today: str | None = None,
    db_path: str | Path = DEFAULT_DB_PATH, store: bool = False,
) -> dict[str, Any]:
    """Project a bounded score range for the nearest upcoming coaching test.

    ``today`` defaults to the local date. Pass ``store=True`` to persist an
    idempotent snapshot (``save_prediction`` is also available standalone).
    Never claims a rank/AIR; returns ``status="unavailable"`` when evidence is
    missing instead of inventing a range.
    """
    today = (today or session_context.local_today_iso())[:10]
    candidates = _upcoming_tests(today, db_path=db_path)
    target = _select_target(candidates, test_id)
    test_id = str(test_id) if test_id else None

    if target is None:
        snapshot = {
            "status": "unavailable",
            "model": MODEL_VERSION,
            "as_of": today,
            "test_id": str(test_id) if test_id else None,
            "test_title": None,
            "test_date": None,
            "days_to_test": None,
            "maximum_marks": None,
            "confidence": "unavailable",
            "evidence_count": 0,
            "total": None,
            "subjects": [],
            "factors": {},
            "risks": ["no upcoming coaching test is recorded"],
            "actions": [],
            "missing": ["upcoming_test"],
        }
        if store and snapshot.get("test_id"):
            save_prediction(snapshot, db_path=db_path)
        return snapshot

    samples, skipped = _historical_samples(db_path=db_path)
    subject_series = _subject_samples(db_path=db_path)
    n = len(samples)
    test_date = target["test_date"]
    days_to_test = max(0, (dt.date.fromisoformat(test_date) - dt.date.fromisoformat(today)).days)
    maximum_marks = target["maximum_marks"]

    coverage = _coverage(today, target, db_path=db_path)
    syllabus_subjects = set(coverage.get("subjects", {}).keys()) or None
    revision = _revision_evidence(
        today, test_date, syllabus_subjects, db_path=db_path,
    )
    plan = _plan_evidence(today, days_to_test, db_path=db_path)

    # ---- total projection -------------------------------------------------
    base = dict(coverage)
    base["syllabus_subjects"] = sorted(syllabus_subjects) if syllabus_subjects else None

    if n == 0:
        factors = {
            "historical": {
                "samples": 0, "skipped": skipped, "mean_pct": None,
                "recent_mean_pct": None, "trend_pct_per_attempt": None,
                "trend_direction": "flat",
            },
            "coverage": base,
            "revision": revision,
            "plan": plan,
            "uncertainty": {"confidence": "unavailable"},
        }
        snapshot = {
            "status": "unavailable",
            "model": MODEL_VERSION,
            "as_of": today,
            "test_id": target["test_id"],
            "test_title": target["test_title"],
            "test_date": test_date,
            "days_to_test": days_to_test,
            "maximum_marks": maximum_marks,
            "confidence": "unavailable",
            "evidence_count": _evidence_count(
                n=0, subject_series=subject_series,
                coverage=coverage, revision=revision, plan=plan,
            ),
            "total": None,
            "subjects": _subject_entries(
                subject_series=subject_series, coverage=coverage,
                days_to_test=days_to_test,
            ),
            "factors": factors,
            "risks": ["no historical scores recorded — cannot project a range"],
            "actions": [],
            "missing": ["historical_scores"],
        }
        if store:
            save_prediction(snapshot, db_path=db_path)
        return snapshot

    mean_pct = round(sum(sample["pct"] for sample in samples) / n, 1)
    recent_mean = round(_recent_mean(samples), 1)
    slope = round(_slope([sample["pct"] for sample in samples]), 2)
    direction = _trend_direction(slope)

    trend_shift = _clamp(slope, -TREND_SHIFT_CAP, TREND_SHIFT_CAP)
    coverage_adjust = float(coverage["coverage_adjust"])
    revision_adjust = 0.0
    if revision["available"]:
        if revision["overdue"]:
            revision_adjust -= min(REVISION_DRAG_CAP, 0.8 * revision["overdue"])
        if (
            revision["due_before_test"]
            and (coverage["covered_fraction"] is None or coverage["covered_fraction"] < 0.6)
        ):
            revision_adjust -= min(1.5, 0.5 * revision["due_before_test"])
    revision_adjust = round(
        _clamp(revision_adjust, -REVISION_DRAG_CAP, 0.5), 1
    )
    plan_adjust = float(plan["plan_adjust"]) if plan else 0.0

    difficulty = _difficulty_evidence(syllabus_subjects, db_path=db_path)
    difficulty_factor = float(difficulty["difficulty_factor"])
    difficulty_adjust = (
        _clamp((1.0 - difficulty_factor) * recent_mean, -DIFFICULTY_SHIFT_CAP, DIFFICULTY_SHIFT_CAP)
        if difficulty["jee_evidence"]
        else 0.0
    )

    center = _clamp(
        recent_mean + trend_shift + coverage_adjust + revision_adjust + plan_adjust
        + difficulty_adjust,
        0.0, 100.0,
    )
    uncertainty = _total_uncertainty(n, slope, days_to_test, coverage["coverage_known"])
    total_bands = _bands(center, uncertainty["total_pct"])
    marks_bands = _marks_bands(total_bands, maximum_marks)

    subjects = _subject_entries(
        subject_series=subject_series, coverage=coverage,
        days_to_test=days_to_test,
    )
    projected_subjects = [s for s in subjects if s["status"] == "projected"]
    confidence = _confidence(n, coverage["coverage_known"], len(projected_subjects))

    factors = {
        "historical": {
            "samples": n,
            "skipped": skipped,
            "mean_pct": mean_pct,
            "recent_mean_pct": recent_mean,
            "trend_pct_per_attempt": slope,
            "trend_direction": direction,
            "latest_percentile": samples[-1].get("percentile"),
        },
        "coverage": base,
        "revision": revision,
        "plan": plan,
        "uncertainty": uncertainty,
        "difficulty": {
            "difficulty_factor": difficulty_factor,
            "jee_evidence": difficulty["jee_evidence"],
        },
        "adjustments_pct": {
            "trend": round(trend_shift, 1),
            "coverage": coverage_adjust,
            "revision": revision_adjust,
            "plan": plan_adjust,
            "difficulty": round(difficulty_adjust, 1),
            "center_pct": round(center, 1),
        },
    }
    risks = _build_risks(
        n=n, skipped=skipped, days_to_test=days_to_test,
        maximum_marks=maximum_marks, coverage=coverage, revision=revision,
        plan=plan, slope=slope, projected_subjects=projected_subjects,
        score_subjects=set(subject_series.keys()),
    )
    actions = _build_actions(
        coverage=coverage, revision=revision, plan=plan,
        projected_subjects=projected_subjects,
    )

    snapshot = {
        "status": "ok",
        "model": MODEL_VERSION,
        "as_of": today,
        "test_id": target["test_id"],
        "test_title": target["test_title"],
        "test_date": test_date,
        "days_to_test": days_to_test,
        "maximum_marks": maximum_marks,
        "confidence": confidence,
        "evidence_count": _evidence_count(
            n=n, subject_series=subject_series,
            coverage=coverage, revision=revision, plan=plan,
        ),
        "total": {
            "pct": total_bands,
            "marks": marks_bands,
            "center_pct": round(center, 1),
        },
        "subjects": subjects,
        "factors": factors,
        "difficulty_factor": difficulty_factor,
        "jee_evidence": difficulty["jee_evidence"],
        "risks": risks,
        "actions": actions,
        "missing": [],
        "rank_statement": (
            "No rank or AIR is claimed: the projection is a bounded score range "
            "from local evidence, not a rank prediction."
        ),
    }
    if store:
        save_prediction(snapshot, db_path=db_path)
    return snapshot


def _evidence_count(
    *, n: int, subject_series: dict[str, list[dict[str, Any]]],
    coverage: dict[str, Any], revision: dict[str, Any], plan: dict[str, Any] | None,
) -> int:
    count = n + sum(1 for series in subject_series.values() if series)
    if coverage.get("coverage_known"):
        count += 1
    if coverage.get("syllabus_known"):
        count += 1
    if revision.get("available"):
        count += min(5, int(revision.get("relevant_count") or 0))
    if plan is not None and plan.get("available"):
        count += 1
    return count


def _subject_entries(
    *, subject_series: dict[str, list[dict[str, Any]]],
    coverage: dict[str, Any], days_to_test: int,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    coverage_subjects = coverage.get("subjects", {})
    all_subjects = sorted(set(subject_series) | set(coverage_subjects))
    for subject in all_subjects:
        series = subject_series.get(subject) or []
        cov = coverage_subjects.get(subject)
        entry: dict[str, Any] = {
            "subject": subject,
            "coverage": cov,
            "weightage_proxy": None,
            "samples": len(series),
        }
        if cov and coverage.get("topic_count"):
            entry["weightage_proxy"] = {
                "topic_share": round(cov["topic_count"] / coverage["topic_count"], 3),
                "basis": "normalized syllabus topic counts, not official weightage",
            }
        if not series:
            entry["status"] = "no_scores"
            entry["mean_pct"] = None
            entry["trend_pct_per_attempt"] = None
            entry["trend_direction"] = "flat"
            entry["pct"] = None
        else:
            values = [sample["pct"] for sample in series]
            mean = round(sum(values) / len(values), 1)
            slope = round(_slope(values), 2)
            fraction = cov["covered_fraction"] if cov else None
            entry.update({
                "status": "projected",
                "mean_pct": mean,
                "trend_pct_per_attempt": slope,
                "trend_direction": _trend_direction(slope),
                "pct": _subject_bands(
                    mean, slope, fraction, days_to_test, len(values)
                ),
            })
        entries.append(entry)
    return entries


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Project a bounded score range for the nearest coaching test."
    )
    parser.add_argument("--test-id", help="project a specific upcoming test id")
    parser.add_argument("--today", help="as-of date (YYYY-MM-DD); defaults to local today")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite mirror path")
    parser.add_argument("--store", action="store_true", help="persist an idempotent snapshot")
    args = parser.parse_args(argv)

    snapshot = project_coaching_score(
        test_id=args.test_id, today=args.today, db_path=args.db, store=args.store,
    )
    print(f"status:     {snapshot['status']}  ({snapshot.get('model')})")
    print(f"as_of:      {snapshot['as_of']}")
    print(f"test:       {snapshot.get('test_title')} ({snapshot.get('test_id')})")
    print(f"date:       {snapshot.get('test_date')}  "
          f"days_to_test: {snapshot.get('days_to_test')}")
    print(f"confidence: {snapshot.get('confidence')}  "
          f"evidence_count: {snapshot.get('evidence_count')}")
    if snapshot.get("status") == "ok":
        total = snapshot["total"]
        print("total pct:  " + ", ".join(
            f"{key}={value}" for key, value in total["pct"].items()
        ))
        if total.get("marks"):
            print("total marks:" + ", ".join(
                f"{key}={value}" for key, value in total["marks"].items()
            ) + f" (max {snapshot.get('maximum_marks')})")
        for subject in snapshot["subjects"]:
            if subject["status"] == "projected":
                print(f"subject {subject['subject']:<12} " + ", ".join(
                    f"{key}={value}" for key, value in subject["pct"].items()
                ) + f"  trend={subject['trend_direction']}")
            else:
                print(f"subject {subject['subject']:<12} no historical scores")
        print("risks:")
        for risk in snapshot["risks"]:
            print(f"  - {risk}")
        print("actions (bounded):")
        for action in snapshot["actions"]:
            print(f"  - {action['action']}  [{action['bound']}]")
    else:
        print("missing:    " + ", ".join(snapshot.get("missing") or []))
        for risk in snapshot.get("risks") or []:
            print(f"  - {risk}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
