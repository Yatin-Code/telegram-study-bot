"""
Thin Notion REST wrapper for the 3 study databases.

Public surface:
    create_page(db_key, properties)   → new Notion page object
    query_database(db_key, filter, sorts, page_size, start_cursor) → list[page]
    update_page(page_id, properties)  → updated Notion page object
    get_page(page_id)                 → Notion page object
    parse_page(page_obj)              → flat {human_name: python_value}
    to_notion_properties(db_key, props_dict) → Notion-formatted properties dict

properties dicts passed IN to create_page / update_page use our human_name keys
(defined in config.notion_schema). The wrapper translates to/from Notion's
nested format. Read-only fields (formula/rollup) are silently dropped on writes
— Notion would reject them anyway, but we don't even try.

Rate limiting: Notion caps at ~3 req/sec per integration. This wrapper is
synchronous and does NOT throttle — callers (sync job, bot write path) are
expected to be sparse. If we hit limits, Notion returns 429 and we raise;
the bot's queue/retry layer (Phase 9 edge case) handles that.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Iterable

import httpx

from config import notion_schema
from config.settings import notion_token, notion_db_id


NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class NotionAPIError(RuntimeError):
    """Raised for any non-2xx response from Notion. Includes status + body."""

    def __init__(self, status: int, body: str, *, method: str, url: str):
        super().__init__(f"Notion API {method} {url} → {status}: {body[:300]}")
        self.status = status
        self.body = body
        self.method = method
        self.url = url


class NotionRateLimitError(NotionAPIError):
    """429 from Notion. Caller should back off and retry."""


class SchemaError(ValueError):
    """Raised when a property value can't be coerced per our known schema."""


# ---------------------------------------------------------------------------
# HTTP core
# ---------------------------------------------------------------------------

def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {notion_token()}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _request(
    method: str,
    url: str,
    *,
    json_body: dict | None = None,
    params: dict | None = None,
    timeout: float = 30.0,
) -> dict:
    with httpx.Client(timeout=timeout) as client:
        resp = client.request(method, url, headers=_headers(), json=json_body, params=params)
    status = resp.status_code
    body = resp.text
    if status == 429:
        raise NotionRateLimitError(status, body, method=method, url=url)
    if status < 200 or status >= 300:
        raise NotionAPIError(status, body, method=method, url=url)
    if not body:
        return {}
    return resp.json()


# ---------------------------------------------------------------------------
# Property value encoding (our format → Notion format)
# ---------------------------------------------------------------------------

def _encode_value(prop_def: dict[str, Any], value: Any) -> dict:
    """Translate a Python value into Notion's nested property payload."""
    t = prop_def["type"]

    # Read-only fields are never writable. Caller shouldn't have included them,
    # but if they did, raise so we surface the bug rather than silently drop.
    if prop_def.get("read_only"):
        raise SchemaError(
            f"Cannot write to read-only property "
            f"{prop_def['notion_name']!r} (type={t})"
        )

    if value is None:
        # Notion accepts {} to clear a property on update; on create it's
        # safer to just skip the key entirely, which the caller does.
        return {}

    if t == "title":
        # Accept str or list of rich-text segments.
        if isinstance(value, str):
            return {"title": [{"text": {"content": value}}]}
        return {"title": value}

    if t == "rich_text":
        if isinstance(value, str):
            return {"rich_text": [{"text": {"content": value}}]}
        return {"rich_text": value}

    if t == "number":
        if isinstance(value, bool):  # bool is int subclass — guard
            raise SchemaError(f"number field got bool: {value!r}")
        if not isinstance(value, (int, float)):
            raise SchemaError(f"number field needs int/float, got {type(value).__name__}: {value!r}")
        return {"number": value}

    if t == "select":
        if value not in prop_def["options"]:
            raise SchemaError(
                f"select {prop_def['notion_name']!r} got {value!r}; "
                f"allowed: {prop_def['options']}"
            )
        return {"select": {"name": value}}

    if t == "status":
        if value not in prop_def["options"]:
            raise SchemaError(
                f"status {prop_def['notion_name']!r} got {value!r}; "
                f"allowed: {prop_def['options']}"
            )
        return {"status": {"name": value}}

    if t == "date":
        return {"date": _encode_date_value(value)}

    if t == "checkbox":
        return {"checkbox": bool(value)}

    if t == "relation":
        # value = page_id (str) or list of page_ids. Notion accepts both shapes;
        # we normalize to the list form for safety on multi-relation fields.
        if isinstance(value, str):
            ids = [value]
        elif isinstance(value, (list, tuple)):
            ids = list(value)
        elif isinstance(value, dict):
            # Already in Notion format, pass through.
            return {"relation": [value] if not value.get("relation") else value["relation"]}
        else:
            raise SchemaError(
                f"relation {prop_def['notion_name']!r} needs str|list[str] of page_ids, "
                f"got {type(value).__name__}"
            )
        return {"relation": [{"id": pid} for pid in ids]}

    raise SchemaError(f"Unsupported property type for writing: {t!r}")


