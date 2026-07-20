"""
Phase 2: Resolve real Notion database IDs.

Calls Notion /v1/search to list all databases the integration can see,
matches against the 3 known titles (with fuzzy fallback), prints each DB's
live property schema so we can verify it matches Section 1 of the spec,
and writes the resolved IDs back to .env.

Run:  python -m config.resolve_db_ids
"""

from __future__ import annotations

import sys
from typing import Any

import httpx

from . import notion_schema
from .settings import notion_token


NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def search_databases(token: str) -> list[dict[str, Any]]:
    """Page through /v1/search filtered to databases. Returns raw page objects."""
    url = f"{NOTION_API}/search"
    headers = _headers(token)
    results: list[dict[str, Any]] = []
    start_cursor: str | None = None

    with httpx.Client(timeout=30.0) as client:
        while True:
            payload: dict[str, Any] = {"filter": {"value": "database", "property": "object"}}
            if start_cursor:
                payload["start_cursor"] = start_cursor
            resp = client.post(url, headers=headers, json=payload)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Notion /v1/search failed: {resp.status_code} {resp.text}"
                )
            data = resp.json()
            results.extend(data.get("results", []))
            if data.get("has_more"):
                start_cursor = data.get("next_cursor")
            else:
                break
    return results


def _db_title(db_obj: dict[str, Any]) -> str:
    """Flatten Notion's rich-text title array to plain text."""
    title_arr = db_obj.get("title", [])
    if not title_arr:
        return ""
    return "".join(run.get("plain_text", "") for run in title_arr).strip()


def match_db_key(title: str) -> str | None:
    """Match a live DB title to one of our 3 known DB keys.

    Strategy: check if our known title appears as a substring (case-insensitive).
    The spec uses prefixes like '1. Daily Execution Ledger...' which won't be
    in the live DB title verbatim, so we also accept a 'Daily Execution Ledger'
    substring for the ledger.
    """
    t = title.lower()
    if "daily execution ledger" in t or "execution ledger" in t:
        return "ledger"
    if t == "doubts" or t.startswith("doubts"):
        return "doubts"
    if t == "revision" or t.startswith("revision"):
        return "revision"
    return None


