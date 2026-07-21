#!/usr/bin/env python3
"""
FULL live E2E test: drives the production bot through ~45 real interactions
from the owner's own Telegram account (Telethon) and logs EVERYTHING —
every message sent, button clicked, reply received, and a PASS/FAIL verdict
— to live_e2e_<timestamp>.log plus stdout.

Covers: core commands, /settings editing, /memory edit+undo, remember
(preference + commitment), context + real logging (writes ONE real ledger
entry + ONE real doubt to Notion, then verifies them back through the SQL
answer loop), follow-ups, /jobs full lifecycle (create → run now → edit →
pause → delete), /setup incl. the `ai` escape hatch, analytics questions,
then cleanup (pause commitment, forget pref, delete job).

USAGE:
  python3 test_live_e2e.py --login   # one-time interactive Telegram login
  python3 test_live_e2e.py           # run the full suite (no prompts)

The bot must be running (production systemd service on SentinelVM is fine —
this script only talks to Telegram).
"""

from __future__ import annotations

import datetime as dt
import sys
import time
from pathlib import Path

from test_live_telegram import BOT_USERNAME, _ensure_session  # reuse harness

PROJECT_ROOT = Path(__file__).resolve().parent
LOG_FILE = PROJECT_ROOT / f"live_e2e_{dt.datetime.now():%Y%m%d_%H%M}.log"

STEP_TIMEOUT = 100          # LLM steps can take a while on the VM
SETTLE = 2.0                # settle time after clicks/edits

results: list[tuple[str, bool, str]] = []
client = None
_last_id = 0


def log(kind: str, text: str) -> None:
    line = f"{dt.datetime.now():%H:%M:%S} [{kind:^5}] {text}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def newest_incoming():
    msgs = client.get_messages(BOT_USERNAME, limit=6)
    incoming = [m for m in msgs if not m.out]
    return incoming[0] if incoming else None


# Transient/status texts the bot edits later, plus one-shot notices — never
# treat these as the actual reply to a step.
_NOISE_PREFIXES = (
    "🔍",                 # answer-loop "Thinking…" trace (edited into the answer)
    "⏰ Working out",      # job parse status (edited into the draft)
    "🤖 Working out",      # setup-AI parse status (edited into the plan)
    "🔄 Syncing",          # /sync status (edited into the result)
    "🚀 First time?",      # one-shot onboarding hint (a second real reply follows)
)


def _is_noise(text: str) -> bool:
    return any((text or "").startswith(p) for p in _NOISE_PREFIXES)


