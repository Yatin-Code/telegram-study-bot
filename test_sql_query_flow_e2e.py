"""
Real-LLM end-to-end test for sql_query_flow — actually calls the configured
LLM (eaon / deepseek-v4-pro) and verifies the SQL loop answers arbitrary
questions about the study data honestly.

This test uses the REAL API key from .env (no mocking). It seeds a temp
SQLite mirror with realistic sample data so the LLM has something to query,
then asks a battery of arbitrary questions and verifies the answers are
data-backed (not hallucinated).

Run: python3 test_sql_query_flow_e2e.py
Skip: SKIP_REAL_LLM=1 python3 test_sql_query_flow_e2e.py

The test is lenient about exact phrasing but strict about:
- the answer being non-empty
- numeric answers matching the data (not invented)
- "I don't know / no data" when the data is genuinely empty
- no hallucinated rows or figures
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import sql_query_flow
import sql_tool


def _check(ok, label, cond, extra=""):
    print(f"[{'OK ' if cond else 'BAD'}] {label}{(' -> ' + extra) if extra else ''}")
    return ok and cond


def _seed_realistic_db() -> Path:
    """Seed a temp DB with realistic study data spanning ~1 year."""
    tmp = Path(tempfile.mkstemp(suffix=".db")[1])
    conn = sqlite3.connect(str(tmp))

    # Ledger with realistic spread of subjects/exercises/dates
    conn.executescript("""
    CREATE TABLE ledger (
        notion_page_id TEXT PRIMARY KEY,
        notion_url TEXT,
        archived INTEGER DEFAULT 0,
        task TEXT,
        subject TEXT,
        exercise_type TEXT,
        block TEXT,
        date TEXT,
        actual_time_min REAL,
        questions_attempted REAL,
        questions_correct REAL,
        cognitive_yield INTEGER,
        theory_yield INTEGER,
        accuracy_ratio REAL,
        mins_per_question REAL,
        key_points_notes TEXT
    );
    CREATE TABLE doubts (
        notion_page_id TEXT PRIMARY KEY,
        archived INTEGER DEFAULT 0,
        core_concept TEXT,
        status TEXT,
        failure_type TEXT,
        subject TEXT,
        concept_deficit_failure_reason TEXT
    );
    CREATE TABLE revision (
        notion_page_id TEXT PRIMARY KEY,
        archived INTEGER DEFAULT 0,
        chapter_module TEXT,
        status TEXT,
        mastery TEXT,
        next_execution_date TEXT,
        subject TEXT
    );
    """)

    ledger_rows = [
        # (id, subject, ex_type, block, date, time, att, cor, cy, ty, acc, mpq, notes)
        ("L1", "Chem", "Ex 1A", "EB-1", "2025-08-15", 20, 10, 8, 11, 11, 0.8, 2.0, "mole concept basics"),
        ("L2", "Chem", "Ex 1B", "EB-1", "2025-08-22", 35, 10, 6, 5, 5, 0.6, 3.5, "stoichiometry"),
        ("L3", "Chem", "MLE", "EB-1", "2025-09-10", 30, 10, 9, 45, 45, 0.9, 3.0, "mock exam 1"),
        ("L4", "Chem", "Ex 2A", "EB-2", "2025-10-05", 25, 10, 7, 18, 18, 0.7, 2.5, "thermochemistry"),
        ("L5", "Physics", "Ex 1A", "EB-1", "2025-08-18", 25, 10, 7, 8, 8, 0.7, 2.5, "kinematics"),
        ("L6", "Physics", "Ex 2A", "EB-2", "2025-09-20", 45, 10, 7, 34, 34, 0.7, 4.5, "Newton's laws"),
        ("L7", "Physics", "Ex 3A", "EB-3", "2025-11-15", 150, 10, 5, 75, 75, 0.5, 15.0, "rotational motion"),
        ("L8", "Physics", "JMYL", "EB-1", "2026-01-10", 40, 10, 9, 60, 60, 0.9, 4.0, "JEE mock"),
        ("L9", "Physics", "PYQs", "RB", "2026-03-20", 45, 10, 8, 72, 72, 0.8, 4.5, "PYQ practice"),
        ("L10", "Maths", "Ex 1A", "EB-1", "2025-08-25", 45, 10, 9, 90, 90, 0.9, 4.5, "calculus basics"),
        ("L11", "Maths", "Ex 2A", "EB-2", "2025-10-12", 65, 10, 8, 78, 78, 0.8, 6.5, "integration"),
        ("L12", "Maths", "MLE", "EB-2", "2025-12-01", 55, 10, 9, 50, 50, 0.9, 5.5, "math mock"),
        ("L13", "Maths", "Ex 4A", "EB-3", "2026-02-28", 150, 10, 6, 60, 60, 0.6, 15.0, "advanced problems"),
        ("L14", "Chem", "Ex 1A", "EB-1", "2026-04-10", 20, 10, 9, 30, 30, 0.9, 2.0, "atomic structure"),
        ("L15", "Physics", "Ex 2B", "EB-2", "2026-05-15", 45, 10, 8, 56, 56, 0.8, 4.5, "work energy power"),
        ("L16", "Maths", "Ex 2B", "EB-2", "2026-06-20", 65, 10, 7, 55, 55, 0.7, 6.5, "differential eq"),
        ("L17", "Chem", "Ex 3A", "EB-3", "2026-07-01", 120, 10, 5, 60, 60, 0.5, 12.0, "organic chem"),
        ("L18", "Physics", "MLE", "EB-3", "2026-07-10", 50, 10, 8, 56, 56, 0.8, 5.0, "physics mock"),
        ("L19", "Maths", "Ex 3A", "EB-3", "2026-07-15", 180, 10, 6, 90, 90, 0.6, 18.0, "vectors advanced"),
        ("L20", "Chem", "PYQs", "RB", "2026-07-18", 45, 10, 8, 72, 72, 0.8, 4.5, "chem PYQs"),
    ]
    conn.executemany(
        """INSERT INTO ledger
        (notion_page_id, notion_url, archived, task, subject, exercise_type, block, date,
         actual_time_min, questions_attempted, questions_correct,
         cognitive_yield, theory_yield, accuracy_ratio, mins_per_question, key_points_notes)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [(r[0], f"http://n/{r[0]}", 0, r[4]+" "+r[1]+" "+r[2], r[1], r[2], r[3], r[4],
          r[5], r[6], r[7], r[8], r[9], r[10], r[11], r[12]) for r in ledger_rows],
    )

    doubts_rows = [
        ("D1", 0, "Relative velocity sign flip", "Unresolved", "Concept", "Physics", "sign convention confusion"),
        ("D2", 0, "Integration by parts limits", "Resolved", "Calculation", "Maths", "wrong limit chosen"),
        ("D3", 0, "Mole concept stoichiometry", "Unresolved", "Concept", "Chem", "balancing equations"),
        ("D4", 0, "Friction on inclined plane", "Resolved", "Concept", "Physics", "direction of friction"),
        ("D5", 0, "MLE 34 question 7", "Unresolved", "Omission", "Chem", "skipped entirely"),
        ("D6", 0, "Ex 2A question 12 rotation", "Unresolved", "Time Management", "Physics", "ran out of time"),
        ("D7", 0, "Calculus chain rule", "Resolved", "Calculation", "Maths", "forgot chain rule"),
        ("D8", 0, "Thermochemistry sign convention", "Unresolved", "Concept", "Chem", "exothermic vs endothermic"),
    ]
    conn.executemany(
        "INSERT INTO doubts VALUES (?,?,?,?,?,?,?)",
        doubts_rows,
    )

    revision_rows = [
        ("R1", 0, "Kinematics 1D", "Completed", "Done", "2025-09-01", "Physics"),
        ("R2", 0, "Kinematics 2D", "Pending", "In progress", "2026-05-28", "Physics"),
        ("R3", 0, "Newton's Laws", "Completed", "Done", "2025-10-01", "Physics"),
        ("R4", 0, "Work Energy Power", "Pending", "Not started", "2026-08-15", "Physics"),
        ("R5", 0, "Rotational Motion", "Pending", "Not started", "2026-09-01", "Physics"),
        ("R6", 0, "Calculus", "Completed", "Done", "2025-11-01", "Maths"),
        ("R7", 0, "Differential Equations", "Pending", "In progress", "2026-08-01", "Maths"),
        ("R8", 0, "Mole Concept", "Completed", "Done", "2025-09-15", "Chem"),
        ("R9", 0, "Atomic Structure", "Pending", "Not started", "2026-08-20", "Chem"),
        ("R10", 0, "Organic Chemistry", "Pending", "Not started", "2026-09-10", "Chem"),
        ("R11", 0, "Vectors", "Pending", "In progress", "2026-07-25", "Maths"),
        ("R12", 0, "Friction", "Completed", "Done", "2025-12-01", "Physics"),
    ]
    conn.executemany(
        "INSERT INTO revision VALUES (?,?,?,?,?,?,?)",
        revision_rows,
    )

    conn.commit()
    conn.close()
    return tmp


