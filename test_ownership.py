"""Ownership registry: Notion vs SQLite boundary."""

from __future__ import annotations

import sqlite3

from config.ownership import (
    NOTION_OWNED_KEYS,
    SQL_OWNED_KEYS,
    is_notion_owned,
    is_sql_owned,
    ownership_prompt_block,
)
import operational_store
import sync


def test_notion_owned_is_exactly_three():
    assert NOTION_OWNED_KEYS == ("ledger", "doubts", "revision")
    assert "daily_plan" not in NOTION_OWNED_KEYS


def test_daily_plan_is_sql_owned():
    assert is_sql_owned("daily_plan")
    assert not is_notion_owned("daily_plan")
    assert operational_store.table_for("daily_plan") == "op_daily_plan"
    assert "daily_plan" in operational_store.OPERATIONAL_KEYS


def test_sync_only_mirrors_notion_owned():
    assert sync.NOTION_SOURCE_KEYS == NOTION_OWNED_KEYS
    assert "daily_plan" not in sync.NOTION_SOURCE_KEYS
    # DB_TABLES must not contain bare SQLite-owned mirror entries anymore —
    # those confused the agent into querying "goals" instead of "op_goals".
    assert set(sync.DB_TABLES) == set(NOTION_OWNED_KEYS)
    for key in SQL_OWNED_KEYS:
        assert key not in sync.DB_TABLES


def test_fresh_db_has_no_bare_sql_mirrors(tmp_path):
    path = tmp_path / "fresh.db"
    with sync.connect(path) as conn:
        sync.init_db(conn)
        operational_store.init_db(conn)
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    for key in SQL_OWNED_KEYS:
        assert key not in names, f"bare mirror {key!r} must not exist"
        assert f"op_{key}" in names


def test_init_db_drops_empty_legacy_bare_mirror(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE goals (notion_page_id TEXT PRIMARY KEY)")
        conn.commit()
    with sync.connect(path) as conn:
        sync.init_db(conn)
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "goals" not in names


def test_init_db_keeps_nonempty_legacy_bare_mirror_for_migration(tmp_path):
    path = tmp_path / "legacy_nonempty.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE goals (notion_page_id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO goals (notion_page_id) VALUES ('g1')")
        conn.commit()
    with sync.connect(path) as conn:
        sync.init_db(conn)
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    # Rows still present → leave for operational_store migration into op_goals.
    assert "goals" in names
    operational_store.init_db(conn)
    row = conn.execute("SELECT notion_page_id FROM op_goals").fetchone()
    assert row[0] == "g1"
    # Once every row is migrated, the bare table is dropped for good.
    sync.init_db(conn)
    names = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "goals" not in names
    assert conn.execute("SELECT COUNT(*) FROM op_goals").fetchone()[0] == 1


def test_ownership_prompt_mentions_both_sides():
    block = ownership_prompt_block()
    assert "ledger" in block
    assert "op_daily_plan" in block or "op_goals" in block
    # Explicit guard rails against the old bare-table confusion.
    assert "op_goals" in block
    assert "Never query" in block
