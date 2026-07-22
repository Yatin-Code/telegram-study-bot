from __future__ import annotations

import asyncio
import datetime as dt
import sqlite3
from types import SimpleNamespace

import pytest

import bot
import notion_client_wrapper as notion
import reset_service


def test_confirmation_is_exact_single_use_and_expiring(tmp_path):
    db = tmp_path / "confirm.db"
    pending = reset_service.create_confirmation(42, "everything", db_path=db)
    assert reset_service.consume_confirmation(42, pending["sentence"].lower(), db_path=db) is None
    assert reset_service.pending_confirmation(42, db_path=db) is not None

    consumed = reset_service.consume_confirmation(42, pending["sentence"], db_path=db)
    assert consumed and consumed["scope"] == "everything"
    assert reset_service.consume_confirmation(42, pending["sentence"], db_path=db) is None

    expired = reset_service.create_confirmation(42, "sqlite", ttl_minutes=0, db_path=db)
    assert reset_service.consume_confirmation(42, expired["sentence"], db_path=db) is None
    assert reset_service.pending_confirmation(42, db_path=db) is None


def test_notion_archive_wrapper_can_only_patch_a_page(monkeypatch):
    calls = []
    monkeypatch.setattr(
        notion, "_request",
        lambda method, url, **kwargs: calls.append((method, url, kwargs)) or {"archived": True},
    )
    notion.archive_page("page-123")
    assert calls == [(
        "PATCH", f"{notion.NOTION_API}/pages/page-123",
        {"json_body": {"archived": True}},
    )]
    assert "/databases/" not in calls[0][1]


def test_archive_all_pages_preserves_database_containers_and_reports_counts():
    active = {
        "ledger": {"l1", "l2"},
        "doubts": {"d1"},
    }
    queries = []
    archives = []

    def query(db_key, **kwargs):
        queries.append((db_key, kwargs))
        return iter({"id": page_id} for page_id in sorted(active[db_key]))

    def archive(page_id):
        archives.append(page_id)
        for pages in active.values():
            pages.discard(page_id)

    result = reset_service.archive_all_notion_pages(
        db_keys=("ledger", "doubts"), query_pages=query,
        archive_page=archive,
        sleep_func=lambda _: None, pause_seconds=0,
    )
    assert result["complete"] is True
    assert result["pages_found"] == result["pages_archived"] == 3
    assert result["databases_preserved"] is True
    assert archives == ["l1", "l2", "d1"]
    assert [row[0] for row in queries] == [
        "ledger", "doubts", "ledger", "doubts",
    ]


def test_everything_stops_before_local_deletion_on_partial_notion_failure(tmp_path):
    db = tmp_path / "state.db"
    settings = tmp_path / "settings.json"
    backups = tmp_path / "backups"
    settings.write_text('{"USER_TIMEZONE": "Asia/Kolkata"}', encoding="utf-8")
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE evidence (value TEXT)")
        conn.execute("INSERT INTO evidence VALUES ('must survive')")
        conn.commit()

    archived = []
    active = {"ok", "bad"}

    def archive(page_id):
        if page_id == "bad":
            raise RuntimeError("simulated Notion failure")
        archived.append(page_id)
        active.discard(page_id)

    result = reset_service.execute(
        "everything", db_path=db, settings_path=settings, backup_root=backups,
        db_keys=("ledger",),
        query_pages=lambda *_args, **_kwargs: iter(
            {"id": page_id} for page_id in sorted(active)
        ),
        archive_page=archive, sleep_func=lambda _: None, pause_seconds=0,
    )
    assert result["complete"] is False
    assert result["blocked_after_notion_failure"] is True
    assert result["notion"]["pages_archived"] == 1
    assert len(result["notion"]["failures"]) == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT value FROM evidence").fetchone()[0] == "must survive"
    assert settings.exists()
    backup_db = next((backups / "resets").glob("reset-*/state.db"))
    with sqlite3.connect(backup_db) as conn:
        assert conn.execute("SELECT value FROM evidence").fetchone()[0] == "must survive"


def test_sqlite_reset_empties_rows_but_preserves_schema_indexes_and_file(tmp_path):
    db = tmp_path / "state.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT UNIQUE)")
        conn.execute("CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent(id))")
        conn.execute("CREATE INDEX idx_child_parent ON child(parent_id)")
        conn.execute("INSERT INTO parent(value) VALUES ('x')")
        conn.execute("INSERT INTO child VALUES (1, 1)")
        before = dict(conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL AND type IN ('table','index')"
        ).fetchall())
        conn.commit()

    result = reset_service.clear_sqlite_data(db_path=db)
    assert db.exists()
    assert result["rows_deleted"] == 2
    with sqlite3.connect(db) as conn:
        after = dict(conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL AND type IN ('table','index')"
        ).fetchall())
        assert after == before
        assert conn.execute("SELECT COUNT(*) FROM parent").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM child").fetchone()[0] == 0
        assert conn.execute("INSERT INTO parent(value) VALUES ('new')").lastrowid == 1
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def test_sqlite_reset_rolls_back_if_a_trigger_recreates_rows(tmp_path):
    db = tmp_path / "trigger.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE a_early (value TEXT)")
        conn.execute("CREATE TABLE z_late (value TEXT)")
        conn.execute("INSERT INTO a_early VALUES ('original-a')")
        conn.execute("INSERT INTO z_late VALUES ('original-z')")
        conn.execute(
            "CREATE TRIGGER refill AFTER DELETE ON z_late "
            "BEGIN INSERT INTO a_early VALUES ('recreated'); END"
        )
        conn.commit()
    with pytest.raises(reset_service.ResetError, match="trigger recreated"):
        reset_service.clear_sqlite_data(db_path=db)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT value FROM a_early").fetchall() == [("original-a",)]
        assert conn.execute("SELECT value FROM z_late").fetchall() == [("original-z",)]


