"""Certification: only 100%-proven routes may serve traffic.

The user's rule: **every model must pass every AI test in this project, at 100%,
for the purpose it will serve** before the router is allowed to select it. Until
a route is certified for a purpose it is excluded from that purpose's pool, so
before any certification run the router falls entirely to the legacy eaon tail.

`python -m llm.certify` runs, for each resolvable route (= each model), the real
prepared batteries harvested from the project's own test suites — driven through
the actual code paths (`intent_parser.parse_message`, `sql_query_flow.answer_
question`, `domain_parser.parse_job`/`parse_goal`), with every router call pinned
to that one model via `router.force_route`. A route is certified for a purpose
only when it passes 100% of that purpose's battery.

Batteries (single source of truth = the test files, not copies):
- intent  -> test_phase5_parser.CASES        (22 parse cases: log/query/context/remember/traps)
- sql     -> test_answers_groundtruth         (30 questions vs a seeded known-total DB, incl.
                                               hallucination traps + a follow-up chain)
- domain  -> parse_job / parse_goal cases     (schedule + goal extraction, deterministic checks)

Persistence: a JSON sidecar `llm/.certified.json` mapping purpose -> [route_id].
The router imports only the read helpers here, so there is no import cycle.

Run:  python -m llm.certify [--purpose intent|domain|sql] [--route ID] [--force]
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Optional

from . import registry

_CERT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".certified.json")

PURPOSES = ("intent", "domain", "sql")


# --- Persistence (no router dependency — safe to import from router) ---------

def load(path: str | None = None) -> dict[str, list[str]]:
    """Return {purpose: [route_id, …]} from the sidecar. Missing/broken → {}."""
    try:
        with open(path or _CERT_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, list[str]] = {}
    for purpose, ids in data.items():
        if isinstance(ids, list):
            out[str(purpose)] = [str(i) for i in ids]
    return out


def certified_route_ids(purpose: str, path: str | None = None) -> set[str]:
    """Route ids certified for ``purpose``."""
    return set(load(path).get(purpose, []))


def _write(data: dict[str, list[str]], path: str | None = None) -> None:
    target = path or _CERT_PATH
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({k: sorted(set(v)) for k, v in data.items()}, fh, indent=2, sort_keys=True)
    os.replace(tmp, target)


def _mark(purpose: str, route_id: str, certified: bool, path: str | None = None) -> None:
    data = load(path)
    ids = set(data.get(purpose, []))
    if certified:
        ids.add(route_id)
    else:
        ids.discard(route_id)
    data[purpose] = sorted(ids)
    _write(data, path)


# --- Battery runners --------------------------------------------------------
# Each returns (passed, total, failures[str]). They call the REAL code paths;
# the caller wraps them in router.force_route(route) so every underlying router
# call is pinned to the model under test. An import failure of a test module
# yields (0, 0, [...]) and the purpose is reported skipped, never crashing.

class BatteryUnavailable(RuntimeError):
    """A test battery could not be loaded (import error in the source suite)."""


def _run_intent_battery() -> tuple[int, int, list[str]]:
    try:
        from test_phase5_parser import CASES
        from intent_parser import IntentParseError, parse_message
    except Exception as exc:  # noqa: BLE001
        raise BatteryUnavailable(f"intent battery unavailable: {exc}") from exc

    passed = 0
    failures: list[str] = []
    for msg, ctx, expect in CASES:
        try:
            intent = parse_message(msg, session_context=ctx)
        except IntentParseError as exc:
            detail = f"{msg!r}: parse error ({exc})"
            failures.append(detail)
            if _is_infrastructure(detail):
                break
            continue
        except Exception as exc:  # noqa: BLE001 — provider/route failure
            detail = f"{msg!r}: {type(exc).__name__}: {exc}"
            failures.append(detail)
            if _is_infrastructure(detail):
                break
            continue
        bad = []
        if "action" in expect and intent.action != expect["action"]:
            bad.append(f"action={intent.action}!={expect['action']}")
        if "database" in expect and intent.database != expect["database"]:
            bad.append(f"database={intent.database}!={expect['database']}")
        if bad:
            failures.append(f"{msg!r}: " + ", ".join(bad))
        else:
            passed += 1
    return passed, len(CASES), failures


def _run_sql_battery() -> tuple[int, int, list[str]]:
    try:
        import test_answers_groundtruth as gt
        import sql_query_flow
    except Exception as exc:  # noqa: BLE001
        raise BatteryUnavailable(f"sql battery unavailable: {exc}") from exc

    tmp = Path(tempfile.mkdtemp()) / "certify_gt.db"
    try:
        gt.seed(tmp)
    except Exception as exc:  # noqa: BLE001
        raise BatteryUnavailable(f"sql battery seed failed: {exc}") from exc

    passed = 0
    failures: list[str] = []
    total = len(gt.QUESTIONS)
    for cat, question, checker, desc in gt.QUESTIONS:
        try:
            if question == "FOLLOWUP":
                first = sql_query_flow.answer_question("how many physics questions did I attempt?", db_path=tmp)
                answer = sql_query_flow.answer_question(
                    "and in chem?", tmp,
                    history=[{"question": "how many physics questions did I attempt?",
                              "answer": first}])
                ok = not answer.startswith("⚠️") and gt.contains_number(answer, gt.G["chem_attempted"])
                label = "and in chem? (follow-up)"
            else:
                answer = sql_query_flow.answer_question(question, db_path=tmp)
                ok = not answer.startswith("⚠️") and bool(checker(answer))
                label = question
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{question!r}: {type(exc).__name__}: {exc}")
            continue
        if ok:
            passed += 1
        else:
            tag = "CRITICAL-HALLUCINATION" if (cat == "trap" and str(desc).startswith("must say")) else "fail"
            detail = f"; answer={answer.replace(chr(10), ' ')[:240]!r}" if answer.startswith("⚠️") else ""
            failures.append(f"[{tag}] {label!r} (expect {desc}){detail}")
            if answer.startswith("⚠️") and _is_infrastructure(answer):
                break
    return passed, total, failures


def _run_domain_battery() -> tuple[int, int, list[str]]:
    """Schedule + goal extraction, mirroring the checks in test_jobs_live."""
    try:
        import domain_parser
        import user_jobs
    except Exception as exc:  # noqa: BLE001
        raise BatteryUnavailable(f"domain battery unavailable: {exc}") from exc

    failures: list[str] = []
    total = 0

    # 1. explicit-time weekday ask-job
    total += 1
    try:
        p = domain_parser.parse_job("every weekday at 9pm tell me my overall week cognitive yield")
        data, err = (None, "clarified") if p.get("needs_clarification") else user_jobs.validate_parsed(p)
        if not (data and data["schedule_kind"] == "weekdays" and data["run_time"] == "21:00"
                and data["action_kind"] == "ask"):
            failures.append(f"weekday-ask: {err or data}")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"weekday-ask: {type(exc).__name__}: {exc}")

    # 2. weekly message reminder
    total += 1
    try:
        p = domain_parser.parse_job("remind me every sunday at 9pm to plan my week")
        data, err = (None, "clarified") if p.get("needs_clarification") else user_jobs.validate_parsed(p)
        if not (data and data["schedule_kind"] == "weekly" and data["weekday"] == 6
                and data["run_time"] == "21:00" and data["action_kind"] == "message"):
            failures.append(f"weekly-reminder: {err or data}")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"weekly-reminder: {type(exc).__name__}: {exc}")

    # 3. once + relative-date grounding (year must resolve to next 9 August)
    total += 1
    try:
        p = domain_parser.parse_job("on 9 august at 8am remind me that my mock exam is tomorrow")
        data, err = (None, "clarified") if p.get("needs_clarification") else user_jobs.validate_parsed(p)
        if not (data and data["schedule_kind"] == "once" and str(data.get("run_date", "")).endswith("-08-09")):
            failures.append(f"once-date: {err or data}")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"once-date: {type(exc).__name__}: {exc}")

    # 4. goal extraction returns a structured object
    total += 1
    try:
        goal = domain_parser.parse_goal("I want to hit 250 questions a day")
        if not isinstance(goal, dict):
            failures.append("goal: not an object")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"goal: {type(exc).__name__}: {exc}")

    return total - len(failures), total, failures


_BATTERIES = {
    "intent": _run_intent_battery,
    "sql": _run_sql_battery,
    "domain": _run_domain_battery,
}


_INFRA_MARKERS = (
    "quota_or_cooldown", "http_500", "http_502", "http_503", "http_504",
    ": network", ": 429", "auth_401", "auth_402", "auth_403",
)


def _is_infrastructure(text: str) -> bool:
    return any(marker in text for marker in _INFRA_MARKERS)


def _infrastructure_only(failures: list[str]) -> bool:
    """True when no executed case demonstrated a model-quality failure."""
    return bool(failures) and all(_is_infrastructure(item) for item in failures)


# --- Harness ----------------------------------------------------------------

def run(
    purposes: Optional[list[str]] = None,
    only_route: Optional[str] = None,
    force: bool = False,
    path: str | None = None,
) -> dict[str, dict[str, bool]]:
    """Certify each resolvable route against every AI battery. 100% = certified.

    Returns {purpose: {route_id: passed}}. Resumable: a (route, purpose) already
    certified is skipped unless ``force``. Every router call inside a battery is
    pinned to the route under test via ``router.force_route``.
    """
    from . import router  # lazy: router imports this module for reads

    purposes = purposes or list(PURPOSES)
    routes = registry.resolve_routes()
    if only_route:
        routes = [r for r in routes if r.id == only_route]

    if not routes:
        print("[certify] no resolvable routes (is ai.env populated?) — nothing to do")
        return {}

    results: dict[str, dict[str, bool]] = {p: {} for p in purposes}
    for route in routes:
        print(f"\n=== {route.id}  (model={route.model}) ===")
        for purpose in purposes:
            already = certified_route_ids(purpose, path)
            if route.id in already and not force:
                print(f"  {purpose:6}: already certified, skipping")
                results[purpose][route.id] = True
                continue
            try:
                with router.force_route(route):
                    passed, total, failures = _BATTERIES[purpose]()
            except BatteryUnavailable as exc:
                print(f"  {purpose:6}: SKIPPED — {exc}")
                continue
            certified = total > 0 and passed == total
            incomplete = not certified and _infrastructure_only(failures)
            results[purpose][route.id] = certified
            _mark(purpose, route.id, certified, path)
            if certified:
                status = f"CERTIFIED {passed}/{total} (100%)"
            elif incomplete:
                status = f"INCOMPLETE {passed}/{total} (provider/quota interruption; not scored)"
            else:
                status = f"FAILED {passed}/{total}"
            print(f"  {purpose:6}: {status}")
            for f in failures[:6]:
                print(f"           - {f}")
            if len(failures) > 6:
                print(f"           … +{len(failures) - 6} more")
    return results


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Certify LLM routes: run every AI battery per model, require 100%.")
    ap.add_argument("--purpose", choices=PURPOSES, action="append", dest="purposes",
                    help="limit to one or more purposes (default: all)")
    ap.add_argument("--route", dest="route", help="limit to one route id")
    ap.add_argument("--force", action="store_true", help="re-certify even if already certified")
    args = ap.parse_args(argv)
    results = run(purposes=args.purposes, only_route=args.route, force=args.force)
    print("\n=== summary ===")
    for purpose, routemap in results.items():
        certified = [rid for rid, ok in routemap.items() if ok]
        print(f"  {purpose:6}: {len(certified)} certified — {', '.join(certified) or '(none)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
