"""
Phase 5 DoD test — run the intent parser in isolation (no Telegram/Notion).

Feeds ~18 sample messages (the Phase 9 test list plus a few edge cases) through
parse_message() and prints the validated Intent for each so it can be manually
verified. Each case carries a lightweight `expect` dict of the fields we most
care about; mismatches are flagged as CHECK (not hard failures) since exact
field extraction is model-dependent and the human reviews the JSON per the DoD.

Usage:
    python test_phase5_parser.py
"""

from __future__ import annotations

import sys

from intent_parser import IntentParseError, parse_message


# (message, session_context, expectations)
CASES: list[tuple[str, dict | None, dict]] = [
    # ---- log flow ----
    ("execution block 1, physics, kinematics, ex 1A, 10 qs 8 correct, 20 mins",
     None, {"action": "log_execution", "database": "ledger"}),
    ("execution block 1: solved kinematics ex 2A, 20 questions, 15 correct, 25 mins, doubt about sign convention",
     {"subject": "PHYSICS", "chapter": "Kinematics", "block": "EB-1"},
     {"action": "log_execution", "database": "ledger"}),
    ("doubt: why relative velocity sign flips",
     {"subject": "PHYSICS", "chapter": "Kinematics", "block": "EB-1"},
     {"action": "log_doubt", "database": "doubts"}),
    ("got a doubt: theta angle sign flip",
     None, {"action": "log_doubt", "database": "doubts"}),
    ("log revision: friction chapter, mastery in progress, next review in 3 days",
     None, {"action": "log_revision", "database": "revision"}),
    ("mark mole concept revision completed",
     None, {"action": "log_revision", "database": "revision"}),

    # ---- set_context ----
    ("starting EB-1 physics kinematics",
     None, {"action": "set_context", "database": None}),
    ("starting block 1, chemistry, mole concept",
     None, {"action": "set_context", "database": None}),
    ("/newsession maths calculus",
     None, {"action": "set_context", "database": None}),

    # ---- query flow ----
    ("list doubts", None, {"action": "query", "database": "doubts"}),
    ("unresolved doubts in kinematics", None,
     {"action": "query", "database": "doubts"}),
    ("kinematics doubts", None, {"action": "query", "database": "doubts"}),
    ("current subj doubts today",
     {"subject": "PHYSICS", "chapter": "Kinematics", "block": "EB-1"},
     {"action": "query", "database": "doubts"}),
    ("what's my cognitive yield today", None,
     {"action": "query", "database": "ledger"}),
    ("show pending revisions overdue", None,
     {"action": "query", "database": "revision"}),
    ("list block donuts", None, {"action": "query"}),

    # ---- edge / clarification ----
    ("doubt about that thing from earlier", None,
     {"action": "log_doubt"}),
    ("asdkjfh qwpoeiu", None, {"action": "unknown"}),
]


def _fmt_intent(intent) -> str:
    return intent.model_dump_json(indent=2)


def main() -> int:
    passed = 0
    checks = 0
    errors = 0

    for i, (msg, ctx, expect) in enumerate(CASES, 1):
        print("=" * 70)
        print(f"[{i:02d}] message: {msg!r}")
        if ctx:
            print(f"     context: {ctx}")
        try:
            intent = parse_message(msg, session_context=ctx)
        except IntentParseError as e:
            print(f"     !! PARSE ERROR: {e}")
            errors += 1
            continue

        print(_fmt_intent(intent))

        mismatches = []
        if "action" in expect and intent.action != expect["action"]:
            mismatches.append(f"action={intent.action} (expected {expect['action']})")
        if "database" in expect and intent.database != expect["database"]:
            mismatches.append(f"database={intent.database} (expected {expect['database']})")

        if mismatches:
            print(f"     CHECK: {'; '.join(mismatches)}")
            checks += 1
        else:
            print("     OK")
            passed += 1

    print("=" * 70)
    print(f"SUMMARY: {passed} matched expectation, {checks} to review, {errors} parse errors "
          f"(of {len(CASES)} total)")
    print("Phase 5 DoD = manually verify each JSON above is sensible.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
