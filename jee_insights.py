"""Personalized JEE coaching insights.

Cross-references the JEE analytics tables (op_jee_*) with the user's own
study data (ledger, doubts, learn_formulas) to produce personalized
recommendations that no generic coach could give.

All functions are read-only, thread db_path from callers, and degrade
gracefully when JEE analytics or user data is missing.

Design rules:
  - Code decides WHAT to compute; the LLM decides HOW to phrase it.
  - No writes, no LLM calls, no external network.
  - All matching is case-insensitive and whitespace-normalized.
  - TypedDict for JSON-compat return shapes; frozen dataclasses for
    internal value objects.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "sqlite_mirror.db"


# ---------------------------------------------------------------------------
# Internal value objects
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ChapterKey:
    """Normalized subject+chapter lookup key."""

    subject: str
    chapter: str

    @staticmethod
    def normalize(subject: str, chapter: str) -> str:
        return f"{subject.strip().lower()}|{chapter.strip().lower()}"


@dataclass(frozen=True, slots=True)
class LedgerRow:
    """One aggregated ledger row (chapter-level)."""

    subject: str
    chapter: str
    minutes: float
    sessions: int
    avg_accuracy: float | None


@dataclass(frozen=True, slots=True)
class JeeChapter:
    """Aggregated JEE stats for one chapter."""

    subject: str
    chapter: str
    total_questions: int
    roi_score: float
    repeat_ratio: float
    easy_ratio: float
    medium_ratio: float
    hard_ratio: float


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _norm(text: str) -> str:
    return " ".join(str(text or "").lower().split())


def _ledger_rows(conn: sqlite3.Connection) -> list[LedgerRow]:
    rows = conn.execute("""
        SELECT subject, chapter_text AS chapter,
               SUM(actual_time_min) AS minutes,
               COUNT(*) AS sessions,
               AVG(CASE WHEN questions_attempted > 0
                   THEN CAST(questions_correct AS REAL) / questions_attempted
                   ELSE NULL END) AS avg_accuracy
        FROM ledger
        WHERE archived=0 AND chapter_text IS NOT NULL AND chapter_text != ''
        GROUP BY subject, chapter_text
        HAVING SUM(actual_time_min) > 0
        ORDER BY minutes DESC
    """).fetchall()
    return [
        LedgerRow(
            subject=r["subject"],
            chapter=r["chapter"],
            minutes=r["minutes"],
            sessions=r["sessions"],
            avg_accuracy=r["avg_accuracy"],
        )
        for r in rows
    ]


def _jee_chapters(conn: sqlite3.Connection) -> dict[str, JeeChapter]:
    rows = conn.execute("""
        SELECT subject, chapter,
               SUM(total_questions) AS total_questions,
               AVG(importance_score) AS roi_score,
               AVG(repeat_ratio) AS repeat_ratio,
               AVG(easy_ratio) AS easy_ratio,
               AVG(medium_ratio) AS medium_ratio,
               AVG(hard_ratio) AS hard_ratio
        FROM op_jee_chapter_stats
        WHERE chapter != 'Unclassified'
        GROUP BY subject, chapter
    """).fetchall()
    result: dict[str, JeeChapter] = {}
    for r in rows:
        key = ChapterKey.normalize(r["subject"], r["chapter"])
        result[key] = JeeChapter(
            subject=r["subject"],
            chapter=r["chapter"],
            total_questions=r["total_questions"],
            roi_score=r["roi_score"],
            repeat_ratio=r["repeat_ratio"],
            easy_ratio=r["easy_ratio"],
            medium_ratio=r["medium_ratio"],
            hard_ratio=r["hard_ratio"],
        )
    return result


# ---------------------------------------------------------------------------
# JSON-compat return shapes (TypedDict)
# ---------------------------------------------------------------------------

class Allocation(TypedDict):
    subject: str
    chapter: str
    minutes: float
    sessions: int
    avg_accuracy: float | None
    time_share_pct: float
    roi_score: float
    total_questions: int
    efficiency_score: float
    recommendation: str


class MissedOpportunitiesResult(TypedDict, total=False):
    status: str
    total_minutes: float
    allocations: list[Allocation]
    over_allocated: list[Allocation]
    under_allocated: list[Allocation]
    top_recommendation: str | None


class PrioritizedDoubt(TypedDict):
    subject: str
    core_concept: str
    created_time: str
    weightage_rank: int | None
    total_questions: int | None
    estimated_marks: int | None
    urgency: str


class DoubtPrioritizationResult(TypedDict, total=False):
    status: str
    doubts: list[PrioritizedDoubt]
    highest_priority: str | None


class ChapterRecommendation(TypedDict):
    subject: str
    chapter: str
    roi_score: float
    total_questions: int
    user_accuracy: float | None
    reason: str


class SkipOrStudyResult(TypedDict, total=False):
    status: str
    prioritize: list[ChapterRecommendation]
    deprioritize: list[ChapterRecommendation]
    balanced: int


class TrendingChapter(TypedDict):
    subject: str
    chapter: str
    recent_avg: float
    previous_avg: float
    change_pct: float


class TrendingResult(TypedDict, total=False):
    status: str
    trending_up: list[TrendingChapter]
    trending_down: list[TrendingChapter]


class RankedFormula(TypedDict):
    formula_id: int
    subject: str
    chapter: str | None
    topic: str | None
    formula_text: str
    mastery: str
    jee_patterns: int
    jee_marks_estimate: int | None
    priority: str


class FormulaPriorityResult(TypedDict, total=False):
    status: str
    formulas: list[RankedFormula]
    highest_priority: str | None


class ChapterStrength(TypedDict):
    subject: str
    chapter: str
    user_accuracy: float
    hard_ratio: float
    gap_score: float
    insight: str


class StrengthsResult(TypedDict, total=False):
    status: str
    strengths: list[ChapterStrength]
    gaps: list[ChapterStrength]
    biggest_gap: str | None


# ---------------------------------------------------------------------------
# 1. Missed opportunities — study time vs chapter ROI
# ---------------------------------------------------------------------------

def missed_opportunities(
    *, db_path: str | Path = DEFAULT_DB_PATH,
) -> MissedOpportunitiesResult:
    """Compare where the user spends time vs where the marks actually are."""
    with _connect(db_path) as conn:
        if not _table_exists(conn, "op_jee_chapter_stats"):
            return MissedOpportunitiesResult(status="no_jee_data")
        if not _table_exists(conn, "ledger"):
            return MissedOpportunitiesResult(status="no_ledger")

        ledger = _ledger_rows(conn)
        if len(ledger) < 5:
            return MissedOpportunitiesResult(
                status="insufficient_sessions", total_minutes=0,
                allocations=[], over_allocated=[], under_allocated=[],
                top_recommendation=None,
            )

        total_minutes = sum(r.minutes for r in ledger)
        jee_map = _jee_chapters(conn)

        allocations: list[Allocation] = []
        for lr in ledger:
            key = ChapterKey.normalize(lr.subject, lr.chapter)
            jee = jee_map.get(key)
            time_share = (lr.minutes / total_minutes * 100) if total_minutes else 0

            roi = jee.roi_score if jee else 0.0
            importance = jee.total_questions if jee else 0
            efficiency = roi / max(lr.minutes, 1.0)

            if jee is None:
                rec = "no_jee_data"
            elif time_share > 15.0 and roi < 50.0:
                rec = "consider_reducing"
            elif time_share < 3.0 and roi > 100.0:
                rec = "consider_increasing"
            else:
                rec = "balanced"

            allocations.append(Allocation(
                subject=lr.subject,
                chapter=lr.chapter,
                minutes=lr.minutes,
                sessions=lr.sessions,
                avg_accuracy=round(lr.avg_accuracy, 3) if lr.avg_accuracy else None,
                time_share_pct=round(time_share, 1),
                roi_score=round(roi, 1),
                total_questions=importance,
                efficiency_score=round(efficiency, 3),
                recommendation=rec,
            ))

        over = sorted(
            [a for a in allocations if a["recommendation"] == "consider_reducing"],
            key=lambda a: a["minutes"], reverse=True,
        )[:5]
        under = sorted(
            [a for a in allocations if a["recommendation"] == "consider_increasing"],
            key=lambda a: a["roi_score"], reverse=True,
        )[:5]

        top_rec: str | None = None
        if under:
            best = under[0]
            top_rec = (
                f"You spend only {best['time_share_pct']}% of time on "
                f"{best['subject']}: {best['chapter']} (ROI {best['roi_score']}). "
                f"Increasing this could yield more marks than any other change."
            )

        return MissedOpportunitiesResult(
            status="ok",
            total_minutes=total_minutes,
            allocations=allocations,
            over_allocated=over,
            under_allocated=under,
            top_recommendation=top_rec,
        )


# ---------------------------------------------------------------------------
# 2. Doubt prioritization — which open doubts cost the most marks
# ---------------------------------------------------------------------------

def doubt_prioritization(
    *, db_path: str | Path = DEFAULT_DB_PATH,
) -> DoubtPrioritizationResult:
    """Rank open doubts by how many JEE marks they're potentially costing."""
    with _connect(db_path) as conn:
        if not _table_exists(conn, "op_jee_chapter_stats"):
            return DoubtPrioritizationResult(status="no_jee_data")
        if not _table_exists(conn, "doubts"):
            return DoubtPrioritizationResult(status="no_doubts")

        doubt_rows = conn.execute("""
            SELECT subject, core_concept, created_time
            FROM doubts
            WHERE archived=0 AND status='open'
            ORDER BY created_time ASC
        """).fetchall()

        if not doubt_rows:
            return DoubtPrioritizationResult(status="no_doubts")

        weight_rows = conn.execute("""
            SELECT subject, chapter, SUM(total_questions) AS total_questions,
                   AVG(importance_score) AS roi_score
            FROM op_jee_chapter_stats
            WHERE chapter != 'Unclassified'
            GROUP BY subject, chapter
            ORDER BY total_questions DESC
        """).fetchall()

        total_all = sum(r["total_questions"] for r in weight_rows) or 1
        weight_map: dict[str, dict[str, float | int]] = {}
        for r in weight_rows:
            key = ChapterKey.normalize(r["subject"], r["chapter"])
            weight_map[key] = {
                "total_questions": r["total_questions"],
                "roi_score": r["roi_score"],
                "weightage_pct": round(r["total_questions"] / total_all * 100, 1),
            }

        ranked = sorted(
            weight_map.values(),
            key=lambda x: x["total_questions"], reverse=True,
        )
        rank_lookup: dict[int, int] = {
            id(r): i + 1 for i, r in enumerate(ranked)
        }

        doubts: list[PrioritizedDoubt] = []
        for d in doubt_rows:
            subject = d["subject"] or ""
            concept = d["core_concept"] or ""

            best_match: dict[str, float | int] | None = None
            best_score = 0
            for key, info in weight_map.items():
                subj_part, chapter_part = key.split("|", 1)
                if _norm(subject) in subj_part or subj_part in _norm(subject):
                    concept_words = set(_norm(concept).split())
                    chapter_words = set(chapter_part.split())
                    overlap = len(concept_words & chapter_words)
                    if overlap > best_score:
                        best_score = overlap
                        best_match = info

            weightage_rank: int | None = None
            estimated_marks: int | None = None
            urgency = "unknown"

            if best_match is not None and best_score > 0:
                weightage_rank = rank_lookup.get(id(best_match), 999)
                estimated_marks = int(best_match["total_questions"]) * 4
                if weightage_rank <= 10:
                    urgency = "high"
                elif weightage_rank <= 30:
                    urgency = "medium"
                else:
                    urgency = "low"

            doubts.append(PrioritizedDoubt(
                subject=subject,
                core_concept=concept,
                created_time=d["created_time"],
                weightage_rank=weightage_rank,
                total_questions=int(best_match["total_questions"]) if best_match else None,
                estimated_marks=estimated_marks,
                urgency=urgency,
            ))

        urgency_order = {"high": 0, "medium": 1, "low": 2, "unknown": 3}
        doubts.sort(key=lambda d: (urgency_order.get(d["urgency"], 3), d["weightage_rank"] or 999))

        highest: str | None = None
        if doubts and doubts[0]["urgency"] == "high":
            top = doubts[0]
            highest = (
                f"Your doubt on '{top['core_concept'][:60]}' is in a chapter worth "
                f"~{top['estimated_marks']} marks in JEE. "
                f"Fixing this is your highest-leverage doubt."
            )

        return DoubtPrioritizationResult(
            status="ok",
            doubts=doubts,
            highest_priority=highest,
        )


