"""
Tests for sql_tool.py — the read-only SQL execution layer.

Pure SQLite, no LLM/Notion needed. Run: python3 test_sql_tool.py

Verifies:
- Valid SELECTs execute and return results
- All write/modification statements are rejected BEFORE execution
- Multi-statement injection is blocked
- The underlying SQLite connection is itself read-only (write fails at OS level)
- Allowed PRAGMAs work, disallowed ones are rejected
- Results are capped and truncation is reported
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

import sql_tool


def _check(ok, label, cond, extra=""):
    print(f"[{'OK ' if cond else 'BAD'}] {label}{(' -> ' + extra) if extra else ''}")
    return ok and cond


def _seed_temp_db() -> Path:
    """Create a temp SQLite DB with a small ledger table for testing."""
    tmp = Path(tempfile.mkstemp(suffix=".db")[1])
    conn = sqlite3.connect(str(tmp))
    conn.execute(
        """CREATE TABLE ledger (
            notion_page_id TEXT PRIMARY KEY,
            archived INTEGER DEFAULT 0,
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
        (notion_page_id, archived, subject, exercise_type, date,
         actual_time_min, questions_attempted, questions_correct,
         cognitive_yield, theory_yield, accuracy_ratio, mins_per_question)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            ("p1", 0, "Chem", "Ex 1A", "2026-07-19", 20, 10, 8, 11, 11, 0.8, 2.0),
            ("p2", 0, "Physics", "Ex 2A", "2026-06-15", 45, 10, 7, 34, 34, 0.7, 4.5),
            ("p3", 0, "Maths", "MLE", "2026-01-10", 55, 10, 9, 0, 0, 0.9, 5.5),
            ("p4", 1, "Chem", "Ex 1B", "2025-12-01", 25, 10, 6, 5, 5, 0.6, 2.5),
        ],
    )
    conn.commit()
    conn.close()
    return tmp


def test_valid_selects() -> bool:
    ok = True
    print("=== Valid SELECTs ===")
    db = _seed_temp_db()

    r = sql_tool.run_sql("SELECT COUNT(*) AS n FROM ledger WHERE archived=0", db_path=db)
    ok = _check(ok, "count active rows", r["row_count"] == 1 and r["rows"][0]["n"] == 3)

    r = sql_tool.run_sql("SELECT subject, cognitive_yield FROM ledger WHERE archived=0 ORDER BY cognitive_yield DESC", db_path=db)
    ok = _check(ok, "ordered by cognitive_yield desc", r["rows"][0]["subject"] == "Physics")

    r = sql_tool.run_sql("SELECT AVG(accuracy_ratio) AS avg_acc FROM ledger WHERE archived=0", db_path=db)
    avg = r["rows"][0]["avg_acc"]
    ok = _check(ok, "avg accuracy ~0.8", abs(avg - (0.8 + 0.7 + 0.9) / 3) < 0.001, f"got {avg}")

    # WITH ... SELECT (CTE)
    r = sql_tool.run_sql(
        "WITH active AS (SELECT * FROM ledger WHERE archived=0) SELECT subject FROM active",
        db_path=db,
    )
    ok = _check(ok, "WITH ... SELECT works", r["row_count"] == 3)

    # Aggregation with GROUP BY
    r = sql_tool.run_sql(
        "SELECT subject, SUM(questions_correct) AS total_correct FROM ledger WHERE archived=0 GROUP BY subject",
        db_path=db,
    )
    ok = _check(ok, "GROUP BY subject returns 3 groups", r["row_count"] == 3)

    # Date range query — should match p1 (2026-07-19) and p2 (2026-06-15)
    r = sql_tool.run_sql(
        "SELECT * FROM ledger WHERE date >= '2026-06-01' AND date < '2026-08-01' AND archived=0 ORDER BY date DESC",
        db_path=db,
    )
    ok = _check(ok, "date range filter returns 2 rows", r["row_count"] == 2, str(r["row_count"]))
    ok = _check(ok, "first row is most recent (Chem)", r["rows"][0]["subject"] == "Chem")

    # Empty result
    r = sql_tool.run_sql("SELECT * FROM ledger WHERE subject = 'Biology'", db_path=db)
    ok = _check(ok, "empty result has row_count=0", r["row_count"] == 0)
    ok = _check(ok, "empty result has columns", len(r["columns"]) > 0)

    db.unlink(missing_ok=True)
    assert ok


def test_reject_writes() -> bool:
    ok = True
    print("\n=== Reject all non-SELECT statements ===")
    db = _seed_temp_db()

    bad_statements = [
        "DELETE FROM ledger",
        "UPDATE ledger SET archived=1",
        "DROP TABLE ledger",
        "INSERT INTO ledger VALUES (1, 0, 'X', 'X', '2026-01-01', 0, 0, 0, 0, 0, 0, 0)",
        "CREATE TABLE evil (x TEXT)",
        "ALTER TABLE ledger ADD COLUMN evil TEXT",
        "ATTACH DATABASE '/tmp/x' AS x",
        "DETACH DATABASE x",
        "REINDEX ledger",
        "VACUUM",
        "PRAGMA writable_schema=1",
        "PRAGMA journal_mode=WAL",
        "PRAGMA table_xinfo(ledger)",
    ]
    for stmt in bad_statements:
        try:
            sql_tool.run_sql(stmt, db_path=db)
            ok = _check(ok, f"rejected {stmt[:40]!r}", False, "WENT THROUGH!")
        except sql_tool.SQLRejectedError:
            ok = _check(ok, f"rejected {stmt[:40]!r}", True)
        except sql_tool.SQLExecutionError as e:
            ok = _check(ok, f"rejected {stmt[:40]!r}", True, f"exec error: {e}")

    db.unlink(missing_ok=True)
    assert ok