def _encode_date_value(value: Any) -> dict:
    """Accept date, datetime, or ISO 8601 str. Returns Notion date payload."""
    if isinstance(value, _dt.datetime):
        # If naive, treat as UTC. Notion wants ISO with timezone offset.
        if value.tzinfo is None:
            value = value.replace(tzinfo=_dt.timezone.utc)
        return {"start": value.isoformat(), "end": None}
    if isinstance(value, _dt.date):
        # Pure date — Notion's "date" type without time.
        return {"start": value.isoformat()}
    if isinstance(value, str):
        # Validate by parsing.
        try:
            _dt.date.fromisoformat(value)
        except ValueError:
            try:
                _dt.datetime.fromisoformat(value)
            except ValueError as e:
                raise SchemaError(f"date string not ISO 8601: {value!r}") from e
        return {"start": value}
    raise SchemaError(f"date field needs str|date|datetime, got {type(value).__name__}")


def to_notion_properties(db_key: str, props: dict[str, Any]) -> dict[str, dict]:
    """Translate {human_name: value} → Notion's {notion_name: {type: value}}."""
    schema = notion_schema.PROPERTIES_BY_DB[db_key]
    out: dict[str, dict] = {}
    for human_name, value in props.items():
        if human_name not in schema:
            raise SchemaError(f"Unknown property {human_name!r} on DB {db_key!r}")
        prop_def = schema[human_name]
        if prop_def.get("read_only"):
            # Silent drop on write paths — caller may include context dicts that
            # contain read-only fields (e.g. parsed from user text). Don't raise
            # here; raising would force every caller to filter. _encode_value
            # raises if called directly, but the write path skips None-ish.
            continue
        if value is None:
            continue
        out[prop_def["notion_name"]] = _encode_value(prop_def, value)
    return out


# ---------------------------------------------------------------------------
# Property value decoding (Notion format → our flat format)
# ---------------------------------------------------------------------------

def _decode_value(prop_def: dict[str, Any], raw: dict) -> Any:
    t = prop_def["type"]

    if t == "title":
        arr = raw.get("title", [])
        return "".join(seg.get("plain_text", "") for seg in arr)

    if t == "rich_text":
        arr = raw.get("rich_text", [])
        return "".join(seg.get("plain_text", "") for seg in arr) or None

    if t == "number":
        return raw.get("number")

    if t == "select":
        sel = raw.get("select")
        return sel["name"] if sel else None

    if t == "status":
        st = raw.get("status")
        return st["name"] if st else None

    if t == "date":
        d = raw.get("date")
        if not d:
            return None
        # Notion returns {"start": "...", "end": "..."}; for our use case we
        # usually want the start. If end exists, return [start, end].
        start = d.get("start")
        end = d.get("end")
        if end:
            return [start, end]
        return start

    if t == "checkbox":
        return bool(raw.get("checkbox", False))

    if t == "relation":
        rels = raw.get("relation", [])
        if not rels:
            return None
        ids = [r["id"] for r in rels if "id" in r]
        return ids[0] if len(ids) == 1 else ids

    if t in ("formula", "rollup"):
        # Both types nest under their key with a .type field describing the
        # underlying value shape. We extract the scalar best we can.
        payload = raw.get(t, {})
        return _decode_formula_or_rollup(payload)

    if t == "unique_id":
        # Notion returns {"number": N, "prefix": "TASK"} — combine for display.
        u = raw.get("unique_id", {})
        if not u:
            return None
        prefix = u.get("prefix")
        num = u.get("number")
        if prefix and num is not None:
            return f"{prefix}-{num}"
        return num

    if t == "people":
        return [p.get("id") for p in raw.get("people", [])]

    # Unknown type — return the raw payload so callers can introspect.
    return raw.get(t)


