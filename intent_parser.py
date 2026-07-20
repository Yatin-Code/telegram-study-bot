"""
Phase 5 — Intent parser (the NLU layer).

One LLM call per incoming message turns loose natural language into a strict,
validated JSON intent. Downstream phases (session context, logging flow, query
flow) consume the validated `Intent` object — they never re-parse free text.

Design notes
------------
- Provider is pluggable via .env (LLM_PROVIDER): "openai" (any OpenAI-compatible
  gateway incl. eaon/kimi), or "anthropic". Set LLM_BASE_URL for gateways.
- Primary model = LLM_MODEL. If it errors (5xx / timeout / unparseable), we retry
  once with LLM_FALLBACK_MODEL when configured. This is why we picked two clean
  eaon models (deepseek-v4-pro primary, deepseek-v4-flash fallback).
- A custom User-Agent is always sent: eaon sits behind Cloudflare, which 403s
  the default urllib/httpx-less UA with "error code: 1010".
- Some models put chain-of-thought in a sibling "reasoning"/"reasoning_content"
  field and keep `content` clean; others wrap JSON in ```json fences or inline
  their thinking. `_extract_json` handles all of these defensively.
- Output is validated with pydantic. If the model can't map a message to a real
  action, it must set needs_clarification=true rather than guessing.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal, Optional

import httpx

from config import notion_schema
from config import settings

# pydantic v2 is the canonical validator (spec Phase 5.2). We only take the
# pydantic path when v2 is present, because the Intent API below (model_validate
# / model_dump / model_dump_json) is v2-only. On pydantic v1 or when pydantic is
# missing entirely, an equivalent hand-rolled validator provides the same public
# surface so the parser behaves identically everywhere (v2 wheels need a Rust
# build that some environments, e.g. Termux, can't produce).
try:
    import pydantic as _pydantic

    if _pydantic.VERSION.startswith("2."):
        from pydantic import BaseModel, Field, ValidationError
        _HAS_PYDANTIC = True
    else:
        _HAS_PYDANTIC = False
except ImportError:  # pragma: no cover - exercised only where pydantic missing
    _HAS_PYDANTIC = False


logger = logging.getLogger(__name__)

USER_AGENT = "study-bot/1.0"
REQUEST_TIMEOUT = 30.0

ACTIONS = (
    "log_execution",
    "log_doubt",
    "log_revision",
    "query",
    "ask",
    "set_context",
    "unknown",
)
DATABASE_KEYS = ("ledger", "doubts", "revision")

Action = Literal[
    "log_execution",
    "log_doubt",
    "log_revision",
    "query",
    "ask",
    "set_context",
    "unknown",
]

DatabaseKey = Literal["ledger", "doubts", "revision"]


class IntentParseError(RuntimeError):
    """Raised when no model produced a valid Intent (after fallback)."""


# ---------------------------------------------------------------------------
# Strict output contract (spec Phase 5.1)
# ---------------------------------------------------------------------------

if _HAS_PYDANTIC:

    class IntentFilters(BaseModel):
        """Retrieval / scoping filters. All optional; null when not mentioned."""

        date: Optional[str] = None       # "today" | ISO date | null
        subject: Optional[str] = None
        chapter: Optional[str] = None
        block: Optional[str] = None
        exercise: Optional[str] = None
        status: Optional[str] = None
        keyword: Optional[str] = None

    class Intent(BaseModel):
        """Validated intent produced by one LLM call."""

        action: Action
        database: Optional[DatabaseKey] = None
        fields: dict[str, Any] = Field(default_factory=dict)
        filters: IntentFilters = Field(default_factory=IntentFilters)
        needs_clarification: bool = False
        clarification_question: Optional[str] = None

    def _validate_intent(data: dict[str, Any]) -> "Intent":
        data = dict(data)
        if str(data.get("database") or "").strip().lower() in {"null", "none", ""}:
            data["database"] = None
        filters = dict(data.get("filters") or {})
        for key, value in list(filters.items()):
            if isinstance(value, str) and value.strip().lower() in {"null", "none", ""}:
                filters[key] = None
        data["filters"] = filters
        if isinstance(data.get("clarification_question"), str) and data["clarification_question"].strip().lower() in {"null", "none"}:
            data["clarification_question"] = None
        return Intent.model_validate(data)

else:
    # --- Fallback validator (no pydantic available) -------------------------
    # Mirrors the pydantic model's contract and public surface: attribute
    # access, .model_dump() and .model_dump_json(). Raises ValidationError on
    # bad enums so parse_message()'s except-clause behaves identically.

    class ValidationError(ValueError):
        """Stand-in matching pydantic's exception name for the except clause."""

    _FILTER_KEYS = ("date", "subject", "chapter", "block", "exercise", "status", "keyword")

    class IntentFilters:
        def __init__(self, **kw: Any) -> None:
            for k in _FILTER_KEYS:
                setattr(self, k, kw.get(k))

        def model_dump(self) -> dict[str, Any]:
            return {k: getattr(self, k) for k in _FILTER_KEYS}

    class Intent:
        def __init__(
            self,
            action: str,
            database: Optional[str] = None,
            fields: Optional[dict[str, Any]] = None,
            filters: Optional[dict[str, Any]] = None,
            needs_clarification: bool = False,
            clarification_question: Optional[str] = None,
        ) -> None:
            if action not in ACTIONS:
                raise ValidationError(f"invalid action: {action!r}")
            if database is not None and database not in DATABASE_KEYS:
                raise ValidationError(f"invalid database: {database!r}")
            self.action = action
            self.database = database
            self.fields = fields or {}
            filters = filters or {}
            if not isinstance(filters, dict):
                raise ValidationError("filters must be an object")
            self.filters = IntentFilters(**filters)
            self.needs_clarification = bool(needs_clarification)
            self.clarification_question = clarification_question

        def model_dump(self) -> dict[str, Any]:
            return {
                "action": self.action,
                "database": self.database,
                "fields": self.fields,
                "filters": self.filters.model_dump(),
                "needs_clarification": self.needs_clarification,
                "clarification_question": self.clarification_question,
            }

        def model_dump_json(self, indent: int | None = None) -> str:
            return json.dumps(self.model_dump(), indent=indent)

    def _validate_intent(data: dict[str, Any]) -> "Intent":
        if not isinstance(data, dict):
            raise ValidationError("intent must be a JSON object")
        data = dict(data)
        if str(data.get("database") or "").strip().lower() in {"null", "none", ""}:
            data["database"] = None
        filters = dict(data.get("filters") or {})
        for key, value in list(filters.items()):
            if isinstance(value, str) and value.strip().lower() in {"null", "none", ""}:
                filters[key] = None
        data["filters"] = filters
        return Intent(
            action=data.get("action"),
            database=data.get("database"),
            fields=data.get("fields"),
            filters=data.get("filters"),
            needs_clarification=data.get("needs_clarification", False),
            clarification_question=data.get("clarification_question"),
        )


