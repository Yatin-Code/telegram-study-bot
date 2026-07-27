"""Idempotently create the Study Bot operational databases in Notion."""

from __future__ import annotations

import os
from typing import Any

from config import notion_schema
from config.resolve_db_ids import NOTION_API, _db_title, search_databases, update_env_file
from config.settings import notion_token
from notion_client_wrapper import _request


HUB_TITLE = "Study Bot System"
# SQL-owned domains live in operational_store (op_*). This script no longer
# creates Notion DBs for them. ledger/doubts/revision are user-authored outside.
MANAGED_KEYS: tuple[str, ...] = ()


def _schema_payload(prop: dict[str, Any], resolved: dict[str, str]) -> dict[str, Any] | None:
    t = prop["type"]
    if t == "title":
        return {"title": {}}
    if t == "rich_text":
        return {"rich_text": {}}
    if t == "number":
        return {"number": {"format": "number"}}
    if t == "date":
        return {"date": {}}
    if t == "checkbox":
        return {"checkbox": {}}
    if t in ("select", "status"):
        return {t: {"options": [{"name": name} for name in prop.get("options") or []]}}
    if t == "relation":
        target_id = resolved.get(prop.get("relates_to", ""))
        if not target_id:
            return None
        return {
            "relation": {
                "database_id": target_id,
                "type": "single_property",
                "single_property": {},
            }
        }
    return None


def _find_exercises_db() -> dict[str, Any]:
    for db in search_databases(notion_token()):
        if _db_title(db).strip().lower() == "exercises":
            return db
    raise RuntimeError("Exercises database is not shared with the integration")


def _page_title(page: dict[str, Any]) -> str:
    for prop in page.get("properties", {}).values():
        if prop.get("type") == "title":
            return "".join(x.get("plain_text", "") for x in prop.get("title", [])).strip()
    return ""


def _find_or_create_hub(exercises_db: dict[str, Any]) -> dict[str, Any]:
    pages = _request(
        "POST", f"{NOTION_API}/databases/{exercises_db['id']}/query",
        json_body={"page_size": 100},
    ).get("results", [])
    for page in pages:
        if _page_title(page) == HUB_TITLE:
            return page

    raw = _request("GET", f"{NOTION_API}/databases/{exercises_db['id']}")
    title_name = next(
        name for name, prop in raw.get("properties", {}).items()
        if prop.get("type") == "title"
    )
    return _request(
        "POST", f"{NOTION_API}/pages",
        json_body={
            "parent": {"database_id": exercises_db["id"]},
            "properties": {title_name: {"title": [{"text": {"content": HUB_TITLE}}]}},
        },
    )


def _create_database(db_key: str, hub_id: str, resolved: dict[str, str]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    for prop in notion_schema.PROPERTIES_BY_DB[db_key].values():
        encoded = _schema_payload(prop, resolved)
        if encoded is not None and prop["type"] != "relation":
            properties[prop["notion_name"]] = encoded
    return _request(
        "POST", f"{NOTION_API}/databases",
        json_body={
            "parent": {"type": "page_id", "page_id": hub_id},
            "title": [{"type": "text", "text": {"content": notion_schema.DATABASES[db_key]["title"]}}],
            "is_inline": True,
            "properties": properties,
        },
    )


def _patch_relations(db_key: str, db_id: str, resolved: dict[str, str]) -> None:
    properties: dict[str, Any] = {}
    for prop in notion_schema.PROPERTIES_BY_DB[db_key].values():
        if prop["type"] != "relation":
            continue
        encoded = _schema_payload(prop, resolved)
        if encoded:
            properties[prop["notion_name"]] = encoded
    if properties:
        _request(
            "PATCH", f"{NOTION_API}/databases/{db_id}",
            json_body={"properties": properties},
        )


def _patch_doubt_workflow() -> None:
    db_id = os.environ.get("NOTION_DOUBTS_DB_ID", "").strip()
    if not db_id:
        return
    managed = (
        "workflow_state", "valid_attempts", "teacher_ready",
        "next_teacher_window", "dismissed_reason", "resolution",
        "resolved_at", "teacher_asked",
    )
    properties = {
        notion_schema.DOUBTS_PROPERTIES[key]["notion_name"]:
            _schema_payload(notion_schema.DOUBTS_PROPERTIES[key], {})
        for key in managed
    }
    properties["Failure Type"] = {
        "select": {"options": [{"name": x} for x in notion_schema.FAILURE_TYPE_OPTIONS]}
    }
    _request(
        "PATCH", f"{NOTION_API}/databases/{db_id}",
        json_body={"properties": properties},
    )


def _patch_operation_ids(resolved: dict[str, str]) -> None:
    for key, db_id in resolved.items():
        if key not in notion_schema.PROPERTIES_BY_DB:
            continue
        _request(
            "PATCH", f"{NOTION_API}/databases/{db_id}",
            json_body={"properties": {"Operation ID": {"rich_text": {}}}},
        )


def setup() -> dict[str, str]:
    existing = {_db_title(db): db for db in search_databases(notion_token())}
    resolved: dict[str, str] = {}
    for key, db in notion_schema.DATABASES.items():
        env_name = f"NOTION_{key.upper()}_DB_ID"
        configured = os.environ.get(env_name, "").strip()
        if configured:
            resolved[key] = configured
        elif db["title"] in existing:
            resolved[key] = existing[db["title"]]["id"]

    hub = _find_or_create_hub(_find_exercises_db())
    for key in MANAGED_KEYS:
        if key not in resolved:
            created = _create_database(key, hub["id"], resolved)
            resolved[key] = created["id"]

    for key, db_id in resolved.items():
        if key in notion_schema.PROPERTIES_BY_DB:
            _patch_relations(key, db_id, resolved)

    env_values = {f"NOTION_{key.upper()}_DB_ID": resolved[key] for key in MANAGED_KEYS}
    update_env_file(env_values)
    os.environ.update(env_values)
    for key, db_id in resolved.items():
        if key in notion_schema.DATABASES:
            notion_schema.DATABASES[key]["database_id"] = db_id
    _patch_doubt_workflow()
    _patch_operation_ids(resolved)
    return {key: resolved[key] for key in MANAGED_KEYS}


def main() -> int:
    result = setup()
    print("Study Bot workspace ready:")
    for key, db_id in result.items():
        print(f"  {key}: {db_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