def _decode_formula_or_rollup(payload: dict) -> Any:
    """Notion formula/rollup values have a 'type' field describing the result."""
    inner_type = payload.get("type")
    if inner_type == "string":
        return payload.get("string")
    if inner_type == "number":
        return payload.get("number")
    if inner_type == "boolean":
        return payload.get("boolean")
    if inner_type == "date":
        d = payload.get("date")
        return d.get("start") if d else None
    if inner_type == "array":
        # Rollups can return arrays (e.g. show_original → list of relation ids
        # or list of select values). Surface as list of decoded items.
        items = payload.get("array", []) or []
        decoded = []
        for item in items:
            if not isinstance(item, dict):
                decoded.append(item)
                continue
            # Each item is itself a typed property value.
            it = item.get("type")
            if it == "select" and item.get("select"):
                decoded.append(item["select"]["name"])
            elif it == "relation":
                decoded.append(item["relation"][0]["id"] if item.get("relation") else None)
            elif it == "string":
                decoded.append(item.get("string"))
            elif it == "number":
                decoded.append(item.get("number"))
            else:
                decoded.append(item)
        return decoded or None
    # Fall through: return payload as-is.
    return payload or None


def parse_page(page_obj: dict) -> dict[str, Any]:
    """Convert a Notion page object → flat {human_name: python_value}.

    Looks up the page's parent database_id to find the right schema.
    Returns a dict with these reserved keys (prefixed with _) plus all
    decoded properties:
        _page_id, _url, _created_time, _last_edited_time, _archived
    """
    parent = page_obj.get("parent", {})
    db_id = parent.get("database_id")
    db_key = _db_key_for_id(db_id) if db_id else None

    out: dict[str, Any] = {
        "_page_id": page_obj.get("id"),
        "_url": page_obj.get("url"),
        "_created_time": page_obj.get("created_time"),
        "_last_edited_time": page_obj.get("last_edited_time"),
        "_archived": page_obj.get("archived", False),
        "_db_key": db_key,
    }

    if db_key is None:
        return out  # Page not from one of our known DBs.

    schema = notion_schema.PROPERTIES_BY_DB[db_key]
    raw_props = page_obj.get("properties", {})

    # Build reverse map notion_name → human_name once.
    name_map = {v["notion_name"]: k for k, v in schema.items()}

    for notion_name, raw_val in raw_props.items():
        human = name_map.get(notion_name)
        if human is None:
            # Live property not in our schema (e.g. we haven't modeled it).
            # Surface it under a namespaced key for debuggability.
            out[f"_extra:{notion_name}"] = raw_val
            continue
        prop_def = schema[human]
        out[human] = _decode_value(prop_def, raw_val)
    return out


def _db_key_for_id(db_id: str) -> str | None:
    for key, db in notion_schema.DATABASES.items():
        if db["database_id"] == db_id:
            return key
    return None


# ---------------------------------------------------------------------------
# Public CRUD
# ---------------------------------------------------------------------------

def create_page(db_key: str, properties: dict[str, Any]) -> dict:
    """Create a new page in the given Notion database.

    properties: {human_name: value} per our schema (see notion_schema).
    Read-only fields are silently dropped.

    Returns the raw Notion page object.
    """
    db_id = notion_db_id(db_key)
    notion_props = to_notion_properties(db_key, properties)

    # Notion requires every DB to have exactly one title property. If the
    # caller didn't provide it and the schema marks it required, raise.
    schema = notion_schema.PROPERTIES_BY_DB[db_key]
    title_prop = next(
        (v for v in schema.values() if v["type"] == "title"),
        None,
    )
    if title_prop and title_prop["required"]:
        if title_prop["notion_name"] not in notion_props:
            raise SchemaError(
                f"DB {db_key!r} requires title property "
                f"{title_prop['notion_name']!r} on create"
            )

    body = {"parent": {"database_id": db_id}, "properties": notion_props}
    return _request("POST", f"{NOTION_API}/pages", json_body=body)


def update_page(page_id: str, properties: dict[str, Any]) -> dict:
    """Update an existing Notion page.

    properties: {human_name: value}. Read-only fields silently dropped.
    Pass None for a value to clear it (Notion accepts {} to clear).

    Returns the updated raw Notion page object.
    """
    # We need the db_key to map human names → notion names. Resolve it from
    # the page's parent by reading the page first. (Phase 7 callers usually
    # already know it; an optimization is to accept db_key explicitly.)
    page = get_page(page_id)
    parent_db_id = page.get("parent", {}).get("database_id")
    db_key = _db_key_for_id(parent_db_id) if parent_db_id else None
    if db_key is None:
        raise SchemaError(
            f"Page {page_id} is not in one of our 3 known databases."
        )

    notion_props = to_notion_properties(db_key, properties)
    return _request("PATCH", f"{NOTION_API}/pages/{page_id}", json_body={"properties": notion_props})


