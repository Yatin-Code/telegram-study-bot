"""Failure drills: the bot must fail HONESTLY — visible ⚠️, never silent loss.

Offline (monkeypatched) versions of: LLM gateway dead during an answer,
LLM dead during intent parsing, Notion dead during a log commit (queued +
flushed on recovery), and bug-report round-trip.
"""

from __future__ import annotations

import httpx
import pytest

import draft_store
import intent_parser
import logging_flow
import sql_query_flow
import sync


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "drill.db"
    with sync.connect(path) as conn:
        sync.init_db(conn)
    return path


def test_answer_loop_llm_dead_is_honest(db, monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("gateway unreachable")
    monkeypatch.setattr(sql_query_flow, "_openai_chat", boom)
    monkeypatch.setattr(sql_query_flow, "_anthropic_chat", boom)
    monkeypatch.setattr(sql_query_flow.time, "sleep", lambda s: None)
    answer = sql_query_flow.answer_question("how many questions today?", db_path=db)
    assert answer.startswith("⚠️")
    assert "couldn't reach" in answer.lower() or "trouble" in answer.lower()


def test_llm_retries_transient_then_succeeds(db, monkeypatch):
    calls = {"n": 0}
    def flaky(client, model, messages):
        calls["n"] += 1
        if calls["n"] == 1:
            request = httpx.Request("POST", "http://x")
            response = httpx.Response(502, request=request)
            raise httpx.HTTPStatusError("502", request=request, response=response)
        return "ANSWER: all good"
    monkeypatch.setattr(sql_query_flow, "_openai_chat", flaky)
    monkeypatch.setattr(sql_query_flow.time, "sleep", lambda s: None)
    answer = sql_query_flow.answer_question("anything?", db_path=db)
    assert answer == "all good" and calls["n"] == 2


def test_intent_parse_llm_dead_raises_cleanly(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("gateway unreachable")
    monkeypatch.setattr(intent_parser, "_call_model", boom)
    with pytest.raises(intent_parser.IntentParseError):
        intent_parser.parse_message("did 10 questions in physics")


def test_notion_down_queues_then_flushes(db, monkeypatch):
    payload = {
        "db_key": "ledger",
        "properties": {"task": "drill entry", "subject": "Physics"},
        "operation_id": "drill-op-1",
    }
    def notion_down(*a, **k):
        raise RuntimeError("Notion 503")
    monkeypatch.setattr(logging_flow.notion, "create_page", notion_down)
    result = logging_flow.commit_write(dict(payload), db_path=db, do_sync=False)
    assert result["status"] == "queued"
    assert logging_flow.pending_count(db_path=db) == 1

    # Notion recovers — the queued write flushes and nothing is lost.
    created = []
    def notion_up(db_key, props):
        created.append((db_key, props))
        return {"id": "page-1", "url": "https://notion/x"}
    monkeypatch.setattr(logging_flow.notion, "create_page", notion_up)
    monkeypatch.setattr(logging_flow.sync, "sync_once", lambda *a, **k: {})
    flushed = logging_flow.flush_pending(db_path=db, sync_after=False)
    assert flushed  # non-empty flush result
    assert logging_flow.pending_count(db_path=db) == 0
    assert created and created[0][0] == "ledger"


def test_bug_report_roundtrip(tmp_path):
    db = tmp_path / "bugs.db"
    bug_id = draft_store.add_bug_report(5, "answer said 20 but I logged 10", db_path=db)
    bugs = draft_store.list_bug_reports(5, db_path=db)
    assert [b["id"] for b in bugs] == [bug_id]
    assert draft_store.close_bug_report(5, bug_id, db_path=db) is True
    assert draft_store.list_bug_reports(5, db_path=db) == []
    assert draft_store.close_bug_report(5, bug_id, db_path=db) is False
