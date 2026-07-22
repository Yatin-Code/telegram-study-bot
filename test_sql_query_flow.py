"""
Tests for sql_query_flow.py — the LLM SQL query loop.

Mocks the LLM transport so no real API calls are made. Run: python3 test_sql_query_flow.py

Verifies:
- SQL extraction from ```sql``` fences, bare ```SELECT```, and SQL: prefix
- ANSWER: extraction
- The loop runs SQL, feeds results back, and stops on ANSWER:
- Max iterations forces a final answer
- LLM failure produces a graceful error message
- Read-only enforcement still applies (the loop can't write)
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import sql_query_flow
import sql_tool


def _check(ok, label, cond, extra=""):
    print(f"[{'OK ' if cond else 'BAD'}] {label}{(' -> ' + extra) if extra else ''}")
    return ok and cond


def _seed_temp_db() -> Path:
    """Create a temp SQLite DB mirroring the real schema for testing."""
    tmp = Path(tempfile.mkstemp(suffix=".db")[1])
    conn = sqlite3.connect(str(tmp))
    conn.execute(
        """CREATE TABLE ledger (
            notion_page_id TEXT PRIMARY KEY,
            notion_url TEXT,
            archived INTEGER DEFAULT 0,
            last_edited_time TEXT,
            subject TEXT,
            exercise_type TEXT,
            date TEXT,
            actual_time_min REAL,
            questions_attempted REAL,
            questions_correct REAL,
            cognitive_yield INTEGER,
            theory_yield INTEGER,
            accuracy_ratio REAL,
            mins_per_question REAL
        )"""
    )
    conn.executemany(
        """INSERT INTO ledger
        (notion_page_id, notion_url, archived, subject, exercise_type, date,
         actual_time_min, questions_attempted, questions_correct,
         cognitive_yield, theory_yield, accuracy_ratio, mins_per_question)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            ("p1", "http://n/p1", 0, "Chem", "Ex 1A", "2026-07-19", 20, 10, 8, 11, 11, 0.8, 2.0),
            ("p2", "http://n/p2", 0, "Physics", "Ex 2A", "2026-06-15", 45, 10, 7, 34, 34, 0.7, 4.5),
            ("p3", "http://n/p3", 0, "Maths", "MLE", "2026-01-10", 55, 10, 9, 50, 50, 0.9, 5.5),
        ],
    )
    conn.execute("CREATE TABLE doubts (notion_page_id TEXT PRIMARY KEY, archived INTEGER DEFAULT 0, subject TEXT, status TEXT, core_concept TEXT)")
    conn.execute("INSERT INTO doubts VALUES ('d1', 0, 'PHYSICS', 'Unresolved', 'velocity sign')")
    conn.execute("INSERT INTO doubts VALUES ('d2', 0, 'PHYSICS', 'Resolved', 'integration limits')")
    conn.execute("INSERT INTO doubts VALUES ('d3', 0, 'CHEMISTRY', 'Unresolved', 'mole concept')")
    conn.commit()
    conn.close()
    return tmp


def test_extract_sql() -> bool:
    ok = True
    print("=== _extract_sql ===")

    # Fenced ```sql``` block
    s = sql_query_flow._extract_sql("Let me check.\n```sql\nSELECT 1\n```\n")
    ok = _check(ok, "fenced sql block", s == "SELECT 1")

    # Bare ```SELECT``` block
    s = sql_query_flow._extract_sql("```\nSELECT * FROM ledger\n```")
    ok = _check(ok, "bare SELECT block", s == "SELECT * FROM ledger")

    # SQL: prefix
    s = sql_query_flow._extract_sql("SQL: SELECT COUNT(*) FROM ledger")
    ok = _check(ok, "SQL: prefix", s == "SELECT COUNT(*) FROM ledger")

    # No SQL (pure answer)
    s = sql_query_flow._extract_sql("ANSWER: There are 3 rows.")
    ok = _check(ok, "answer returns None for SQL", s is None)

    # Multiline SQL in fence
    s = sql_query_flow._extract_sql("```sql\nSELECT a, b\nFROM t\nWHERE x = 1\n```")
    ok = _check(ok, "multiline fenced SQL", s is not None and "FROM t" in s)

    assert ok


