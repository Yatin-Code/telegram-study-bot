"""
Try fetching via the data_source endpoint to see if it reveals
a different set of properties than the database endpoint.

The user suggested fetching by data-source URL, not just database URL.
"""

import json
import sys

import httpx

from config.settings import notion_token

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# Try newer API version too — data sources were introduced after 2022-06-28
NEWER_VERSIONS = ["2025-09-03", "2025-05-20", "2025-01-09", "2024-11-19"]


def fetch_database(token: str, db_id: str, version: str) -> dict:
    url = f"{NOTION_API}/databases/{db_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": version,
    }
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(url, headers=headers)
        return {"status": resp.status_code, "body": resp.json() if resp.status_code == 200 else resp.text}


def try_data_source_endpoint(token: str, data_source_id: str, version: str) -> dict:
    url = f"{NOTION_API}/data_sources/{data_source_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": version,
    }
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(url, headers=headers)
        return {"status": resp.status_code, "body": resp.json() if resp.status_code == 200 else resp.text[:200]}


def main() -> int:
    token = notion_token()

    # Known data_source_ids from the raw schema dump:
    data_source_ids = {
        "ledger_db_id": "36dbc6be-f0c2-81db-9da5-f2d1856408ae",
        "doubts_ds_id": "36dbc6be-f0c2-8176-aad1-000b8d794f72",   # from Logged Errors relation
        "revision_ds_id": "36dbc6be-f0c2-81eb-bf60-000b01056c00",  # from Chapter relation (target=revision)
        "ledger_ds_id": "36dbc6be-f0c2-81d5-8c3a-000b886f68ce",   # from Ledger Entries relation (target=ledger)
    }

    # First, try fetching the Ledger DB with progressively newer API versions.
    print("=" * 70)
    print("Test 1: GET /v1/databases/ledger with various Notion-Version headers")
    print("=" * 70)
    for v in [NOTION_VERSION] + NEWER_VERSIONS:
        r = fetch_database(token, data_source_ids["ledger_db_id"], v)
        if r["status"] == 200:
            props = r["body"].get("properties", {})
            prop_names = sorted(props.keys())
            has_subject = "Subject" in props
            has_exercise = "Exercise" in props
            print(f"  version={v}  status=200  props={len(props)}  has_Subject={has_subject}  has_Exercise={has_exercise}")
            if has_subject or has_exercise:
                print(f"    *** FOUND missing relations with version {v}! ***")
                if has_subject:
                    print(f"    Subject: {json.dumps(props['Subject'], indent=2)[:300]}")
                if has_exercise:
                    print(f"    Exercise: {json.dumps(props['Exercise'], indent=2)[:300]}")
        else:
            print(f"  version={v}  status={r['status']}  body={r['body'][:100]}")

    # Try the data_sources endpoint.
    print()
    print("=" * 70)
    print("Test 2: GET /v1/data_sources/{id} for each known data_source_id")
    print("=" * 70)
    for label, ds_id in data_source_ids.items():
        if "db_id" in label:
            continue
        for v in [NOTION_VERSION] + NEWER_VERSIONS:
            r = try_data_source_endpoint(token, ds_id, v)
            if r["status"] == 200:
                body = r["body"]
                print(f"  {label}  version={v}  status=200")
                print(f"    keys: {list(body.keys())}")
                if "properties" in body:
                    print(f"    property count: {len(body['properties'])}")
                    print(f"    property names: {sorted(body['properties'].keys())}")
                break
            else:
                print(f"  {label}  version={v}  status={r['status']}  body={str(r['body'])[:100]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
