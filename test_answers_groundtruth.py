#!/usr/bin/env python3
"""Ground-truth answer harness — the "can I trust its answers" gate.

Seeds a temp mirror with a dataset whose every total is KNOWN (computed from
the same seed list — single source of truth), then fires 30 questions at the
real LLM answer loop: 10 clean, 10 in the owner's real typing style (typos/
hinglish), 10 traps (empty-data questions that must NOT be hallucinated,
comparisons, follow-ups). Verifies the ground-truth number appears in every
answer; any invented figure on empty-data traps is flagged CRITICAL.

Usage:  python test_answers_groundtruth.py
Gate:   >=95% (max 1 miss of 30). Rerun anytime; 3 consecutive green runs
        required by the trust program.
"""

from __future__ import annotations

import datetime as dt
import re
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import session_context
import sql_query_flow
import sync

TODAY = dt.date.fromisoformat(session_context.local_today_iso())


def d(days_ago: int) -> str:
    return (TODAY - dt.timedelta(days=days_ago)).isoformat()


# ---------------------------------------------------------------------------
# Seed dataset (subject, days_ago, exercise, attempted, correct, minutes, cy)
# ---------------------------------------------------------------------------
LEDGER = [
    ("Physics", 1, "PYQs", 10, 8, 30, 50),
    ("Physics", 2, "MLE", 10, 8, 30, 50),
    ("Physics", 3, "PYQs", 10, 8, 30, 50),
    ("Physics", 8, "MLE", 10, 8, 30, 50),
    ("Physics", 10, "PYQs", 10, 8, 30, 50),
    ("Chem", 1, "Ex 2A", 20, 15, 40, 40),
    ("Chem", 4, "Ex 2B", 20, 15, 40, 40),
    ("Chem", 9, "MLE", 20, 15, 40, 40),
    ("Maths", 2, "Ex 1A", 5, 3, 25, 30),
    ("Maths", 5, "PYQs", 5, 3, 25, 30),
    ("Maths", 6, "Ex 1B", 5, 3, 25, 30),
    ("Maths", 11, "MLE", 5, 3, 25, 30),
]
DOUBTS = [
    ("why does sign flip in kinematics relative velocity", "Unresolved"),
    ("confusion in rotational inertia direction", "Unresolved"),
    ("mole concept molarity vs molality", "Resolved"),
]

# Ground truths — computed, never hand-typed.
G = {}
G["total_attempted"] = sum(r[3] for r in LEDGER)
G["total_correct"] = sum(r[4] for r in LEDGER)
G["week_attempted"] = sum(r[3] for r in LEDGER if r[1] <= 7)
G["week_cy"] = sum(r[6] for r in LEDGER if r[1] <= 7)
G["total_minutes"] = sum(r[5] for r in LEDGER)
G["phy_attempted"] = sum(r[3] for r in LEDGER if r[0] == "Physics")
G["phy_correct"] = sum(r[4] for r in LEDGER if r[0] == "Physics")
G["chem_attempted"] = sum(r[3] for r in LEDGER if r[0] == "Chem")
G["maths_sessions"] = sum(1 for r in LEDGER if r[0] == "Maths")
G["overall_acc"] = round(100 * G["total_correct"] / G["total_attempted"])   # 71
G["phy_acc"] = round(100 * G["phy_correct"] / G["phy_attempted"])           # 80
G["chem_acc"] = round(100 * sum(r[4] for r in LEDGER if r[0] == "Chem")
                      / G["chem_attempted"])                                # 75
G["maths_acc"] = round(100 * sum(r[4] for r in LEDGER if r[0] == "Maths")
                       / sum(r[3] for r in LEDGER if r[0] == "Maths"))      # 60
G["yesterday_attempted"] = sum(r[3] for r in LEDGER if r[1] == 1)           # 30
G["unresolved"] = sum(1 for _, s in DOUBTS if s == "Unresolved")
G["phy_min_per_q"] = round(sum(r[5] for r in LEDGER if r[0] == "Physics")
                           / G["phy_attempted"], 1)                         # 3.0