def test_context_reset_isolated_from_study_records_and_other_chat(tmp_path):
    db = tmp_path / "context.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE chat_context (chat_id TEXT PRIMARY KEY, subject TEXT)")
        conn.execute("CREATE TABLE chat_qa_history (id INTEGER PRIMARY KEY, chat_id INTEGER, answer TEXT)")
        conn.execute("CREATE TABLE ledger (notion_page_id TEXT PRIMARY KEY, task TEXT)")
        conn.execute("CREATE TABLE user_prefs (id INTEGER PRIMARY KEY, chat_id INTEGER, text TEXT)")
        conn.executemany("INSERT INTO chat_context VALUES (?, ?)", (("1", "Physics"), ("2", "Chem")))
        conn.executemany("INSERT INTO chat_qa_history VALUES (?, ?, ?)", ((1, 1, "a"), (2, 2, "b")))
        conn.execute("INSERT INTO ledger VALUES ('l1', 'Rotation PYQs')")
        conn.execute("INSERT INTO user_prefs VALUES (1, 1, 'short answers')")
        conn.commit()

    result = reset_service.clear_context(db_path=db, chat_id=1)
    assert result["rows_deleted"] == 2
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT chat_id FROM chat_context").fetchall() == [("2",)]
        assert conn.execute("SELECT chat_id FROM chat_qa_history").fetchall() == [(2,)]
        assert conn.execute("SELECT task FROM ledger").fetchall() == [("Rotation PYQs",)]
        assert conn.execute("SELECT text FROM user_prefs").fetchall() == [("short answers",)]


def test_successful_everything_backup_then_archives_pages_and_restarts_locally(tmp_path):
    db = tmp_path / "state.db"
    settings = tmp_path / "settings.json"
    backups = tmp_path / "backups"
    settings.write_text('{"LLM_MODEL": "custom"}', encoding="utf-8")
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE onboarding_state (chat_id INTEGER PRIMARY KEY, done INTEGER)")
        conn.execute("INSERT INTO onboarding_state VALUES (7, 1)")
        conn.execute("CREATE TABLE ledger (id INTEGER PRIMARY KEY, task TEXT)")
        conn.execute("INSERT INTO ledger VALUES (1, 'keep only in backup')")
        conn.commit()
    archived = []
    active = {"page-1"}

    def archive(page_id):
        archived.append(page_id)
        active.discard(page_id)
    result = reset_service.execute(
        "everything", db_path=db, settings_path=settings, backup_root=backups,
        db_keys=("ledger",),
        query_pages=lambda *_args, **_kwargs: iter(
            {"id": page_id} for page_id in sorted(active)
        ),
        archive_page=archive,
        sleep_func=lambda _: None, pause_seconds=0,
    )
    assert result["complete"] is True
    assert archived == ["page-1"]
    assert result["notion"]["databases_preserved"] is True
    assert settings.exists() is False
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM onboarding_state").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0
        assert {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")} >= {
            "onboarding_state", "ledger",
        }
    backup_dir = next((backups / "resets").glob("reset-*"))
    assert (backup_dir / "settings.json").exists()
    with sqlite3.connect(backup_dir / "state.db") as conn:
        assert conn.execute("SELECT task FROM ledger").fetchone()[0] == "keep only in backup"


def test_post_archive_verification_blocks_a_concurrently_created_page():
    calls = 0

    def query(_db_key, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return iter(({"id": "original"},))
        return iter(({"id": "created-during-reset"},))

    result = reset_service.archive_all_notion_pages(
        db_keys=("ledger",), query_pages=query, archive_page=lambda _page_id: None,
        sleep_func=lambda _: None, pause_seconds=0,
    )
    assert result["complete"] is False
    assert result["pages_archived"] == 1
    assert result["per_database"]["ledger"]["remaining"] == 1
    assert "created-during-reset" in result["failures"][0]["page_id"]


def test_notion_reset_rejects_nonconfirming_archive_response():
    calls = 0

    def query(_db_key, **_kwargs):
        nonlocal calls
        calls += 1
        return iter(({"id": "p1"},)) if calls == 1 else iter(())

    result = reset_service.archive_all_notion_pages(
        db_keys=("ledger",), query_pages=query,
        archive_page=lambda _page_id: {"archived": False},
        sleep_func=lambda _: None, pause_seconds=0,
    )
    assert result["complete"] is False
    assert result["pages_archived"] == 0
    assert "did not confirm" in result["failures"][0]["error"]


def test_explicit_empty_notion_scope_never_falls_back_to_live_configuration():
    result = reset_service.archive_all_notion_pages(
        db_keys=(), query_pages=lambda *_args, **_kwargs: iter(()),
        archive_page=lambda _page_id: None, sleep_func=lambda _: None,
        pause_seconds=0,
    )
    assert result["complete"] is False
    assert result["configured_databases"] == 0


def test_unauthorized_reset_command_is_silent(monkeypatch):
    replies = []

    class Message:
        text = "/reset"

        async def reply_text(self, text, **kwargs):
            replies.append(text)

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=999), effective_chat=SimpleNamespace(id=999),
        effective_message=Message(),
    )
    monkeypatch.setattr(bot, "telegram_allowed_user_id", lambda: 1)
    asyncio.run(bot.reset_command(update, SimpleNamespace()))
    assert replies == []
