#!/usr/bin/env python3
"""Stress-test Eaon models with the real agent system prompt + history."""

from __future__ import annotations

import os
import time

import httpx
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from agent import _build_system_prompt  # noqa: E402
import conversation_history  # noqa: E402

MODELS = ["gemini-3.5", "deepseek-v4-pro", "gemini-3", "gemini-3.1-flash-lite"]
CHAT_ID = int(os.environ.get("TELEGRAM_ALLOWED_USER_ID", "8685767260"))
USER_TEXT = "Check my recent scores"


def main() -> None:
    key = os.environ["LLM_API_KEY"]
    base = os.environ.get("LLM_BASE_URL", "https://api.eaon.dev/v1").rstrip("/")
    system = _build_system_prompt(CHAT_ID, user_text=USER_TEXT)
    hist = conversation_history.recent_messages(CHAT_ID, limit=15)
    msgs = [
        {"role": "system", "content": system},
        *hist,
        {"role": "user", "content": USER_TEXT},
    ]
    total_chars = sum(len(m["content"]) for m in msgs)
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "User-Agent": "study-bot-test/1.0",
    }
    print(f"msgs={len(msgs)} chars={total_chars} approx_tokens~{total_chars // 4}")
    print(f"base={base}")
    for model in MODELS:
        for stream in (False, True):
            mode = "stream" if stream else "nostream"
            start = time.monotonic()
            payload = {
                "model": model,
                "messages": msgs,
                "temperature": 0.0,
                "max_tokens": 512,
                "stream": stream,
            }
            try:
                with httpx.Client(timeout=90) as client:
                    if stream:
                        with client.stream(
                            "POST",
                            f"{base}/chat/completions",
                            json=payload,
                            headers=headers,
                        ) as resp:
                            if resp.status_code != 200:
                                body = resp.read().decode()[:200]
                                ms = int((time.monotonic() - start) * 1000)
                                print(f"{model:25s} {mode:8s} FAIL {resp.status_code} {ms}ms {body}")
                                continue
                            lines = 0
                            for line in resp.iter_lines():
                                if line:
                                    lines += 1
                            ms = int((time.monotonic() - start) * 1000)
                            print(f"{model:25s} {mode:8s} OK   200 {ms}ms lines={lines}")
                    else:
                        resp = client.post(
                            f"{base}/chat/completions",
                            json=payload,
                            headers=headers,
                        )
                        ms = int((time.monotonic() - start) * 1000)
                        if resp.status_code != 200:
                            print(f"{model:25s} {mode:8s} FAIL {resp.status_code} {ms}ms {resp.text[:200]}")
                            continue
                        data = resp.json()
                        text = data["choices"][0]["message"]["content"]
                        usage = data.get("usage", {})
                        print(
                            f"{model:25s} {mode:8s} OK   200 {ms}ms "
                            f"len={len(text)} tok={usage}"
                        )
            except Exception as exc:  # noqa: BLE001
                ms = int((time.monotonic() - start) * 1000)
                print(f"{model:25s} {mode:8s} EXC  {ms}ms {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