# ---------------------------------------------------------------------------
# 3. Skip or study — chapter prioritization
# ---------------------------------------------------------------------------

def skip_or_study(
    *, db_path: str | Path = DEFAULT_DB_PATH,
) -> SkipOrStudyResult:
    """Recommend which chapters to prioritize vs deprioritize."""
    with _connect(db_path) as conn:
        if not _table_exists(conn, "op_jee_chapter_stats"):
            return SkipOrStudyResult(status="no_jee_data")

        accuracy_map: dict[str, float | None] = {}
        if _table_exists(conn, "ledger"):
            acc_rows = conn.execute("""
                SELECT subject, chapter_text AS chapter,
                       AVG(CASE WHEN questions_attempted > 0
                           THEN CAST(questions_correct AS REAL) / questions_attempted
                           ELSE NULL END) AS avg_accuracy
                FROM ledger
                WHERE archived=0 AND chapter_text IS NOT NULL AND chapter_text != ''
                GROUP BY subject, chapter_text
                HAVING COUNT(*) >= 2
            """).fetchall()
            for r in acc_rows:
                key = ChapterKey.normalize(r["subject"], r["chapter"])
                accuracy_map[key] = round(r["avg_accuracy"], 3) if r["avg_accuracy"] else None

        jee_rows = conn.execute("""
            SELECT subject, chapter,
                   SUM(total_questions) AS total_questions,
                   AVG(importance_score) AS roi_score,
                   AVG(hard_ratio) AS hard_ratio
            FROM op_jee_chapter_stats
            WHERE chapter != 'Unclassified'
            GROUP BY subject, chapter
            ORDER BY roi_score DESC
        """).fetchall()

        prioritize: list[ChapterRecommendation] = []
        deprioritize: list[ChapterRecommendation] = []
        balanced = 0

        for r in jee_rows:
            key = ChapterKey.normalize(r["subject"], r["chapter"])
            user_acc = accuracy_map.get(key)
            roi = r["roi_score"] or 0.0
            total_q = r["total_questions"] or 0
            hard = r["hard_ratio"] or 0.0

            reason_parts: list[str] = []
            if user_acc is not None:
                if user_acc < 0.5 and roi > 80:
                    reason_parts.append("high ROI, low accuracy")
                elif user_acc > 0.8 and roi < 40:
                    reason_parts.append("strong already, low ROI")
                else:
                    reason_parts.append("moderate")
            else:
                reason_parts.append("no user data")

            if hard > 0.4:
                reason_parts.append("hard-heavy")

            reason = "; ".join(reason_parts)

            if user_acc is not None and user_acc < 0.5 and roi > 80:
                prioritize.append(ChapterRecommendation(
                    subject=r["subject"], chapter=r["chapter"],
                    roi_score=round(roi, 1), total_questions=total_q,
                    user_accuracy=user_acc, reason=reason,
                ))
            elif roi < 30 or (user_acc is not None and user_acc > 0.8 and roi < 40):
                deprioritize.append(ChapterRecommendation(
                    subject=r["subject"], chapter=r["chapter"],
                    roi_score=round(roi, 1), total_questions=total_q,
                    user_accuracy=user_acc, reason=reason,
                ))
            else:
                balanced += 1

        return SkipOrStudyResult(
            status="ok",
            prioritize=prioritize[:10],
            deprioritize=deprioritize[:5],
            balanced=balanced,
        )