# ---------------------------------------------------------------------------
# System prompt built from the live Notion schema
# ---------------------------------------------------------------------------

def _schema_digest() -> str:
    """Compact, model-friendly description of writable fields + option lists."""
    lines: list[str] = []
    for db_key in ("ledger", "doubts", "revision"):
        writable = notion_schema.writable_properties(db_key)
        options = notion_schema.select_option_lists(db_key)
        field_bits = []
        for human, prop in writable.items():
            if human in {"operation_id", "work_item"}:
                continue
            t = prop["type"]
            if human in options:
                field_bits.append(f"{human}({t}: {'/'.join(options[human])})")
            else:
                field_bits.append(f"{human}({t})")
        lines.append(f"- {db_key}: " + ", ".join(field_bits))
    return "\n".join(lines)


SYSTEM_PROMPT_TEMPLATE = """You are the intent parser for a personal study-logging Telegram bot.
Convert ONE user message into a STRICT JSON object. Output JSON ONLY — no prose,
no markdown, no code fences.

The JSON schema (all keys required):
{{
  "action": "log_execution | log_doubt | log_revision | query | ask | set_context | unknown",
  "database": "ledger | doubts | revision | null",
  "fields": {{ ... human field names -> values ... }},
  "filters": {{
    "date": "today | <ISO date> | null",
    "subject": "Chem | Maths | Physics | null",
    "chapter": "string | null",
    "block": "string | null",
    "exercise": "string | null",
    "status": "string | null",
    "keyword": "string | null"
  }},
  "needs_clarification": true | false,
  "clarification_question": "string | null"
}}

Action meaning:
- log_execution -> database "ledger". A COMPLETED study session with results
                   (questions attempted/correct, time spent, exercise done).
- log_doubt     -> database "doubts". The user is recording a NEW doubt/question
                   they are stuck on. There is an actual doubt statement.
- log_revision  -> database "revision"
- query         -> database is the one being read (ledger|doubts|revision). The
                   user wants to VIEW/LIST/SEE existing records with simple
                   filters (by date, subject, chapter, status, keyword).
- ask           -> the user is asking a QUESTION about their data that needs
                   aggregation, comparison, date ranges, or computed columns
                   (e.g. "what's my cognitive yield this week", "compare my
                   chemistry vs physics accuracy", "which exercise type has the
                   worst yield", "how many doubts in the last month",
                   "total questions attempted this year"). Set database=null,
                   and put the full question text in fields.question. The SQL
                   query loop will figure out which tables to read.
- set_context   -> user is announcing they are ABOUT TO START / are currently
                   studying something (subject/chapter/block/exercise), with no
                   results yet. database is null, no write happens. If the
                   message names an exercise type (MLE, Ex 2A, PYQs, JMYL, ...),
                   put it in filters.exercise.
- unknown       -> cannot be mapped; set needs_clarification=true

Disambiguation (apply in order):
- Words like "starting", "start", "beginning", "about to", "now doing",
  "working on" with NO results reported -> set_context (NOT log_execution),
  even if a block/subject/chapter is named.
- A bare topic + a records-noun ("kinematics doubts", "physics doubts",
  "my doubts", "list doubts", "show revisions") with no new question stated
  -> query (the user wants to VIEW them). Only use log_doubt when the message
  states an actual doubt/question (e.g. "doubt: why does X happen",
  "I don't get why Y").
- A question about computed metrics, trends, comparisons, totals, averages,
  ranges, or anything beyond simple listing/filtering -> ask (NOT query).
  When in doubt between query and ask, prefer ask — the SQL loop handles
  simple listing too, and ask is strictly more capable.

Rules:
- Use ONLY the exact human field names and the exact option values listed below.
  Do not invent new field names, databases, or option values.
- Put extracted values to WRITE into "fields" (for log_* actions). Put values to
  FILTER/SEARCH by into "filters" (for query actions). For ask, put the full
  question text in fields.question and leave filters empty.
- Subject must be one of Chem, Maths, Physics (the live Notion option values)
  or null.
- "today" stays literally "today" in filters.date (don't resolve to a date).
- If required info is genuinely missing or ambiguous (e.g. a chapter that could
  match nothing), set needs_clarification=true and put a single short question in
  clarification_question. Never guess a chapter/subject you weren't given.
- If the user gives no subject/chapter/block but current session context is
  provided below, inherit it silently (do NOT ask).

Writable fields per database (name(type) and allowed options):
{schema_digest}

{context_block}
Respond with the JSON object only."""


