from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

import bot
import logging_flow
import operational_store
import sync


def test_rows_rejects_sql_injection_and_keeps_parameterized_filters(tmp_path):
    db = tmp_path / "phase1-sql.db"
    operational_store.create(
        "goals",
        {"title": "Daily PYQs", "status": "Active", "operation_id": "safe-goal"},
        db_path=db,
    )

    rows = operational_store.rows(
        "goals", "archived=0 AND status IN ('Active','Draft')", db_path=db
    )
    assert [row["title"] for row in rows] == ["Daily PYQs"]
    assert operational_store.rows(
        "goals", "archived=0 AND title=?", ("Daily PYQs",), db_path=db
    )

    attacks = [
        "archived=0; DELETE FROM op_goals",
        "archived=0 UNION SELECT * FROM op_exams",
        "archived=0 -- hide the rest",
        "archived=0 OR EXISTS(SELECT 1 FROM sqlite_master)",
        "archived=0 OR randomblob(1000000) IS NOT NULL",
    ]
    for where in attacks:
        with pytest.raises(operational_store.OperationalStoreError):
            operational_store.rows("goals", where, db_path=db)

    assert len(operational_store.rows("goals", db_path=db)) == 1


def test_sync_error_survives_rollback_and_partial_rows_do_not(tmp_path, monkeypatch):
    db = tmp_path / "phase1-sync.db"

    def broken_sync(conn, db_key):
        conn.execute(
            "INSERT INTO ledger "
            "(notion_page_id,last_synced_at,raw_json,archived,task) "
            "VALUES ('partial','now','{}',0,'must rollback')"
        )
        raise RuntimeError("Notion exploded")

    monkeypatch.setattr(sync, "sync_database", broken_sync)
    with pytest.raises(RuntimeError, match="Notion exploded"):
        sync.sync_once(db_path=db, db_keys=("ledger",))

    with sync.connect(db) as conn:
        partial = conn.execute(
            "SELECT 1 FROM ledger WHERE notion_page_id='partial'"
        ).fetchone()
        meta = conn.execute(
            "SELECT last_error FROM sync_meta WHERE db_key='ledger'"
        ).fetchone()
    assert partial is None
    assert meta is not None and "Notion exploded" in meta["last_error"]


def test_sync_once_serializes_threads(tmp_path, monkeypatch):
    db = tmp_path / "phase1-lock.db"
    state = {"active": 0, "max_active": 0}
    guard = threading.Lock()

    def observed_sync(_conn, _db_key):
        with guard:
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
        time.sleep(0.03)
        with guard:
            state["active"] -= 1
        return 0

    monkeypatch.setattr(sync, "sync_database", observed_sync)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(sync.sync_once, db_path=db, db_keys=("ledger",))
            for _ in range(2)
        ]
        assert [future.result() for future in futures] == [
            {"ledger": 0}, {"ledger": 0}
        ]
    assert state["max_active"] == 1


def test_flush_pending_dead_letters_after_bounded_attempts(tmp_path, monkeypatch):
    db = tmp_path / "phase1-queue.db"
    logging_flow.enqueue_pending(
        {
            "db_key": "ledger",
            "properties": {"task": "permanently broken"},
            "operation_id": "broken-op",
        },
        db_path=db,
    )
    calls = {"n": 0}

    def always_fails(*_args, **_kwargs):
        calls["n"] += 1
        raise RuntimeError("permanent payload failure")

    monkeypatch.setattr(logging_flow.notion, "query_database", always_fails)
    for _ in range(logging_flow.MAX_PENDING_ATTEMPTS + 2):
        logging_flow.flush_pending(db_path=db, sync_after=False)

    assert calls["n"] == logging_flow.MAX_PENDING_ATTEMPTS
    assert logging_flow.pending_count(db_path=db) == 1
    assert logging_flow.exhausted_pending_count(db_path=db) == 1
    with sqlite3.connect(db) as conn:
        attempts, exhausted, error = conn.execute(
            f"SELECT attempts, exhausted, last_error "
            f"FROM {logging_flow.WRITE_QUEUE_TABLE} WHERE operation_id='broken-op'"
        ).fetchone()
    assert attempts == logging_flow.MAX_PENDING_ATTEMPTS
    assert exhausted == 1
    assert "permanent payload failure" in error


def test_unknown_intent_routes_to_general_assistant(monkeypatch):
    replies: list[str] = []

    class Message:
        text = "what can you do?"

        async def reply_text(self, text, **_kwargs):
            replies.append(text)

    update = SimpleNamespace(
        effective_message=Message(),
        effective_chat=SimpleNamespace(id=42),
        effective_user=SimpleNamespace(id=42),
    )

    async def allowed(_update):
        return False

    routed: list[tuple[str, int]] = []

    async def assistant(_update, text, chat_id):
        routed.append((text, chat_id))

    monkeypatch.setattr(bot, "_reject_if_unauthorized", allowed)
    monkeypatch.setattr(bot.reset_service, "pending_confirmation", lambda _chat: None)
    monkeypatch.setattr(bot.exam_readiness, "pending_resolution", lambda _chat: None)
    monkeypatch.setattr(bot.draft_store, "get_pending_setting_edit", lambda _chat: None)
    monkeypatch.setattr(bot.user_jobs, "get_pending_edit", lambda _chat: None)
    monkeypatch.setattr(bot.draft_store, "get_session_debrief", lambda _chat: None)
    monkeypatch.setattr(bot.onboarding, "active_section", lambda _chat: None)
    monkeypatch.setattr(bot.onboarding, "is_complete", lambda _chat: True)
    monkeypatch.setattr(bot.draft_store, "get_editing_draft_for_chat", lambda _chat: None)
    monkeypatch.setattr(bot.draft_store, "get_pending_clarification", lambda _chat: None)
    monkeypatch.setattr(bot, "_try_pattern_match", lambda _text: None)
    monkeypatch.setattr(
        bot,
        "parse_message",
        lambda *_args, **_kwargs: SimpleNamespace(
            action="unknown",
            needs_clarification=True,
            clarification_question="What did you mean?",
        ),
    )
    monkeypatch.setattr(bot, "_handle_general_assistant", assistant)

    asyncio.run(bot.catch_all(update, SimpleNamespace()))

    assert routed == [("what can you do?", 42)]
    assert "received" not in replies