def test_extract_answer() -> bool:
    ok = True
    print("\n=== _extract_answer ===")

    a = sql_query_flow._extract_answer("ANSWER: You have 3 entries.")
    ok = _check(ok, "ANSWER: prefix", a == "You have 3 entries.")

    a = sql_query_flow._extract_answer("Some prose without SQL or ANSWER.")
    ok = _check(ok, "no SQL -> whole text is answer", a == "Some prose without SQL or ANSWER.")

    # When SQL is present, no ANSWER: -> None (waiting for answer)
    a = sql_query_flow._extract_answer("```sql\nSELECT 1\n```")
    ok = _check(ok, "SQL present, no ANSWER -> None", a is None)

    # Multi-line answer
    a = sql_query_flow._extract_answer("ANSWER:\nLine 1\nLine 2")
    ok = _check(ok, "multi-line answer", a is not None and "Line 1" in a)

    assert ok


def test_loop_single_query_then_answer() -> bool:
    ok = True
    print("\n=== Loop: one SQL then answer ===")
    db = _seed_temp_db()

    # Mock LLM: first call returns SQL, second returns ANSWER.
    responses = iter([
        "```sql\nSELECT COUNT(*) AS n FROM ledger WHERE archived=0\n```",
        f"ANSWER: You have 3 active ledger entries.",
    ])
    def mock_call(messages, **kw):
        return next(responses)

    with patch.object(sql_query_flow, "_call_llm", side_effect=mock_call):
        answer = sql_query_flow.answer_question("how many ledger entries?", db_path=db)
    ok = _check(ok, "returns the answer text", "3 active ledger" in answer, answer)

    db.unlink(missing_ok=True)
    assert ok


def test_loop_multi_query() -> bool:
    ok = True
    print("\n=== Loop: multiple SQL queries ===")
    db = _seed_temp_db()

    responses = iter([
        "```sql\nSELECT subject, COUNT(*) AS n FROM ledger WHERE archived=0 GROUP BY subject\n```",
        "```sql\nSELECT subject, SUM(questions_correct) AS correct FROM ledger WHERE archived=0 GROUP BY subject\n```",
        "ANSWER: Physics has 7 correct, Maths 9, Chem 8.",
    ])
    def mock_call(messages, **kw):
        return next(responses)

    with patch.object(sql_query_flow, "_call_llm", side_effect=mock_call):
        answer = sql_query_flow.answer_question("breakdown by subject", db_path=db)
    ok = _check(ok, "final answer mentions all subjects",
                "Physics" in answer and "Maths" in answer and "Chem" in answer, answer)

    db.unlink(missing_ok=True)
    assert ok


def test_loop_sql_error_recovery() -> bool:
    ok = True
    print("\n=== Loop: SQL error -> fix and retry ===")
    db = _seed_temp_db()

    responses = iter([
        "```sql\nSELECT * FROM nonexistent_table\n```",  # will error
        "```sql\nSELECT COUNT(*) AS n FROM ledger WHERE archived=0\n```",  # fixed
        "ANSWER: Found 3 entries after fixing my query.",
    ])
    def mock_call(messages, **kw):
        return next(responses)

    with patch.object(sql_query_flow, "_call_llm", side_effect=mock_call):
        answer = sql_query_flow.answer_question("count entries", db_path=db)
    ok = _check(ok, "recovered from SQL error", "3 entries" in answer, answer)

    db.unlink(missing_ok=True)
    assert ok


def test_loop_max_iterations() -> bool:
    ok = True
    print("\n=== Loop: max iterations forces answer ===")
    db = _seed_temp_db()

    # Always returns SQL, never answers — should hit max and force an answer.
    call_count = [0]
    max_iters = 2
    def mock_call(messages, **kw):
        call_count[0] += 1
        # The loop calls the LLM max_iters times for SQL, then once more after
        # the force-answer prompt (total max_iters + 1 calls).
        if call_count[0] <= max_iters:
            return "```sql\nSELECT 1 AS x\n```"
        return "ANSWER: I hit my query limit but here's what I found."

    with patch.object(sql_query_flow, "_call_llm", side_effect=mock_call):
        answer = sql_query_flow.answer_question("something", db_path=db, max_iterations=max_iters)
    ok = _check(ok, "forced answer after max iterations",
                "query limit" in answer or "what I found" in answer, answer)

    db.unlink(missing_ok=True)
    assert ok


