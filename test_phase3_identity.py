from __future__ import annotations

import bot
import bot_identity
import domain_parser
import intent_parser
import sql_query_flow
import sync


def _assert_identity(prompt: str) -> None:
    assert bot_identity.IDENTITY_MARKER in prompt
    assert "AIR 1" in prompt
    assert "never a rank prediction or guarantee" in prompt
    assert "/setup" in prompt
    assert "/today" in prompt
    assert "Never claim that data was saved" in prompt


def test_command_catalog_is_the_single_telegram_source():
    specs = [(item.command, item.description) for item in bot_identity.COMMANDS]
    telegram = [(item.command, item.description) for item in bot.BOT_COMMANDS]
    assert telegram == specs
    assert len({command for command, _description in specs}) == len(specs)
    assert {"sync", "bugs", "readiness", "complete_exam_analysis"} <= {
        command for command, _description in specs
    }


def test_identity_is_injected_into_intent_domain_setup_and_sql_prompts(tmp_path):
    db = tmp_path / "identity.db"
    with sync.connect(db) as conn:
        sync.init_db(conn)

    _assert_identity(intent_parser._build_system_prompt(None))
    _assert_identity(
        domain_parser._build_domain_prompt(
            "setup assistant plan", '{"actions": [], "needs_clarification": false}'
        )
    )
    _assert_identity(sql_query_flow._build_system_prompt(db_path=db))


def test_general_assistant_uses_the_same_identity(monkeypatch):
    monkeypatch.setattr(
        bot.session_context,
        "context_for_parser",
        lambda _chat_id: {"subject": "Physics", "chapter": "Rotation"},
    )
    prompt = bot._assistant_system_prompt(42)
    _assert_identity(prompt)
    assert "subject=Physics" in prompt
    assert "chapter=Rotation" in prompt
