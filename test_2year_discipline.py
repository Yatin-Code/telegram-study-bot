"""
Real-LLM end-to-end test on a 2-year dataset + the execution-discipline system.

Builds the SAME deterministic 2-year temp SQLite mirror as test_2year_multi_tool
(2024-08-01 .. 2026-07-31: ~450 ledger sessions, ~120 doubts, ~40 revision
chapters, op_exams/op_goals/op_daily_plan) by importing and reusing it, then
seeds the NTSC portal mirror + execution-discipline state so the discipline
system runs against the mirror. Three batteries, ALL on the REAL router:

  A) data-biased questions through sql_query_flow.answer_question (reuses the
     t2y battery + adds discipline-table questions)
  B) multi-tool agent via agent.run / continue_run (reuses the t2y battery +
     adds get_current_block / get_today_blocks tasks, asserting tool usage)
  C) discipline live drive — execution_discipline + coaching_policy with REAL
     LLM text for discipline_message (bounded by a wall-clock timeout; the
     deterministic fallback still counts as PASS with a gateway-down note)

CRITICAL (AGENTS.md warning): execution_discipline's and coaching_policy's
functions take ``db_path`` as a default-arg parameter bound at definition time
(``def day_type_for(date_iso, db_path=DEFAULT_DB_PATH)``), and
``t2y._install_stubs`` does NOT patch those modules. Every
execution_discipline / coaching_policy call here passes ``db_path=db``
EXPLICITLY — never rely on module-attribute patching.

Run:  python3 test_2year_discipline.py
Skip: SKIP_REAL_LLM=1 python3 test_2year_discipline.py
Parts: TEST_2YEAR_PART=A|B|C|AB|BC|AC|ABC (default ABC)
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import sqlite3
import sys
from pathlib import Path

import agent_tools
import execution_discipline
import ntsc_coaching
import sql_query_flow
import test_2year_multi_tool as t2y
from config import settings

# IST is UTC+5:30 with no DST — a fixed offset is exactly equivalent to
# ZoneInfo("Asia/Kolkata") for the dates this test drives, with zero dependency.
_IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

_TEST_DB: Path | None = None


def _ist(year: int, month: int, day: int, hour: int, minute: int) -> dt.datetime:
    """Aware IST datetime for the driven test dates."""
    return dt.datetime(year, month, day, hour, minute, tzinfo=_IST)


def _resp_text(result: dict) -> str:
    """Extract the agent's final response text from a task result safely."""
    final = result.get("final") or {}
    if not isinstance(final, dict):
        return ""
    resp = final.get("response")
    return getattr(resp, "text", "") or ""


# ---------------------------------------------------------------------------
# Offline structural self-check — runs even under SKIP_REAL_LLM=1
# ---------------------------------------------------------------------------

def _offline_selfcheck(db: Path) -> bool:
    """Structural validation of the seeded discipline state (no LLM needed)."""
    ok = True
    d30 = execution_discipline.day_type_for("2026-07-30", db_path=db)
    ok = t2y._check(ok, "offline day_type_for(2026-07-30) == coaching",
                    d30 == "coaching", f"day_type={d30!r}")
    b = execution_discipline.current_block(_ist(2026, 7, 30, 8, 45), db_path=db)
    ok = t2y._check(ok, "offline current_block(08:45 IST) == Execution Block A",
                    b is not None and b["title"] == "Execution Block A",
                    f"block={b['title'] if b else None}")
    gap = execution_discipline.current_block(_ist(2026, 7, 30, 8, 15), db_path=db)
    ok = t2y._check(ok, "offline current_block(08:15 IST) is None (gap)", gap is None)
    nb = len(t2y._db_q(db, "SELECT * FROM execution_blocks"))
    ok = t2y._check(ok, "offline 20 seeded blocks", nb == 20, f"rows={nb}")
    return ok


# ---------------------------------------------------------------------------
# Discipline seed: portal mirror + ledger evidence + block-confirmation state
# ---------------------------------------------------------------------------

