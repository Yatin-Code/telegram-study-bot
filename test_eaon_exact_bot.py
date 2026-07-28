#!/usr/bin/env python3
"""Reproduce the exact bot agent LLM call path."""

from __future__ import annotations

import os
import time
import traceback

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from agent import _build_system_prompt  # noqa: E402
import conversation_history  # noqa: E402
from llm.router import LLMRequest, complete, stream_complete, _legacy_call, _legacy_stream, _legacy_models  # noqa: E402
from config import settings  # noqa: E402

CHAT_ID = int(os.environ.get("TELEGRAM_ALLOWED_USER_ID", "8685767260"))
USER_TEXT = "Check my recent scores"


def main() -> None:
    print("models:", _legacy_models())
    print("primary:", settings.llm_model())
    print("fallbacks:", settings.llm_fallback_models())
    system = _build_system_prompt(CHAT_ID, user_text=USER_TEXT)
    hist = conversation_history.recent_messages(CHAT_ID, limit=15)
    msgs = [
        {"role": "system", "content": system},
        *hist,
        {"role": "user", "content": USER_TEXT},
    ]
    print(f"msgs={len(msgs)} chars={sum(len(m['content']) for m in msgs)}")

    for max_tok in (512, 2048):
        req = LLMRequest(
            messages=msgs,
            purpose="domain",
            max_output_tokens=max_tok,
            temperature=0.0,
        )
        print(f"\n=== complete max_tokens={max_tok} ===")
        t0 = time.monotonic()
        try:
            resp = complete(req)
            print(f"OK {int((time.monotonic()-t0)*1000)}ms route={resp.route_id} model={resp.model} len={len(resp.text)}")
            print("preview:", resp.text[:200].replace("\n", " "))
        except Exception as exc:
            print(f"FAIL {int((time.monotonic()-t0)*1000)}ms {type(exc).__name__}: {exc}")
            traceback.print_exc()

        print(f"\n=== stream max_tokens={max_tok} ===")
        t0 = time.monotonic()
        try:
            parts = list(stream_complete(req))
            text = "".join(parts)
            print(f"OK {int((time.monotonic()-t0)*1000)}ms chunks={len(parts)} len={len(text)}")
            print("preview:", text[:200].replace("\n", " "))
        except Exception as exc:
            print(f"FAIL {int((time.monotonic()-t0)*1000)}ms {type(exc).__name__}: {exc}")

        print(f"\n=== raw _legacy_call max_tokens={max_tok} ===")
        t0 = time.monotonic()
        try:
            text, ms = _legacy_call(req)
            print(f"OK wall={int((time.monotonic()-t0)*1000)}ms reported={ms}ms len={len(text)}")
            print("preview:", text[:200].replace("\n", " "))
        except Exception as exc:
            print(f"FAIL {int((time.monotonic()-t0)*1000)}ms {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