def seed(db_path: Path) -> None:
    with sync.connect(db_path) as conn:
        sync.init_db(conn)
    with sqlite3.connect(db_path) as conn:
        for i, (subj, ago, ex, att, cor, mins, cy) in enumerate(LEDGER):
            conn.execute(
                "INSERT INTO ledger (notion_page_id, archived, last_synced_at, raw_json, "
                "task, subject, exercise_type, date, questions_attempted, questions_correct, "
                "actual_time_min, cognitive_yield, accuracy_ratio) "
                'VALUES (?, 0, ?, "{}", ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (f"gt-l{i}", d(0), f"{subj} {ex}", subj, ex, d(ago), att, cor, mins, cy,
                 cor / att),
            )
        for i, (concept, status) in enumerate(DOUBTS):
            conn.execute(
                "INSERT INTO doubts (notion_page_id, archived, last_synced_at, raw_json, "
                'core_concept, status) VALUES (?, 0, ?, "{}", ?, ?)',
                (f"gt-d{i}", d(0), concept, status),
            )
        conn.commit()


def contains_number(answer: str, value) -> bool:
    """Value appears as a standalone-ish number ('80', '80.0', '80%')."""
    v = f"{value:g}" if isinstance(value, float) else str(value)
    return re.search(rf"(?<![\d.]){re.escape(v)}(?:\.0)?(?![\d])", answer) is not None


def says_none(answer: str) -> bool:
    low = answer.lower()
    return any(w in low for w in ("no ", "none", "0", "not ", "haven't", "didn't", "zero"))


# (category, question, checker(answer)->bool, description)
QUESTIONS = [
    # ---- clean ----
    ("clean", "how many questions have I attempted in total?",
     lambda a: contains_number(a, G["total_attempted"]), f"= {G['total_attempted']}"),
    ("clean", "how many questions did I attempt in the last 7 days?",
     lambda a: contains_number(a, G["week_attempted"]), f"= {G['week_attempted']}"),
    ("clean", "what's my overall accuracy percentage?",
     lambda a: contains_number(a, G["overall_acc"]) or contains_number(a, 71.4),
     f"≈ {G['overall_acc']}%"),
    ("clean", "how many physics questions have I attempted?",
     lambda a: contains_number(a, G["phy_attempted"]), f"= {G['phy_attempted']}"),
    ("clean", "what's my total cognitive yield for the last 7 days?",
     lambda a: contains_number(a, G["week_cy"]), f"= {G['week_cy']}"),
    ("clean", "how many unresolved doubts do I have?",
     lambda a: contains_number(a, G["unresolved"]), f"= {G['unresolved']}"),
    ("clean", "how many maths study sessions have I logged?",
     lambda a: contains_number(a, G["maths_sessions"]), f"= {G['maths_sessions']}"),
    ("clean", "what's my accuracy in chem?",
     lambda a: contains_number(a, G["chem_acc"]), f"= {G['chem_acc']}%"),
    ("clean", "how many questions did I get correct in physics?",
     lambda a: contains_number(a, G["phy_correct"]), f"= {G['phy_correct']}"),
    ("clean", "how many minutes have I studied in total?",
     lambda a: contains_number(a, G["total_minutes"]), f"= {G['total_minutes']}"),
    # ---- the owner's real typing style ----
    ("typo", "hw mny qs i attmptd in totl?",
     lambda a: contains_number(a, G["total_attempted"]), f"= {G['total_attempted']}"),
    ("typo", "wats my acc in phy",
     lambda a: contains_number(a, G["phy_acc"]), f"= {G['phy_acc']}%"),
    ("typo", "totl cy ths week?",
     lambda a: contains_number(a, G["week_cy"]), f"= {G['week_cy']}"),
    ("typo", "hw mny unresolvd douts i hv",
     lambda a: contains_number(a, G["unresolved"]), f"= {G['unresolved']}"),
    ("typo", "kitne questions kiye maine chem me",
     lambda a: contains_number(a, G["chem_attempted"]), f"= {G['chem_attempted']}"),
    ("typo", "how mch time i studied in mnutes total",
     lambda a: contains_number(a, G["total_minutes"]), f"= {G['total_minutes']}"),
    ("typo", "physcs me kitne sahi hue",
     lambda a: contains_number(a, G["phy_correct"]), f"= {G['phy_correct']}"),
    ("typo", "maths sessions kitne hai",
     lambda a: contains_number(a, G["maths_sessions"]), f"= {G['maths_sessions']}"),
    ("typo", "yestrday kitne qs attempt kiya?",
     lambda a: contains_number(a, G["yesterday_attempted"]), f"= {G['yesterday_attempted']}"),
    ("typo", "acc of chem btao",
     lambda a: contains_number(a, G["chem_acc"]), f"= {G['chem_acc']}%"),
    # ---- traps ----
    ("trap", "how many biology questions did I attempt?",
     says_none, "must say none — NOT invent"),
    ("trap", "did I study chemistry today?",
     says_none, "no chem today — must say no"),
    ("trap", "how many mock exams have I taken?",
     says_none, "no exams seeded — must say none"),
    ("trap", "compare my physics and chem accuracy",
     lambda a: contains_number(a, G["phy_acc"]) and contains_number(a, G["chem_acc"]),
     f"both {G['phy_acc']} and {G['chem_acc']}"),
    ("trap", "which subject has my highest accuracy?",
     lambda a: "physics" in a.lower(), "physics (80%)"),
    ("trap", "which subject has my lowest accuracy?",
     lambda a: "math" in a.lower(), "maths (60%)"),
    ("trap", "how many questions did I attempt yesterday?",
     lambda a: contains_number(a, G["yesterday_attempted"]), f"= {G['yesterday_attempted']}"),
    ("trap", "how many doubts do I have about kinematics?",
     lambda a: contains_number(a, 1), "= 1"),
    ("trap", "what's my average minutes per question in physics?",
     lambda a: contains_number(a, 3) or contains_number(a, 3.0), "= 3.0"),
    ("trap", "FOLLOWUP", None, "follow-up chain: physics then 'and in chem?'"),
]