def archive_page(page_id: str) -> dict:
    """Archive one page while leaving its parent database untouched.

    This intentionally accepts only a page ID and only emits Notion's page
    archive payload.  Reset code therefore has no path to archive/delete a
    database container or alter its schema.
    """
    page_id = str(page_id or "").strip()
    if not page_id:
        raise ValueError("page_id is required")
    return _request(
        "PATCH", f"{NOTION_API}/pages/{page_id}",
        json_body={"archived": True},
    )


def get_page(page_id: str) -> dict:
    """Fetch a single Notion page by ID."""
    return _request("GET", f"{NOTION_API}/pages/{page_id}")


def query_database(
    db_key: str,
    *,
    filter: dict | None = None,
    sorts: list[dict] | None = None,
    page_size: int = 100,
    start_cursor: str | None = None,
    max_pages: int | None = None,
) -> list[dict]:
    """Query a database, returning all matching pages (auto-paginates).

    Returns a list of raw Notion page objects. Use parse_page() to flatten.

    filter: Notion filter dict (see Notion API docs).
    sorts: list of {property, direction} dicts.
    page_size: 1..100 (Notion max per page).
    max_pages: safety cap to prevent runaway pagination; None = unlimited.
    """
    db_id = notion_db_id(db_key)
    url = f"{NOTION_API}/databases/{db_id}/query"

    body: dict[str, Any] = {"page_size": min(max(page_size, 1), 100)}
    if filter is not None:
        body["filter"] = filter
    if sorts is not None:
        body["sorts"] = sorts

    pages: list[dict] = []
    cursor = start_cursor
    page_count = 0
    while True:
        if cursor:
            body["start_cursor"] = cursor
        result = _request("POST", url, json_body=body)
        pages.extend(result.get("results", []))
        page_count += 1
        if max_pages is not None and page_count >= max_pages:
            break
        if not result.get("has_more"):
            break
        cursor = result.get("next_cursor")
        if not cursor:
            break
    return pages


def get_block_children(block_id: str, *, page_size: int = 100) -> list[dict]:
    """Fetch all child blocks of a page/block (auto-paginates)."""
    url = f"{NOTION_API}/blocks/{block_id}/children"
    blocks: list[dict] = []
    params: dict[str, Any] = {"page_size": min(max(page_size, 1), 100)}
    while True:
        result = _request("GET", url, params=params)
        blocks.extend(result.get("results", []))
        if not result.get("has_more"):
            break
        cursor = result.get("next_cursor")
        if not cursor:
            break
        params["start_cursor"] = cursor
    return blocks


# Block types whose payload carries a rich_text array we can flatten.
_TEXT_BLOCK_TYPES = {
    "paragraph",
    "heading_1",
    "heading_2",
    "heading_3",
    "bulleted_list_item",
    "numbered_list_item",
    "to_do",
    "toggle",
    "quote",
    "callout",
    "code",
}


def _block_line(block: dict) -> str | None:
    btype = block.get("type")
    if btype not in _TEXT_BLOCK_TYPES:
        return None
    payload = block.get(btype, {}) or {}
    text = "".join(
        seg.get("plain_text", "") for seg in payload.get("rich_text", [])
    ).strip()
    if not text:
        return None
    if btype in ("bulleted_list_item", "numbered_list_item"):
        return f"- {text}"
    if btype == "to_do":
        mark = "x" if payload.get("checked") else " "
        return f"[{mark}] {text}"
    return text


def page_plain_text(page_id: str, *, max_depth: int = 2) -> str:
    """Flatten a page's body blocks into plain text (depth-capped recursion)."""
    lines: list[str] = []

    def walk(block_id: str, depth: int) -> None:
        for block in get_block_children(block_id):
            line = _block_line(block)
            if line:
                lines.append(line)
            if block.get("has_children") and depth < max_depth:
                walk(block["id"], depth + 1)

    walk(page_id, 0)
    return "\n".join(lines)


def query_database_iter(
    db_key: str,
    *,
    filter: dict | None = None,
    sorts: list[dict] | None = None,
    page_size: int = 100,
) -> Iterable[dict]:
    """Generator version of query_database — yields pages one at a time.

    Useful for sync (Phase 3) where we don't want to hold all pages in memory
    if a DB grows large.
    """
    db_id = notion_db_id(db_key)
    url = f"{NOTION_API}/databases/{db_id}/query"
    body: dict[str, Any] = {"page_size": min(max(page_size, 1), 100)}
    if filter is not None:
        body["filter"] = filter
    if sorts is not None:
        body["sorts"] = sorts

    cursor: str | None = None
    while True:
        if cursor:
            body["start_cursor"] = cursor
        result = _request("POST", url, json_body=body)
        for page in result.get("results", []):
            yield page
        if not result.get("has_more"):
            break
        cursor = result.get("next_cursor")
        if not cursor:
            break