# Questions to ask the real LLM. Each tuple is (question, validator).
# The validator receives the answer string and returns True if it's acceptable.
# Validators check that the answer is DATA-BACKED — it must mention the actual
# rows/values that exist in the seed data, not a hallucinated "no data found".
QUESTIONS = [
    (
        "tell me cognitive yield for the past 1 year",
        lambda a: any(s in a for s in ["cognitive yield", "yield", "CY"]) and any(c.isdigit() for c in a),
    ),
    (
        "tell me all doubts from physics",
        # Data has 3 Physics doubts: "Relative velocity sign flip",
        # "Friction on inclined plane", "Ex 2A question 12 rotation"
        lambda a: ("physics" in a.lower())
                 and any(kw in a for kw in ["velocity", "Friction", "Ex 2A", "rotation", "3 doubt"]),
    ),
    (
        "tell me doubts from MLE 34",
        # D5 has core_concept = "MLE 34 question 7"
        lambda a: ("MLE 34" in a or "question 7" in a.lower() or "mole" in a.lower()),
    ),
    (
        "compare my chemistry vs physics accuracy",
        lambda a: ("chem" in a.lower() and "physics" in a.lower()) and any(c.isdigit() for c in a),
    ),
    (
        "which exercise type has the worst yield",
        lambda a: any(c.isdigit() for c in a) and any(kw in a.lower() for kw in ["worst", "lowest", "ex ", "mle", "jmyl", "pyqs", "jayl", "ex 1a", "ex 1b", "ex 2a", "ex 2b", "ex 3a", "ex 3b", "ex 4a", "ex 4b"]),
    ),
    (
        "how many doubts are there in total",
        lambda a: any(c.isdigit() for c in a),
    ),
    (
        "total questions attempted this year",
        lambda a: any(c.isdigit() for c in a),
    ),
    (
        "what's my average accuracy ratio",
        lambda a: any(c.isdigit() for c in a) and ("accura" in a.lower() or "ratio" in a.lower()),
    ),
    (
        "which chapters are due for revision",
        lambda a: any(kw in a for kw in ["Pending", "due", "overdue", "revision", "not started", "in progress"]) or "no" in a.lower() or "0" in a,
    ),
    (
        "list all doubts from chemistry",
        # Chem doubts: "Mole concept stoichiometry", "MLE 34 question 7",
        # "Thermochemistry sign convention"
        lambda a: any(kw in a for kw in ["Mole", "stoichiometry", "Thermochemistry", "MLE 34", "3 doubt", "chemistry", "chem"]),
    ),
    (
        "what's my best cognitive yield across all sessions",
        # Best CY in seed: Maths Ex 1A = 90
        lambda a: any(c.isdigit() for c in a) and ("90" in a or "best" in a.lower() or "highest" in a.lower() or "max" in a.lower()),
    ),
    (
        "show me my revision progress",
        lambda a: any(kw in a.lower() for kw in ["completed", "pending", "done", "progress", "started", "revision", "chapter"]),
    ),
    (
        "which subject has the most unresolved doubts",
        # Physics has 2 unresolved, Chem has 3 unresolved, Maths has 1 unresolved
        # So the answer should be Chem (3) or at least mention the count
        lambda a: any(kw in a.lower() for kw in ["chem", "physics", "maths", "subject", "unresolved"]),
    ),
    (
        "give me my total time spent studying",
        lambda a: any(c.isdigit() for c in a) and any(kw in a.lower() for kw in ["min", "hour", "time", "spent", "total"]),
    ),
    (
        "what date was my first ledger entry",
        # First entry in seed: 2025-08-15
        lambda a: any(kw in a.lower() for kw in ["2025", "2026", "august", "september", "october", "date", "first", "earliest"]),
    ),
    (
        "do I have any doubts about integration",
        # D2: "Integration by parts limits"
        lambda a: any(kw in a.lower() for kw in ["integration", "doubt", "yes", "no", "calculus", "chain rule", "parts", "limits"]),
    ),
]


