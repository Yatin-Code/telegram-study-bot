"""
Phase 9 end-to-end acceptance test — LIVE against Notion + LLM.

Runs the spec's flagship scenario for real:
    "execution block 1: solved kinematics ex 2A, 20 questions, 15 correct,
     25 mins, doubt about sign convention"
  -> parse (LLM) -> build plan -> confirm -> create Ledger page in Notion
  -> cross-log the doubt into Doubts and link via Logged Errors
  -> incremental sync -> query it back from the SQLite mirror.

Then it CLEANS UP by archiving the two pages it created so the workspace and
mirror are left as they were.

This creates real Notion pages briefly. Run intentionally:
    python test_phase9_e2e.py
"""

from __future__ import annotations

import sys

import httpx

import logging_flow as lf
import notion_client_wrapper as notion
import query_flow as qf
import session_context as sc
import sync
from intent_parser import parse_message


CHAT_ID = 900900


def _archive(page_id: str) -> None:
    """Archive (soft-delete) a Notion page so the test leaves no trace."""
    notion._request(
        "PATCH", f"{notion.NOTION_API}/pages/{page_id}", json_body={"archived": True}
    )


def _check(ok, label, cond, extra=""):
    print(f"[{'OK ' if cond else 'BAD'}] {label}" + (f" -> {extra}" if extra else ""))
    return ok and cond


def run() -> bool:
    ok = True
    created_pages: list[str] = []

    # Context first, so the log inherits subject/chapter/block (Phase 6 link).
    sc.set_context(CHAT_ID, subject="PHYSICS", chapter="kinematics", block="EB-1")

    msg = ("execution block 1: solved kinematics ex 2A, 20 questions, 15 correct, "
           "25 mins, doubt about sign convention")
    print("=== Phase 9: parse (live LLM) ===")
    intent = parse_message(msg, session_context=sc.context_for_parser(CHAT_ID))
    ok = _check(ok, "action is log_execution", intent.action == "log_execution", intent.action)
    ok = _check(ok, "database is ledger", intent.database == "ledger", str(intent.database))

    print("\n=== Phase 9: build plan ===")
    plan = lf.build_write_plan(intent, CHAT_ID)
    ok = _check(ok, "no clarification needed", not plan.needs_clarification,
                plan.clarification_question or "")
    ok = _check(ok, "doubt flagged for cross-log", bool(plan.cross_log_doubt),
                plan.cross_log_doubt or "")
    ok = _check(ok, "chapter resolved to a page id",
                plan.properties.get("chapter", "").replace("-", "").isalnum()
                and len(plan.properties.get("chapter", "")) >= 30,
                plan.properties.get("chapter"))
    print("preview:\n  " + "\n  ".join(plan.preview_lines))

    print("\n=== Phase 9: commit (live write to Notion) ===")
    result = lf.commit_write(plan.to_payload())
    ok = _check(ok, "commit saved (not queued)", result["status"] == "saved", str(result))
    ledger_url = result.get("url")
    cross_url = result.get("cross_url")
    page_id = result.get("page_id")
    cross_page_id = result.get("cross_page_id")
    ok = _check(ok, "ledger page url returned", bool(ledger_url), ledger_url or "")
    ok = _check(ok, "doubt cross-logged url returned", bool(cross_url), cross_url or "")
    ok = _check(ok, "ledger page_id returned", bool(page_id), repr(page_id))
    ok = _check(ok, "doubt cross-logged page_id returned", bool(cross_page_id), repr(cross_page_id))

    # Register for cleanup IMMEDIATELY using the ground-truth ids Notion handed
    # back on creation — before any later step that might bail and skip cleanup.
    # Never identify created pages by mirror recency: sync upsert order is not
    # creation order, so ORDER BY last_synced_at is unsafe against live data.
    if page_id:
        created_pages.append(page_id)
    if cross_page_id:
        created_pages.append(cross_page_id)

    print("\n=== Phase 9: query back from mirror ===")
    with sync.connect() as conn:
        led = conn.execute(
            "SELECT notion_page_id, task, subject, block, exercise_type, "
            "questions_attempted, questions_correct, chapter "
            "FROM ledger WHERE notion_page_id = ?",
            (page_id,),
        ).fetchone()
    ok = _check(ok, "ledger row mirrored", led is not None)
    if led:
        ok = _check(ok, "exercise_type persisted", led["exercise_type"] == "Ex 2A",
                    led["exercise_type"])
        ok = _check(ok, "questions_attempted persisted", led["questions_attempted"] == 20,
                    str(led["questions_attempted"]))
        ok = _check(ok, "subject inherited from context", led["subject"] == "Physics",
                    led["subject"])
        ok = _check(ok, "block inherited from context", led["block"] == "EB-1", led["block"])

    # The cross-logged doubt should now be queryable as an unresolved doubt.
    def q(**f):
        from intent_parser import _validate_intent
        return _validate_intent({"action": "query", "database": f.pop("database", None),
                                 "fields": {}, "filters": f,
                                 "needs_clarification": False, "clarification_question": None})

    res = qf.run_query(q(database="doubts", keyword="sign convention"))
    ok = _check(ok, "doubt is queryable from mirror", res["count"] >= 1, str(res["count"]))

    # ---- cleanup: archive created pages, re-sync so mirror matches Notion ----
    print("\n=== Phase 9: cleanup (archive test pages) ===")
    for pid in created_pages:
        try:
            _archive(pid)
            print(f"  archived {pid}")
        except Exception as e:
            print(f"  WARN could not archive {pid}: {e}")
    try:
        sync.sync_once()
    except Exception as e:
        print(f"  WARN cleanup sync failed: {e}")
    sc.clear_context(CHAT_ID)

    print("\n" + ("ALL PHASE 9 E2E CHECKS PASSED" if ok else "SOME PHASE 9 E2E CHECKS FAILED"))
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
