"""
Replication of Notion's read-only Ledger formula columns.

The Notion API only returns formula *results*, not expressions — these were
extracted manually from Notion's formula editor and replicated here so the
SQLite mirror can materialise them as real, queryable columns during sync.

Formulas (confirmed 2026-07-19):
- cognitive_yield: round(1.666 * T_target * Accuracy^(10/t_q) * velocity)
  where velocity = min(T_target / T, 1.5)
- theory_yield:    same but velocity UNCAPPED (Base_Velocity = T_target / T)
- accuracy_ratio:  C / A  (0 if A==0, 1 if C > A)
- mins_per_question: T / A

t_q = target minutes per question, varies by Exercise Type and Subject.
Subject strings are "Chem"/"Physics"/"Maths" (live Notion select options).
"""

from __future__ import annotations

from typing import Optional


# Target minutes per question by exercise type + subject.
# Rows: exercise type. Columns: subject. Fallback per row then 4.
# Values confirmed from the live Notion formula (2026-07-19).
_TQ_TABLE: dict[str, dict[str, float]] = {
    "JMYL": {},                       # 4 for all subjects
    "JAYL": {},                       # 8 for all subjects
    "PYQs": {},                       # 4.5 for all subjects
    "Ex 1A": {"Chem": 2.0, "Physics": 2.5, "Maths": 4.5, "_": 3.0},
    "Ex 1B": {"Chem": 2.5, "Physics": 3.5, "Maths": 6.0, "_": 4.0},
    "Ex 2A": {"Chem": 2.5, "Physics": 4.5, "Maths": 6.5, "_": 4.5},
    "Ex 2B": {"Chem": 2.5, "Physics": 4.5, "Maths": 6.5, "_": 4.5},
    "MLE":  {"Chem": 3.0, "Physics": 5.0, "Maths": 5.5, "_": 4.5},
    "Ex 4A": {"Chem": 10.0, "Physics": 13.0, "Maths": 15.0, "_": 12.0},
    "Ex 4B": {"Chem": 10.0, "Physics": 13.0, "Maths": 15.0, "_": 12.0},
    "Ex 3A": {"Chem": 12.0, "Physics": 15.0, "Maths": 18.0, "_": 15.0},
    "Ex 3B": {"Chem": 12.0, "Physics": 15.0, "Maths": 18.0, "_": 15.0},
}

# Flat fallback for uniform-per-type rows.
_TQ_UNIFORM: dict[str, float] = {
    "JMYL": 4.0,
    "JAYL": 8.0,
    "PYQs": 4.5,
}

_DEFAULT_TQ = 4.0


def t_q_for(subject: Optional[str], exercise_type: Optional[str]) -> float:
    """Target minutes per question for a (subject, exercise_type) pair."""
    if exercise_type is None:
        return _DEFAULT_TQ
    et = str(exercise_type).strip()
    if et in _TQ_UNIFORM:
        return _TQ_UNIFORM[et]
    row = _TQ_TABLE.get(et)
    if row is None:
        return _DEFAULT_TQ
    s = str(subject).strip() if subject else ""
    return row.get(s, row.get("_", _DEFAULT_TQ))


def accuracy_ratio(
    questions_attempted: Optional[float],
    questions_correct: Optional[float],
) -> float:
    """C / A, capped at 1. Returns 0 if A is 0/None."""
    if questions_attempted is None or questions_correct is None:
        return 0.0
    A = questions_attempted
    C = questions_correct
    if A < 0 or C < 0:
        raise ValueError("question counts cannot be negative")
    if A == 0:
        return 0.0
    if C > A:
        return 1.0
    return C / A


def mins_per_question(
    actual_time_min: Optional[float],
    questions_attempted: Optional[float],
) -> Optional[float]:
    """T / A. Returns None if A is 0/None (Notion shows blank)."""
    if actual_time_min is None or questions_attempted is None:
        return None
    T = actual_time_min
    A = questions_attempted
    if T < 0 or A < 0:
        raise ValueError("time and attempted count cannot be negative")
    if A == 0:
        return None
    return T / A


def _accuracy(questions_attempted: float, questions_correct: float) -> float:
    if questions_attempted < 0 or questions_correct < 0:
        raise ValueError("question counts cannot be negative")
    if questions_attempted == 0:
        return 0.0
    if questions_correct > questions_attempted:
        return 1.0
    return questions_correct / questions_attempted


def cognitive_yield(
    subject: Optional[str],
    exercise_type: Optional[str],
    actual_time_min: Optional[float],
    questions_attempted: Optional[float],
    questions_correct: Optional[float],
) -> int:
    """Round(1.666 * T_target * Accuracy^(10/t_q) * velocity), velocity capped at 1.5."""
    t_q = t_q_for(subject, exercise_type)
    T = float(actual_time_min or 0)
    A = float(questions_attempted or 0)
    C = float(questions_correct or 0)
    T_target = A * t_q
    accuracy = _accuracy(A, C)
    if T == 0:
        velocity = 0.0
    else:
        ratio = T_target / T
        velocity = 1.5 if ratio > 1.5 else ratio
    if T > 0 and A > 0:
        return round(1.666 * T_target * (accuracy ** (10 / t_q)) * velocity)
    return 0


def theory_yield(
    subject: Optional[str],
    exercise_type: Optional[str],
    actual_time_min: Optional[float],
    questions_attempted: Optional[float],
    questions_correct: Optional[float],
) -> int:
    """Same as cognitive_yield but velocity UNCAPPED (Base_Velocity = T_target/T)."""
    t_q = t_q_for(subject, exercise_type)
    T = float(actual_time_min or 0)
    A = float(questions_attempted or 0)
    C = float(questions_correct or 0)
    T_target = A * t_q
    accuracy = _accuracy(A, C)
    velocity = 0.0 if T == 0 else T_target / T
    if T > 0 and A > 0:
        return round(1.666 * T_target * (accuracy ** (10 / t_q)) * velocity)
    return 0
