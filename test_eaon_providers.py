#!/usr/bin/env python3
"""Test all 4 Eaon gateway models with simple + large payloads."""

import httpx
import json
import time
import os

# Load .env
from dotenv import load_dotenv
load_dotenv()

LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.eaon.dev/v1")
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "openai")

MODELS = ["gemini-3.5", "deepseek-v4-pro", "gemini-3", "gemini-3.1-flash-lite"]

HEADERS = {
    "Authorization": f"Bearer {LLM_API_KEY}",
    "Content-Type": "application/json",
    "User-Agent": "study-bot-test/1.0",
}

SIMPLE_MESSAGES = [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Say hello in exactly 3 words."},
]

# Large payload simulating our Telegram agent context (~4k tokens)
LARGE_PAYLOAD_CONTEXT = """You are SENTINEL, an AI study coach for a JEE aspirant. Today is July 28, 2026 (Monday).

ACTIVE GOALS:
- Goal 1: Score 99.5 percentile in JEE Mains April 2027
  Status: in_progress | Priority: p0 | Deadline: 2027-04-15
- Goal 2: Complete Class 12 Physics syllabus by October 2026
  Status: in_progress | Priority: p1 | Deadline: 2026-10-31

ACTIVE WORK ITEMS (due today/tomorrow):
- [ ] Solve JEE Mains 2025 Physics Paper 2 (2h) — due 2026-07-28
- [ ] Revise Electrostatics formulas (1h) — due 2026-07-28
- [ ] Complete HC Verma Ch.31 problems — due 2026-07-29
- [ ] Start Thermodynamics chapter reading — due 2026-07-29

EXAM SCHEDULE:
- JEE Mains Session 1: 2027-01-20 (176 days away)
- JEE Mains Session 2: 2027-04-15 (261 days away)
- JEE Advanced: 2027-06-01 (308 days away)
- CBSE Board Exams: 2027-02-15 (202 days away)

RECENT DOUBT ATTEMPTS (last 5):
1. Q: "Why does electric field inside a conductor vanish?"
   Topic: Electrostatics | Correct: Yes | Attempted: 2026-07-27
2. Q: "Derive the expression for capacitance of a parallel plate capacitor"
   Topic: Electrostatics | Correct: No | Attempted: 2026-07-27
3. Q: "What is the difference between EMF and potential difference?"
   Topic: Current Electricity | Correct: Yes | Attempted: 2026-07-26
4. Q: "Why does the resistance of a wire increase with temperature?"
   Topic: Current Electricity | Correct: Yes | Attempted: 2026-07-26
5. Q: "State and prove Gauss's law for electric fields"
   Topic: Electrostatics | Correct: No | Attempted: 2026-07-25

DAILY PLAN (today):
- 06:00-07:00: Morning revision (Electrostatics formulas)
- 09:00-11:00: Physics problem solving (JEE Mains papers)
- 14:00-16:00: New chapter study (Thermodynamics intro)
- 19:00-21:00: Doubt resolution + homework
- 21:00-22:00: Daily review + planning

USER PREFERENCES:
- Timezone: Asia/Kolkata
- Preferred study hours: 06:00-22:00
- Difficulty preference: Progressive (start easy, ramp up)
- Explanation style: Step-by-step with examples
- Language: English with Hindi explanations when stuck

CONVERSATION MEMORY (last 5 turns):
1. User: "How many hours should I study for JEE?"
   Bot: "For JEE preparation, aim for 8-10 focused hours daily. Quality matters more than quantity. Use the Pomodoro technique: 50 min study + 10 min break."
2. User: "I'm struggling with Electrostatics"
   Bot: "Electrostatics is foundational. Start with Coulomb's law, then Gauss's law. Practice 20-30 problems daily. Focus on understanding the concept, not just formulas."
3. User: "What's my current study streak?"
   Bot: "You've been studying for 12 consecutive days! Great momentum. Keep it up."
4. User: "Should I solve previous year papers now?"
   Bot: "Yes, but focus on topic-wise practice first. Save full-length mock tests for 2-3 months before the exam."
5. User: "I feel demotivated"
   Bot: "Remember why you started. You're 176 days away from JEE Mains. That's enough time to improve significantly if you stay consistent."

TASK: Based on the above context, provide a brief motivational message (2-3 sentences) that references at least 2 specific data points from the context."""