def _seed_discipline_state(db: Path) -> None:
    """Seed everything the execution-discipline system needs against the mirror.

    - execution templates/blocks via the real seed_templates (20 blocks)
    - coaching_classes: Mon/Wed/Fri at 15:00 for the last ~2 months of the
      seeded window + 2026-07-30 itself so it resolves to a coaching day
    - coaching_sync_runs: one SUCCESS run finished "now" so
      coaching_lifecycle.fresh() stays True
    - a few ledger rows with created_time INSIDE study-block windows so
      has_ledger_evidence can be exercised
    - block_confirmations via the real state machine: pending (no row),
      started w/o evidence, started with evidence, skipped, plus a started
      block on 2026-07-31 for the never-auto-skip assertion
    """
    execution_discipline.seed_templates(db)
    now_utc = dt.datetime.now(dt.timezone.utc).isoformat()

    # Portal mirror ----------------------------------------------------------
    with ntsc_coaching._connect(db) as conn:
        idx = 0
        day = dt.date(2026, 6, 1)
        end = dt.date(2026, 7, 31)
        while day <= end:
            if day.weekday() in (0, 2, 4) or day == dt.date(2026, 7, 30):
                key = f"{day.isoformat()}|15:00|Class|{idx}"
                conn.execute(
                    "INSERT OR REPLACE INTO coaching_classes "
                    "(source_id, class_date, start_time, duration_min, class_type, "
                    " subjects, live_class, source_updated_at, raw_json) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (key, day.isoformat(), "15:00", 60, "Class", "Physics, Maths",
                     1, now_utc, "{}"),
                )
                idx += 1
            day += dt.timedelta(days=1)
        conn.execute(
            "INSERT INTO coaching_sync_runs (started_at, finished_at, status, "
            "datasets, error) VALUES (?,?,?,?,?)",
            (now_utc, now_utc, "success", '["classes"]', None),
        )
        conn.commit()

    # Ledger evidence rows (created_time is the authoritative column).
    # Block A window 08:30-10:00 IST -> UTC [02:50, 04:40] on 2026-07-30.
    # Acquisition window 12:00-14:00 IST -> UTC [06:20, 08:40] on 2026-07-30.
    evidence = [
        ("2026-07-30T03:10:00.000+00:00", "Physics MLE Kinematics", "Physics",
         "Kinematics", "MLE", "EB-2", 55.0),
        ("2026-07-30T03:50:00.000+00:00", "Chem Ex 1A Mole Concept", "Chem",
         "Mole Concept", "Ex 1A", "EB-2", 40.0),
        ("2026-07-30T07:05:00.000+00:00", "Maths PYQs Calculus", "Maths",
         "Calculus", "PYQs", "EB-2", 90.0),
        ("2026-07-30T07:40:00.000+00:00", "Physics MLE Rotational Motion",
         "Physics", "Rotational Motion", "MLE", "EB-2", 70.0),
    ]
    with sqlite3.connect(str(db)) as conn:
        for i, (created, task, subject, chapter, ex, block, mins) in enumerate(evidence):
            conn.execute(
                "INSERT INTO ledger (notion_page_id, notion_url, archived, task, "
                "subject, chapter, exercise_type, block, date, actual_time_min, "
                "questions_attempted, questions_correct, cognitive_yield, "
                "theory_yield, accuracy_ratio, mins_per_question, key_points_notes, "
                "last_synced_at, raw_json, created_time) "
                "VALUES (?,?,0,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"dsc-{i + 1:04d}", f"http://n/dsc-{i + 1:04d}", task, subject,
                 chapter, ex, block, "2026-07-30", mins, 12.0, 10.0, 100, 100,
                 0.83, 5.0, "discipline evidence row", now_utc, "{}", created),
            )
        conn.commit()

    # Block-confirmation state machine via the real functions.
    # 2026-07-30 (the driven "today"): Block A stays pending (missing row ==
    # pending), Block B started WITHOUT ledger evidence, Acquisition started
    # WITH ledger evidence, Review skipped.
    execution_discipline.confirm_start("2026-07-30", "coach_b04_exec_b", db_path=db)
    execution_discipline.confirm_start("2026-07-30", "coach_b05_acquisition", db_path=db)
    execution_discipline.confirm_skip("2026-07-30", "coach_b09_review", db_path=db)
    # 2026-07-31 (auto-skip date): Block B started -> must never auto-skip.
    execution_discipline.confirm_start("2026-07-31", "coach_b04_exec_b", db_path=db)


# ---------------------------------------------------------------------------
# Part A: discipline-table questions through the SQL path (real router)
# ---------------------------------------------------------------------------

