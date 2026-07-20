"""
Diagnostic: dump raw JSON of database schemas for inspection.
No caching, fresh fetch, explicit no-cache headers.
"""

import json
import sys

import httpx

from config.settings import notion_token, notion_db_id

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


def fetch_db_raw(token: str, db_id: str) -> dict:
    url = f"{NOTION_API}/databases/{db_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    }
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(url, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(f"GET failed: {resp.status_code} {resp.text}")
        return resp.json()


def main() -> int:
    token = notion_token()

    for db_key in ("ledger", "doubts", "revision"):
        db_id = notion_db_id(db_key)
        print(f"\n{'='*70}")
        print(f"DB: {db_key}  id={db_id}")
        print('='*70)
        raw = fetch_db_raw(token, db_id)

        # Print the high-level fields
        print(f"title: {_plain(raw.get('title', []))!r}")
        print(f"url:   {raw.get('url')}")
        print(f"object: {raw.get('object')}")

        # Print properties with full detail
        props = raw.get("properties", {})
        print(f"\nproperties ({len(props)}):")
        for name, prop in props.items():
            print(f"\n  --- {name!r} ---")
            print(json.dumps(prop, indent=2, ensure_ascii=False))

    return 0


def _plain(title_arr: list) -> str:
    return "".join(r.get("plain_text", "") for r in title_arr).strip()


if __name__ == "__main__":
    sys.exit(main())
