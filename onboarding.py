"""
/setup wizard: first-run onboarding + permanent gap-fixing hub.

A fresh install knows nothing about the student — no exam date (planner
stuck in Foundation phase), no timetable (teacher alerts never fire), UTC
timezone (every "today" wrong for India). This module owns the section
definitions, per-chat wizard state, ✅/⚠️ status detectors, and the routing
of answers into the EXISTING capture machinery (settings overrides,
study_domain.create_*, commitments.add_pref). It stores no domain data of
its own — only which section a chat is currently answering.

bot.py renders the hub/prompts and drives callbacks; everything here is
Telegram-free and unit-testable.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path
from typing import Any

import commitments
import session_context
import study_domain
from config import settings

DEFAULT_DB_PATH = commitments.DEFAULT_DB_PATH

STATE_TABLE = "onboarding_state"

SUBJECTS = ["Physics", "Chem", "Maths"]

_WEEKDAY_ALIASES = {
    "mon": "Monday", "monday": "Monday",
    "tue": "Tuesday", "tues": "Tuesday", "tuesday": "Tuesday",
    "wed": "Wednesday", "wednesday": "Wednesday",
    "thu": "Thursday", "thur": "Thursday", "thurs": "Thursday", "thursday": "Thursday",
    "fri": "Friday", "friday": "Friday",
    "sat": "Saturday", "saturday": "Saturday",
    "sun": "Sunday", "sunday": "Sunday",
}

_SUBJECT_ALIASES = {
    "physics": "Physics", "phy": "Physics",
    "chem": "Chem", "chemistry": "Chem",
    "maths": "Maths", "math": "Maths", "mathematics": "Maths",
}

# Ordered wizard sections. kind: "buttons" (tap an option), "text" (one
# typed answer), "loop" (typed answers until Done). All skippable.
SECTIONS: list[dict[str, Any]] = [
    {
        "id": "timezone", "title": "🌍 Timezone",
        "kind": "buttons",
        "prompt": (
            "Which timezone are you in? This drives every reminder and what "
            "counts as \"today\".\n⏱ Applies fully after a restart."
        ),
        "options": [
            ("Asia/Kolkata (India)", "Asia/Kolkata"),
            ("Asia/Dubai", "Asia/Dubai"),
            ("UTC", "UTC"),
        ],
        "hint": "Or send any IANA name, e.g. Asia/Kolkata",
    },
    {
        "id": "target_exam", "title": "🎯 Target exam",
        "kind": "buttons",
        "prompt": (
            "Which JEE are you preparing for? I'll pencil in tentative JEE "
            "Main (January) + Advanced (May) dates so the planner can phase "
            "your prep — refine them anytime with /exam."
        ),
        "options": [("JEE 2027", "2027"), ("JEE 2028", "2028"), ("JEE 2029", "2029")],
        "hint": "Or send a year like 2028",
    },
    {
        "id": "next_mock", "title": "📝 Next mock/test",
        "kind": "text",
        "prompt": (
            "When is your next mock or coaching test? Send:\n"
            "`title | YYYY-MM-DD`\ne.g. `Coaching major test | 2026-08-10`"
        ),
    },
    {
        "id": "timetable", "title": "🏫 Coaching timetable",
        "kind": "loop",
        "prompt": (
            "Your class & teacher schedule powers teacher-doubt alerts.\n"
            "Send ONE class per message:\n"
            "`Subject | day | HH:MM-HH:MM | teacher`\n"
            "e.g. `Physics | Mon | 17:00-19:00 | Ramesh sir`\n"
            "Tap Done ✅ when finished."
        ),
    },
    {
        "id": "chapters", "title": "📚 Current chapters",
        "kind": "text",
        "prompt": "",  # built dynamically per remaining subject
    },
    {
        "id": "backlog", "title": "📋 Backlog",
        "kind": "loop",
        "prompt": (
            "Anything already pending — unfinished homework, skipped "
            "exercises? Send ONE item per message:\n"
            "`title | subject`  (subject optional)\n"
            "e.g. `Ex 2B rotational motion | Physics`\nTap Done ✅ when finished."
        ),
    },
    {
        "id": "commitments", "title": "🔥 Daily commitments",
        "kind": "loop",
        "prompt": (
            "What will you do EVERY day? I verify these nightly against your "
            "logs and track streaks.\nSend one per message, e.g. "
            "`PYQs every day` or `2 hours of maths daily`.\nTap Done ✅ when finished."
        ),
    },
    {
        "id": "capacity", "title": "💪 Daily capacity",
        "kind": "buttons",
        "prompt": (
            "Daily cognitive-yield range: baseline is what a normal day "
            "should reach, ceiling is the hard cap that blocks over-planning."
        ),
        "options": [("Keep 240 / 300 (recommended)", "keep")],
        "hint": "Or send two numbers: `baseline ceiling`, e.g. `260 320`",
    },
    {
        "id": "rhythm", "title": "⏰ Reminder rhythm",
        "kind": "buttons",
        "prompt": (
            "Current rhythm: planning check 01:00 · morning nudge 07:30 · "
            "weekly report 20:00. Change them anytime in /settings → ⏰."
        ),
        "options": [("Keep defaults", "keep")],
    },
    {
        "id": "prefs", "title": "❤️ Preferences",
        "kind": "loop",
        "prompt": (
            "Any study facts I should always keep in mind? e.g. "
            "`I focus best on maths in the morning`.\n"
            "Send one per message, Done ✅ when finished."
        ),
    },
]

SECTION_IDS = [s["id"] for s in SECTIONS]


def section_by_id(section_id: str) -> dict[str, Any] | None:
    return next((s for s in SECTIONS if s["id"] == section_id), None)


# ---------------------------------------------------------------------------
# Wizard state
# ---------------------------------------------------------------------------

def _connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {STATE_TABLE} (
            chat_id INTEGER PRIMARY KEY,
            section TEXT,
            mode TEXT,
            done INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def is_complete(chat_id: int, *, db_path: str | Path = DEFAULT_DB_PATH) -> bool:
    with _connect(db_path) as conn:
        row = conn.execute(
            f"SELECT done FROM {STATE_TABLE} WHERE chat_id = ?", (chat_id,)
        ).fetchone()
    return bool(row and row["done"])


def mark_complete(chat_id: int, *, db_path: str | Path = DEFAULT_DB_PATH) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            f"INSERT INTO {STATE_TABLE} (chat_id, section, mode, done, updated_at) "
            "VALUES (?, NULL, NULL, 1, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET section=NULL, mode=NULL, done=1, updated_at=excluded.updated_at",
            (chat_id, _now()),
        )
        conn.commit()


def start(
    chat_id: int, section_id: str, mode: str = "single",
    *, db_path: str | Path = DEFAULT_DB_PATH,
) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            f"INSERT INTO {STATE_TABLE} (chat_id, section, mode, done, updated_at) "
            "VALUES (?, ?, ?, COALESCE((SELECT done FROM "
            f"{STATE_TABLE} WHERE chat_id = ?), 0), ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET section=excluded.section, "
            "mode=excluded.mode, updated_at=excluded.updated_at",
            (chat_id, section_id, mode, chat_id, _now()),
        )
        conn.commit()


def active_section(
    chat_id: int, *, db_path: str | Path = DEFAULT_DB_PATH
) -> tuple[str, str] | None:
    """Return (section_id, mode) when a wizard step is awaiting input."""
    with _connect(db_path) as conn:
        row = conn.execute(
            f"SELECT section, mode FROM {STATE_TABLE} WHERE chat_id = ?", (chat_id,)
        ).fetchone()
    if row and row["section"]:
        return row["section"], row["mode"] or "single"
    return None


def clear(chat_id: int, *, db_path: str | Path = DEFAULT_DB_PATH) -> None:
    """Leave any active section (keeps the done flag)."""
    with _connect(db_path) as conn:
        conn.execute(
            f"UPDATE {STATE_TABLE} SET section = NULL, mode = NULL, updated_at = ? "
            "WHERE chat_id = ?",
            (_now(), chat_id),
        )
        conn.commit()


def advance(
    chat_id: int, *, db_path: str | Path = DEFAULT_DB_PATH
) -> str | None:
    """Move to the next unfilled section (run_all) or back to the hub (single).

    Returns the new active section id, or None when back at the hub.
    """
    state = active_section(chat_id, db_path=db_path)
    if state is None:
        return None
    current, mode = state
    if mode == "run_all":
        stats = status(db_path=db_path)
        idx = SECTION_IDS.index(current) if current in SECTION_IDS else -1
        for section_id in SECTION_IDS[idx + 1:]:
            if not stats.get(section_id, {}).get("ok"):
                start(chat_id, section_id, "run_all", db_path=db_path)
                return section_id
    clear(chat_id, db_path=db_path)
    return None


# ---------------------------------------------------------------------------
# Status detectors (✅ / ⚠️ per section)
# ---------------------------------------------------------------------------

def status(*, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, dict[str, Any]]:
    today = session_context.local_today_iso()
    result: dict[str, dict[str, Any]] = {}

    tz = settings.user_timezone()
    result["timezone"] = {
        "ok": tz != "UTC",
        "detail": tz if tz != "UTC" else "UTC — almost certainly wrong for you",
    }

    exams = study_domain._rows(
        "exams",
        "archived=0 AND status NOT IN ('Cancelled') AND exam_date IS NOT NULL "
        "AND substr(exam_date,1,10) >= ?",
        (today,), db_path=db_path,
    )
    jee = [e for e in exams if "jee" in str(e.get("title") or "").lower()]
    result["target_exam"] = {
        "ok": bool(jee),
        "detail": (
            f"{jee[0].get('title')} on {str(jee[0].get('exam_date'))[:10]}" if jee
            else "no JEE date — planner stays in Foundation phase"
        ),
    }
    mocks = [e for e in exams if e not in jee]
    result["next_mock"] = {
        "ok": bool(mocks),
        "detail": (
            f"{mocks[0].get('title')} on {str(mocks[0].get('exam_date'))[:10]}" if mocks
            else "no upcoming mock recorded (optional)"
        ),
    }

    timetable = study_domain._rows("timetable", "archived=0 AND active=1", db_path=db_path)
    result["timetable"] = {
        "ok": bool(timetable),
        "detail": f"{len(timetable)} entr(ies)" if timetable else "empty — teacher alerts can't fire",
    }

    syllabus = study_domain._rows(
        "work_items",
        "archived=0 AND kind='Current Syllabus' AND status NOT IN ('Completed','Dismissed')",
        db_path=db_path,
    )
    covered = {str(r.get("subject") or "") for r in syllabus}
    missing = [s for s in SUBJECTS if s not in covered]
    result["chapters"] = {
        "ok": not missing,
        "detail": (
            "all subjects placed" if not missing
            else f"missing: {', '.join(missing)}"
        ),
        "missing_subjects": missing,
    }

    backlog = study_domain._rows(
        "work_items", "archived=0 AND status IN ('Backlog','Inbox')", db_path=db_path
    )
    result["backlog"] = {
        "ok": True,
        "detail": f"{len(backlog)} item(s) tracked",
    }

    daily = commitments.active_daily_goals(db_path=db_path)
    result["commitments"] = {
        "ok": bool(daily),
        "detail": f"{len(daily)} daily commitment(s)" if daily else "none — nothing to verify nightly",
    }

    result["capacity"] = {
        "ok": True,
        "detail": f"{settings.daily_cy_baseline()}/{settings.daily_cy_ceiling()} CY",
    }
    result["rhythm"] = {
        "ok": True,
        "detail": (
            f"plan {settings.planning_reminder_time()} · nudge "
            f"{settings.commitment_nudge_time()} · report {settings.weekly_report_time()}"
        ),
    }
    prefs_count = 0
    try:
        with _connect(db_path) as conn:
            prefs_count = int(conn.execute(
                "SELECT COUNT(*) FROM user_prefs WHERE active = 1"
            ).fetchone()[0])
    except sqlite3.OperationalError:
        pass
    result["prefs"] = {"ok": True, "detail": f"{prefs_count} saved"}
    return result


def chapters_prompt(*, db_path: str | Path = DEFAULT_DB_PATH) -> str:
    stats = status(db_path=db_path)
    missing = stats["chapters"].get("missing_subjects") or []
    subject = missing[0] if missing else None
    if subject is None:
        return "All three subjects already have a current chapter. Send a new one to update, or Skip."
    return (
        f"Which chapter are you currently on in {subject}?\n"
        "Send just the chapter name, e.g. `Rotational Motion`."
    )


# ---------------------------------------------------------------------------
# Answer routing
# ---------------------------------------------------------------------------

def apply_answer(
    chat_id: int, section_id: str, text: str,
    *, db_path: str | Path = DEFAULT_DB_PATH,
) -> tuple[bool, str, bool]:
    """Apply one typed/tapped answer. Returns (ok, reply, advance_now)."""
    text = (text or "").strip()
    try:
        if section_id == "timezone":
            ok, result = settings.validate_setting("USER_TIMEZONE", text)
            if not ok:
                return False, f"⚠️ {result}", False
            settings.set_override("USER_TIMEZONE", result)
            return True, f"✅ Timezone → {result} ⏱ fully applies after restart", True

        if section_id == "target_exam":
            try:
                year = int(text)
            except ValueError:
                return False, "⚠️ send a year like 2028", False
            if not 2026 <= year <= 2035:
                return False, "⚠️ year looks off — 2026-2035", False
            existing = study_domain._rows(
                "exams", "archived=0 AND LOWER(title) LIKE 'jee%'", db_path=db_path
            )
            if existing:
                return True, (
                    f"✅ Keeping existing: {existing[0].get('title')} — "
                    "adjust with /exam if needed"
                ), True
            study_domain.create_exam({
                "title": f"JEE Main {year}", "kind": "JEE Main",
                "exam_date": f"{year}-01-24", "date_confidence": "Tentative",
            }, db_path=db_path)
            study_domain.create_exam({
                "title": f"JEE Advanced {year}", "kind": "JEE Advanced",
                "exam_date": f"{year}-05-18", "date_confidence": "Tentative",
            }, db_path=db_path)
            return True, (
                f"✅ JEE Main {year} (Jan, tentative) + JEE Advanced {year} "
                "(May, tentative) recorded — the planner now phases your prep. "
                "Refine dates with /exam."
            ), True

        if section_id == "next_mock":
            parts = [p.strip() for p in text.split("|")]
            if len(parts) != 2:
                return False, "⚠️ format: `title | YYYY-MM-DD`", False
            title, date = parts
            study_domain.create_exam({
                "title": title or "Mock test", "kind": "Coaching Test",
                "exam_date": date, "date_confidence": "Official",
            }, db_path=db_path)
            return True, f"✅ {title} on {date} recorded.", True

        if section_id == "timetable":
            parts = [p.strip() for p in text.split("|")]
            if len(parts) < 3:
                return False, (
                    "⚠️ format: `Subject | day | HH:MM-HH:MM | teacher`"
                ), False
            subject = _SUBJECT_ALIASES.get(parts[0].lower())
            if subject is None:
                return False, "⚠️ subject must be Physics, Chem or Maths", False
            weekday = _WEEKDAY_ALIASES.get(parts[1].lower())
            if weekday is None:
                return False, "⚠️ day must be Mon…Sun", False
            times = parts[2].replace("–", "-").split("-")
            if len(times) != 2:
                return False, "⚠️ time must be HH:MM-HH:MM", False
            teacher = parts[3] if len(parts) > 3 else None
            study_domain.create_timetable_entry({
                "title": f"{subject} {weekday} {times[0].strip()}",
                "subject": subject, "weekday": weekday,
                "start_time": times[0].strip(), "end_time": times[1].strip(),
                "teacher": teacher, "kind": "Class",
            }, db_path=db_path)
            return True, f"✅ {subject} on {weekday} {parts[2]} added. Next class, or Done ✅.", False

        if section_id == "chapters":
            stats = status(db_path=db_path)
            missing = stats["chapters"].get("missing_subjects") or []
            subject = missing[0] if missing else None
            if subject is None:
                return True, "✅ All subjects already placed.", True
            study_domain.create_work_item({
                "title": f"{subject}: {text}", "kind": "Current Syllabus",
                "status": "Active", "subject": subject, "chapter": text,
            }, db_path=db_path)
            remaining = missing[1:]
            if remaining:
                return True, (
                    f"✅ {subject} → {text}.\n"
                    f"Which chapter in {remaining[0]}?"
                ), False
            return True, f"✅ {subject} → {text}. All subjects placed.", True

        if section_id == "backlog":
            parts = [p.strip() for p in text.split("|")]
            props: dict[str, Any] = {
                "title": parts[0], "kind": "Backlog", "status": "Backlog",
            }
            if len(parts) > 1:
                subject = _SUBJECT_ALIASES.get(parts[1].lower())
                if subject:
                    props["subject"] = subject
            study_domain.create_work_item(props, db_path=db_path)
            return True, f"✅ Backlog: {parts[0]}. Next item, or Done ✅.", False

        if section_id == "capacity":
            if text.lower() == "keep":
                return True, "✅ Keeping 240 / 300 CY.", True
            parts = text.split()
            if len(parts) != 2:
                return False, "⚠️ send two numbers: `baseline ceiling`, e.g. `260 320`", False
            try:
                baseline, ceiling = int(parts[0]), int(parts[1])
            except ValueError:
                return False, "⚠️ both must be whole numbers", False
            if not 0 < baseline <= ceiling:
                return False, "⚠️ need 0 < baseline ≤ ceiling", False
            settings.set_override("DAILY_CY_BASELINE", str(baseline))
            settings.set_override("DAILY_CY_CEILING", str(ceiling))
            return True, f"✅ Capacity → {baseline}/{ceiling} CY.", True

        if section_id == "rhythm":
            return True, "✅ Rhythm kept — change anytime in /settings → ⏰.", True

        if section_id == "prefs":
            commitments.add_pref(chat_id, text, db_path=db_path)
            return True, "✅ Noted. Another, or Done ✅.", False

        if section_id == "commitments":
            # bot.py routes this through the /remember confirm flow instead.
            return False, "handled by _handle_remember", False

        return False, "unknown setup section", False
    except Exception as exc:
        return False, f"⚠️ Couldn't save that: {exc}", False


# ---------------------------------------------------------------------------
# AI escape hatch: "ai <free form>" answers become a bounded action plan
# ---------------------------------------------------------------------------
# The LLM (domain_parser.parse_setup_ai) may only propose these action types;
# everything is re-validated deterministically here BEFORE the user sees the
# confirm preview, and nothing executes until they tap Confirm.

_WORK_KINDS = {
    "coaching homework": "Coaching Homework", "current syllabus": "Current Syllabus",
    "revision": "Revision", "pyq": "PYQ", "short notes": "Short Notes",
    "backlog": "Backlog", "other": "Other",
}
_EXAM_KINDS = {
    "jee main mock": "JEE Main Mock", "jee advanced mock": "JEE Advanced Mock",
    "coaching test": "Coaching Test", "jee main": "JEE Main",
    "jee advanced": "JEE Advanced", "other": "Other",
}


def _valid_iso(value: Any) -> str | None:
    try:
        return study_domain._iso_date(value) if value else None
    except Exception:
        return None


def validate_ai_actions(actions: list[dict]) -> tuple[list[dict], list[str]]:
    """Deterministically filter an LLM action list. Returns (valid, errors)."""
    valid: list[dict] = []
    errors: list[str] = []
    for raw in actions or []:
        if not isinstance(raw, dict):
            errors.append("dropped a non-object action")
            continue
        kind = str(raw.get("type") or "").strip()
        if kind == "remember_preference":
            text = str(raw.get("text") or "").strip()
            if text:
                valid.append({"type": kind, "text": text})
            else:
                errors.append("preference had no text")
        elif kind == "create_commitment":
            statement = str(raw.get("statement") or "").strip()
            if statement:
                valid.append({"type": kind, "statement": statement})
            else:
                errors.append("commitment had no statement")
        elif kind == "create_work_item":
            title = str(raw.get("title") or "").strip()
            work_kind = _WORK_KINDS.get(str(raw.get("kind") or "other").strip().lower())
            subject = _SUBJECT_ALIASES.get(str(raw.get("subject") or "").strip().lower())
            due = raw.get("due_date")
            if not title or work_kind is None:
                errors.append(f"work item invalid: {raw.get('title')!r}/{raw.get('kind')!r}")
                continue
            if due and _valid_iso(due) is None:
                errors.append(f"work item date invalid: {due!r}")
                continue
            valid.append({"type": kind, "title": title, "kind": work_kind,
                          "subject": subject, "due_date": due or None})
        elif kind == "create_exam":
            title = str(raw.get("title") or "").strip()
            exam_kind = _EXAM_KINDS.get(str(raw.get("kind") or "other").strip().lower())
            date = _valid_iso(raw.get("exam_date"))
            if not title or exam_kind is None or date is None:
                errors.append(f"exam invalid: {raw.get('title')!r}/{raw.get('exam_date')!r}")
                continue
            valid.append({"type": kind, "title": title, "kind": exam_kind, "exam_date": date})
        elif kind == "create_timetable_entry":
            subject = _SUBJECT_ALIASES.get(str(raw.get("subject") or "").strip().lower())
            weekday = _WEEKDAY_ALIASES.get(str(raw.get("weekday") or "").strip().lower())
            start = str(raw.get("start") or "").strip()
            end = str(raw.get("end") or "").strip()
            if subject is None or weekday is None or not start or not end:
                errors.append("timetable entry incomplete")
                continue
            valid.append({"type": kind, "subject": subject, "weekday": weekday,
                          "start": start, "end": end,
                          "teacher": (str(raw.get("teacher")).strip() or None)
                          if raw.get("teacher") else None})
        elif kind == "set_setting":
            key = str(raw.get("key") or "").strip()
            ok, result = settings.validate_setting(key, str(raw.get("value") or ""))
            if ok:
                valid.append({"type": kind, "key": key, "value": result})
            else:
                errors.append(f"setting {key or '?'}: {result}")
        elif kind == "skip_section":
            valid.append({"type": kind})
        else:
            errors.append(f"unknown action type {kind!r}")
    return valid, errors


def describe_ai_actions(actions: list[dict]) -> list[str]:
    lines: list[str] = []
    for action in actions:
        kind = action["type"]
        if kind == "remember_preference":
            lines.append(f"🧠 Remember: “{action['text']}”")
        elif kind == "create_commitment":
            lines.append(f"✅ Commit: “{action['statement']}” (tracked nightly)")
        elif kind == "create_work_item":
            extra = " · ".join(x for x in (action.get("subject"),
                               f"due {action['due_date']}" if action.get("due_date") else None) if x)
            lines.append(f"📋 Task: {action['title']} [{action['kind']}]"
                         + (f" ({extra})" if extra else ""))
        elif kind == "create_exam":
            lines.append(f"📝 Exam: {action['title']} on {action['exam_date'][:10]}")
        elif kind == "create_timetable_entry":
            lines.append(
                f"🏫 Class: {action['subject']} {action['weekday']} "
                f"{action['start']}-{action['end']}"
                + (f" with {action['teacher']}" if action.get("teacher") else "")
            )
        elif kind == "set_setting":
            entry = settings.setting_entry(action["key"])
            label = entry["label"] if entry else action["key"]
            shown = action["value"]
            if entry and entry["type"] == "weekday":
                try:
                    shown = settings.WEEKDAY_NAMES[int(action["value"])]
                except (ValueError, IndexError):
                    pass
            lines.append(f"⚙️ Setting: {label} → {shown}")
        elif kind == "skip_section":
            lines.append("▸ Skip this setup step")
    return lines


def apply_ai_actions(
    chat_id: int, actions: list[dict], *, db_path: str | Path = DEFAULT_DB_PATH
) -> tuple[list[str], bool]:
    """Execute confirmed actions. Returns (result lines, skip_requested)."""
    results: list[str] = []
    skip = False
    for action in actions:
        kind = action["type"]
        try:
            if kind == "remember_preference":
                commitments.add_pref(chat_id, action["text"], db_path=db_path)
                results.append("🧠 remembered")
            elif kind == "create_commitment":
                import domain_parser
                parsed = domain_parser.parse_commitment(action["statement"])
                if parsed.get("needs_clarification"):
                    results.append(
                        f"⚠️ commitment unclear — try /remember {action['statement']}"
                    )
                elif parsed.get("kind") == "preference":
                    commitments.add_pref(
                        chat_id, parsed.get("title") or action["statement"], db_path=db_path
                    )
                    results.append("🧠 saved as preference (not measurable)")
                else:
                    goal_data = {k: parsed.get(k) for k in
                                 ("title", "goal_type", "metric", "target", "period",
                                  "subject", "source_text")}
                    study_domain.create_goal(goal_data, db_path=db_path)
                    results.append(f"✅ commitment: {goal_data['title']}")
            elif kind == "create_work_item":
                props = {"title": action["title"], "kind": action["kind"],
                         "status": "Backlog" if action["kind"] == "Backlog" else "Inbox"}
                if action.get("subject"):
                    props["subject"] = action["subject"]
                if action.get("due_date"):
                    props["due_date"] = action["due_date"]
                study_domain.create_work_item(props, db_path=db_path)
                results.append(f"📋 task: {action['title']}")
            elif kind == "create_exam":
                study_domain.create_exam({
                    "title": action["title"], "kind": action["kind"],
                    "exam_date": action["exam_date"], "date_confidence": "Tentative",
                }, db_path=db_path)
                results.append(f"📝 exam: {action['title']}")
            elif kind == "create_timetable_entry":
                study_domain.create_timetable_entry({
                    "title": f"{action['subject']} {action['weekday']} {action['start']}",
                    "subject": action["subject"], "weekday": action["weekday"],
                    "start_time": action["start"], "end_time": action["end"],
                    "teacher": action.get("teacher"), "kind": "Class",
                }, db_path=db_path)
                results.append(f"🏫 class: {action['subject']} {action['weekday']}")
            elif kind == "set_setting":
                settings.set_override(action["key"], action["value"])
                results.append(f"⚙️ {action['key']} → {action['value']}")
            elif kind == "skip_section":
                skip = True
                results.append("▸ step skipped")
        except Exception as exc:
            results.append(f"⚠️ {kind} failed: {exc}")
    return results, skip