# ---------------------------------------------------------------------------
# 4. Trending chapters — which chapters are getting more questions recently
# ---------------------------------------------------------------------------

def trending_chapters(
    *, db_path: str | Path = DEFAULT_DB_PATH,
) -> TrendingResult:
    """Show which JEE chapters are trending up or down in recent years."""
    with _connect(db_path) as conn:
        if not _table_exists(conn, "op_jee_trends"):
            return TrendingResult(status="no_jee_data")

        rows = conn.execute("""
            SELECT subject, chapter, year, question_count
            FROM op_jee_trends
            WHERE chapter != 'Unclassified'
            ORDER BY subject, chapter, year
        """).fetchall()

        if not rows:
            return TrendingResult(status="no_jee_data")

        # Group by (subject, chapter), split into recent (2023+) vs previous
        from collections import defaultdict
        recent: dict[str, list[int]] = defaultdict(list)
        previous: dict[str, list[int]] = defaultdict(list)

        for r in rows:
            key = f"{r['subject']}|{r['chapter']}"
            year = int(r["year"]) if r["year"] else 0
            if year >= 2023:
                recent[key].append(r["question_count"])
            else:
                previous[key].append(r["question_count"])

        trending_up: list[TrendingChapter] = []
        trending_down: list[TrendingChapter] = []

        for key in set(recent) | set(previous):
            subject, chapter = key.split("|", 1)
            recent_vals = recent.get(key, [])
            prev_vals = previous.get(key, [])

            recent_avg = sum(recent_vals) / len(recent_vals) if recent_vals else 0.0
            prev_avg = sum(prev_vals) / len(prev_vals) if prev_vals else 0.0

            if prev_avg > 0:
                change_pct = ((recent_avg - prev_avg) / prev_avg) * 100
            elif recent_avg > 0:
                change_pct = 100.0
            else:
                continue

            entry = TrendingChapter(
                subject=subject, chapter=chapter,
                recent_avg=round(recent_avg, 1),
                previous_avg=round(prev_avg, 1),
                change_pct=round(change_pct, 1),
            )
            if change_pct > 15:
                trending_up.append(entry)
            elif change_pct < -15:
                trending_down.append(entry)

        trending_up.sort(key=lambda x: x["change_pct"], reverse=True)
        trending_down.sort(key=lambda x: x["change_pct"])

        return TrendingResult(
            status="ok",
            trending_up=trending_up[:10],
            trending_down=trending_down[:5],
        )


