"""Single source of truth: Notion-owned vs SQLite-owned study data.

Notion (human-authored, long-lived, teacher-visible):
  ledger, doubts, revision

SQLite operational (bot-owned planning / analysis):
  goals, work_items, exams, exam_questions, doubt_attempts,
  timetable, daily_plan  → tables op_<key>

SQLite local (never mirrored, not in notion_schema property registry):
  user_prefs, user_jobs, conversation_history, chat_context, …
"""

from __future__ import annotations

# Mirrored from Notion → bare SQLite tables of the same name.
NOTION_OWNED_KEYS: tuple[str, ...] = (
    "ledger",
    "doubts",
    "revision",
)

# Bot-owned domains. Physical tables are op_<key> via operational_store.
SQL_OWNED_KEYS: tuple[str, ...] = (
    "work_items",
    "goals",
    "exams",
    "exam_questions",
    "doubt_attempts",
    "timetable",
    "daily_plan",
)

# Local tables the bot creates for memory / scheduling (not in PROPERTIES_BY_DB).
LOCAL_SQL_TABLES: tuple[str, ...] = (
    "user_prefs",
    "user_jobs",
    "conversation_history",
    "chat_context",
    "commitment_checks",
    "op_execution_links",
    "execution_templates",
    "execution_blocks",
    "block_confirmations",
    "execution_day_types",
    "chapter_classifications",
    "chapter_lifecycle_meta",
)

NOTION_OWNED_LABELS: dict[str, str] = {
    "ledger": "study session logs",
    "doubts": "tracked doubts",
    "revision": "revision schedule",
}

SQL_OWNED_LABELS: dict[str, str] = {
    "work_items": "tasks / backlog",
    "goals": "goals and targets",
    "exams": "exams / mocks",
    "exam_questions": "question-level exam reviews",
    "doubt_attempts": "doubt attempt logs",
    "timetable": "coaching / weekly slots",
    "daily_plan": "today's planned sequence",
}


def is_notion_owned(db_key: str) -> bool:
    return db_key in NOTION_OWNED_KEYS


def is_sql_owned(db_key: str) -> bool:
    return db_key in SQL_OWNED_KEYS


def ownership_prompt_block() -> str:
    """Short block for the agent system prompt."""
    notion = ", ".join(f"`{k}`" for k in NOTION_OWNED_KEYS)
    sql = ", ".join(f"`op_{k}`" for k in SQL_OWNED_KEYS)
    local = ", ".join(
        f"`{t}`" for t in ("user_prefs", "user_jobs", "conversation_history", "chat_context")
    )
    return (
        "## Data ownership (do not dual-write)\n"
        f"- Notion-owned (mirror tables, human-editable in Notion): {notion}.\n"
        "  These are the ONLY tables that mirror Notion; write them via the "
        "log_*/doubt write tools (they sync back to Notion).\n"
        f"- SQLite-owned operational tables (NOTION NEVER MIRRORS THESE): {sql}.\n"
        "  There are NO bare mirror tables for these domains — data lives ONLY in "
        "the op_* tables above. Never query `goals`, `work_items`, `exams`, "
        "`daily_plan`, or `timetable`; query `op_goals`, `op_work_items`, etc.\n"
        "  Write via the named domain tools (create_goal, create_exam, ...).\n"
        "  sql_select reads from all of these; raw writes are not possible.\n"
        f"- Local memory/schedule only: {local}."
    )
