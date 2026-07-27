"""Auto-mock LLM/HTTP transport for the test suite.

All tests use fast mock responses by default.
Tests that need real APIs (Notion + real LLM) must use @pytest.mark.live
to opt out of mocks.

Usage:
    pytest           # fast, mocked (default)
    pytest -m live   # real calls (explicit)
    SKIP_MOCK=1 pytest  # disable auto-mock (debugging)
"""

from __future__ import annotations

import json
import os
from typing import Any
import pytest

from llm.router import LLMResponse


# ---------------------------------------------------------------------------
# Mock response factory — returns a real LLMResponse so route_id etc work.
# ---------------------------------------------------------------------------

def _make_mock_response(user_text: str = "") -> LLMResponse:
    text = f"Mocked: {user_text[:80] if user_text else 'test query'}"
    return LLMResponse(
        text=json.dumps({
            "text": text,
            "parse_mode": "markdown",
            "response_type": "text",
            "inline_buttons": [],
            "reply_options": [],
            "poll_question": "",
            "poll_options": [],
        }),
        value={"text": text},
        route_id="_mock_",
        provider="_mock_",
        model="_mock_",
        latency_ms=0,
        attempts=1,
    )


# ---------------------------------------------------------------------------
# Pytest configuration
# ---------------------------------------------------------------------------

def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live: requires configured external services (Notion, real LLM API). "
        "Excluded from the default test run (-m 'not live').",
    )


# ---------------------------------------------------------------------------
# Auto-mock injection — add fixture to every non-live test
# --------------------------------------------------------------------------

def pytest_collection_modifyitems(config, items):
    if os.environ.get("SKIP_REAL_LLM") == "1" or os.environ.get("SKIP_MOCK") == "1":
        return

    # Tests in llm/ and test_failure_drills have their own patching; don't
    # override them with the blanket auto-mock.
    _SKIP_MODULES = {"llm", "test_failure_drills", "test_adaptive_reminders"}

    for item in items:
        if item.get_closest_marker("live"):
            continue
        if "_llm_auto_mock" in item.fixturenames:
            continue
        fspath = str(getattr(item, "fspath", "") or getattr(item, "path", ""))
        if any(seg in fspath for seg in _SKIP_MODULES):
            continue
        item.fixturenames.append("_llm_auto_mock")


# ---------------------------------------------------------------------------
# The auto-mock fixture — applied to all non-live tests
# --------------------------------------------------------------------------

@pytest.fixture(scope="function")
def _llm_auto_mock(monkeypatch):
    """Patch llm.router.complete for every non-live test.

    Makes the test suite run in seconds instead of minutes.
    Tests marked @pytest.mark.live get real LLM calls.
    """
    import llm.router as router_mod

    def _mock_complete(request, **kwargs):
        user_text = ""
        if request.messages:
            for msg in reversed(request.messages):
                if msg.get("role") == "user":
                    user_text = msg["content"]
                    break
        return _make_mock_response(user_text)

    monkeypatch.setattr(router_mod, "complete", _mock_complete, raising=False)