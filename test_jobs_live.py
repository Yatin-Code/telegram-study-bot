"""LIVE test: can a real LLM build /jobs schedules + does date grounding work?

Calls the configured LLM through domain_parser.parse_job / parse_commitment
for realistic requests — including the user's exact "every week day tell me
my overall week cognitive yield" — and checks the results pass deterministic
validation and make semantic sense.

Usage:
    python test_jobs_live.py
"""

from __future__ import annotations

import json
import sys

import domain_parser
import user_jobs


def show(parsed):
    print("raw:", json.dumps(parsed, indent=1, ensure_ascii=False)[:700])


def main() -> int:
    failures = 0

    # 1. User's exact sentence — no time stated: clarifying is the right move,
    #    a sensible default evening time is acceptable too.
    print("=" * 72)
    print("[1] exact user sentence, no time given")
    parsed = domain_parser.parse_job(
        "from now on evry week day u will tell me my overall week cognitive yield"
    )
    show(parsed)
    if parsed.get("needs_clarification"):
        print("  PASS — asked:", parsed.get("clarification_question"))
    else:
        data, err = user_jobs.validate_parsed(parsed)
        if data and data["schedule_kind"] == "weekdays" and data["action_kind"] == "ask":
            print("  PASS (chose a default time) —", user_jobs.describe(data | {"enabled": 1}))
        else:
            print("  BAD:", err or data)
            failures += 1

    # 2. Same request with an explicit time.
    print("=" * 72)
    print("[2] same request with explicit time")
    parsed = domain_parser.parse_job(
        "every weekday at 9pm tell me my overall week cognitive yield"
    )
    show(parsed)
    data, err = user_jobs.validate_parsed(parsed) if not parsed.get("needs_clarification") else (None, "clarified")
    if (
        data and data["schedule_kind"] == "weekdays" and data["run_time"] == "21:00"
        and data["action_kind"] == "ask" and "yield" in data["action_text"].lower()
    ):
        print("  PASS —", user_jobs.describe(data | {"enabled": 1}))
    else:
        print("  BAD:", err or data)
        failures += 1

    # 3. Weekly fixed reminder.
    print("=" * 72)
    print("[3] weekly reminder")
    parsed = domain_parser.parse_job("remind me every sunday at 9pm to plan my week")
    show(parsed)
    data, err = user_jobs.validate_parsed(parsed) if not parsed.get("needs_clarification") else (None, "clarified")
    if (
        data and data["schedule_kind"] == "weekly" and data["weekday"] == 6
        and data["run_time"] == "21:00" and data["action_kind"] == "message"
    ):
        print("  PASS —", user_jobs.describe(data | {"enabled": 1}))
    else:
        print("  BAD:", err or data)
        failures += 1

    # 4. One-time job with a bare "9 august" — date grounding must resolve the year.
    print("=" * 72)
    print("[4] once + relative date resolution")
    parsed = domain_parser.parse_job(
        "on 9 august at 8am remind me that my mock exam is tomorrow"
    )
    show(parsed)
    data, err = user_jobs.validate_parsed(parsed) if not parsed.get("needs_clarification") else (None, "clarified")
    if (
        data and data["schedule_kind"] == "once" and data["run_date"] == "2026-08-09"
        and data["run_time"] == "08:00"
    ):
        print("  PASS —", user_jobs.describe(data | {"enabled": 1}))
    else:
        print("  BAD (year/date not grounded?):", err or data)
        failures += 1

    # 5. The remember-intent path must now classify bot instructions as such.
    print("=" * 72)
    print("[5] parse_commitment routes bot instructions")
    parsed = domain_parser.parse_commitment(
        "from now on every weekday you will tell me my overall week cognitive yield"
    )
    show(parsed)
    if parsed.get("kind") == "bot_instruction":
        print("  PASS — classified as bot_instruction (routes to /jobs flow)")
    elif parsed.get("needs_clarification"):
        print("  PASS (clarified) —", parsed.get("clarification_question"))
    else:
        print("  BAD — classified as", parsed.get("kind"))
        failures += 1

    print("=" * 72)
    print(f"SUMMARY: {5 - failures}/5 scenarios passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
