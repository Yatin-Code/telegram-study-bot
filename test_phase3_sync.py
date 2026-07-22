"""
Phase 3 sync — test the archived-page diff detection.

Notion's /query endpoints don't expose archived pages (verified live:
`is_archived=true` on /v1/data_sources/{id}/query is accepted but returns
an empty result set, and /v1/databases/{id}/query rejects the param with 400).
The only way to keep the mirror's `archived` column faithful is a diff: after
a full successful pagination, any mirror row that's still archived=0 but
wasn't in the returned set has been archived in Notion since the last sync.

This test monkeypatches query_database_iter so we control the "live" set,
and verifies:
  - pre-existing active row missing from next fetch -> archived=1
  - rows present in fetch stay archived=0
  - already-archived rows are untouched (not re-touched by the UPDATE)
  - mid-sync error -> NO archival marking (partial fetches must not cause
    false positives, since a transient Notion outage would otherwise mark
    every row in the DB as archived)
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

import sync


def _check(ok, label, cond, extra=""):
    print(f"[{'OK ' if cond else 'BAD'}] {label}{(' -> ' + extra) if extra else ''}")
    return ok and cond


def _seed_mirror(db_path: Path, rows: list[dict]) -> None:
    """Seed the mirror with rows that look like sync.upsert_page would write."""
    with sync.connect(db_path) as conn:
        sync.init_db(conn)
        for r in rows:
            r = dict(r)
            r.setdefault("notion_url", None)
            r.setdefault("created_time", None)
            r.setdefault("last_edited_time", None)
            r.setdefault("last_synced_at", "2026-01-01T00:00:00+00:00")
            r.setdefault("archived", 0)
            r.setdefault("raw_json", "{}")
            cols = list(r.keys())
            placeholders = ", ".join("?" for _ in cols)
            col_sql = ", ".join(sync._quote_ident(c) for c in cols)
            conn.execute(
                f"INSERT INTO {sync._quote_ident('ledger')} ({col_sql}) VALUES ({placeholders})",
                [r[c] for c in cols],
            )
        conn.commit()


def _archived_state(db_path: Path, table: str = "ledger") -> dict[str, int]:
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT notion_page_id, archived FROM {sync._quote_ident(table)}"
        ).fetchall()
        return {r["notion_page_id"]: r["archived"] for r in rows}


def run() -> bool:
    ok = True

    print("=== Phase 3 sync: archived diff — basic case ===")
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "t.db"
        _seed_mirror(db_path, [
            {"notion_page_id": "p1", "task": "active row 1"},
            {"notion_page_id": "p2", "task": "active row 2"},
            {"notion_page_id": "p3", "task": "already archived", "archived": 1},
        ])

        # Simulate a Notion fetch that returns p1 only — p2 has been archived
        # in Notion since the last sync, p3 was already archived.
        orig_iter = sync.query_database_iter
        orig_content = sync.page_plain_text
        sync.query_database_iter = lambda db_key, page_size=100: iter([
            {"id": "p1", "archived": False, "parent": {"database_id": "x"},
             "properties": {}, "url": None, "created_time": None,
             "last_edited_time": None},
        ])
        sync.page_plain_text = lambda page_id: ""
        # notion_schema.PROPERTIES_BY_DB is read by upsert_page via parse_page;
        # the fake page has parent.database_id="x" which won't map to a known
        # db_key, so parse_page returns just the reserved keys. That's fine for
        # this test — we only care about the archived diff logic.
        try:
            counts = sync.sync_once(db_path=db_path, db_keys=("ledger",))
        finally:
            sync.query_database_iter = orig_iter
            sync.page_plain_text = orig_content

        state = _archived_state(db_path)
        ok = _check(ok, "p1 stays active", state.get("p1") == 0, str(state))
        ok = _check(ok, "p2 marked archived (missing from fetch)", state.get("p2") == 1, str(state))
        ok = _check(ok, "p3 stays archived (untouched)", state.get("p3") == 1, str(state))

    print("\n=== Phase 3 sync: archived diff — mid-sync error safety ===")
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "t.db"
        _seed_mirror(db_path, [
            {"notion_page_id": "p1", "task": "active 1"},
            {"notion_page_id": "p2", "task": "active 2"},
            {"notion_page_id": "p3", "task": "active 3"},
        ])

        # Simulate a fetch that raises mid-pagination (after p1 only).
        def failing_iter(db_key, page_size=100):
            yield {"id": "p1", "archived": False, "parent": {"database_id": "x"},
                   "properties": {}, "url": None, "created_time": None,
                   "last_edited_time": None}
            raise RuntimeError("Notion API 503: down")

        orig_iter = sync.query_database_iter
        orig_content = sync.page_plain_text
        sync.query_database_iter = failing_iter
        sync.page_plain_text = lambda page_id: ""
        try:
            try:
                sync.sync_once(db_path=db_path, db_keys=("ledger",))
                ok = _check(ok, "sync_database raised on mid-sync error", False,
                            "expected exception, got success")
            except RuntimeError:
                ok = _check(ok, "sync_database raised on mid-sync error", True)
        finally:
            sync.query_database_iter = orig_iter
            sync.page_plain_text = orig_content

        state = _archived_state(db_path)
        # CRITICAL: p2 and p3 must NOT be marked archived — we only saw p1
        # before the error, so we don't know if p2/p3 are still active in Notion.
        ok = _check(ok, "p2 NOT marked archived (partial fetch)", state.get("p2") == 0, str(state))
        ok = _check(ok, "p3 NOT marked archived (partial fetch)", state.get("p3") == 0, str(state))

    print("\n=== Phase 3 sync: archived diff — empty fetch edge case ===")
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "t.db"
        _seed_mirror(db_path, [
            {"notion_page_id": "p1", "task": "only row"},
        ])
        # Notion returned 0 pages. This is indistinguishable from a silent
        # partial-outage response. We now refuse to archive-sweep when count==0
        # to prevent mass-wiping the mirror on transient API failures — the cost
        # of a stale row vastly outweighs silently destroying all study data.
        orig_iter = sync.query_database_iter
        sync.query_database_iter = lambda db_key, page_size=100: iter([])
        try:
            sync.sync_once(db_path=db_path, db_keys=("ledger",))
        finally:
            sync.query_database_iter = orig_iter
        state = _archived_state(db_path)
        ok = _check(ok, "p1 NOT archived on empty fetch (outage protection)",
                    state.get("p1") == 0, str(state))

    print("\n=== Phase 3 sync: page_plain_text block flattening (offline) ===")
    import notion_client_wrapper as notion

    root_blocks = [
        {"id": "b1", "type": "heading_2", "has_children": False,
         "heading_2": {"rich_text": [{"plain_text": "Notes"}]}},
        {"id": "b2", "type": "paragraph", "has_children": False,
         "paragraph": {"rich_text": [{"plain_text": "theta is measured "},
                                     {"plain_text": "from vertical"}]}},
        {"id": "b3", "type": "bulleted_list_item", "has_children": True,
         "bulleted_list_item": {"rich_text": [{"plain_text": "step one"}]}},
        {"id": "b4", "type": "to_do", "has_children": False,
         "to_do": {"checked": True, "rich_text": [{"plain_text": "redo q36"}]}},
        {"id": "b5", "type": "image", "has_children": False, "image": {}},
        {"id": "b6", "type": "paragraph", "has_children": False,
         "paragraph": {"rich_text": []}},
    ]
    child_blocks = [
        {"id": "c1", "type": "paragraph", "has_children": False,
         "paragraph": {"rich_text": [{"plain_text": "nested detail"}]}},
    ]

    def fake_children(block_id, page_size=100):
        if block_id == "page-1":
            return root_blocks
        if block_id == "b3":
            return child_blocks
        return []

    orig_children = notion.get_block_children
    notion.get_block_children = fake_children
    try:
        text = notion.page_plain_text("page-1")
    finally:
        notion.get_block_children = orig_children
    expected = ("Notes\ntheta is measured from vertical\n- step one\n"
                "nested detail\n[x] redo q36")
    ok = _check(ok, "flattens headings/paragraphs/lists/to-dos, skips "
                    "images and empty blocks, recurses into children",
                text == expected, repr(text))

    print("\n=== Phase 3 sync: page_content migration on existing table ===")
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "t.db"
        with sync.connect(db_path) as conn:
            # Old-style table without page_content.
            conn.execute(
                "CREATE TABLE ledger (notion_page_id TEXT PRIMARY KEY, "
                "task TEXT, last_synced_at TEXT NOT NULL DEFAULT '', "
                "raw_json TEXT NOT NULL DEFAULT '{}', "
                "archived INTEGER NOT NULL DEFAULT 0, last_edited_time TEXT)"
            )
            sync.init_db(conn)
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(ledger)")}
        ok = _check(ok, "init_db adds page_content to a pre-existing table",
                    "page_content" in cols, str(sorted(cols)))

    print("\n=== Phase 3 sync: incremental page_content fetch ===")
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "t.db"

        def page(edited):
            return {"id": "p1", "archived": False,
                    "parent": {"database_id": "x"}, "properties": {},
                    "url": None, "created_time": None,
                    "last_edited_time": edited}

        calls = {"n": 0}

        def fake_content(page_id):
            calls["n"] += 1
            return f"body v{calls['n']}"

        orig_iter = sync.query_database_iter
        orig_content = sync.page_plain_text
        sync.page_plain_text = fake_content
        try:
            # First sync: new page -> content fetched.
            sync.query_database_iter = lambda db_key, page_size=100: iter([page("T1")])
            sync.sync_once(db_path=db_path, db_keys=("ledger",))
            # Second sync, unchanged last_edited_time -> no refetch.
            sync.query_database_iter = lambda db_key, page_size=100: iter([page("T1")])
            sync.sync_once(db_path=db_path, db_keys=("ledger",))
            unchanged_calls = calls["n"]
            # Third sync, page edited -> refetched.
            sync.query_database_iter = lambda db_key, page_size=100: iter([page("T2")])
            sync.sync_once(db_path=db_path, db_keys=("ledger",))
            refetch_calls = calls["n"]

            # Fetch failure on a further edit -> old content kept, sync survives.
            def boom(page_id):
                raise RuntimeError("Notion 429")

            sync.page_plain_text = boom
            sync.query_database_iter = lambda db_key, page_size=100: iter([page("T3")])
            sync.sync_once(db_path=db_path, db_keys=("ledger",))
        finally:
            sync.query_database_iter = orig_iter
            sync.page_plain_text = orig_content

        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT page_content FROM ledger WHERE notion_page_id='p1'"
            ).fetchone()
        ok = _check(ok, "content fetched once for new page", unchanged_calls == 1,
                    f"calls={unchanged_calls}")
        ok = _check(ok, "unchanged page not refetched", unchanged_calls == 1)
        ok = _check(ok, "edited page refetched", refetch_calls == 2,
                    f"calls={refetch_calls}")
        ok = _check(ok, "fetch failure keeps old content",
                    row["page_content"] == "body v2", repr(row["page_content"]))

    print("\n" + ("ALL PHASE 3 ARCHIVED-DIFF CHECKS PASSED" if ok
                  else "SOME PHASE 3 ARCHIVED-DIFF CHECKS FAILED"))
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
