"""Real-LLM capability-gates lifecycle test.

Sends the same 10 prompts through the REAL LLM router at each of 4 lifecycle
stages (week 1, week 4, week 16, 2 years), with the capability-gate state
injected into the system prompt.  Shows how the LLM personalizes its response
based on what the bot actually knows about the user at each stage.

Run: python test_capability_lifecycle_llm.py
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import capability_gates

# Reuse the stage builders from the deterministic test
from test_capability_lifecycle import (
    _build_stage_1_week1,
    _build_stage_2_week4,
    _build_stage_3_week16,
    _build_stage_4_two_years,
)

PROMPTS = [
    "What should I study today?",
    "How am I doing? Give me a progress summary.",
    "What are my weak areas?",
    "Can you build me a 2-day plan before my next mock test?",
    "Which JEE chapters give the easiest marks?",
    "Quiz me on the formulas I've learned.",
    "I'm stuck on Electrostatics. Should I ask my teacher?",
    "I want to start my study block. What do I do?",
    "What did I accomplish this week?",
    "Which completed chapters should I tag as mastery vs revision vs hard?",
]


def _build_system_prompt(stage_name: str, gates: dict, db_path: Path) -> str:
    """Build a system prompt that tells the LLM exactly what the bot knows."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    ledger_count = conn.execute("SELECT COUNT(*) FROM ledger WHERE archived=0").fetchone()[0] \
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='ledger'").fetchone() else 0
    doubt_count = conn.execute("SELECT COUNT(*) FROM doubts WHERE archived=0").fetchone()[0] \
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='doubts'").fetchone() else 0
    exam_count = conn.execute("SELECT COUNT(*) FROM op_exams WHERE archived=0").fetchone()[0] \
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='op_exams'").fetchone() else 0
    formula_count = conn.execute("SELECT COUNT(*) FROM learn_formulas").fetchone()[0] \
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='learn_formulas'").fetchone() else 0
    completed_ch = conn.execute(
        "SELECT COUNT(*) FROM op_work_items WHERE archived=0 AND status='Completed'"
    ).fetchone()[0] if conn.execute("SELECT 1 FROM sqlite_master WHERE name='op_work_items'").fetchone() else 0
    doubt_attempts = conn.execute("SELECT COUNT(*) FROM op_doubt_attempts").fetchone()[0] \
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='op_doubt_attempts'").fetchone() else 0
    block_count = conn.execute("SELECT COUNT(*) FROM execution_blocks").fetchone()[0] \
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='execution_blocks'").fetchone() else 0
    jee_meta = conn.execute("SELECT COUNT(*) FROM op_jee_metadata").fetchone()[0] \
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='op_jee_metadata'").fetchone() else 0

    # Recent subjects studied
    subjects_studied = []
    if ledger_count > 0:
        rows = conn.execute(
            "SELECT DISTINCT subject FROM ledger WHERE archived=0 ORDER BY created_time DESC LIMIT 5"
        ).fetchall()
        subjects_studied = [r["subject"] for r in rows if r["subject"]]

    # Recent chapters
    recent_chapters = []
    if ledger_count > 0:
        rows = conn.execute(
            "SELECT DISTINCT subject, chapter_text FROM ledger WHERE archived=0 "
            "AND chapter_text IS NOT NULL ORDER BY created_time DESC LIMIT 5"
        ).fetchall()
        recent_chapters = [f"{r['subject']}: {r['chapter_text']}" for r in rows if r["chapter_text"]]

    conn.close()

    unlocked = [c for c, v in gates.items() if v["unlocked"]]
    locked = {c: v["reason"] for c, v in gates.items() if not v["unlocked"]}

    prompt = f"""You are a strict-but-caring JEE study coach bot. You are talking to a JEE aspirant.

## User's current stage: {stage_name}

## What you ACTUALLY know about this user (from the database):
- Study sessions logged: {ledger_count}
- Open doubts: {doubt_count}
- Upcoming exams: {exam_count}
- Completed chapters: {completed_ch}
- Formulas stored: {formula_count}
- Doubt attempts: {doubt_attempts}
- Study blocks configured: {block_count}
- JEE analytics loaded: {'yes' if jee_meta else 'no'}
- Recent subjects studied: {', '.join(subjects_studied) if subjects_studied else 'none yet'}
- Recent chapters: {'; '.join(recent_chapters) if recent_chapters else 'none yet'}

## Capabilities UNLOCKED for this user:
{chr(10).join(f'  ✅ {c} — {gates[c]["reason"]}' for c in unlocked) if unlocked else '  (none beyond basic chat)'}

## Capabilities LOCKED (do NOT pretend these work — tell the user what they need to unlock them):
{chr(10).join(f'  ❌ {c} — {reason}' for c, reason in locked.items()) if locked else '  (all unlocked)'}

## RULES:
1. NEVER pretend to know something you don't. If a capability is locked, explain what the user needs to do to unlock it.
2. Use the actual numbers above. Don't invent statistics.
3. Keep responses concise (2-4 sentences). Be encouraging but honest.
4. If the user asks for something that requires a locked capability, tell them exactly what's missing.
5. If the user asks for something unlocked, give a real, data-grounded answer.
6. Never mention "capability gates" or "unlocked" — speak naturally as their coach.
"""
    return prompt


def _run_llm(system_prompt: str, user_message: str) -> str:
    """Send a prompt through the real LLM router."""
    try:
        from llm.router import complete, LLMRequest
        from llm.errors import RouterUnavailable, AllRoutesExhausted
    except ImportError as e:
        return f"[LLM import error: {e}]"

    req = LLMRequest(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        purpose="sql",  # reuse a purpose that has routes configured
        max_output_tokens=300,
        temperature=0.3,
    )

    try:
        resp = complete(req)
        return resp.text
    except RouterUnavailable as e:
        return f"[RouterUnavailable: {e}]"
    except AllRoutesExhausted as e:
        return f"[AllRoutesExhausted: {e}]"
    except Exception as e:
        return f"[Error: {type(e).__name__}: {e}]"


def run() -> None:
    print("=" * 80)
    print("CAPABILITY GATES — REAL LLM LIFECYCLE TEST")
    print("Sending 10 prompts through the REAL LLM router at 4 lifecycle stages")
    print("=" * 80)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        stages = [
            ("STAGE 1: Week 1 (Fresh Start)", _build_stage_1_week1(tmpdir)),
            ("STAGE 2: Week 4 (Getting Into Rhythm)", _build_stage_2_week4(tmpdir)),
            ("STAGE 3: Week 16 (3 Months In)", _build_stage_3_week16(tmpdir)),
            ("STAGE 4: 2 Years (Full History)", _build_stage_4_two_years(tmpdir)),
        ]

        for stage_name, db_path in stages:
            print("\n" + "=" * 80)
            print(f"  {stage_name}")
            print("=" * 80)

            gates = capability_gates.check_all(db_path=db_path)
            summary = capability_gates.progress_summary(db_path=db_path)
            print(f"  Unlocked: {summary['unlocked_count']}/{summary['total']} | "
                  f"Locked: {len(summary['locked'])}")

            system_prompt = _build_system_prompt(stage_name, gates, db_path)

            for prompt in PROMPTS:
                print(f"\n  👤 User: {prompt}")
                response = _run_llm(system_prompt, prompt)
                # Indent the response for readability
                for line in response.strip().split("\n"):
                    print(f"  🤖 Bot:  {line}")
                print()

        print("\n" + "=" * 80)
        print("DONE — Real LLM lifecycle test complete.")
        print("=" * 80)


if __name__ == "__main__":
    run()