def get_database_schema(token: str, db_id: str) -> dict[str, Any]:
    """Fetch a database's full property schema (one GET to /v1/databases/{id})."""
    url = f"{NOTION_API}/databases/{db_id}"
    headers = _headers(token)
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(url, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(
                f"GET /v1/databases/{db_id} failed: {resp.status_code} {resp.text}"
            )
        return resp.json()


def property_type_of(prop: dict[str, Any]) -> str:
    """Map Notion property schema → our internal type vocab.

    Notion uses 'rich_text', 'multi_select', 'relation', 'rollup', 'formula',
    'status', etc. We collapse 'rich_text' → 'rich_text' (kept) so callers
    can compare directly to notion_schema field 'type' values.
    """
    t = prop.get("type", "")
    # Our schema uses 'rich_text' for both Notion 'rich_text' and 'text' cases.
    return t


def compare_schema(
    db_key: str,
    live_props: dict[str, dict[str, Any]],
) -> list[str]:
    """Compare live Notion property schema against our hardcoded notion_schema.

    Returns a list of human-readable discrepancy strings (empty = perfect match).
    We compare property names (case-sensitive, since writes depend on them),
    types, and select/status option lists.
    """
    discrepancies: list[str] = []
    expected = notion_schema.PROPERTIES_BY_DB[db_key]

    # Index expected by notion_name for easy lookup.
    expected_by_name = {v["notion_name"]: v for v in expected.values()}

    # Check every expected property exists in live, with matching type.
    for prop_name, exp in expected_by_name.items():
        if prop_name not in live_props:
            discrepancies.append(f"  MISSING in Notion: '{prop_name}' (expected {exp['type']})")
            continue
        live_type = property_type_of(live_props[prop_name])
        # Notion 'relation' and our 'relation' match; 'rollup'/'formula' match.
        # 'rich_text' matches 'rich_text'.
        if live_type != exp["type"]:
            discrepancies.append(
                f"  TYPE mismatch for '{prop_name}': "
                f"expected {exp['type']!r}, Notion says {live_type!r}"
            )
        # If select/status, verify option lists.
        if exp["type"] in ("select", "status") and exp["options"]:
            live_opts = [o["name"] for o in live_props[prop_name].get(exp["type"], {}).get("options", [])]
            missing = set(exp["options"]) - set(live_opts)
            extra = set(live_opts) - set(exp["options"])
            if missing:
                discrepancies.append(f"  OPTIONS missing in Notion for '{prop_name}': {sorted(missing)}")
            if extra:
                discrepancies.append(f"  OPTIONS extra in Notion for '{prop_name}': {sorted(extra)}")

    # Check for unexpected live properties (informational only — not a hard fail).
    for live_name in live_props:
        if live_name not in expected_by_name:
            discrepancies.append(f"  NOTE: extra property in Notion not in our schema: '{live_name}'")

    return discrepancies


def update_env_file(resolved: dict[str, str]) -> None:
    """Write resolved IDs back to .env, preserving other lines.

    resolved = { "NOTION_LEDGER_DB_ID": "<id>", ... }
    """
    import os
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    lines: list[str] = []
    seen_keys: set[str] = set()

    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if "=" in stripped and not stripped.startswith("#"):
                    key = stripped.split("=", 1)[0]
                    if key in resolved:
                        lines.append(f"{key}={resolved[key]}\n")
                        seen_keys.add(key)
                        continue
                lines.append(line)

    # Append any resolved keys not already in the file.
    for key, val in resolved.items():
        if key not in seen_keys:
            if lines and not lines[-1].endswith("\n"):
                lines.append("\n")
            lines.append(f"{key}={val}\n")

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(lines)


def main() -> int:
    token = notion_token()
    print("→ Searching Notion for databases visible to this integration...")
    all_dbs = search_databases(token)
    print(f"  Found {len(all_dbs)} database(s) total.\n")

    resolved: dict[str, str] = {}
    db_key_to_env_var = {
        "ledger": "NOTION_LEDGER_DB_ID",
        "doubts": "NOTION_DOUBTS_DB_ID",
        "revision": "NOTION_REVISION_DB_ID",
    }

    # First pass: print everything we see, attempt to match.
    matched: dict[str, dict[str, Any]] = {}
    for db_obj in all_dbs:
        title = _db_title(db_obj)
        db_id = db_obj["id"]
        key = match_db_key(title)
        marker = f"  → MATCH ({key})" if key else "    (no match)"
        print(f"  • {title!r}  id={db_id}{marker}")

        if key:
            matched[key] = db_obj

    print()

    # Verify all 3 matched.
    # Operational Study Bot databases are nested under the managed hub and are
    # provisioned by setup_study_workspace.py. This resolver owns only the
    # original three user databases.
    missing_keys = {"ledger", "doubts", "revision"} - set(matched)
    if missing_keys:
        print(f"✗ Could not match these DBs: {sorted(missing_keys)}")
        print("  Make sure you shared each database with the integration in Notion,")
        print("  then re-run this script.")
        return 1

    # Second pass: fetch full schema and compare against our hardcoded one.
    all_discrepancies: dict[str, list[str]] = {}
    for db_key, db_obj in matched.items():
        db_id = db_obj["id"]
        print(f"→ Fetching schema for {db_key} ({db_id[:8]}...)...")
        live = get_database_schema(token, db_id)
        live_props = live.get("properties", {})
        print(f"  Live properties ({len(live_props)}):")
        for name, prop in live_props.items():
            t = prop.get("type", "?")
            extra = ""
            if t in ("select", "status"):
                opts = [o["name"] for o in prop.get(t, {}).get("options", [])]
                extra = f"  opts={opts}"
            elif t == "relation":
                rels = prop.get("relation", {})
                extra = f"  → {rels.get('database_id','?')[:8]}..."
            print(f"    - {name!r}  type={t}{extra}")

        disc = compare_schema(db_key, live_props)
        all_discrepancies[db_key] = disc
        if disc:
            print(f"\n  ⚠ Discrepancies for {db_key}:")
            for d in disc:
                print(d)
        else:
            print(f"  ✓ Schema matches Section 1 of spec exactly.\n")

        resolved[db_key_to_env_var[db_key]] = db_id

    # Write resolved IDs to .env.
    update_env_file(resolved)
    print(f"→ Wrote {len(resolved)} DB IDs to .env:")
    for k, v in resolved.items():
        print(f"    {k}={v}")

    # Summary.
    hard_fail = any(
        any("MISSING" in d or "TYPE mismatch" in d for d in discs)
        for discs in all_discrepancies.values()
    )
    if hard_fail:
        print("\n✗ Phase 2 DoD NOT met — schema mismatches found above.")
        return 2

    print("\n✓ Phase 2 DoD met: all 3 DBs resolved, live schemas match spec.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
