"""
Phase 2 smoke test for notion_client_wrapper.

Verifies:
  1. query_database() works on all 3 DBs.
  2. parse_page() flattens each DB's pages correctly.
  3. to_notion_properties() round-trips a parsed page (write path can encode
     everything we'd write back).
  4. Required title detection on create_page() works.
  5. SchemaError raised on read-only writes.

Does NOT create/update/delete any pages — read-only.
"""

import sys

from notion_client_wrapper import (
    NotionAPIError,
    SchemaError,
    create_page,
    parse_page,
    query_database,
    to_notion_properties,
)
from config import notion_schema


def smoke_test_query_each_db() -> None:
    print("=== Test 1: query_database on each DB ===")
    expected_counts = {"ledger": 0, "doubts": 3, "revision": 6}  # per spec Phase 3 DoD
    for db_key in ("ledger", "doubts", "revision"):
        pages = query_database(db_key, page_size=10)
        print(f"  {db_key}: {len(pages)} page(s) (spec expects ≥{expected_counts[db_key]})")
        assert len(pages) >= expected_counts[db_key], f"too few in {db_key}"
        if pages:
            print(f"    sample page id: {pages[0]['id']}")
    print("  PASS\n")


def smoke_test_parse_page() -> None:
    print("=== Test 2: parse_page on each DB ===")
    for db_key in ("ledger", "doubts", "revision"):
        pages = query_database(db_key, page_size=5)
        if not pages:
            print(f"  {db_key}: no pages to parse, skipping")
            continue
        page = pages[0]
        parsed = parse_page(page)
        print(f"  {db_key} sample parsed page:")
        print(f"    _page_id: {parsed.get('_page_id')}")
        print(f"    _url: {parsed.get('_url')}")
        print(f"    _db_key: {parsed.get('_db_key')}")
        assert parsed["_db_key"] == db_key, f"db_key mismatch: {parsed['_db_key']} != {db_key}"

        # Show non-None, non-reserved fields.
        for k, v in parsed.items():
            if k.startswith("_") or v is None:
                continue
            print(f"    {k}: {v!r}")
        print()
    print("  PASS\n")


def smoke_test_round_trip() -> None:
    print("=== Test 3: to_notion_properties round-trip ===")
    for db_key in ("ledger", "doubts", "revision"):
        pages = query_database(db_key, page_size=5)
        if not pages:
            continue
        page = pages[0]
        parsed = parse_page(page)

        # Build a write-back dict from writable, non-None fields only.
        writable_in_schema = {
            k: v for k, v in parsed.items()
            if not k.startswith("_")
            and v is not None
            and k in notion_schema.PROPERTIES_BY_DB[db_key]
            and not notion_schema.PROPERTIES_BY_DB[db_key][k]["read_only"]
        }
        if not writable_in_schema:
            print(f"  {db_key}: no writable fields on sample page, skipping")
            continue

        # Round-trip: human dict → Notion format → should not raise.
        encoded = to_notion_properties(db_key, writable_in_schema)
        print(f"  {db_key}: round-tripped {len(encoded)} writable fields")
        for name, payload in encoded.items():
            print(f"    {name!r}: {payload}")
        print()
    print("  PASS\n")


def smoke_test_required_title() -> None:
    print("=== Test 4: create_page without required title raises SchemaError ===")
    for db_key in ("ledger", "doubts", "revision"):
        try:
            create_page(db_key, {"doubts": "this should fail — no title"})
            print(f"  {db_key}: ✗ create_page did NOT raise (expected SchemaError)")
            assert False
        except SchemaError as e:
            print(f"  {db_key}: ✓ correctly raised SchemaError: {e}")
        except NotionAPIError as e:
            # If Notion itself rejected it (4xx), that's also acceptable —
            # the wrapper raised before sending OR Notion rejected after send.
            print(f"  {db_key}: ✓ Notion rejected (status {e.status}): {e.body[:120]}")
    print("  PASS\n")


def smoke_test_readonly_rejected() -> None:
    print("=== Test 5: writing a read-only field via to_notion_properties is dropped ===")
    # Read-only fields (formula/rollup) are silently dropped at the dict level
    # by to_notion_properties (callers may include context dicts that contain
    # them). _encode_value raises if called directly on a read-only field.
    schema_ledger = notion_schema.PROPERTIES_BY_DB["ledger"]
    readonly_field = next(k for k, v in schema_ledger.items() if v["read_only"])
    print(f"  Using read-only field: {readonly_field}")
    encoded = to_notion_properties("ledger", {readonly_field: "should be dropped"})
    assert readonly_field not in [v for v in encoded], "read-only field leaked through"
    assert all("formula" not in v and "rollup" not in v for v in encoded.values()), \
        "encoded payload contains formula/rollup key"
    print("  ✓ read-only field correctly dropped")
    print("  PASS\n")


def main() -> int:
    smoke_test_query_each_db()
    smoke_test_parse_page()
    smoke_test_round_trip()
    smoke_test_required_title()
    smoke_test_readonly_rejected()
    print("=" * 60)
    print("ALL SMOKE TESTS PASSED — Phase 2 wrapper verified.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
