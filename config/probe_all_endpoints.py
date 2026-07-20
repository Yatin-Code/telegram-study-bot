"""
Transparent diagnostic: for each known database_id and data_source_id, fetch
via /v1/data_sources/{id} (newest API) AND /v1/databases/{id} (stable API),
print the exact endpoint URL + version header + ID used, then the DB title
and full property list.

Goal: let the user verify which data_source_id corresponds to which DB, and
see if any of them returns a property set that includes 'Subject' or
'Exercise' relations.
"""

import json
import sys

import httpx

from config.settings import notion_token

NOTION_API = "https://api.notion.com/v1"
STABLE_VERSION = "2022-06-28"   # /v1/databases/{id}
NEWEST_VERSION = "2025-09-03"   # /v1/data_sources/{id} (only this version supports it)


# All known IDs from the raw schema dump:
#   - 3 database_ids (one per DB, returned by /v1/search)
#   - 3 data_source_ids observed inside relation property definitions
IDS_TO_PROBE = [
    # database_ids (used with /v1/databases/{id})
    ("database_id", "ledger",  "36dbc6be-f0c2-81db-9da5-f2d1856408ae"),
    ("database_id", "doubts",  "36dbc6be-f0c2-81ba-a6bd-f3e39886eb50"),
    ("database_id", "revision","36dbc6be-f0c2-81fe-a1d9-ee8def93d63e"),
    # data_source_ids (used with /v1/data_sources/{id})
    # Source: relation property definitions in the raw schema dump.
    #   - 36dbc6be-f0c2-8176-aad1-000b8d794f72 : from Ledger's 'Logged Errors' relation (target = Doubts)
    #   - 36dbc6be-f0c2-81eb-bf60-000b01056c00 : from Ledger's 'Chapter' relation (target = Revision)
    #   - 36dbc6be-f0c2-81d5-8c3a-000b886f68ce : from Revision's 'Ledger Entries' AND Doubts's 'Ledger Entry' (target = Ledger)
    ("data_source_id", "doubts_via_ledger",   "36dbc6be-f0c2-8176-aad1-000b8d794f72"),
    ("data_source_id", "revision_via_ledger", "36dbc6be-f0c2-81eb-bf60-000b01056c00"),
    ("data_source_id", "ledger_via_others",   "36dbc6be-f0c2-81d5-8c3a-000b886f68ce"),
]


def _plain(title_arr: list) -> str:
    return "".join(r.get("plain_text", "") for r in title_arr).strip()


def fetch(url: str, token: str, version: str) -> tuple[int, dict | str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": version,
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
    }
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(url, headers=headers)
        if resp.status_code == 200:
            return 200, resp.json()
        return resp.status_code, resp.text[:300]


def main() -> int:
    token = notion_token()

    for kind, label, obj_id in IDS_TO_PROBE:
        print("=" * 78)
        print(f"PROBE: {label}  ({kind})")
        print(f"  ID: {obj_id}")
        print()

        if kind == "database_id":
            endpoint = f"{NOTION_API}/databases/{obj_id}"
            version = STABLE_VERSION
        else:
            endpoint = f"{NOTION_API}/data_sources/{obj_id}"
            version = NEWEST_VERSION

        print(f"  Endpoint URL : {endpoint}")
        print(f"  Notion-Version: {version}")
        print()

        status, body = fetch(endpoint, token, version)
        print(f"  HTTP status  : {status}")

        if status != 200:
            print(f"  Body         : {body}")
            print()
            continue

        title = _plain(body.get("title", []))
        props = body.get("properties", {})
        print(f"  DB title     : {title!r}")
        print(f"  Property count: {len(props)}")
        print()
        print(f"  Properties (name | type):")
        for name, prop in sorted(props.items()):
            has_subject = "yes" if name == "Subject" else ""
            has_exercise = "yes" if name == "Exercise" else ""
            marker = ""
            if has_subject or has_exercise:
                marker = f"   *** FOUND {'/'.join(filter(None, [has_subject, has_exercise]))} ***"
            print(f"    - {name!r:55} type={prop.get('type','?'):12}{marker}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