# ---------------------------------------------------------------------------
# 5. Formula priority — rank formulas by JEE importance
# ---------------------------------------------------------------------------

def formula_priority(
    *, db_path: str | Path = DEFAULT_DB_PATH,
) -> FormulaPriorityResult:
    """Rank stored formulas by how many JEE patterns reference their chapter."""
    with _connect(db_path) as conn:
        if not _table_exists(conn, "learn_formulas"):
            return FormulaPriorityResult(status="no_formulas")
        if not _table_exists(conn, "op_jee_patterns"):
            return FormulaPriorityResult(status="no_jee_data")

        formulas = conn.execute("""
            SELECT formula_id, subject, chapter, topic, formula_text, mastery
            FROM learn_formulas
            ORDER BY created_at ASC
        """).fetchall()

        if not formulas:
            return FormulaPriorityResult(status="no_formulas")

        # Count JEE patterns per chapter
        pattern_rows = conn.execute("""
            SELECT chapter, COUNT(*) AS pattern_count,
                   SUM(frequency) AS total_frequency
            FROM op_jee_patterns
            WHERE chapter != 'Unclassified'
            GROUP BY chapter
        """).fetchall()

        pattern_map: dict[str, dict[str, int]] = {}
        for r in pattern_rows:
            pattern_map[_norm(r["chapter"])] = {
                "pattern_count": r["pattern_count"],
                "total_frequency": r["total_frequency"],
            }

        ranked: list[RankedFormula] = []
        for f in formulas:
            chapter_key = _norm(f["chapter"] or "")
            info = pattern_map.get(chapter_key)
            patterns = info["pattern_count"] if info else 0
            frequency = info["total_frequency"] if info else 0

            # Rough estimate: 4 marks per pattern appearance
            marks = round(frequency * 4, 0) if frequency > 0 else None

            if patterns >= 10:
                priority = "high"
            elif patterns >= 5:
                priority = "medium"
            else:
                priority = "low"

            ranked.append(RankedFormula(
                formula_id=f["formula_id"],
                subject=f["subject"],
                chapter=f["chapter"],
                topic=f["topic"],
                formula_text=f["formula_text"],
                mastery=f["mastery"],
                jee_patterns=patterns,
                jee_marks_estimate=int(marks) if marks else None,
                priority=priority,
            ))

        # Sort: high priority first, then by pattern count
        priority_order = {"high": 0, "medium": 1, "low": 2}
        ranked.sort(key=lambda f: (priority_order.get(f["priority"], 2), -f["jee_patterns"]))

        highest: str | None = None
        if ranked and ranked[0]["priority"] == "high":
            top = ranked[0]
            highest = (
                f"'{top['formula_text'][:50]}' appears in {top['jee_patterns']} JEE patterns "
                f"(~{top['jee_marks_estimate']} marks). Master this first."
            )

        return FormulaPriorityResult(
            status="ok",
            formulas=ranked,
            highest_priority=highest,
        )