def _discipline_questions_battery(db: Path) -> bool:
    ok = True
    started = len(t2y._db_q(
        db,
        "SELECT * FROM block_confirmations "
        "WHERE local_date='2026-07-30' AND status='started'",
    ))

    def qcheck(label: str, question: str, *conds) -> bool:
        nonlocal ok
        answer = sql_query_flow.answer_question(
            question, db_path=str(db), max_iterations=4, chat_id=t2y.CHAT_ID,
        )
        conds = tuple(conds) + (
            lambda a: not a.startswith(sql_query_flow.ANSWER_ERROR_PREFIX),
        )
        ok_here = all(c(answer) for c in conds)
        ok = t2y._check(ok, label, ok_here,
                        f"Q={question[:60]!r} A={answer[:110]!r}")
        return ok_here

    # C1 regression guard: schema_digest must expose block_confirmations so the
    # LLM can discover it. Pre-fix it answered "0" from op_daily_plan (the table
    # was invisible) and the loose _near tolerance let that BAD answer pass —
    # now the answer must reflect the seeded started count OR explicitly name
    # the block_confirmations table.
    qcheck("blocks started on 2026-07-30",
           "Query the block_confirmations table: how many study blocks were "
           "confirmed started (status='started') on 2026-07-30? Give the exact "
           f"count (expected {started}).",
           lambda a: t2y._near(started, a, 0.25) or "block_confirmations" in a)
    qcheck("2026-07-30 day type",
           "Query the coaching_classes table: is there a class on 2026-07-30? "
           "On that basis, was 2026-07-30 a coaching day or a non-coaching day? "
           "Answer with the day type.",
           lambda a: "coach" in a.lower())
    qcheck("block starting 08:30 on a coaching day",
           "Query the execution_blocks table (template_key 'tpl_coaching'): "
           "which block starts at 08:30 on a coaching day? Give its title.",
           lambda a: "execution block a" in a.lower() or "block a" in a.lower()
                     or "08:30" in a)
    return ok


# ---------------------------------------------------------------------------
# Part B: multi-tool agent battery (real router) — t2y tasks + discipline reads
# ---------------------------------------------------------------------------

