"""LIVE test: does a real LLM turn free-form /setup answers into safe actions?

Calls the configured LLM (LLM_MODEL from .env) through
domain_parser.parse_setup_ai for realistic scenarios — including the exact
"my schedule changes constantly, I'll tell you weekly" case — and checks the
proposed actions pass deterministic validation AND make semantic sense.

Usage:
    python test_setup_ai_live.py

Not collected by pytest (script-style) so the offline suite stays
deterministic and network-free.
"""

from __future__ import annotations

import json
import sys

import domain_parser
import onboarding

SCENARIOS = [
    {
        "name": "weekly-changing coaching schedule (user's example)",
        "section": ("🏫 Coaching timetable",
                    "Send one class per message: Subject | day | HH:MM-HH:MM | teacher"),
        "text": ("hey you know what, my coaching schedule changes constantly, "
                 "so I'll just tell you at the end of every week — is that okay?"),
        "expect_any": {"remember_preference", "set_setting", "skip_section"},
        "must_include_one_of": {"remember_preference", "set_setting"},
    },
    {
        "name": "change baseline via capacity step",
        "section": ("💪 Daily capacity", "Daily cognitive-yield range."),
        "text": "make my baseline 260 please, keep the ceiling as it is",
        "expect_any": {"set_setting"},
        "must_include_one_of": {"set_setting"},
        "check": lambda actions: any(
            a["type"] == "set_setting" and a["key"] == "DAILY_CY_BASELINE"
            and a["value"] == "260" for a in actions
        ),
    },
    {
        "name": "mock exam in free form",
        "section": ("📝 Next mock/test", "Send: title | YYYY-MM-DD"),
        "text": "i have a major coaching test on 10th august 2026",
        "expect_any": {"create_exam"},
        "must_include_one_of": {"create_exam"},
        "check": lambda actions: any(
            a["type"] == "create_exam" and a["exam_date"][:10] == "2026-08-10"
            for a in actions
        ),
    },
    {
        "name": "plain skip",
        "section": ("📋 Backlog", "Send one pending item per message."),
        "text": "nah skip this whole thing please",
        "expect_any": {"skip_section"},
        "must_include_one_of": {"skip_section"},
    },
    {
        "name": "daily commitment said sideways",
        "section": ("✅ Daily commitments", "What will you do EVERY day?"),
        "text": "honestly the only promise i can keep is solving pyqs every single day",
        "expect_any": {"create_commitment", "remember_preference"},
        "must_include_one_of": {"create_commitment"},
    },
]


def main() -> int:
    failures = 0
    for scenario in SCENARIOS:
        print("=" * 72)
        print(f"[{scenario['name']}]")
        print(f"user: {scenario['text']!r}")
        try:
            parsed = domain_parser.parse_setup_ai(
                scenario["section"][0], scenario["section"][1], scenario["text"]
            )
        except Exception as exc:
            print(f"  !! LLM CALL FAILED: {exc}")
            failures += 1
            continue
        print("raw:", json.dumps(parsed, indent=1, ensure_ascii=False)[:800])
        if parsed.get("needs_clarification"):
            print("  -> model asked for clarification:",
                  parsed.get("clarification_question"))
            print("  [ACCEPTABLE] (clarifying is always safe)")
            continue
        actions, errors = onboarding.validate_ai_actions(parsed.get("actions") or [])
        types = {a["type"] for a in actions}
        print("validated types:", types, "| dropped:", errors)
        ok = True
        if not types:
            print("  BAD: no valid actions at all")
            ok = False
        if not types & scenario["must_include_one_of"]:
            print(f"  BAD: expected one of {scenario['must_include_one_of']}")
            ok = False
        unexpected = types - scenario["expect_any"]
        if unexpected:
            print(f"  CHECK: unexpected extra actions {unexpected} (review above)")
        if "check" in scenario and ok and not scenario["check"](actions):
            print("  BAD: semantic check failed (wrong key/value/date)")
            ok = False
        if ok:
            print("  PASS — preview would read:")
            for line in onboarding.describe_ai_actions(actions):
                print("   ", line)
        else:
            failures += 1
    print("=" * 72)
    print(f"SUMMARY: {len(SCENARIOS) - failures}/{len(SCENARIOS)} scenarios passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
