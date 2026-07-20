"""
Phase 7 tests — logging flow.

Offline: build_write_plan + normalisation + relation matching run against the
real SQLite mirror (read-only). commit_write is exercised with the Notion
wrapper monkeypatched, so no live API calls happen — including the "Notion
down -> queue locally" edge case.

Run: python3 test_phase7_logging.py
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import logging_flow as lf
import notion_client_wrapper as notion
import session_context as sc
from intent_parser import _validate_intent


def _intent(action, database, fields=None, filters=None):
    return _validate_intent({
        "action": action,
        "database": database,
        "fields": fields or {},
        "filters": filters or {},
        "needs_clarification": False,
        "clarification_question": None,
    })


def _check(ok_all, label, cond, extra=""):
    print(f"[{'OK ' if cond else 'BAD'}] {label}{(' -> ' + extra) if extra else ''}")
    return ok_all and cond


def run() -> bool:
    ok = True
    print("=== Phase 7: option normalisation ===")
    ok = _check(ok, "physics -> PHYSICS",
                lf.normalise_option("physics", ["CHEMISTRY", "MATHS", "PHYSICS"]) == "PHYSICS")
    ok = _check(ok, "eb1 -> EB-1", lf.normalise_option("eb1", ["EB-1", "EB-2"]) == "EB-1")
    ok = _check(ok, "ex2a -> Ex 2A", lf.normalise_option("ex2a", ["Ex 1A", "Ex 2A"]) == "Ex 2A")
    ok = _check(ok, "bogus -> None", lf.normalise_option("bogus", ["Ex 1A", "Ex 2A"]) is None)

    print("\n=== Phase 7: relation fuzzy-match (real mirror) ===")
    pid, cands = lf.resolve_relation("revision", "kinematics")
    ok = _check(ok, "kinematics matches a chapter", pid is not None, str(cands))
    pid, cands = lf.resolve_relation("revision", "friction")
    ok = _check(ok, "friction matches a chapter", pid is not None, str(cands))
    pid, cands = lf.resolve_relation("revision", "quantum wizardry")
    ok = _check(ok, "nonsense matches nothing", pid is None, str(cands))

    print("\n=== Phase 7: build_write_plan with context inheritance ===")
    sc.set_context(7001, subject="PHYSICS", chapter="kinematics", block="EB-1")
    intent = _intent("log_execution", "ledger", fields={
        "exercise_type": "ex 2a", "questions_attempted": 20,
        "questions_correct": 15, "actual_time_min": 25,
        "doubts": "sign convention on relative velocity",
    })
    plan = lf.build_write_plan(intent, 7001)
    sc.clear_context(7001)
    ok = _check(ok, "no clarification needed", not plan.needs_clarification, plan.clarification_question or "")
    ok = _check(ok, "subject inherited", plan.properties.get("subject") == "Physics")
    ok = _check(ok, "block inherited", plan.properties.get("block") == "EB-1")
    ok = _check(ok, "exercise normalised", plan.properties.get("exercise_type") == "Ex 2A")
    ok = _check(ok, "chapter resolved to page_id",
                isinstance(plan.properties.get("chapter"), str) and len(plan.properties["chapter"]) > 20)
    ok = _check(ok, "date defaulted to today", plan.properties.get("date") == sc.local_today_iso())
    ok = _check(ok, "title synthesised", bool(plan.properties.get("task")))
    ok = _check(ok, "doubt flagged for cross-log", plan.cross_log_doubt is not None)
    ok = _check(ok, "preview uses Notion names", any("Actual Time Spent (mins)" in l for l in plan.preview_lines))

    print("\n=== Phase 7: invalid option -> clarification ===")
    bad = lf.build_write_plan(_intent("log_execution", "ledger",
                                      fields={"exercise_type": "ex 99Z", "subject": "PHYSICS"}), 7002)
    ok = _check(ok, "bad exercise asks for clarification", bad.needs_clarification, bad.clarification_question or "")

    print("\n=== Phase 7: unresolvable relation -> clarification ===")
    badrel = lf.build_write_plan(_intent("log_execution", "ledger",
                                         fields={"chapter": "nonexistent chapter xyz", "exercise_type": "MLE"}), 7003)
    ok = _check(ok, "bad chapter asks for clarification", badrel.needs_clarification, badrel.clarification_question or "")

    print("\n=== Phase 7: doubt auto-link to ledger entry ===")

    def _seed_ledger(db_path, rows):
        with lf.sync.connect(db_path) as conn:
            lf.sync.init_db(conn)
            for r in rows:
                r = dict(r)
                r.setdefault("last_synced_at", "2026-01-01T00:00:00+00:00")
                r.setdefault("raw_json", "{}")
                r.setdefault("archived", 0)
                cols = list(r.keys())
                conn.execute(
                    "INSERT INTO ledger ({}) VALUES ({})".format(
                        ", ".join(f'"{c}"' for c in cols),
                        ", ".join("?" for _ in cols)),
                    [r[c] for c in cols],
                )
            conn.commit()

    today = sc.local_today_iso()
    with tempfile.TemporaryDirectory() as td:
        tmp_db = Path(td) / "t.db"
        _seed_ledger(tmp_db, [
            {"notion_page_id": "L1", "task": "MLE Physics kinematics",
             "date": today, "subject": "Physics", "exercise_type": "MLE"},
            {"notion_page_id": "L2", "task": "Ex 2A Chem",
             "date": "2026-01-05", "subject": "Chem", "exercise_type": "Ex 2A"},
        ])
        plan = lf.build_write_plan(
            _intent("log_doubt", "doubts",
                    fields={"core_concept": "how to do mle q36"},
                    filters={"subject": "Physics"}),
            7101, db_path=tmp_db)
        ok = _check(ok, "unique match links without asking",
                    not plan.needs_clarification, plan.clarification_question or "")
        ok = _check(ok, "linked to the Physics MLE entry",
                    plan.properties.get("ledger_entry") == "L1", str(plan.properties))
        ok = _check(ok, "preview shows the resolved task title",
                    plan.resolved_names.get("ledger_entry") == "MLE Physics kinematics")

        # Two equally good candidates -> ask, in the tap-to-pick format.
        _seed_ledger(tmp_db, [
            {"notion_page_id": "L3", "task": "MLE Physics vectors",
             "date": today, "subject": "Physics", "exercise_type": "MLE"},
        ])
        amb = lf.build_write_plan(
            _intent("log_doubt", "doubts",
                    fields={"core_concept": "mle sign confusion"},
                    filters={"subject": "Physics"}),
            7102, db_path=tmp_db)
        ok = _check(ok, "tie asks for clarification", amb.needs_clarification,
                    amb.clarification_question or "")
        ok = _check(ok, "clarification uses Did-you-mean format with both tasks",
                    amb.clarification_question is not None
                    and "Did you mean:" in amb.clarification_question
                    and "MLE Physics kinematics" in amb.clarification_question
                    and "MLE Physics vectors" in amb.clarification_question,
                    amb.clarification_question or "")

    with tempfile.TemporaryDirectory() as td:
        tmp_db = Path(td) / "t.db"
        _seed_ledger(tmp_db, [])
        none = lf.build_write_plan(
            _intent("log_doubt", "doubts",
                    fields={"core_concept": "random standalone doubt"}),
            7103, db_path=tmp_db)
        ok = _check(ok, "no match still saves (no clarification)",
                    not none.needs_clarification, none.clarification_question or "")
        ok = _check(ok, "no match warns about missing link",
                    any("no ledger entry linked" in w for w in none.warnings),
                    str(none.warnings))

    print("\n=== Phase 7: session timer auto-fills actual time ===")
    import datetime as dt
    import sqlite3 as _sqlite3
    sc.set_context(7201, subject="PHYSICS", exercise="MLE")
    backdated = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=33)).isoformat()
    with _sqlite3.connect(str(lf.DEFAULT_DB_PATH)) as _c:
        _c.execute(
            f"UPDATE {sc.CONTEXT_TABLE} SET session_started_at = ? WHERE chat_id = '7201'",
            (backdated,),
        )
        _c.commit()
    timed = lf.build_write_plan(
        _intent("log_execution", "ledger",
                fields={"questions_attempted": 20, "questions_correct": 15}),
        7201)
    sc.clear_context(7201)
    ok = _check(ok, "no clarification", not timed.needs_clarification,
                timed.clarification_question or "")
    t = timed.properties.get("actual_time_min")
    ok = _check(ok, "time auto-filled ≈ 33 min", isinstance(t, int) and 32 <= t <= 34, str(t))
    ok = _check(ok, "exercise inherited from context",
                timed.properties.get("exercise_type") == "MLE", str(timed.properties))
    ok = _check(ok, "warning notes the timer",
                any("session timer" in w for w in timed.warnings), str(timed.warnings))

    print("\n=== Phase 7: guided completion prompts ===")
    missing = lf.build_write_plan(
        _intent("log_execution", "ledger", fields={"exercise_type": "Ex 2A"}), 7202)
    ok = _check(ok, "missing questions -> asks", missing.needs_clarification,
                missing.clarification_question or "")
    ok = _check(ok, "question covers attempted+correct",
                missing.clarification_question is not None
                and "attempt" in missing.clarification_question
                and "correct" in missing.clarification_question,
                missing.clarification_question or "")
    second = lf.build_write_plan(
        _intent("log_execution", "ledger", fields={"exercise_type": "Ex 2A"}),
        7202, first_round=False)
    ok = _check(ok, "second round does not re-ask", not second.needs_clarification,
                second.clarification_question or "")
    theory = lf.build_write_plan(
        _intent("log_execution", "ledger", fields={"exercise_type": "Theory"}), 7203)
    ok = _check(ok, "theory session not asked for questions",
                not theory.needs_clarification, theory.clarification_question or "")
    halfway = lf.build_write_plan(
        _intent("log_execution", "ledger",
                fields={"exercise_type": "Ex 2A", "questions_attempted": 20}), 7204)
    ok = _check(ok, "attempted without correct -> asks for correct",
                halfway.needs_clarification
                and halfway.clarification_question is not None
                and "20" in halfway.clarification_question,
                halfway.clarification_question or "")

    print("\n=== Phase 7: commit_write (Notion patched) ===")
    created = {}

    def fake_create(db_key, props):
        created["db_key"] = db_key
        created["props"] = props
        return {"id": "fake-page-id-123", "url": "https://notion.so/fake-123"}

    def fake_update(page_id, props):
        created["updated"] = (page_id, props)
        return {"id": page_id}

    orig_create, orig_update = notion.create_page, notion.update_page
    orig_sync = lf.sync.sync_once
    notion.create_page = fake_create
    notion.update_page = fake_update
    lf.notion.create_page = fake_create
    lf.notion.update_page = fake_update
    lf.sync.sync_once = lambda *a, **k: {}  # keep flush offline
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp_db = Path(td) / "t.db"
            payload = {"db_key": "doubts", "properties": {"core_concept": "x", "status": "Unresolved"},
                       "cross_log_doubt": None}
            res = lf.commit_write(payload, db_path=tmp_db, do_sync=False)
            ok = _check(ok, "commit returns saved", res["status"] == "saved", str(res))
            ok = _check(ok, "create_page was called", created.get("db_key") == "doubts")

            # Notion-down path: create_page raises -> queued locally.
            def boom(*a, **k):
                raise notion.NotionAPIError(503, "down", method="POST", url="x")

            notion.create_page = boom
            lf.notion.create_page = boom
            res2 = lf.commit_write({"db_key": "doubts",
                                    "properties": {"core_concept": "y", "status": "Unresolved"},
                                    "cross_log_doubt": None}, db_path=tmp_db, do_sync=False)
            ok = _check(ok, "commit queues on Notion outage", res2["status"] == "queued", str(res2))
            ok = _check(ok, "one write pending", lf.pending_count(db_path=tmp_db) == 1)

            # Recover: create_page works again -> flush drains the queue.
            notion.create_page = fake_create
            lf.notion.create_page = fake_create
            flushed = lf.flush_pending(db_path=tmp_db)
            ok = _check(ok, "flush drains queue", flushed["flushed"] == 1 and flushed["remaining"] == 0, str(flushed))
    finally:
        notion.create_page, notion.update_page = orig_create, orig_update
        lf.notion.create_page, lf.notion.update_page = orig_create, orig_update
        lf.sync.sync_once = orig_sync

    print("\n" + ("ALL PHASE 7 CHECKS PASSED" if ok else "SOME PHASE 7 CHECKS FAILED"))
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if run() else 1)