def wait_reply(after_id: int, timeout: int = STEP_TIMEOUT, allow_thinking: bool = False):
    """Newest incoming message with id > after_id; waits out status edits."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        m = newest_incoming()
        if m and m.id > after_id and (allow_thinking or not _is_noise(m.text or "")):
            time.sleep(SETTLE)
            m2 = client.get_messages(BOT_USERNAME, ids=m.id)
            final = m2 or m
            if allow_thinking or not _is_noise(final.text or ""):
                return final
        time.sleep(1.5)
    return None


def send(text: str, timeout: int = STEP_TIMEOUT):
    global _last_id
    sent = client.send_message(BOT_USERNAME, text)
    _last_id = sent.id
    log("SENT", text)
    reply = wait_reply(_last_id, timeout)
    if reply is not None:
        _last_id = max(_last_id, reply.id)
        log("RECV", (reply.text or "")[:600].replace("\n", " ⏎ "))
    else:
        log("RECV", "<no reply within timeout>")
    return reply


def click(msg, label_part: str, wait_new: bool = False, new_timeout: int = 25):
    """Click the inline button whose text contains label_part.

    Confirm-style buttons sometimes EDIT the message in place and sometimes
    send a NEW message — with wait_new=True we wait briefly for a new one,
    then fall back to refetching the edit, so neither pattern stalls us.
    """
    global _last_id
    if msg is None or not msg.buttons:
        log("CLICK", f"<no buttons to click for {label_part!r}>")
        return None
    for row in msg.buttons:
        for btn in row:
            if label_part.lower() in (btn.text or "").lower():
                log("CLICK", f"[{btn.text}] on msg {msg.id}")
                try:
                    msg.click(text=btn.text)
                except Exception as exc:
                    log("CLICK", f"click failed: {exc}")
                    return None
                time.sleep(SETTLE + 1)
                if wait_new:
                    fresh = wait_reply(_last_id, timeout=new_timeout)
                    if fresh:
                        _last_id = max(_last_id, fresh.id)
                        log("RECV", (fresh.text or "")[:500].replace("\n", " ⏎ "))
                        return fresh
                refreshed = client.get_messages(BOT_USERNAME, ids=msg.id)
                log("EDIT", (refreshed.text or "")[:500].replace("\n", " ⏎ ") if refreshed else "<gone>")
                return refreshed
    log("CLICK", f"<button containing {label_part!r} not found: "
        f"{[b.text for row in msg.buttons for b in row]}>")
    return None


def check(label: str, cond: bool, detail: str = "") -> None:
    results.append((label, bool(cond), detail))
    log("PASS" if cond else "FAIL", f"{label}{(' — ' + detail) if detail else ''}")


def text_of(msg) -> str:
    return (msg.text or "") if msg else ""


def main() -> int:
    global client
    client = _ensure_session()
    if "--login" in sys.argv:
        print("Login complete — session saved. Run again without --login.")
        return 0
    me = client.get_me()
    log("INFO", f"Running as {me.first_name} (id={me.id}) against @{BOT_USERNAME}")
    log("INFO", f"Transcript: {LOG_FILE.name}")

    # ---------------- 0. Pre-flight cleanup (ignore outcomes; makes reruns clean) ----------------
    log("INFO", "Pre-flight cleanup of any leftovers from earlier runs…")
    send("/goal cancel Daily PYQs", timeout=30)
    send("/forget testing this bot late at night", timeout=30)
    send("/newsession", timeout=30)
    results.clear()

    # ---------------- A. Core commands ----------------
    r = send("/start")
    check("A1 /start", "Study Logger Bot" in text_of(r) or "Setup" in text_of(r))
    r = send("/help")
    check("A2 /help lists new commands", "/jobs" in text_of(r) and "/memory" in text_of(r))
    r = send("/health")
    check("A3 /health", "Bot Health" in text_of(r) and "ledger=" in text_of(r))
    r = send("/sync", timeout=120)
    check("A4 /sync", "Synced" in text_of(r) or "✅" in text_of(r))

    # ---------------- B. /settings interactive edit ----------------
    r = send("/settings")
    check("B1 /settings hub", "tap a category" in text_of(r).lower() and bool(r and r.buttons))
    cat = click(r, "Query & memory")
    check("B2 category view", "Query loop ceiling" in text_of(cat))
    edit = click(cat, "Query loop ceiling")
    check("B3 edit prompt", "Send the new value" in text_of(edit))
    r = send("15")
    check("B4 set value", "Query loop ceiling → 15" in text_of(r))
    r = send("/settings")
    cat = click(r, "Query & memory")
    edit = click(cat, "Query loop ceiling")
    reset = click(edit, "Reset")
    check("B5 reset to default", "reset to default" in text_of(reset).lower())

    # ---------------- C. remember: preference + /memory edit/undo ----------------
    r = send("remember that i prefer testing this bot late at night")
    check("C1 pref preview", "Preference" in text_of(r) and bool(r and r.buttons))
    saved = click(r, "Save", wait_new=False)
    check("C2 pref saved", "remembered" in text_of(saved).lower())
    r = send("/memory")
    check("C3 /memory shows pref", "testing this bot late at night" in text_of(r))
    removed = click(r, "testing this bot")     # 🗑 row
    check("C4 pref removed", "testing this bot late at night" not in text_of(removed)
          or "Undo" in str([b.text for row in (removed.buttons or []) for b in row]))
    undone = click(removed, "Undo")
    check("C5 undo restores", "testing this bot late at night" in text_of(undone))
    raw = click(undone, "Raw view")
    check("C6 raw view", "injected" in text_of(raw).lower() or "USER PREFERENCES" in text_of(raw))
    click(raw, "Back")

    # ---------------- D. commitment ----------------
    r = send("from now on i will do pyqs every day")
    check("D1 commitment draft", "Commitment draft" in text_of(r))
    saved = click(r, "Save")
    check("D2 commitment saved", "verify it nightly" in text_of(saved) or "Commitment saved" in text_of(saved))
    r = send("/remember")
    check("D3 /remember lists", "PYQ" in text_of(r).upper())

    # ---------------- E. context + REAL log to Notion + verify back ----------------
    r = send("starting EB-1 physics kinematics mle", timeout=120)
    check("E1 set context + briefing", "Context set" in text_of(r))
    r = send("done, solved 10 questions 8 correct in 25 mins", timeout=120)
    check("E2 log preview", bool(r and r.buttons) and "10" in text_of(r))
    confirmed = click(r, "Confirm", wait_new=True)
    check("E3 log committed", "logged" in text_of(confirmed).lower()
          or "✅" in text_of(confirmed) or "saved" in text_of(confirmed).lower())
    time.sleep(5)
    r = send("/sync", timeout=120)
    r = send("how many questions did i attempt today?", timeout=150)
    check("E4 answer from mirror", "10" in text_of(r))
    r = send("and how many of those were correct?", timeout=150)
    check("E5 follow-up window", "8" in text_of(r))

    # ---------------- F. doubt ----------------
    r = send("doubt: why does the sign flip in relative velocity E2E-test case", timeout=120)
    if r and r.buttons:
        confirmed = click(r, "Confirm", wait_new=True)
        check("F1 doubt logged", "✅" in text_of(confirmed) or "logged" in text_of(confirmed).lower())
    else:
        check("F1 doubt logged", "✅" in text_of(r) or "doubt" in text_of(r).lower())
    r = send("list doubts", timeout=150)
    check("F2 doubt listed", "relative velocity" in text_of(r).lower())

    # ---------------- G. /jobs lifecycle ----------------
    r = send("/jobs")
    check("G1 /jobs view", "scheduled jobs" in text_of(r).lower())
    r = send("/jobs every day at 23:57 tell me my total questions attempted today", timeout=120)
    check("G2 job draft", "Job draft" in text_of(r))
    created = click(r, "Create", wait_new=True)
    check("G3 job created", "Job created" in text_of(created))
    r = send("/jobs")
    detail = click(r, "⚙")
    check("G4 job detail", "Does:" in text_of(detail))
    ran = click(detail, "Run now", wait_new=True, new_timeout=90)
    check("G5 run now answers", "⏰" in text_of(ran) or "🔔" in text_of(ran),
          "must be the job's delivery message, not the detail view")
    r = send("/jobs")
    detail = click(r, "⚙")
    prompt = click(detail, "Time")
    check("G6 time edit prompt", "HH:MM" in text_of(prompt))
    r = send("23:58")
    check("G7 time updated", "23:58" in text_of(r))
    r = send("/jobs")
    detail = click(r, "⚙")
    paused = click(detail, "Pause")
    check("G8 paused", "paused" in text_of(paused).lower())
    resumed = click(paused, "Resume")
    check("G9 resumed", "active" in text_of(resumed).lower())
    delview = click(resumed, "Delete")
    deleted = click(delview, "Yes, delete")
    check("G10 deleted", "Deleted" in text_of(deleted) or "No jobs yet" in text_of(deleted))

    # ---------------- H. /setup + ai hatch ----------------
    r = send("/setup")
    check("H1 setup hub", "Setup" in text_of(r) and bool(r and r.buttons))
    prefs_sec = click(r, "Preferences")
    check("H2 section prompt", "one per message" in text_of(prefs_sec).lower()
          or "study facts" in text_of(prefs_sec).lower())
    r = send("ai you know what, skip this section for me please", timeout=120)
    check("H3 ai hatch plan", "🤖" in text_of(r) and bool(r and r.buttons))
    done = click(r, "Confirm")  # bot EDITS this message to "🤖 Done" (hub arrives separately)
    check("H4 ai actions applied", "Done" in text_of(done) or "skipped" in text_of(done).lower())

    # ---------------- I. analytics / reports ----------------
    r = send("/goal")
    check("I1 /goal lists commitment", "PYQ" in text_of(r).upper())
    r = send("/today", timeout=150)
    check("I2 /today", "Plan" in text_of(r))
    r = send("/weekly", timeout=150)
    check("I3 /weekly", len(text_of(r)) > 20)
    r = send("what's my accuracy this week?", timeout=150)
    check("I4 analytics answer", len(text_of(r)) > 15 and not text_of(r).startswith("⚠️"))

    # ---------------- J. cleanup ----------------
    r = send("/goal cancel Daily PYQs")
    check("J1 commitment cancelled", "Cancelled" in text_of(r) or "cancel" in text_of(r).lower())
    r = send("/forget testing this bot late at night")
    check("J2 pref forgotten", "Forgotten" in text_of(r))
    r = send("/newsession")
    check("J3 context cleared", "cleared" in text_of(r).lower())

    # ---------------- summary ----------------
    passed = sum(1 for _, ok, _ in results if ok)
    log("INFO", "=" * 50)
    for label, ok, detail in results:
        if not ok:
            log("INFO", f"FAILED: {label} {detail}")
    log("INFO", f"SUMMARY: {passed}/{len(results)} checks passed. "
        f"Note: one real ledger entry + one real doubt were written to Notion "
        f"(marked E2E where possible) — delete them there if unwanted.")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