# ---------------------------------------------------------------------------
# 6. Strengths vs JEE reality — accuracy vs difficulty distribution
# ---------------------------------------------------------------------------

def strengths_vs_reality(
    *, db_path: str | Path = DEFAULT_DB_PATH,
) -> StrengthsResult:
    """Compare user's accuracy per chapter with the chapter's JEE difficulty."""
    with _connect(db_path) as conn:
        if not _table_exists(conn, "op_jee_chapter_stats"):
            return StrengthsResult(status="no_jee_data")
        if not _table_exists(conn, "ledger"):
            return StrengthsResult(status="no_ledger")

        # User accuracy per chapter (need 2+ sessions for signal)
        acc_rows = conn.execute("""
            SELECT subject, chapter_text AS chapter,
                   AVG(CASE WHEN questions_attempted > 0
                       THEN CAST(questions_correct AS REAL) / questions_attempted
                       ELSE NULL END) AS avg_accuracy
            FROM ledger
            WHERE archived=0 AND chapter_text IS NOT NULL AND chapter_text != ''
            GROUP BY subject, chapter_text
            HAVING COUNT(*) >= 2
        """).fetchall()

        if not acc_rows:
            return StrengthsResult(status="no_ledger")

        jee_map = _jee_chapters(conn)

        strengths: list[ChapterStrength] = []
        gaps: list[ChapterStrength] = []

        for r in acc_rows:
            key = ChapterKey.normalize(r["subject"], r["chapter"])
            jee = jee_map.get(key)
            if jee is None:
                continue

            user_acc = r["avg_accuracy"] or 0.0
            hard = jee.hard_ratio

            # gap_score: how much harder the chapter is vs how well you do
            # positive gap = chapter is harder than your accuracy suggests
            gap_score = hard - user_acc

            if user_acc >= 0.7 and hard <= 0.3:
                insight = f"Strong in an easy chapter (accuracy {user_acc:.0%}, {hard:.0%} hard)"
                strengths.append(ChapterStrength(
                    subject=r["subject"], chapter=r["chapter"],
                    user_accuracy=round(user_acc, 3),
                    hard_ratio=round(hard, 3),
                    gap_score=round(gap_score, 3),
                    insight=insight,
                ))
            elif gap_score > 0.15:
                insight = (
                    f"Accuracy {user_acc:.0%} but chapter is {hard:.0%} hard — "
                    f"you may be avoiding the tough questions"
                )
                gaps.append(ChapterStrength(
                    subject=r["subject"], chapter=r["chapter"],
                    user_accuracy=round(user_acc, 3),
                    hard_ratio=round(hard, 3),
                    gap_score=round(gap_score, 3),
                    insight=insight,
                ))

        gaps.sort(key=lambda g: g["gap_score"], reverse=True)
        strengths.sort(key=lambda s: s["user_accuracy"], reverse=True)

        biggest: str | None = None
        if gaps:
            top = gaps[0]
            biggest = (
                f"Biggest gap: {top['subject']}: {top['chapter']} — "
                f"accuracy {top['user_accuracy']:.0%} in a chapter that's "
                f"{top['hard_ratio']:.0%} hard questions."
            )

        return StrengthsResult(
            status="ok",
            strengths=strengths[:10],
            gaps=gaps[:10],
            biggest_gap=biggest,
        )