LARGE_MESSAGES = [
    {"role": "system", "content": LARGE_PAYLOAD_CONTEXT},
    {"role": "user", "content": "I'm feeling overwhelmed with my preparation. What should I focus on today?"},
]


def test_model(model: str, messages: list, label: str, timeout: float = 30) -> dict:
    """Test a model with given messages. Returns result dict."""
    url = LLM_BASE_URL.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.0,
        "max_tokens": 256,
    }
    start = time.monotonic()
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, json=payload, headers=HEADERS)
            latency_ms = int((time.monotonic() - start) * 1000)
            if resp.status_code != 200:
                return {
                    "model": model,
                    "label": label,
                    "status": "error",
                    "http_status": resp.status_code,
                    "latency_ms": latency_ms,
                    "error": resp.text[:300],
                }
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
            return {
                "model": model,
                "label": label,
                "status": "ok",
                "http_status": 200,
                "latency_ms": latency_ms,
                "response_len": len(content),
                "response_preview": content[:120],
                "prompt_tokens": usage.get("prompt_tokens", "?"),
                "completion_tokens": usage.get("completion_tokens", "?"),
            }
    except httpx.TimeoutException:
        return {
            "model": model,
            "label": label,
            "status": "timeout",
            "latency_ms": int((time.monotonic() - start) * 1000),
            "error": f"timed out after {timeout}s",
        }
    except Exception as e:
        return {
            "model": model,
            "label": label,
            "status": "exception",
            "latency_ms": int((time.monotonic() - start) * 1000),
            "error": str(e)[:300],
        }


def main():
    print(f"Provider: {LLM_PROVIDER} | Base: {LLM_BASE_URL}")
    print(f"Models: {MODELS}")
    print(f"API key: {'set (' + LLM_API_KEY[:8] + '...)' if LLM_API_KEY else 'MISSING'}")
    print("=" * 70)

    results = []

    for model in MODELS:
        print(f"\n--- {model}: simple hello ---")
        r = test_model(model, SIMPLE_MESSAGES, "simple", timeout=30)
        results.append(r)
        status_icon = "✅" if r["status"] == "ok" else "❌"
        print(f"  {status_icon} {r['status']} | {r.get('http_status','')} | {r['latency_ms']}ms")
        if r["status"] == "ok":
            print(f"     tokens: {r.get('prompt_tokens','?')}+{r.get('completion_tokens','?')} | len={r['response_len']}")
            print(f"     response: {r['response_preview']}")
        else:
            print(f"     error: {r.get('error','')[:200]}")

    print("\n" + "=" * 70)
    print("LARGE PAYLOAD TESTS (simulating Telegram agent context ~4k tokens)")
    print("=" * 70)

    for model in MODELS:
        print(f"\n--- {model}: large payload ---")
        r = test_model(model, LARGE_MESSAGES, "large", timeout=60)
        results.append(r)
        status_icon = "✅" if r["status"] == "ok" else "❌"
        print(f"  {status_icon} {r['status']} | {r.get('http_status','')} | {r['latency_ms']}ms")
        if r["status"] == "ok":
            print(f"     tokens: {r.get('prompt_tokens','?')}+{r.get('completion_tokens','?')} | len={r['response_len']}")
            print(f"     response: {r['response_preview']}")
        else:
            print(f"     error: {r.get('error','')[:200]}")

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    simple = [r for r in results if r["label"] == "simple"]
    large = [r for r in results if r["label"] == "large"]
    for r in simple:
        icon = "✅" if r["status"] == "ok" else "❌"
        print(f"  {icon} {r['model']:25s} simple: {r['status']:8s} {r.get('latency_ms','?')}ms")
    print()
    for r in large:
        icon = "✅" if r["status"] == "ok" else "❌"
        print(f"  {icon} {r['model']:25s} large:  {r['status']:8s} {r.get('latency_ms','?')}ms")


if __name__ == "__main__":
    main()
