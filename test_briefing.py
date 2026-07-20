"""
Briefing tests — start-of-block briefing rendered from a seeded temp mirror.

Offline: no LLM, no Notion. Run: python test_briefing.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import briefing
import sync


def _check(ok, label, cond, extra=""):
    print(f"[{'OK ' if cond else 'BAD'}] {label}{(' -> ' + extra) if extra else ''}")
    return ok and cond


def _seed(db_path: Path) -> None:
    with sync.connect(db_path) as conn:
        sync.init_db(conn)
        conn.execute(
            'INSERT INTO ledger (notion_page_id, task, date, subject, exercise_type, '
            'cognitive_yield, accuracy_ratio, actual_time_min, archived, '
            'last_synced_at, raw_json) '
            "VALUES ('L1', 'MLE kinematics', '2026-07-12', 'Physics', 'MLE', "
            "68, 0.75, 42, 0, '', '{}')"
        )
        conn.execute(
            'INSERT INTO doubts (notion_page_id, core_concept, status, chapter, '
            'archived, last_synced_at, raw_json) '
            "VALUES ('D1', 'sign confusion', 'Unresolved', "
            "'[\"Kinematics 2D\"]', 0, '', '{}')"
        )
        conn.execute(
            'INSERT INTO doubts (notion_page_id, core_concept, status, chapter, '
            'archived, last_synced_at, raw_json) '
            "VALUES ('D2', 'resolved one', 'Resolved', "
            "'[\"Kinematics 2D\"]', 0, '', '{}')"
        )
        conn.execute(
            'INSERT INTO revision (notion_page_id, chapter_module, status, mastery, '
            'next_execution_date, archived, last_synced_at, raw_json) '
            "VALUES ('R1', 'Kinematics 2D', 'Not started', NULL, "
            "'2026-05-28', 0, '', '{}')"
        )
        conn.commit()


def run() -> bool:
    ok = True
    ctx = {
        "subject": "Physics", "chapter": "kinematics", "block": "EB-1",
        "exercise": "MLE",
        "session_started_at": "2026-07-20T10:42:00+05:30",
    }

    print("=== Briefing: seeded mirror ===")
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "t.db"
        _seed(db)
        text = briefing.build_briefing(ctx, db_path=db)
        print(text)
        ok = _check(ok, "header names block/subject/exercise/chapter",
                    "EB-1 Physics MLE (kinematics)" in text)
        ok = _check(ok, "header shows start time", "10:42" in text)
        ok = _check(ok, "target pace from formulas (Physics MLE = 5)",
                    "Target pace: 5 min/q" in text)
        ok = _check(ok, "last-session line with yield/accuracy/time",
                    "Last MLE: yield 68 · accuracy 75% · 42 min (2026-07-12)" in text)
        ok = _check(ok, "counts only unresolved doubts in chapter",
                    "1 unresolved doubt in this chapter" in text)
        ok = _check(ok, "revision line with overdue flag",
                    "next due 2026-05-28 (overdue)" in text)
        ok = _check(ok, "footer mentions running timer", "Timer running" in text)

    print("\n=== Briefing: empty mirror degrades gracefully ===")
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "t.db"
        with sync.connect(db) as conn:
            sync.init_db(conn)
        text = briefing.build_briefing(ctx, db_path=db)
        ok = _check(ok, "still renders header + pace + footer",
                    "EB-1 Physics MLE" in text and "Target pace" in text
                    and "Timer running" in text, repr(text))
        ok = _check(ok, "no stats lines invented",
                    "Last" not in text and "doubt" not in text and "Revision" not in text)

    print("\n=== Briefing: missing DB never raises ===")
    text = briefing.build_briefing({"subject": "Chem"}, db_path="/nonexistent/x.db")
    ok = _check(ok, "returns text on unreadable mirror", "Chem" in text, repr(text))

    print("\n" + ("ALL BRIEFING CHECKS PASSED" if ok else "SOME BRIEFING CHECKS FAILED"))
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