def _build_system_prompt(session_context: Optional[dict[str, Any]]) -> str:
    if session_context:
        ctx_lines = ", ".join(f"{k}={v}" for k, v in session_context.items() if v)
        context_block = (
            f"Current session context (inherit when the message omits these): "
            f"{ctx_lines or 'none'}.\n"
        )
    else:
        context_block = "Current session context: none set.\n"
    return SYSTEM_PROMPT_TEMPLATE.format(
        schema_digest=_schema_digest(),
        context_block=context_block,
    )


# ---------------------------------------------------------------------------
# Response text -> JSON dict
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _extract_json(content: str) -> dict[str, Any]:
    """Best-effort extraction of a JSON object from a model's text response."""
    text = content.strip()

    # 1. Whole thing is JSON.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. Fenced ```json ... ``` block.
    m = _FENCE_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # 3. First balanced {...} span.
    start = text.find("{")
    if start != -1:
        depth = 0
        for i in range(start, len(text)):
            c = text[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        break

    raise ValueError(f"No JSON object found in model response: {content[:200]!r}")


# ---------------------------------------------------------------------------
# LLM transport (provider-pluggable)
# ---------------------------------------------------------------------------

def _openai_compatible_call(
    client: httpx.Client,
    model: str,
    system_prompt: str,
    user_message: str,
) -> str:
    """Call an OpenAI-compatible /chat/completions endpoint; return content str."""
    url = settings.llm_base_url().rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "temperature": 0,
    }
    resp = client.post(
        url,
        json=payload,
        headers={
            "Authorization": f"Bearer {settings.llm_api_key()}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def _anthropic_call(
    client: httpx.Client,
    model: str,
    system_prompt: str,
    user_message: str,
) -> str:
    """Call Anthropic's Messages API; return the concatenated text content."""
    url = settings.llm_base_url().rstrip("/") + "/messages"
    # If base URL points at OpenAI default, correct it for Anthropic.
    if "api.openai.com" in url:
        url = "https://api.anthropic.com/v1/messages"
    payload = {
        "model": model,
        "max_tokens": 1024,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_message}],
    }
    resp = client.post(
        url,
        json=payload,
        headers={
            "x-api-key": settings.llm_api_key(),
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    resp.raise_for_status()
    data = resp.json()
    return "".join(
        block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
    )


def _call_model(model: str, system_prompt: str, user_message: str) -> str:
    provider = settings.llm_provider()
    with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
        if provider == "anthropic":
            return _anthropic_call(client, model, system_prompt, user_message)
        # "openai", "kimi", and any OpenAI-compatible gateway.
        return _openai_compatible_call(client, model, system_prompt, user_message)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_message(
    user_message: str,
    session_context: Optional[dict[str, Any]] = None,
) -> Intent:
    """Parse a raw Telegram message into a validated Intent.

    Tries the primary model (LLM_MODEL); on any error (HTTP failure, missing
    JSON, or schema-invalid JSON) retries once with LLM_FALLBACK_MODEL if set.
    Raises IntentParseError if all attempts fail.
    """
    system_prompt = _build_system_prompt(session_context)

    models = [settings.llm_model()]
    for fallback in settings.llm_fallback_models():
        if fallback not in models:
            models.append(fallback)

    last_error: Optional[Exception] = None
    for model in models:
        try:
            raw = _call_model(model, system_prompt, user_message)
            data = _extract_json(raw)
            intent = _validate_intent(data)
            logger.info("intent parsed via %s: action=%s db=%s", model, intent.action, intent.database)
            return intent
        except (httpx.HTTPError, ValueError, ValidationError, KeyError) as e:
            last_error = e
            logger.warning("intent parse failed on model %s: %s", model, e)
            continue

    raise IntentParseError(
        f"All models failed to parse message {user_message!r}: {last_error}"
    )