def test_loop_llm_failure() -> bool:
    ok = True
    print("\n=== Loop: LLM failure -> graceful error ===")
    db = _seed_temp_db()

    def mock_call(messages, **kw):
        raise ConnectionError("LLM unreachable")

    with patch.object(sql_query_flow, "_call_llm", side_effect=mock_call):
        answer = sql_query_flow.answer_question("anything", db_path=db)
    ok = _check(ok, "graceful error message", "couldn't reach the LLM" in answer, answer)

    db.unlink(missing_ok=True)
    assert ok


def test_loop_cannot_write() -> bool:
    ok = True
    print("\n=== Loop: LLM tries to write -> blocked ===")
    db = _seed_temp_db()

    responses = iter([
        # Malicious/buggy LLM tries a DELETE
        "```sql\nDELETE FROM ledger WHERE archived=0\n```",
        "ANSWER: I couldn't modify the data (read-only).",
    ])
    def mock_call(messages, **kw):
        return next(responses)

    with patch.object(sql_query_flow, "_call_llm", side_effect=mock_call):
        answer = sql_query_flow.answer_question("delete everything", db_path=db)
    ok = _check(ok, "write was blocked, answer given", "couldn't" in answer or "read-only" in answer, answer)

    # Verify data is intact
    r = sql_tool.run_sql("SELECT COUNT(*) AS n FROM ledger WHERE archived=0", db_path=db)
    ok = _check(ok, "data intact after write attempt", r["rows"][0]["n"] == 3)

    db.unlink(missing_ok=True)
    assert ok


def test_loop_realistic_questions() -> bool:
    ok = True
    print("\n=== Loop: realistic study questions ===")
    db = _seed_temp_db()

    # Question 1: "cognitive yield for physics"
    responses = iter([
        "```sql\nSELECT cognitive_yield FROM ledger WHERE subject='Physics' AND archived=0\n```",
        "ANSWER: Your Physics cognitive yield is 34.",
    ])
    with patch.object(sql_query_flow, "_call_llm", side_effect=lambda *a, **k: next(responses)):
        answer = sql_query_flow.answer_question("cognitive yield for physics", db_path=db)
    ok = _check(ok, "physics cognitive yield answered", "34" in answer, answer)

    # Question 2: "all unresolved doubts"
    responses = iter([
        "```sql\nSELECT core_concept FROM doubts WHERE status='Unresolved' AND archived=0\n```",
        "ANSWER: You have 2 unresolved doubts: velocity sign and mole concept.",
    ])
    with patch.object(sql_query_flow, "_call_llm", side_effect=lambda *a, **k: next(responses)):
        answer = sql_query_flow.answer_question("unresolved doubts", db_path=db)
    ok = _check(ok, "unresolved doubts answered", "2" in answer, answer)

    # Question 3: "compare accuracy across subjects"
    responses = iter([
        "```sql\nSELECT subject, AVG(accuracy_ratio) AS avg_acc FROM ledger WHERE archived=0 GROUP BY subject ORDER BY avg_acc DESC\n```",
        "ANSWER: Maths has the highest avg accuracy (0.9), then Chem (0.8), then Physics (0.7).",
    ])
    with patch.object(sql_query_flow, "_call_llm", side_effect=lambda *a, **k: next(responses)):
        answer = sql_query_flow.answer_question("compare accuracy across subjects", db_path=db)
    ok = _check(ok, "comparison answered", "Maths" in answer and "0.9" in answer, answer)

    db.unlink(missing_ok=True)
    assert ok


def main() -> int:
    test_extract_sql()
    test_extract_answer()
    test_loop_single_query_then_answer()
    test_loop_multi_query()
    test_loop_sql_error_recovery()
    test_loop_max_iterations()
    test_loop_llm_failure()
    test_loop_cannot_write()
    test_loop_realistic_questions()
    print("\n" + "=" * 70)
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
