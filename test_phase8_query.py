"""
Phase 8 tests — retrieval / query flow.

Runs against the real SQLite mirror (read-only). Covers the spec's canonical
query shapes. Assertions are on shape/behaviour, not exact row counts (the
mirror content can change), except where the mirror is known to hold data.

Run: python3 test_phase8_query.py
"""

from __future__ import annotations

import query_flow as qf
from intent_parser import _validate_intent


def _q(database=None, **filters):
    return _validate_intent({
        "action": "query",
        "database": database,
        "fields": {},
        "filters": filters,
        "needs_clarification": False,
        "clarification_question": None,
    })


def _check(ok_all, label, cond, extra=""):
    print(f"[{'OK ' if cond else 'BAD'}] {label}{(' -> ' + extra) if extra else ''}")
    return ok_all and cond


def run() -> bool:
    ok = True

    print("=== Phase 8: unresolved doubts ===")
    r = qf.run_query(_q("doubts", status="Unresolved"))
    ok = _check(ok, "targets doubts db", r["db_key"] == "doubts")
    ok = _check(ok, "returns >=1 unresolved (mirror seeded)", r["count"] >= 1, str(r["count"]))
    ok = _check(ok, "every row has a url", all(row["url"] for row in r["rows"]))

    print("\n=== Phase 8: due for revision ===")
    r = qf.run_query(_q("revision", keyword="due"))
    ok = _check(ok, "targets revision db", r["db_key"] == "revision")
    ok = _check(ok, "'due' not treated as text search", r["count"] >= 1, str(r["count"]))

    print("\n=== Phase 8: keyword search (kinematics) ===")
    r = qf.run_query(_q("revision", keyword="kinematics"))
    ok = _check(ok, "finds kinematics chapter", r["count"] >= 1
                and any("kinematics" in row["title"].lower() for row in r["rows"]))

    print("\n=== Phase 8: chapter filter behaves as keyword ===")
    r = qf.run_query(_q("revision", chapter="friction"))
    ok = _check(ok, "chapter filter matches friction", r["count"] >= 1
                and any("friction" in row["title"].lower() for row in r["rows"]))

    print("\n=== Phase 8: today's ledger ===")
    r = qf.run_query(_q("ledger", date="today"))
    ok = _check(ok, "targets ledger db", r["db_key"] == "ledger")
    ok = _check(ok, "count is an int >= 0", isinstance(r["count"], int) and r["count"] >= 0, str(r["count"]))

    print("\n=== Phase 8: db inference when database is null ===")
    r = qf.run_query(_q(None, keyword="doubt"))
    ok = _check(ok, "infers doubts from keyword", r["db_key"] == "doubts")

    print("\n=== Phase 8: empty result formatting ===")
    r = qf.run_query(_q("doubts", keyword="zzz_no_match_zzz"))
    ok = _check(ok, "no matches -> count 0", r["count"] == 0)
    ok = _check(ok, "formats a 'no matching' line", "No matching" in qf.format_result(r))

    print("\n=== Phase 8: result formatting has links ===")
    r = qf.run_query(_q("doubts"))
    txt = qf.format_result(r)
    ok = _check(ok, "formatted output includes markdown link", "](http" in txt if r["count"] else True)

    print("\n" + ("ALL PHASE 8 CHECKS PASSED" if ok else "SOME PHASE 8 CHECKS FAILED"))
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if run() else 1)