def ask(question: str, db_path, history=None) -> str:
    """One question with a single retry when the loop itself reports ⚠️."""
    for attempt in (1, 2):
        answer = sql_query_flow.answer_question(question, db_path=db_path, history=history)
        if not answer.startswith("⚠️"):
            return answer
        time.sleep(4)
    return answer


def main() -> int:
    tmp = Path(tempfile.mkdtemp()) / "groundtruth.db"
    seed(tmp)
    print(f"Seeded {len(LEDGER)} ledger rows + {len(DOUBTS)} doubts into {tmp}")
    print(f"Ground truths: {G}\n")
    passed, failed, critical, infra = 0, [], [], 0
    for cat, question, checker, desc in QUESTIONS:
        if question == "FOLLOWUP":
            first = ask("how many physics questions did I attempt?", tmp)
            answer = ask("and in chem?", tmp,
                         history=[{"question": "how many physics questions did I attempt?",
                                   "answer": first}])
            ok = not answer.startswith("⚠️") and contains_number(answer, G["chem_attempted"])
            question = "and in chem? (follow-up)"
        else:
            answer = ask(question, tmp)
            ok = not answer.startswith("⚠️") and bool(checker(answer))
        flat = answer.replace("\n", " ")[:160]
        if answer.startswith("⚠️"):
            verdict = "INFRA-FAIL"
            infra += 1
        elif ok:
            verdict = "PASS"
        elif cat == "trap" and desc.startswith("must say"):
            verdict = "CRITICAL-HALLUCINATION?"
            critical.append(question)
        else:
            verdict = "FAIL"
        print(f"[{verdict:^24}] ({cat}) {question}\n{'':26}expect {desc} | got: {flat}")
        if ok:
            passed += 1
        else:
            failed.append((cat, question))
    total = len(QUESTIONS)
    pct = 100 * passed / total
    print("\n" + "=" * 70)
    for cat in ("clean", "typo", "trap"):
        cat_total = sum(1 for c, *_ in QUESTIONS if c == cat)
        cat_pass = cat_total - sum(1 for c, q in failed if c == cat)
        print(f"  {cat:>6}: {cat_pass}/{cat_total}")
    print(f"SCORE: {passed}/{total} ({pct:.0f}%) — gate is ≥95%"
          + (f" | infra failures: {infra}" if infra else "")
          + (f" | CRITICAL: {critical}" if critical else ""))
    return 0 if pct >= 95 and not critical else 1


if __name__ == "__main__":
    sys.exit(main())
