"""
Central settings + env loading.

Loads .env once at import time and exposes typed accessors. Notion DB IDs
are loaded from env but ALSO mirrored back into notion_schema.DATABASES so
the schema module (which other code reads from) stays the single source of
truth for database identity.
"""

from __future__ import annotations

import os
from functools import lru_cache

from dotenv import load_dotenv

# Load .env from project root (one level up from this file's package).
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))


class ConfigError(RuntimeError):
    """Raised when a required env var is missing or malformed."""


def _require(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise ConfigError(
            f"Missing required env var {name}. "
            f"Copy .env.example to .env and fill it in."
        )
    return val


# --- Notion -----------------------------------------------------------------

def notion_token() -> str:
    return _require("NOTION_TOKEN")


# Mapping: DB key (used in code) → env var name that holds its resolved ID.
_DB_ID_ENV_VARS = {
    "ledger": "NOTION_LEDGER_DB_ID",
    "doubts": "NOTION_DOUBTS_DB_ID",
    "revision": "NOTION_REVISION_DB_ID",
    "work_items": "NOTION_WORK_ITEMS_DB_ID",
    "goals": "NOTION_GOALS_DB_ID",
    "exams": "NOTION_EXAMS_DB_ID",
    "exam_questions": "NOTION_EXAM_QUESTIONS_DB_ID",
    "doubt_attempts": "NOTION_DOUBT_ATTEMPTS_DB_ID",
    "timetable": "NOTION_TIMETABLE_DB_ID",
    "daily_plan": "NOTION_DAILY_PLAN_DB_ID",
}


def notion_db_id(db_key: str) -> str:
    env_var = _DB_ID_ENV_VARS[db_key]
    val = os.environ.get(env_var, "").strip()
    if not val:
        raise ConfigError(
            f"DB ID for '{db_key}' not resolved yet. "
            f"Run: python -m config.resolve_db_ids"
        )
    return val


def has_all_db_ids() -> bool:
    return all(os.environ.get(v, "").strip() for v in _DB_ID_ENV_VARS.values())


def configured_db_keys() -> list[str]:
    """Return database keys whose IDs are currently configured."""
    return [k for k, env_var in _DB_ID_ENV_VARS.items() if os.environ.get(env_var, "").strip()]


# --- Telegram (Phase 4) -----------------------------------------------------

def telegram_bot_token() -> str:
    return _require("TELEGRAM_BOT_TOKEN")


def telegram_allowed_user_id() -> int:
    raw = _require("TELEGRAM_ALLOWED_USER_ID")
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(
            f"TELEGRAM_ALLOWED_USER_ID must be an integer, got: {raw!r}"
        )


# --- LLM (Phase 5) ----------------------------------------------------------

def llm_provider() -> str:
    return _require("LLM_PROVIDER").lower()


def llm_api_key() -> str:
    return _require("LLM_API_KEY")


def llm_model() -> str:
    return _require("LLM_MODEL")


def llm_base_url() -> str:
    """OpenAI-compatible base URL (e.g. https://api.eaon.dev/v1).

    Required for provider=openai when pointing at a non-OpenAI gateway.
    Falls back to the official OpenAI endpoint when unset.
    """
    return os.environ.get("LLM_BASE_URL", "").strip() or "https://api.openai.com/v1"


def llm_fallback_models() -> list[str]:
    """Ordered secondary models tried when the primary model errors out.

    LLM_FALLBACK_MODEL is a comma-separated list; each is tried in order until
    one succeeds. Empty means "no fallback" — the parser uses only LLM_MODEL.
    """
    raw = os.environ.get("LLM_FALLBACK_MODEL", "")
    return [m.strip() for m in raw.split(",") if m.strip()]


# --- Misc -------------------------------------------------------------------

def user_timezone() -> str:
    return os.environ.get("USER_TIMEZONE", "UTC").strip() or "UTC"


def planning_reminder_time() -> str:
    return os.environ.get("PLANNING_REMINDER_TIME", "01:00").strip() or "01:00"


def timetable_reminder_weekday() -> int:
    raw = os.environ.get("TIMETABLE_REMINDER_WEEKDAY", "6").strip()
    try:
        day = int(raw)
    except ValueError as exc:
        raise ConfigError("TIMETABLE_REMINDER_WEEKDAY must be 0..6") from exc
    if not 0 <= day <= 6:
        raise ConfigError("TIMETABLE_REMINDER_WEEKDAY must be 0..6")
    return day


def weekly_report_time() -> str:
    return os.environ.get("WEEKLY_REPORT_TIME", "20:00").strip() or "20:00"


def daily_cy_baseline() -> int:
    return int(os.environ.get("DAILY_CY_BASELINE", "240"))


def daily_cy_ceiling() -> int:
    return int(os.environ.get("DAILY_CY_CEILING", "300"))


# --- Schema patching --------------------------------------------------------
# Mirror resolved DB IDs into notion_schema.DATABASES so that module stays the
# single source of truth for code that consumes it. Called once on import of
# config.settings; safe to call before resolution (IDs just stay None).

@lru_cache(maxsize=1)
def _patch_schema_with_env_ids() -> None:
    from . import notion_schema
    for db_key, env_var in _DB_ID_ENV_VARS.items():
        val = os.environ.get(env_var, "").strip()
        if val:
            notion_schema.DATABASES[db_key]["database_id"] = val


_patch_schema_with_env_ids()
