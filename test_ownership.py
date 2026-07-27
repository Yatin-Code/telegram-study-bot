"""Ownership registry: Notion vs SQLite boundary."""

from __future__ import annotations

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


def test_ownership_prompt_mentions_both_sides():
    block = ownership_prompt_block()
    assert "ledger" in block
    assert "op_daily_plan" in block or "op_goals" in block