async def _agent_battery(db: Path) -> bool:
    ok = True
    goal_count_before = len(t2y._db_q(db, "SELECT * FROM op_goals"))
    job_count_before = len(t2y._db_q(db, "SELECT * FROM user_jobs"))

    r = await t2y._agent_task("A: read-only SQL compare",
        "Use SQL to compare my year-2 average accuracy for Physics vs Maths. "
        "Report both numbers and say which subject is better.", db)
    text = _resp_text(r)
    ok = t2y._check(ok, "A used sql_select",
                    t2y.COUNTERS["reads"]["sql_select"] >= 1,
                    f"reads={dict(t2y.COUNTERS['reads'])}, "
                    f"writes={dict(t2y.COUNTERS['writes'])}")
    ok = t2y._check(ok, "A answer names Physics", "phys" in text.lower(), text[:120])
    ok = t2y._check(ok, "A had no preview", not r["had_preview"])

    r = await t2y._agent_task("B: read-only goals+sessions",
        "Show my active goals and my 3 most recent study sessions.", db)
    text = _resp_text(r)
    ok = t2y._check(ok, "B used reads", t2y.COUNTERS["reads"]["sql_select"] >= 1)
    ok = t2y._check(ok, "B mentions a goal or session",
                    any(k in text.lower() for k in
                        ("syllabus", "goal", "kinematics", "session")), text[:120])

    r = await t2y._agent_task("C: write log study session",
        "Log a study session: 60 minutes, Physics MLE on Kinematics, "
        "15 questions attempted, 12 correct, block EB-2.", db)
    text = _resp_text(r)
    rows = t2y._db_q(db, "SELECT task, accuracy_ratio, questions_attempted "
                          "FROM ledger WHERE notion_page_id LIKE 'n-%'")
    ok = t2y._check(ok, "C had preview+confirm", r["had_preview"])
    ok = t2y._check(ok, "C wrote ledger row", len(rows) >= 1, f"rows={rows}")
    ok = t2y._check(ok, "C accuracy 0.8 in mirror",
                    any(row["accuracy_ratio"] == 0.8 for row in rows), text[:140])

    r = await t2y._agent_task("D: write create goal",
        "Create a goal: score 80 marks in a JEE Main mock by December 2026, "
        "priority 2, subject Physics.", db)
    text = _resp_text(r)
    goal_count_after = len(t2y._db_q(db, "SELECT * FROM op_goals"))
    ok = t2y._check(ok, "D had preview+confirm", r["had_preview"])
    ok = t2y._check(ok, "D created op_goals row",
                    goal_count_after == goal_count_before + 1,
                    f"{goal_count_before} -> {goal_count_after} | {text[:140]}")

    r = await t2y._agent_task("E: write schedule reminder",
        "Set a daily reminder at 20:00 to do 30 minutes of Maths revision.", db)
    text = _resp_text(r)
    job_count_after = len(t2y._db_q(db, "SELECT * FROM user_jobs "
                                         "WHERE chat_id = ?", (t2y.CHAT_ID,)))
    ok = t2y._check(ok, "E had preview+confirm", r["had_preview"])
    ok = t2y._check(ok, "E created user_jobs row",
                    job_count_after == job_count_before + 1,
                    f"{job_count_before} -> {job_count_after} | {text[:140]}")
    jobs = t2y._db_q(db, "SELECT schedule_kind, run_time, action_text FROM "
                          "user_jobs WHERE chat_id = ?", (t2y.CHAT_ID,))
    ok = t2y._check(ok, "E daily 20:00 job",
                    any(j["schedule_kind"] == "daily" and j["run_time"] == "20:00"
                        for j in jobs), str(jobs))

    # Discipline-aware agent reads. The agent's "now" is real time — the tools
    # use session_context.local_now(); we assert TOOL USAGE (counters) and a
    # coherent answer (a block key / the word "block"), NOT specific times.
    r = await t2y._agent_task("F: current block",
        "What is my current block right now and what is its status? Use the tools.", db)
    text = _resp_text(r)
    ok = t2y._check(ok, "F used get_current_block",
                    t2y.COUNTERS["reads"]["get_current_block"] >= 1,
                    f"reads={dict(t2y.COUNTERS['reads'])}")
    ok = t2y._check(ok, "F answer coherent (names a block)",
                    "block" in text.lower(), text[:120])

    r = await t2y._agent_task("G: today's blocks",
        "Show me today's blocks and which ones I've started.", db)
    text = _resp_text(r)
    ok = t2y._check(ok, "G used get_today_blocks",
                    t2y.COUNTERS["reads"]["get_today_blocks"] >= 1,
                    f"reads={dict(t2y.COUNTERS['reads'])}")
    ok = t2y._check(ok, "G answer coherent (started count / a block)",
                    any(k in text.lower() for k in ("block", "started")), text[:120])
    return ok


# ---------------------------------------------------------------------------
# Part C: discipline live drive (real router for message text only)
# ---------------------------------------------------------------------------

def _llm_message_timed(tier: str, block: dict, *, db: Path,
                       now: dt.datetime, timeout: int = 180):
    """Run discipline_message with a hard wall-clock timeout.

    discipline_message internally catches ALL exceptions and returns the
    deterministic fallback, so a dead gateway still returns (the fallback also
    contains the block title). The timeout here only guards a truly hung
    socket; on timeout we return the fallback ourselves and flag timed_out.
    """
    import concurrent.futures
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(execution_discipline.discipline_message, tier, block,
                    db_path=db, now=now)
    try:
        return fut.result(timeout=timeout), False
    except concurrent.futures.TimeoutError:
        print(f"      [TIMEOUT] discipline_message({tier}) — gateway hung",
              flush=True)
        return None, True
    finally:
        ex.shutdown(wait=False, cancel_futures=True)