def test_multi_statement_injection() -> bool:
    ok = True
    print("\n=== Multi-statement injection blocked ===")
    db = _seed_temp_db()

    injection_attempts = [
        "SELECT * FROM ledger; DROP TABLE ledger; --",
        "SELECT 1; DELETE FROM ledger",
        "SELECT * FROM ledger; SELECT * FROM ledger",
    ]
    for stmt in injection_attempts:
        try:
            sql_tool.run_sql(stmt, db_path=db)
            ok = _check(ok, f"blocked {stmt[:40]!r}", False, "WENT THROUGH!")
        except (sql_tool.SQLRejectedError, sql_tool.SQLExecutionError):
            ok = _check(ok, f"blocked {stmt[:40]!r}", True)

    # Verify ledger still has all rows (no DROP happened)
    r = sql_tool.run_sql("SELECT COUNT(*) AS n FROM ledger", db_path=db)
    ok = _check(ok, "ledger survived injection", r["rows"][0]["n"] == 4)

    db.unlink(missing_ok=True)
    assert ok


def test_read_only_connection() -> bool:
    ok = True
    print("\n=== Underlying SQLite connection is read-only ===")
    db = _seed_temp_db()

    # Even if a write somehow passed validation, the connection should block it.
    # Test by constructing a read-only connection directly and attempting a write.
    uri = sql_tool._read_only_uri(db)
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.execute("DELETE FROM ledger WHERE 1=0")
        ok = _check(ok, "write blocked at SQLite level", False, "WENT THROUGH!")
    except sqlite3.OperationalError as e:
        ok = _check(ok, "write blocked at SQLite level", True, str(e))
    conn.close()

    # Verify all rows still present
    r = sql_tool.run_sql("SELECT COUNT(*) AS n FROM ledger", db_path=db)
    ok = _check(ok, "rows intact after write attempt", r["rows"][0]["n"] == 4)

    db.unlink(missing_ok=True)
    assert ok


def test_allowed_pragmas() -> bool:
    ok = True
    print("\n=== Allowed PRAGMAs ===")
    db = _seed_temp_db()

    # table_info is allowed (needed for schema discovery)
    r = sql_tool.run_sql("PRAGMA table_info(ledger)", db_path=db)
    ok = _check(ok, "PRAGMA table_info works", r["row_count"] > 0)
    ok = _check(ok, "table_info has column names",
                any(row.get("name") == "subject" for row in r["rows"]))

    # Disallowed pragmas (anything that mutates)
    for pragma in [
        "PRAGMA writable_schema=1",
        "PRAGMA journal_mode=WAL",
        "PRAGMA foreign_keys=ON",
    ]:
        try:
            sql_tool.run_sql(pragma, db_path=db)
            ok = _check(ok, f"rejected {pragma}", False, "WENT THROUGH!")
        except sql_tool.SQLRejectedError:
            ok = _check(ok, f"rejected {pragma}", True)

    db.unlink(missing_ok=True)
    assert ok


def test_row_limit_and_truncation() -> bool:
    ok = True
    print("\n=== Row limit + truncation ===")
    db = _seed_temp_db()

    # Only 4 rows total; ask for max_rows=2
    r = sql_tool.run_sql("SELECT * FROM ledger", db_path=db, max_rows=2)
    ok = _check(ok, "capped at max_rows=2", r["row_count"] == 2)
    ok = _check(ok, "truncated flag set", r["truncated"] is True)

    # max_rows=10 covers all 4
    r = sql_tool.run_sql("SELECT * FROM ledger", db_path=db, max_rows=10)
    ok = _check(ok, "all 4 rows returned", r["row_count"] == 4)
    ok = _check(ok, "not truncated", r["truncated"] is False)

    db.unlink(missing_ok=True)
    assert ok


def test_edge_cases() -> bool:
    ok = True
    print("\n=== Edge cases ===")
    db = _seed_temp_db()

    # Empty SQL
    try:
        sql_tool.run_sql("", db_path=db)
        ok = _check(ok, "empty SQL rejected", False)
    except sql_tool.SQLRejectedError:
        ok = _check(ok, "empty SQL rejected", True)

    # Only semicolons
    try:
        sql_tool.run_sql(";;;", db_path=db)
        ok = _check(ok, "semicolons-only rejected", False)
    except sql_tool.SQLRejectedError:
        ok = _check(ok, "semicolons-only rejected", True)

    # SELECT with trailing semicolon (should be allowed)
    r = sql_tool.run_sql("SELECT 1 AS x;", db_path=db)
    ok = _check(ok, "trailing semicolon OK", r["rows"][0]["x"] == 1)

    # Case-insensitive SELECT
    r = sql_tool.run_sql("select 1 as x", db_path=db)
    ok = _check(ok, "lowercase select works", r["rows"][0]["x"] == 1)

    # Whitespace-only
    try:
        sql_tool.run_sql("   \n\t  ", db_path=db)
        ok = _check(ok, "whitespace rejected", False)
    except sql_tool.SQLRejectedError:
        ok = _check(ok, "whitespace rejected", True)

    db.unlink(missing_ok=True)
    assert ok


def main() -> int:
    test_valid_selects()
    test_reject_writes()
    test_multi_statement_injection()
    test_read_only_connection()
    test_allowed_pragmas()
    test_row_limit_and_truncation()
    test_edge_cases()
    print("\n" + "=" * 70)
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
