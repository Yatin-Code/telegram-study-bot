"""
Phase 6 DoD test — session/context state.

Two layers:
  1. Store logic (offline, no LLM): set/get/clear, per-chat isolation, and
     local-midnight expiry (simulated by rewriting the stored local_date).
  2. DoD scenario (uses the real parser): "starting EB-1 physics kinematics"
     then a bare "doubt: sign of relative velocity" inherits physics/
     kinematics/EB-1 without re-asking.

Usage:
    python test_phase6_context.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

import session_context as sc
from intent_parser import IntentParseError, parse_message


def _fresh_db() -> Path:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return Path(tmp.name)


def test_store_logic() -> bool:
    ok = True
    db = _fresh_db()
    conn = sc.connect(db)
    sc.init_db(conn)

    # set + get round-trip
    sc.set_context(1, subject="PHYSICS", chapter="kinematics", block="EB-1",
                   exercise="MLE", conn=conn)
    ctx = sc.get_context(1, conn=conn)
    got = ((ctx or {}).get("subject"), (ctx or {}).get("chapter"),
           (ctx or {}).get("block"), (ctx or {}).get("exercise"))
    passed = got == ("PHYSICS", "kinematics", "EB-1", "MLE")
    ok &= passed
    print(f"[{'OK ' if passed else 'BAD'}] set/get round-trip -> {got}")

    # per-chat isolation
    empty = sc.get_context(2, conn=conn)
    passed = empty is None
    ok &= passed
    print(f"[{'OK ' if passed else 'BAD'}] other chat isolated -> {empty}")

    # clear
    sc.clear_context(1, conn=conn)
    passed = sc.get_context(1, conn=conn) is None
    ok &= passed
    print(f"[{'OK ' if passed else 'BAD'}] clear_context -> {sc.get_context(1, conn=conn)}")

    # midnight expiry: set, then backdate local_date -> should expire on read
    sc.set_context(3, subject="MATHS", chapter="calculus", block="ADV.", conn=conn)
    conn.execute(
        f"UPDATE {sc.CONTEXT_TABLE} SET local_date = '2000-01-01' WHERE chat_id = '3'"
    )
    conn.commit()
    expired = sc.get_context(3, conn=conn)
    passed = expired is None
    ok &= passed
    print(f"[{'OK ' if passed else 'BAD'}] midnight expiry -> {expired}")

    # exercise column migration: old-style table without it gains it on init_db
    db2 = _fresh_db()
    conn2 = sc.connect(db2)
    conn2.execute(
        f"CREATE TABLE {sc.CONTEXT_TABLE} (chat_id TEXT PRIMARY KEY, subject TEXT, "
        "chapter TEXT, block TEXT, session_started_at TEXT NOT NULL, "
        "local_date TEXT NOT NULL)"
    )
    sc.init_db(conn2)
    cols = {r["name"] for r in conn2.execute(f"PRAGMA table_info({sc.CONTEXT_TABLE})")}
    passed = "exercise" in cols
    ok &= passed
    print(f"[{'OK ' if passed else 'BAD'}] migration adds exercise column -> {sorted(cols)}")
    conn2.close()
    db2.unlink(missing_ok=True)

    # timer: elapsed_minutes reflects a backdated start; restart_timer resets it
    sc.set_context(4, subject="PHYSICS", conn=conn)
    import datetime as dt
    backdated = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=33)).isoformat()
    conn.execute(
        f"UPDATE {sc.CONTEXT_TABLE} SET session_started_at = ? WHERE chat_id = '4'",
        (backdated,),
    )
    conn.commit()
    elapsed = sc.elapsed_minutes(4, conn=conn)
    passed = elapsed is not None and 32 <= elapsed <= 34
    ok &= passed
    print(f"[{'OK ' if passed else 'BAD'}] elapsed_minutes ≈ 33 -> {elapsed}")

    sc.restart_timer(4, conn=conn)
    elapsed2 = sc.elapsed_minutes(4, conn=conn)
    kept = sc.get_context(4, conn=conn)
    passed = elapsed2 is not None and elapsed2 < 1 and (kept or {}).get("subject") == "PHYSICS"
    ok &= passed
    print(f"[{'OK ' if passed else 'BAD'}] restart_timer resets elapsed, keeps context -> {elapsed2}")

    conn.close()
    db.unlink(missing_ok=True)
    assert ok


@pytest.mark.live
def test_dod_scenario() -> bool:
    """DoD: bare doubt inherits previously-set context via the parser prompt."""
    db = _fresh_db()
    conn = sc.connect(db)
    sc.init_db(conn)
    ok = True

    # 1. "starting EB-1 physics kinematics" -> set_context
    try:
        intent = parse_message("starting EB-1 physics kinematics")
    except IntentParseError as e:
        print(f"[ERR] set_context parse failed: {e}")
        conn.close(); db.unlink(missing_ok=True)
        raise AssertionError(f"set_context parse failed: {e}") from e
    passed = intent.action == "set_context"
    ok &= passed
    print(f"[{'OK ' if passed else 'BAD'}] parse set_context -> {intent.action} "
          f"(subj={intent.filters.subject}, chap={intent.filters.chapter}, blk={intent.filters.block})")
    sc.set_context(
        99, subject=intent.filters.subject, chapter=intent.filters.chapter,
        block=intent.filters.block, conn=conn,
    )

    # 2. bare "doubt: sign of relative velocity" with stored context injected
    stored = None
    row = sc.get_context(99, conn=conn)
    if row:
        stored = {k: row.get(k) for k in sc.CONTEXT_KEYS if row.get(k)}
    try:
        intent2 = parse_message("doubt: sign of relative velocity", session_context=stored)
    except IntentParseError as e:
        print(f"[ERR] doubt parse failed: {e}")
        conn.close(); db.unlink(missing_ok=True)
        raise AssertionError(f"doubt parse failed: {e}") from e

    passed = intent2.action == "log_doubt"
    ok &= passed
    print(f"[{'OK ' if passed else 'BAD'}] parse log_doubt -> {intent2.action}")

    # authoritative merge fills subject/chapter/block from stored context
    effective = sc.merge_into_intent(intent2, 99, conn=conn)
    inherits = (
        effective.get("subject") and effective.get("chapter") and effective.get("block")
    )
    ok &= bool(inherits)
    print(f"[{'OK ' if inherits else 'BAD'}] doubt inherits context -> {effective}")

    conn.close()
    db.unlink(missing_ok=True)
    assert ok


def main() -> int:
    print("=== Phase 6: store logic (offline) ===")
    test_store_logic()
    print("\n=== Phase 6: DoD scenario (live parser) ===")
    test_dod_scenario()
    print()
    print("ALL PHASE 6 CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