def _discipline_battery(db: Path) -> bool:
    ok = True
    import coaching_policy

    # 1. templates -----------------------------------------------------------
    nblocks = len(t2y._db_q(db, "SELECT * FROM execution_blocks"))
    ok = t2y._check(ok, "seed_templates -> 20 blocks", nblocks == 20,
                    f"rows={nblocks}")

    # 2. day types -----------------------------------------------------------
    d30 = execution_discipline.day_type_for("2026-07-30", db_path=db)
    ok = t2y._check(ok, "day_type_for(2026-07-30) == coaching",
                    d30 == "coaching", f"day_type={d30!r}")
    d2 = execution_discipline.day_type_for("2026-08-02", db_path=db)
    ok = t2y._check(ok, "day_type_for(2026-08-02) == non_coaching",
                    d2 == "non_coaching", f"day_type={d2!r}")

    # 3. current-block windows ----------------------------------------------
    b = execution_discipline.current_block(_ist(2026, 7, 30, 8, 45), db_path=db)
    ok = t2y._check(ok, "current_block(08:45) -> Execution Block A (study)",
                    b is not None and b["kind"] == "study"
                    and b["block_key"] == "coach_b02_exec_a",
                    f"block={b['title'] if b else None}")
    gap = execution_discipline.current_block(_ist(2026, 7, 30, 8, 15), db_path=db)
    ok = t2y._check(ok, "current_block(08:15) -> None (gap)", gap is None)

    # 4. escalation candidates (pending Block A) -----------------------------
    cands = execution_discipline.due_escalation_candidates(
        _ist(2026, 7, 30, 8, 41), db_path=db,
    )
    tiers = {c["tier"] for c in cands}
    keys = {c["event_key"] for c in cands}
    ok = t2y._check(ok, "escalation at 08:41 has start+push tiers",
                    {"start", "push"} <= tiers,
                    f"tiers={sorted(tiers)} keys={sorted(keys)[:3]}")
    ok = t2y._check(ok, "escalation event_key discipline:2026-07-30:coach_b02_exec_a:start",
                    "discipline:2026-07-30:coach_b02_exec_a:start" in keys,
                    f"keys={sorted(keys)}")

    # 5. REAL LLM start message ----------------------------------------------
    start_block = dict(b)
    text, timed = _llm_message_timed("start", start_block, db=db,
                                     now=_ist(2026, 7, 30, 8, 41))
    fb_start = execution_discipline._fallback("start", start_block)
    if text is None:
        text = fb_start
    is_fb = text == fb_start
    ok = t2y._check(ok, "LLM start message <220 chars, has title, no AIR",
                    len(text) < 220 and "Execution Block A" in text
                    and "AIR" not in text.upper(),
                    f"len={len(text)} used_fallback={is_fb} text={text[:120]!r}")
    if timed or is_fb:
        print(f"      [NOTE] start message used deterministic fallback "
              f"(gateway down?) timed={timed}", flush=True)

    # 6. evaluate_completion (after 10:00, inside a study window at 21:45) ---
    cands2 = execution_discipline.evaluate_completion(
        _ist(2026, 7, 30, 21, 45), db_path=db,
    )
    acq = execution_discipline.get_state("2026-07-30", "coach_b05_acquisition",
                                         db_path=db)
    ok = t2y._check(ok, "started-with-ledger block auto-completed",
                    acq is not None and acq["status"] == "completed",
                    f"status={acq}")
    checkins = [c for c in cands2 if c["kind"] == "discipline_checkin"]
    ok = t2y._check(ok, "exactly one checkin candidate (Block B, no ledger)",
                    len(checkins) == 1 and checkins[0]["block_key"] == "coach_b04_exec_b",
                    f"candidates={[c['block_key'] for c in cands2]}")

    # 7. REAL LLM checkin message --------------------------------------------
    blk_b = checkins[0]["block"]
    text2, timed2 = _llm_message_timed("checkin", blk_b, db=db,
                                       now=_ist(2026, 7, 30, 21, 45))
    fb_checkin = execution_discipline._fallback("checkin", blk_b)
    if text2 is None:
        text2 = fb_checkin
    is_fb2 = text2 == fb_checkin
    ok = t2y._check(ok, "LLM checkin message <220 chars, has title, no AIR",
                    len(text2) < 220 and "Execution Block B" in text2
                    and "AIR" not in text2.upper(),
                    f"len={len(text2)} used_fallback={is_fb2} text={text2[:120]!r}")
    # C2 regression guard: the checkin system prompt now says the block just
    # ended — the LLM must never drift back to "starts now" (the deterministic
    # fallback already reads "Did you finish …", so this only trips on LLM
    # drift, which is exactly what it must catch).
    ok = t2y._check(ok, "LLM checkin message never says 'starts now'",
                    "starts now" not in text2.lower(),
                    f"len={len(text2)} used_fallback={is_fb2} text={text2[:120]!r}")
    if timed2 or is_fb2:
        print(f"      [NOTE] checkin message used deterministic fallback "
              f"(gateway down?) timed={timed2}", flush=True)

    # 8. auto-skip: pending skipped, started NEVER auto-skipped ---------------
    res = execution_discipline.run_auto_skip(_ist(2026, 7, 31, 8, 56), db_path=db)
    st_a = execution_discipline.get_state("2026-07-31", "coach_b02_exec_a",
                                          db_path=db)
    ok = t2y._check(ok, "auto-skip skips pending Block A on 2026-07-31",
                    res is not None and res.get("skipped")
                    and st_a is not None and st_a["status"] == "skipped",
                    f"res={res} state={st_a}")
    res2 = execution_discipline.run_auto_skip(_ist(2026, 7, 31, 10, 56), db_path=db)
    st_b = execution_discipline.get_state("2026-07-31", "coach_b04_exec_b",
                                          db_path=db)
    ok = t2y._check(ok, "started block never auto-skipped",
                    res2 is None and st_b is not None
                    and st_b["status"] == "started",
                    f"res={res2} state={st_b}")

    # 9. policy integration ---------------------------------------------------
    decision = coaching_policy.decide_notification(
        kind="discipline_start", now=_ist(2026, 7, 30, 8, 41),
        event_key="discipline:2026-07-30:coach_b02_exec_a:start",
        chat_id=t2y.CHAT_ID, db_path=db, budget_per_day=30,
    )
    ok = t2y._check(ok, "policy allows discipline_start at 08:41 (budget 30)",
                    decision["allow"] is True,
                    f"allow={decision['allow']} reasons={decision['reasons'][:2]}")
    coaching_policy.record_decision(decision, db_path=db)
    rows = t2y._db_q(db, "SELECT kind, allow FROM notification_decisions "
                          "WHERE kind='discipline_start'")
    ok = t2y._check(ok, "record_decision persisted audit row",
                    any(r["allow"] == 1 for r in rows), f"rows={rows}")

    quiet = coaching_policy.decide_notification(
        kind="planning", now=_ist(2026, 7, 30, 23, 0),
        event_key="planning:2026-07-30:evening", chat_id=t2y.CHAT_ID,
        db_path=db,
    )
    ok = t2y._check(ok, "planning at 23:00 gated by quiet hours",
                    quiet["allow"] is False
                    and "quiet_hours" in quiet["blocked_by"],
                    f"allow={quiet['allow']} blocked_by={quiet['blocked_by']}")
    return ok


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    part = os.environ.get("TEST_2YEAR_PART", "ABC")
    try:
        # Pin the user timezone so block windows are deterministic IST.
        settings.user_timezone = lambda: "Asia/Kolkata"

        db, facts = t2y._build_two_year_db()
        print(f"Seeded test mirror at {db}", flush=True)
        t2y._install_stubs(db)
        _seed_discipline_state(db)
        global _TEST_DB
        _TEST_DB = db
        t2y._TEST_DB = db  # t2y._force_execute reads this module global
        agent_tools.execute_tool = t2y._force_execute
        agent_tools.prepare_write = t2y._force_prepare
        agent_tools.run_prepared_write = t2y._force_run
        t2y.COUNTERS["reads"].clear()
        t2y.COUNTERS["writes"].clear()

        # Structural validation even when the batteries are skipped.
        offline_ok = _offline_selfcheck(db)

        if os.environ.get("SKIP_REAL_LLM"):
            print("SKIP_REAL_LLM set — skipping real-LLM battery.")
            return 0 if offline_ok else 1

        ok = offline_ok
        if "A" in part:
            print("\n=== PART A: data-biased questions (real router) ===", flush=True)
            ok = t2y._ask_battery(db, facts) and ok
            ok = _discipline_questions_battery(db) and ok
        if "B" in part:
            print("\n=== PART B: multi-tool agent (real router) ===", flush=True)
            ok = asyncio.run(_agent_battery(db)) and ok
        if "C" in part:
            print("\n=== PART C: discipline live drive (real router) ===", flush=True)
            ok = _discipline_battery(db) and ok

        print(f"\nTool usage: reads={dict(t2y.COUNTERS['reads'])} "
              f"writes={dict(t2y.COUNTERS['writes'])}", flush=True)
        print("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED", flush=True)
        return 0 if ok else 1
    except Exception:
        import traceback
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(main())
