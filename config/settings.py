"""
Central settings + env loading.

Loads .env once at import time and exposes typed accessors. Notion DB IDs
are loaded from env but ALSO mirrored back into notion_schema.DATABASES so
the schema module (which other code reads from) stays the single source of
truth for database identity.

Tunable (non-secret) settings can be OVERRIDDEN at runtime by settings.json
in the project root — written by the bot's /settings editor. Resolution
order: settings.json → .env → default. Secrets (tokens, API keys, IDs)
never consult settings.json.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from functools import lru_cache

from dotenv import load_dotenv

# Load .env from project root (one level up from this file's package).
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))

_OVERRIDES_PATH = os.path.join(_PROJECT_ROOT, "settings.json")


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


# --- settings.json override layer -------------------------------------------
# Read on every access (file is tiny) so edits from the /settings editor apply
# instantly without a restart wherever the accessor is called per-use.

def _load_overrides() -> dict[str, str]:
    try:
        with open(_OVERRIDES_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def _write_overrides(data: dict[str, str]) -> None:
    fd, tmp = tempfile.mkstemp(dir=_PROJECT_ROOT, prefix=".settings-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, _OVERRIDES_PATH)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def get_override(name: str) -> str | None:
    val = _load_overrides().get(name)
    return val if val is not None and str(val).strip() else None


def set_override(name: str, value: str) -> None:
    data = _load_overrides()
    data[name] = str(value)
    _write_overrides(data)


def clear_override(name: str) -> None:
    data = _load_overrides()
    if name in data:
        del data[name]
        _write_overrides(data)


def _get(name: str, default: str) -> str:
    val = get_override(name)
    if val is None:
        val = os.environ.get(name, "").strip()
    return str(val).strip() or default


def _get_int(name: str, default: int, *, lo: int | None = None, hi: int | None = None) -> int:
    try:
        val = int(_get(name, str(default)))
    except ValueError:
        return default
    if lo is not None:
        val = max(lo, val)
    if hi is not None:
        val = min(hi, val)
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
    return get_override("LLM_MODEL") or _require("LLM_MODEL")


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
    raw = _get("LLM_FALLBACK_MODEL", "")
    return [m.strip() for m in raw.split(",") if m.strip()]


# --- Misc -------------------------------------------------------------------

def user_timezone() -> str:
    return _get("USER_TIMEZONE", "UTC")


def planning_reminder_time() -> str:
    return _get("PLANNING_REMINDER_TIME", "01:00")


def timetable_reminder_weekday() -> int:
    raw = _get("TIMETABLE_REMINDER_WEEKDAY", "6")
    try:
        day = int(raw)
    except ValueError as exc:
        raise ConfigError("TIMETABLE_REMINDER_WEEKDAY must be 0..6") from exc
    if not 0 <= day <= 6:
        raise ConfigError("TIMETABLE_REMINDER_WEEKDAY must be 0..6")
    return day


def weekly_report_time() -> str:
    return _get("WEEKLY_REPORT_TIME", "20:00")


def daily_cy_baseline() -> int:
    return _get_int("DAILY_CY_BASELINE", 240, lo=1)


def daily_cy_ceiling() -> int:
    return _get_int("DAILY_CY_CEILING", 300, lo=1)


def sync_interval_seconds() -> int:
    """Notion→SQLite mirror refresh cadence. Applies after restart."""
    return _get_int("SYNC_INTERVAL_SECONDS", 240, lo=60, hi=3600)


def draft_ttl_minutes() -> int:
    """How long a Confirm/Edit/Cancel preview stays alive."""
    return _get_int("DRAFT_TTL_MINUTES", 15, lo=1, hi=120)


# --- LLM SQL query loop -----------------------------------------------------

def query_max_iterations() -> int:
    """Safety ceiling on SQL drilling steps per question.

    The loop stops as soon as the model produces an answer; this is only the
    backstop that prevents a confused model from querying forever. Generous by
    default so complex questions can drill deeply, tunable via .env.
    """
    return _get_int("QUERY_MAX_ITERATIONS", 12, lo=1, hi=50)


def query_history_pairs() -> int:
    """How many prior (question, answer) turns to feed back for follow-ups."""
    return _get_int("QUERY_HISTORY_PAIRS", 5, lo=0, hi=10)


def qa_history_ttl_minutes() -> int:
    """How long conversation-window turns stay usable for follow-ups."""
    return _get_int("QA_HISTORY_TTL_MINUTES", 60, lo=1, hi=1440)


def memory_context_budget() -> int:
    """Chars of always-in-context memory before /memory warns to prune."""
    return _get_int("MEMORY_CONTEXT_BUDGET_CHARS", 4000, lo=500, hi=20000)


# --- Commitments / adherence ------------------------------------------------

def commitment_check_time() -> str:
    """Local HH:MM for the nightly ledger verification of daily commitments."""
    return _get("COMMITMENT_CHECK_TIME", "23:50")


def commitment_nudge_time() -> str:
    """Local HH:MM for the morning adherence nudge (re-checks yesterday first)."""
    return _get("COMMITMENT_NUDGE_TIME", "07:30")


def commitment_warn_after() -> str:
    """Local HH:MM after which log previews warn about still-unmet commitments."""
    return _get("COMMITMENT_WARN_AFTER", "19:00")


def max_daily_committed_minutes() -> int:
    """Total daily Duration commitments above this is flagged as unrealistic."""
    return _get_int("MAX_DAILY_COMMITTED_MINUTES", 600, lo=60, hi=1440)


def accuracy_drop_pts() -> int:
    """Week-over-week accuracy fall (percentage points) worth flagging."""
    return _get_int("ACCURACY_DROP_PTS", 10, lo=1, hi=50)


def low_adherence_pct() -> int:
    """7-day commitment adherence below this percentage is flagged."""
    return _get_int("LOW_ADHERENCE_PCT", 60, lo=1, hi=100)


# --- /settings editor registry ----------------------------------------------
# Single source of truth for every knob editable from Telegram. Secrets and
# provider plumbing are deliberately absent — those stay .env-only.

CAT_REMINDERS = "⏰ Reminders & jobs"
CAT_TARGETS = "🎯 Study targets"
CAT_QUERY = "🧠 Query & memory"
CAT_ADVISOR = "📣 Advisor"
CAT_LLM = "🤖 LLM"
CAT_GENERAL = "🌍 General"

SETTINGS_CATEGORIES = [CAT_REMINDERS, CAT_TARGETS, CAT_QUERY, CAT_ADVISOR, CAT_LLM, CAT_GENERAL]

SETTINGS_REGISTRY: list[dict] = [
    {"key": "PLANNING_REMINDER_TIME", "label": "Planning reminder", "category": CAT_REMINDERS,
     "type": "time", "default": "01:00", "restart": True},
    {"key": "WEEKLY_REPORT_TIME", "label": "Weekly report time", "category": CAT_REMINDERS,
     "type": "time", "default": "20:00", "restart": True},
    {"key": "TIMETABLE_REMINDER_WEEKDAY", "label": "Timetable check day", "category": CAT_REMINDERS,
     "type": "weekday", "default": "6", "restart": False},
    {"key": "COMMITMENT_CHECK_TIME", "label": "Nightly commitment check", "category": CAT_REMINDERS,
     "type": "time", "default": "23:50", "restart": True},
    {"key": "COMMITMENT_NUDGE_TIME", "label": "Morning nudge time", "category": CAT_REMINDERS,
     "type": "time", "default": "07:30", "restart": True},
    {"key": "SYNC_INTERVAL_SECONDS", "label": "Notion sync interval (s)", "category": CAT_REMINDERS,
     "type": "int", "default": "240", "min": 60, "max": 3600, "restart": True},
    {"key": "DAILY_CY_BASELINE", "label": "Daily CY baseline", "category": CAT_TARGETS,
     "type": "int", "default": "240", "min": 1, "max": 10000, "restart": False},
    {"key": "DAILY_CY_CEILING", "label": "Daily CY ceiling", "category": CAT_TARGETS,
     "type": "int", "default": "300", "min": 1, "max": 10000, "restart": False},
    {"key": "MAX_DAILY_COMMITTED_MINUTES", "label": "Max committed min/day", "category": CAT_TARGETS,
     "type": "int", "default": "600", "min": 60, "max": 1440, "restart": False},
    {"key": "QUERY_MAX_ITERATIONS", "label": "Query loop ceiling", "category": CAT_QUERY,
     "type": "int", "default": "12", "min": 1, "max": 50, "restart": False},
    {"key": "QUERY_HISTORY_PAIRS", "label": "Follow-up window (pairs)", "category": CAT_QUERY,
     "type": "int", "default": "5", "min": 0, "max": 10, "restart": False},
    {"key": "QA_HISTORY_TTL_MINUTES", "label": "Follow-up window TTL (min)", "category": CAT_QUERY,
     "type": "int", "default": "60", "min": 1, "max": 1440, "restart": False},
    {"key": "MEMORY_CONTEXT_BUDGET_CHARS", "label": "Memory budget (chars)", "category": CAT_QUERY,
     "type": "int", "default": "4000", "min": 500, "max": 20000, "restart": False},
    {"key": "COMMITMENT_WARN_AFTER", "label": "Warn about unmet after", "category": CAT_ADVISOR,
     "type": "time", "default": "19:00", "restart": False},
    {"key": "ACCURACY_DROP_PTS", "label": "Accuracy drop alert (pts)", "category": CAT_ADVISOR,
     "type": "int", "default": "10", "min": 1, "max": 50, "restart": False},
    {"key": "LOW_ADHERENCE_PCT", "label": "Low adherence alert (%)", "category": CAT_ADVISOR,
     "type": "int", "default": "60", "min": 1, "max": 100, "restart": False},
    {"key": "LLM_MODEL", "label": "Model", "category": CAT_LLM,
     "type": "str", "default": "", "restart": False},
    {"key": "LLM_FALLBACK_MODEL", "label": "Fallback models (comma)", "category": CAT_LLM,
     "type": "str", "default": "", "restart": False},
    {"key": "USER_TIMEZONE", "label": "Timezone", "category": CAT_GENERAL,
     "type": "tz", "default": "UTC", "restart": True},
    {"key": "DRAFT_TTL_MINUTES", "label": "Draft preview TTL (min)", "category": CAT_GENERAL,
     "type": "int", "default": "15", "min": 1, "max": 120, "restart": False},
]

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")

WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def validate_hhmm(raw: str) -> tuple[bool, str]:
    """Validate a 24h HH:MM string. Returns (True, canonical) or (False, error)."""
    match = _TIME_RE.match(str(raw).strip())
    if not match:
        return False, "use HH:MM (24h), e.g. 21:30"
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return False, "hours 0-23, minutes 0-59"
    return True, f"{hour:02d}:{minute:02d}"


def setting_entry(key: str) -> dict | None:
    return next((e for e in SETTINGS_REGISTRY if e["key"] == key), None)


def current_setting_value(key: str) -> str:
    """Effective value as a display string (override → env → default)."""
    entry = setting_entry(key)
    default = str(entry.get("default", "")) if entry else ""
    val = _get(key, default)
    if entry and entry["type"] == "weekday":
        try:
            return WEEKDAY_NAMES[int(val)]
        except (ValueError, IndexError):
            return val
    return val or "(unset)"


def validate_setting(key: str, raw: str) -> tuple[bool, str]:
    """Return (True, canonical_value) or (False, error_message)."""
    entry = setting_entry(key)
    if entry is None:
        return False, "unknown setting"
    raw = str(raw).strip()
    kind = entry["type"]
    if kind == "time":
        return validate_hhmm(raw)
    if kind == "weekday":
        try:
            day = int(raw)
        except ValueError:
            return False, "send a number 0=Mon … 6=Sun"
        if not 0 <= day <= 6:
            return False, "send a number 0=Mon … 6=Sun"
        return True, str(day)
    if kind == "int":
        try:
            val = int(raw)
        except ValueError:
            return False, "must be a whole number"
        lo, hi = entry.get("min"), entry.get("max")
        if lo is not None and val < lo:
            return False, f"minimum is {lo}"
        if hi is not None and val > hi:
            return False, f"maximum is {hi}"
        return True, str(val)
    if kind == "tz":
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(raw)
        except Exception:
            return False, "not a valid timezone (e.g. Asia/Kolkata)"
        return True, raw
    if not raw:
        return False, "value cannot be empty"
    return True, raw


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
