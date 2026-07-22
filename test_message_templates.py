from __future__ import annotations

import message_templates


def test_doubt_dashboard_shows_all_workflow_groups():
    text = message_templates.doubt_dashboard([
        {"core_concept": "Rotation", "subject": "Physics", "valid_attempts": 2,
         "readiness": "ready", "metadata_incomplete": False},
        {"core_concept": "Mole sign", "subject": "Chem", "valid_attempts": 1,
         "readiness": "attempting", "metadata_incomplete": False},
        {"core_concept": "Limit", "subject": "Maths", "valid_attempts": 0,
         "readiness": "new", "metadata_incomplete": False},
        {"core_concept": "Legacy row", "valid_attempts": 0,
         "readiness": "new", "metadata_incomplete": True},
    ])
    assert "4 open · 1 teacher-ready" in text
    assert "Teacher-ready" in text and "Attempting" in text and "New" in text
    assert "Data cleanup" in text and "Legacy row" in text
    assert "Next\n→" in text


def test_action_card_omits_empty_sections_cleanly():
    text = message_templates.action_card(
        "🟢", "Check", conclusion="Everything is current.",
        sections=(("Empty", []),), action="Continue the plan.",
    )
    assert "Empty" not in text
    assert text.endswith("→ Continue the plan.")


def test_insert_section_preserves_next_as_final_instruction():
    card = message_templates.action_card(
        "🔴", "Check", conclusion="One miss.", action="Recover today.",
        footer="Evidence date: yesterday",
    )
    result = message_templates.insert_section(card, "Risks", ["4 revisions overdue"])
    assert result.index("Risks") < result.index("Next") < result.index("Evidence date")
