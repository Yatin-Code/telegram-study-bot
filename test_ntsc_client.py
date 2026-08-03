"""Pagination tests for the NTSC portal client (offline, mocked transport).

Bug #8: course_results / appeared_results must follow pagination instead of
only ever returning page 1, so replace_results never deletes what wasn't
fetched. The transport is mocked; nothing touches the real portal.
"""

from __future__ import annotations

from ntsc_client import Client


def _client():
    c = Client("u", "p", base_url="https://example.test")
    c.token = "fake-token"  # skip the real login
    return c


def _served(pages, *, meta_fn=None):
    """Return (handler, calls): a fake _request serving ``pages``.

    Each entry of ``pages`` is the list of rows for that page. ``meta_fn``
    receives the data dict (default adds totalPages/totalCount metadata).
    """
    calls = []

    def handler(method, path, body=None):
        calls.append(dict(body or {}))
        page = int((body or {}).get("pageNumber", 1))
        items = pages[page - 1]
        data = {"result": items}
        if meta_fn is not None:
            meta_fn(data)
        else:
            data["totalPages"] = len(pages)
            data["totalCount"] = sum(len(p) for p in pages)
        return {"data": data}

    return handler, calls


def test_course_results_aggregates_all_pages(monkeypatch):
    pages = [[{"id": i} for i in range(100)], [{"id": i} for i in range(100, 175)]]
    handler, calls = _served(pages)
    client = _client()
    monkeypatch.setattr(client, "_request", handler)

    payload = client.course_results(course_id=7)
    rows = payload["data"]["result"]
    assert len(rows) == 175
    assert [r["id"] for r in rows[:2]] == [0, 1]
    assert [r["id"] for r in rows[-2:]] == [173, 174]
    assert [c.get("pageNumber") for c in calls] == [1, 2]
    assert all(c["pageSize"] == 100 for c in calls)


def test_appeared_results_aggregates_all_pages(monkeypatch):
    pages = [[{"examId": 1}, {"examId": 2}], [{"examId": 3}]]
    handler, calls = _served(pages)
    client = _client()
    monkeypatch.setattr(client, "_request", handler)

    payload = client.appeared_results(result_id=42)
    rows = payload["data"]["result"]
    assert len(rows) == 3
    assert [r["examId"] for r in rows] == [1, 2, 3]
    assert [c.get("pageNumber") for c in calls] == [1, 2]


def test_stops_when_page_short_without_pagination_signal(monkeypatch):
    # No totalPages/totalCount/hasMore in the payload: a short page ends it.
    pages = [[{"id": i} for i in range(4)], [{"id": i} for i in range(4, 8)]]

    def handler(method, path, body=None):
        page = int((body or {}).get("pageNumber", 1))
        return {"data": {"result": pages[page - 1]}}

    client = _client()
    monkeypatch.setattr(client, "_request", handler)
    rows = client._fetch_all_pages(
        "/x", {"pageSize": 100}, list_key="result",
    )
    # First page is short (4 < 100): the loop stops after page 1.
    assert [r["id"] for r in rows] == [0, 1, 2, 3]


def test_has_more_false_stops_fetching(monkeypatch):
    calls = []

    def handler(method, path, body=None):
        calls.append((body or {}).get("pageNumber"))
        page = int((body or {}).get("pageNumber", 1))
        return {"data": {"result": [{"id": page}], "hasMore": page < 3}}

    client = _client()
    monkeypatch.setattr(client, "_request", handler)
    rows = client._fetch_all_pages("/x", {"pageSize": 20}, list_key="result")
    assert [r["id"] for r in rows] == [1, 2, 3]
    assert calls == [1, 2, 3]


def test_short_page_with_has_more_true_still_fetches_next(monkeypatch):
    """A short page is not a stop signal when an explicit hasMore says more."""
    calls = []

    def handler(method, path, body=None):
        page = int((body or {}).get("pageNumber", 1))
        calls.append(page)
        return {"data": {"result": [{"id": page}], "hasMore": page < 2}}

    client = _client()
    monkeypatch.setattr(client, "_request", handler)
    rows = client._fetch_all_pages("/x", {"pageSize": 100}, list_key="result")
    # Both pages are short (1 < 100) yet hasMore=true drives the fetch on.
    assert [r["id"] for r in rows] == [1, 2]
    assert calls == [1, 2]


def test_broken_echo_cannot_infinite_loop(monkeypatch):
    """A portal that keeps echoing a growing totalPages is bounded by max_pages."""
    import ntsc_client

    original = ntsc_client.Client._fetch_all_pages
    max_seen = []

    def counting(self, path, body, *, list_key):
        max_seen.append(True)
        return original(self, path, body, list_key=list_key)

    monkeypatch.setattr(ntsc_client.Client, "_fetch_all_pages", counting)

    def handler(method, path, body=None):
        page = int((body or {}).get("pageNumber", 1))
        # Broken echo: totalPages always one beyond the requested page.
        return {"data": {"result": [{"id": page}], "totalPages": page + 1}}

    client = _client()
    monkeypatch.setattr(client, "_request", handler)
    rows = client._fetch_all_pages("/x", {"pageSize": 100}, list_key="result")
    assert len(rows) == 500  # max_pages bound, not an infinite loop
    assert len(max_seen) == 1


def test_unexpected_shape_returns_what_was_fetched(monkeypatch):
    calls = []

    def handler(method, path, body=None):
        page = int((body or {}).get("pageNumber", 1))
        calls.append(page)
        if page == 1:
            return {"data": {"result": [{"id": 1}]}}
        return {"data": {"result": "not-a-list"}}

    client = _client()
    monkeypatch.setattr(client, "_request", handler)
    # pageSize 1 makes the single-row page 1 "full", so the loop advances to
    # page 2 and only then discovers the malformed shape.
    rows = client._fetch_all_pages("/x", {"pageSize": 1}, list_key="result")
    assert [r["id"] for r in rows] == [1]
    assert calls == [1, 2]
