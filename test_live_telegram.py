#!/usr/bin/env python3
"""
Live end-to-end test: real Telegram → real bot → real LLM → real Notion.

USAGE:
  python3 test_live_telegram.py

FIRST RUN:
  Prompts for api_id, api_hash (from https://my.telegram.org),
  phone number, then the verification code — all inline.
  Saves the session and immediately runs the live test.

SUBSEQUENT RUNS:
  Uses the saved session, runs the test directly — no prompts.

PREREQUISITES:
  - pip install telethon
  - Bot must be running (python3 bot.py or systemd)
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from telethon.sync import TelegramClient
from telethon.errors import SessionPasswordNeededError

PROJECT_ROOT = Path(__file__).resolve().parent
SESSION_FILE = str(PROJECT_ROOT / "test_session")
BOT_USERNAME = "yatSentinalbot"
REPLY_TIMEOUT = 90


def _load_env() -> dict[str, str]:
    env = {}
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def _save_to_env(api_id: int, api_hash: str) -> None:
    env_path = PROJECT_ROOT / ".env"
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    has_id = any("TELETHON_API_ID" in l for l in lines)
    if not has_id:
        with open(env_path, "a") as f:
            f.write(f"\nTELETHON_API_ID={api_id}\n")
            f.write(f"TELETHON_API_HASH={api_hash}\n")


def _ensure_session() -> TelegramClient:
    """Connect to Telethon, prompting for login if no session exists."""
    env = _load_env()
    api_id = int(env.get("TELETHON_API_ID", 0))
    api_hash = env.get("TELETHON_API_HASH", "")

    session_exists = Path(SESSION_FILE + ".session").exists()

    if session_exists and api_id and api_hash:
        client = TelegramClient(SESSION_FILE, api_id, api_hash)
        client.connect()
        if client.is_user_authorized():
            return client
        print("  Session expired — logging in again.\n")
        client.disconnect()

    if not api_id or not api_hash:
        print("=" * 60)
        print("  FIRST-TIME SETUP — Telegram user login")
        print("=" * 60)
        print()
        print("  You need api_id and api_hash from https://my.telegram.org")
        print("  (API development tools → create a new app)")
        print()
        api_id = int(input("  api_id: ").strip())
        api_hash = input("  api_hash: ").strip()
        _save_to_env(api_id, api_hash)

    print()
    phone = input("  Phone number (with country code, e.g. +91...): ").strip()
    print()

    client = TelegramClient(SESSION_FILE, api_id, api_hash)
    client.connect()

    if not client.is_user_authorized():
        print(f"  Sending verification code to {phone}...")
        client.send_code_request(phone)
        code = input("  Verification code: ").strip()
        print()
        try:
            client.sign_in(phone=phone, code=code)
        except SessionPasswordNeededError:
            password = input("  2FA password: ").strip()
            print()
            client.sign_in(password=password)

    me = client.get_me()
    print(f"  ✅ Logged in as {me.first_name} (id={me.id})")
    print(f"  Session saved. Next run skips login.\n")
    return client


def _check(ok: bool, label: str, cond: bool, extra: str = "") -> bool:
    print(f"  [{'OK ' if cond else 'BAD'}] {label}{(' -> ' + extra) if extra else ''}")
    return ok and cond


def _wait_for_reply(client: TelegramClient, after_id: int, timeout: int = REPLY_TIMEOUT) -> str | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        msgs = client.get_messages(BOT_USERNAME, limit=3)
        if msgs:
            for m in msgs:
                if m.id > after_id and m.out is False:
                    return m.text
        time.sleep(1.5)
    return None


def run_live_test() -> int:
    print("=" * 60)
    print("  LIVE TELEGRAM TEST — bot must be running!")
    print("=" * 60)
    print()

    client = _ensure_session()

    me = client.get_me()
    print(f"  User: {me.first_name} (id={me.id})")
    print(f"  Bot: @{BOT_USERNAME}")
    print()

    ok = True
    passed = 0
    failed = 0

    tests = [
        {
            "label": "/health",
            "msg": "/health",
            "timeout": 30,
            "validate": lambda r: r is not None and len(r) > 0,
            "desc": "bot responded",
        },
        {
            "label": "ask: unresolved doubts count",
            "msg": "how many doubts are unresolved?",
            "timeout": 90,
            "validate": lambda r: r is not None and any(c.isdigit() for c in r),
            "desc": "reply has a number",
        },
        {
            "label": "ask: compare subjects",
            "msg": "compare my chemistry vs physics accuracy",
            "timeout": 90,
            "validate": lambda r: r is not None and "chem" in r.lower() and "physics" in r.lower(),
            "desc": "mentions both subjects",
        },
        {
            "label": "set_context",
            "msg": "starting physics, chapter kinematics",
            "timeout": 30,
            "validate": lambda r: r is not None and ("context" in r.lower() or "set" in r.lower()),
            "desc": "context acknowledged",
        },
        {
            "label": "query: list doubts",
            "msg": "show me my doubts",
            "timeout": 90,
            "validate": lambda r: r is not None and (
                "doubt" in r.lower() or "no " in r.lower() or "0" in r or "unresolved" in r.lower()
            ),
            "desc": "response about doubts",
        },
        {
            "label": "ask: cognitive yield for past year",
            "msg": "what's my cognitive yield for the past year?",
            "timeout": 90,
            "validate": lambda r: r is not None and (
                "yield" in r.lower() or any(c.isdigit() for c in r)
            ),
            "desc": "reply has data",
        },
        {
            "label": "ask: which chapters due for revision",
            "msg": "which chapters are due for revision?",
            "timeout": 90,
            "validate": lambda r: r is not None and (
                any(kw in r.lower() for kw in ["due", "revision", "chapter", "pending", "no "])
            ),
            "desc": "response about revision",
        },
        {
            "label": "ask: total time spent studying",
            "msg": "give me my total time spent studying",
            "timeout": 90,
            "validate": lambda r: r is not None and (
                any(c.isdigit() for c in r) and
                any(kw in r.lower() for kw in ["min", "hour", "time", "spent", "total"])
            ),
            "desc": "reply has time data",
        },
    ]

    for t in tests:
        print(f"--- {t['label']} ---")
        print(f"  Sending: {t['msg']}")
        msg = client.send_message(BOT_USERNAME, t["msg"])
        reply = _wait_for_reply(client, msg.id, timeout=t["timeout"])
        if reply:
            print(f"  Reply: {reply[:300]}")
        else:
            print(f"  Reply: (timeout after {t['timeout']}s)")
        cond = t["validate"](reply)
        ok = _check(ok, t["desc"], cond, (reply[:80] if reply else "no reply"))
        if cond:
            passed += 1
        else:
            failed += 1
        print()

    client.disconnect()

    print("=" * 60)
    print(f"  LIVE TELEGRAM TEST: {passed} passed, {failed} failed (of {passed + failed})")
    print("=" * 60)
    return 0 if ok else 1


def main() -> int:
    return run_live_test()


if __name__ == "__main__":
    sys.exit(main())