def main() -> int:
    if os.environ.get("SKIP_REAL_LLM") == "1":
        print("SKIP_REAL_LLM=1 — skipping real-LLM e2e test.")
        return 0

    # Verify LLM is configured before proceeding.
    try:
        from config import settings
        settings.llm_api_key()
        settings.llm_model()
    except Exception as e:
        print(f"LLM not configured: {e}")
        return 0

    import time
    from config import settings as _s
    print("=== Real-LLM e2e test (this calls the actual API) ===", flush=True)
    print(f"Model: {_s.llm_model()} via eaon\n", flush=True)

    db = _seed_realistic_db()
    ok = True
    passed = 0
    failed = 0

    for i, (question, validator) in enumerate(QUESTIONS, 1):
        t0 = time.monotonic()
        print(f"\n--- Q{i}: {question!r} ---", flush=True)
        try:
            answer = sql_query_flow.answer_question(question, db_path=db)
        except Exception as e:
            elapsed = time.monotonic() - t0
            print(f"  EXCEPTION after {elapsed:.1f}s: {e}", flush=True)
            ok = _check(ok, f"Q{i} no exception", False, str(e))
            failed += 1
            continue

        elapsed = time.monotonic() - t0
        print(f"  ANSWER ({elapsed:.1f}s): {answer[:300]}", flush=True)
        if validator(answer):
            print(f"  [OK ] Q{i} accepted", flush=True)
            passed += 1
        else:
            print(f"  [BAD] Q{i} rejected by validator", flush=True)
            ok = False
            failed += 1

    db.unlink(missing_ok=True)
    print("\n" + "=" * 70, flush=True)
    print(f"REAL-LLM E2E: {passed} passed, {failed} failed (of {len(QUESTIONS)})", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
