"""
Real-LLM end-to-end test on a 2-year dataset: data-biased questions + multi-tool agent.

Builds a temp SQLite mirror that looks like a 2-year Notion+schema mirror
(2024-08-01 .. 2026-07-31, ~450 ledger sessions, ~120 doubts, ~40 revision
chapters, op_exams/op_goals/op_daily_plan), then:

  A) asks data-biased questions through sql_query_flow.answer_question and
     checks the answers are data-backed (real numbers/directions from the seed)
  B) drives the agent (agent.run / continue_run) through multi-tool tasks and
     verifies tool usage, write previews, confirmed writes and DB mutations

Uses the REAL router (curated ladder) — no mocked transport.

Run:  python3 test_2year_multi_tool.py
Skip: SKIP_REAL_LLM=1 python3 test_2year_multi_tool.py
"""

from __future__ import annotations

import asyncio
import collections
import datetime as dt
import os
import random
import re
import sqlite3
import sys
import tempfile
import uuid
from pathlib import Path

import agent
import agent_tools
import conversation_history
import draft_store
import logging_flow
import notion_client_wrapper
import operational_store
import reminders
import session_context
import sql_query_flow
import sql_tool
import sync
import user_jobs
from config import notion_schema

CHAT_ID = 424242
TODAY = dt.date(2026, 7, 31)


def _check(ok: bool, label: str, cond: bool, extra: str = "") -> bool:
    print(f"[{'OK ' if cond else 'BAD'}] {label}{(' -> ' + extra) if extra else ''}")
    return ok and cond


def _near(needle: float, answer: str, tol_frac: float = 0.12) -> bool:
    """Any number in the answer within tolerance of the expected fact."""
    nums = [float(m) for m in re.findall(r"-?\d+(?:\.\d+)?", answer.replace(",", ""))]
    return any(abs(n - needle) <= max(tol_frac * needle, 1.0) for n in nums)


# ---------------------------------------------------------------------------
# Seed data: deterministic 2-year dataset with embedded, computable trends
# ---------------------------------------------------------------------------

SUBJECTS = ("Chem", "Physics", "Maths")
CHAPTERS = {
    "Physics": ["Kinematics", "Newton's Laws", "Work Energy Power",
                "Rotational Motion", "Gravitation", "Electrostatics",
                "Optics", "Thermodynamics", "Waves", "Semiconductors"],
    "Chem": ["Mole Concept", "Atomic Structure", "Chemical Bonding",
             "Thermochemistry", "Chemical Kinetics", "Equilibrium",
             "Organic Chemistry", "Coordination Compounds",
             "Electrochemistry", "Polymers"],
    "Maths": ["Calculus", "Integration", "Differential Equations", "Vectors",
              "Matrices", "Probability", "Trigonometry",
              "Coordinate Geometry", "Sequences & Series", "Complex Numbers"],
}
EXERCISE_TYPES = ("MLE", "Ex 1A", "PYQs", "JMYL")
BLOCKS = ("EB-1", "EB-2", "EB-3", "RB")

# Year-2 vs year-1 accuracy target per subject: Physics improves the most,
# Chemistry is flat, Maths improves moderately.
ACC_Y1 = {"Physics": 0.55, "Chem": 0.62, "Maths": 0.58}
ACC_Y2 = {"Physics": 0.85, "Chem": 0.64, "Maths": 0.73}
ACC_SIGMA = 0.06

START = dt.date(2024, 8, 1)


def _months() -> list[tuple[int, dt.date]]:
    """[(year_index, first_day_of_month)] for 24 months starting 2024-08."""
    out = []
    for m in range(24):
        y = START.year + (START.month - 1 + m) // 12
        mo = (START.month - 1 + m) % 12 + 1
        out.append((1 if m < 12 else 2, dt.date(y, mo, 1)))
    return out


def _month_days(month_first: dt.date) -> list[dt.date]:
    if month_first.month == 12:
        nxt = dt.date(month_first.year + 1, 1, 1)
    else:
        nxt = dt.date(month_first.year, month_first.month + 1, 1)
    return [month_first + dt.timedelta(days=d) for d in range((nxt - month_first).days)]


def _build_two_year_db() -> tuple[Path, dict]:
    """Create + seed the test mirror. Returns (db_path, ground-truth facts)."""
    tmp = Path(tempfile.mkstemp(suffix=".db")[1])
    conn = sync.connect(tmp)
    sync.init_db(conn)
    operational_store.init_db(conn)
    conn.commit()
    user_jobs._connect(tmp).close()
    rng = random.Random(2026)

    ledger_rows: list[tuple] = []
    id_seq = 0

    def nid(prefix: str) -> str:
        nonlocal id_seq
        id_seq += 1
        return f"{prefix}-{id_seq:05d}"

    # --- ledger: ~450 active sessions over 24 months -------------------------
    for year_idx, month_first in _months():
        month_days = _month_days(month_first)
        for _ in range(rng.randint(14, 24)):
            day = rng.choice(month_days)
            subject = rng.choice(SUBJECTS)
            acc = (ACC_Y1 if year_idx == 1 else ACC_Y2)[subject]
            acc = max(0.30, min(0.98, round(rng.gauss(acc, ACC_SIGMA), 2)))
            attempted = rng.randint(8, 30)
            correct = max(0, min(attempted, round(attempted * acc)))
            acc = round(correct / attempted, 2)
            minutes = rng.randint(30, 140)
            chapter = rng.choice(CHAPTERS[subject])
            ex = rng.choice(EXERCISE_TYPES)
            block = rng.choice(BLOCKS)
            cy = round((acc - 0.35) * 150 + rng.uniform(-12, 12))
            ledger_rows.append((
                nid("L"), f"http://n/{nid('L')}", 0,
                f"{subject} {ex} {chapter}", subject, chapter, ex, block,
                day.isoformat(), float(minutes), float(attempted), float(correct),
                max(0, int(cy)), max(0, int(cy)), acc,
                round(minutes / max(attempted, 1), 2),
                f"notes: {subject} {chapter}",
                day.isoformat(), "{}",
            ))
    # marker facts for trends computed later from real rows

    # anomaly: very long session, poor accuracy
    ledger_rows.append((
        nid("L"), f"http://n/{nid('L')}", 0,
        "Physics PYQs Rotational Motion", "Physics", "Rotational Motion", "PYQs",
        "RB", "2025-05-20", 300.0, 10.0, 4.0, 8, 8, 0.4, 30.0,
        "burned out, very long session with low yield", "2025-05-20", "{}",
    ))
    # three archived rows that must be excluded by default
    for arch_date in ("2025-02-10", "2025-11-03", "2026-06-07"):
        ledger_rows.append((
            nid("L"), f"http://n/{nid('L')}", 1,
            "Archived old entry", "Maths", "Vectors", "Ex 1A", "EB-1",
            arch_date, 20.0, 5.0, 5.0, 10, 10, 1.0, 4.0, "archived",
            arch_date, "{}",
        ))

    _LEDGER_COLS = (
        "notion_page_id", "notion_url", "archived", "task", "subject",
        "chapter", "exercise_type", "block", "date", "actual_time_min",
        "questions_attempted", "questions_correct", "cognitive_yield",
        "theory_yield", "accuracy_ratio", "mins_per_question",
        "key_points_notes", "last_synced_at", "raw_json",
    )
    conn.executemany(
        f"INSERT INTO ledger ({', '.join(_LEDGER_COLS)}) "
        f"VALUES ({', '.join('?' * len(_LEDGER_COLS))})",
        ledger_rows,
    )
    conn.commit()
    _seed_doubts(conn, rng, nid)
    _seed_revision(conn, rng, nid)
    _seed_op_tables(conn, rng, nid)
    facts = _compute_facts(tmp)
    conn.close()
    return tmp, facts

def _seed_doubts(conn: sqlite3.Connection, rng: random.Random, nid) -> None:
    """~120 doubts; resolution rate improves from year 1 (40%) to year 2 (75%)."""
    rows = []
    fail_types = ("Concept", "Calculation", "Omission", "Time Management")
    for year_idx, month_first in _months():
        n = rng.randint(4, 6)
        res_rate = 0.40 if year_idx == 1 else 0.75
        for _ in range(n):
            subject = rng.choice(SUBJECTS)
            concept = f"{subject} doubt on {rng.choice(CHAPTERS[subject])} #{nid('C')}"
            resolved = rng.random() < res_rate
            day = rng.choice(_month_days(month_first))
            resolved_at = (day + dt.timedelta(days=rng.randint(1, 21))).isoformat()
            rows.append((
                nid("D"), 0, concept,
                "Resolved" if resolved else "Unresolved",
                rng.choice(fail_types), subject,
                resolved_at if resolved else None,
                rng.choice(CHAPTERS[subject]),
                day.isoformat(), "{}",
            ))
    conn.executemany(
        """INSERT INTO doubts
           (notion_page_id, archived, core_concept, status, failure_type,
            subject, resolved_at, chapter, last_synced_at, raw_json)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    conn.commit()


def _seed_revision(conn: sqlite3.Connection, rng: random.Random, nid) -> None:
    """40 chapters; ~25% of them overdue (next_execution_date in the past)."""
    rows = []
    for subject in SUBJECTS:
        for chapter in CHAPTERS[subject]:
            overdue = rng.random() < 0.25
            base = TODAY - dt.timedelta(days=rng.randint(10, 90)) if overdue \
                else TODAY + dt.timedelta(days=rng.randint(1, 180))
            status = "Pending" if overdue else rng.choice(("Pending", "Completed"))
            mastery = "Not started" if status == "Pending" \
                else rng.choice(("In progress", "Done"))
            rows.append((
                nid("R"), 0, f"{subject} {chapter}", status, mastery,
                base.isoformat(), subject, base.isoformat(), "{}",
            ))
    conn.executemany(
        """INSERT INTO revision
           (notion_page_id, archived, chapter_module, status, mastery,
            next_execution_date, subject, last_synced_at, raw_json)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    conn.commit()


def _seed_op_tables(conn: sqlite3.Connection, rng: random.Random, nid) -> None:
    """op_exams (June/Dec mocks, improving in year 2), goals, daily plan."""
    exams = []
    for i, (year_idx, month_first) in enumerate(_months()):
        if month_first.month not in (6, 12):
            continue
        marks = round(rng.uniform(52, 62), 1) if year_idx == 1 \
            else round(rng.uniform(68, 76), 1)
        exam_date = month_first.replace(day=15)
        exams.append((
            nid("E"), 0, exam_date.isoformat(), exam_date.isoformat(),
            f"Mock exam {month_first.isoformat()[:7]}", "JEE Main Mock",
            exam_date.isoformat(), 100.0, 80.0, marks,
        ))
    conn.executemany(
        """INSERT INTO op_exams
           (notion_page_id, archived, created_time, last_edited_time,
            title, kind, exam_date, max_marks, target_marks, actual_marks)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        exams,
    )
    goals = [
        (nid("G"), 0, "2026-07-01", "2026-07-31", "Finish Physics syllabus",
         "Coverage", "Active", "syllabus_pct", 90.0, "Deadline",
         "Physics", "2026-12-31", 3, 60.0, 0.0),
        (nid("G"), 0, "2026-07-01", "2026-07-31", "Maths accuracy 75%",
         "Accuracy", "Active", "accuracy", 0.75, "Deadline",
         "Maths", "2026-11-30", 2, 0.65, 0.0),
        (nid("G"), 0, "2026-07-01", "2026-07-31", "Chem accuracy 70%",
         "Accuracy", "Active", "accuracy", 0.70, "Deadline",
         "Chem", "2026-10-31", 2, 0.64, 0.0),
        (nid("G"), 0, "2026-07-01", "2026-07-31", "Solve 500 PYQs",
         "Coverage", "Active", "count", 500.0, "Deadline",
         None, "2026-12-31", 1, 120.0, 0.0),
        (nid("G"), 0, "2026-07-01", "2026-07-31", "Daily 2h study",
         "Duration", "Active", "hours_per_day", 2.0, "Daily",
         None, None, 1, 1.5, 0.0),
    ]
    conn.executemany(
        """INSERT INTO op_goals
           (notion_page_id, archived, created_time, last_edited_time,
            title, goal_type, status, metric, target, period, subject,
            deadline, priority, current_value, minimum)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        goals,
    )
    plan = []
    for i in range(8):
        plan.append((
            nid("P"), 0, "2026-07-31", "2026-07-31",
            f"Plan item {i + 1}", (TODAY + dt.timedelta(days=i)).isoformat(),
            i + 1, "Physics" if i % 2 == 0 else "Maths", "Revision", 40.0, 45.0,
            "Planned", 2, 0,
        ))
    conn.executemany(
        """INSERT INTO op_daily_plan
           (notion_page_id, archived, created_time, last_edited_time,
            title, plan_date, sequence, subject, kind, expected_cy,
            estimated_min, status, priority, interruptible)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        plan,
    )
    conn.commit()


def _compute_facts(db: Path) -> dict:
    """Ground truth computed from the seeded rows, used by the validators."""
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    f: dict = {}

    q = conn.execute(
        """SELECT subject, date, actual_time_min, questions_attempted,
                  questions_correct, accuracy_ratio, cognitive_yield
           FROM ledger WHERE archived = 0"""
    ).fetchall()
    rows = [dict(r) for r in q]
    f["total_sessions"] = len(rows)
    f["total_minutes"] = round(sum(r["actual_time_min"] for r in rows))
    f["avg_session_min"] = round(f["total_minutes"] / f["total_sessions"], 1)

    y1 = [r for r in rows if r["date"] < "2025-08-01"]
    y2 = [r for r in rows if r["date"] >= "2025-08-01"]
    f["y1_avg_acc"] = round(sum(r["accuracy_ratio"] for r in y1) / len(y1), 3)
    f["y2_avg_acc"] = round(sum(r["accuracy_ratio"] for r in y2) / len(y2), 3)

    subj = {s: {"y1": [], "y2": [], "min": 0.0} for s in SUBJECTS}
    for r in rows:
        s = r["subject"]
        subj[s]["min"] += r["actual_time_min"]
        (subj[s]["y1"] if r["date"] < "2025-08-01" else subj[s]["y2"]).append(
            r["accuracy_ratio"]
        )
    f["subject_minutes"] = {s: round(v["min"]) for s, v in subj.items()}
    f["top_subject"] = max(subj, key=lambda s: subj[s]["min"])
    f["subj_acc"] = {
        s: (round(sum(v["y1"]) / max(len(v["y1"]), 1), 3),
            round(sum(v["y2"]) / max(len(v["y2"]), 1), 3))
        for s, v in subj.items()
    }
    f["most_improved"] = max(
        SUBJECTS, key=lambda s: f["subj_acc"][s][1] - f["subj_acc"][s][0]
    )

    f["y2025_sessions"] = sum(1 for r in rows if r["date"].startswith("2025"))
    month_2025 = {}
    for r in rows:
        if r["date"].startswith("2025"):
            month_2025[r["date"][:7]] = month_2025.get(r["date"][:7], 0) + r["actual_time_min"]
    f["top_2025_month"] = max(month_2025, key=month_2025.get) if month_2025 else None

    doubts = conn.execute(
        """SELECT resolved_at, subject FROM doubts WHERE archived = 0"""
    ).fetchall()
    f["total_doubts"] = len(doubts)
    f["resolved_y1"] = sum(1 for r in doubts if r["resolved_at"] and r["resolved_at"] < "2025-08-01")
    f["resolved_y2"] = sum(1 for r in doubts if r["resolved_at"] and r["resolved_at"] >= "2025-08-01")
    by_subj = {}
    for r in doubts:
        if r["resolved_at"] and r["resolved_at"] >= "2025-08-01":
            by_subj[r["subject"]] = by_subj.get(r["subject"], 0) + 1
    f["top_resolved_y2"] = max(by_subj, key=by_subj.get)

    f["overdue_revision"] = conn.execute(
        """SELECT COUNT(*) FROM revision
           WHERE archived = 0 AND next_execution_date IS NOT NULL
             AND next_execution_date < ?""", (TODAY.isoformat(),)
    ).fetchone()[0]

    exams = conn.execute(
        """SELECT exam_date, actual_marks FROM op_exams WHERE archived = 0"""
    ).fetchall()
    y1m = [r["actual_marks"] for r in exams if r["exam_date"] < "2025-08-01"]
    y2m = [r["actual_marks"] for r in exams if r["exam_date"] >= "2025-08-01"]
    f["exam_y1_avg"] = round(sum(y1m) / max(len(y1m), 1), 1)
    f["exam_y2_avg"] = round(sum(y2m) / max(len(y2m), 1), 1)
    conn.close()
    return f

# ---------------------------------------------------------------------------
# Part B helpers: offline stubs (no live Notion) + tool counters
# ---------------------------------------------------------------------------

class _FakeNotion:
    """Materialises commit_write pages straight into the test mirror."""

    def __init__(self, db: Path):
        self.db = db

    def create_page(self, db_key: str, properties: dict):
        page_id = f"n-{uuid.uuid4().hex[:10]}"
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        schema = notion_schema.PROPERTIES_BY_DB[db_key]
        cols = ["notion_page_id", "created_time", "last_edited_time",
                "last_synced_at", "archived", "raw_json"]
        vals = [page_id, now, now, now, 0, "{}"]
        for name in schema:
            if name not in properties or properties[name] is None:
                continue
            val = properties[name]
            if schema[name]["type"] == "checkbox":
                val = 1 if val else 0
            cols.append(name)
            vals.append(val)
        if db_key == "ledger":
            try:
                att = float(properties["questions_attempted"])
                corr = float(properties["questions_correct"])
                cols.append("accuracy_ratio")
                vals.append(round(corr / att, 2))
            except Exception:
                pass
        with sqlite3.connect(str(self.db)) as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO {db_key} ({', '.join(cols)}) "
                f"VALUES ({', '.join('?' * len(cols))})",
                vals,
            )
            conn.commit()
        return {"id": page_id, "url": f"http://n/{page_id}",
                "created_time": now, "last_edited_time": now,
                "archived": False}

    def update_page(self, page_id: str, properties: dict):
        return {"id": page_id}

    def query_database(self, db_key: str, **kwargs):
        return []


def _install_stubs(db: Path) -> None:
    for mod in (agent_tools, conversation_history, draft_store,
                logging_flow, operational_store, reminders, session_context,
                sql_tool, sync, user_jobs):
        mod.DEFAULT_DB_PATH = db
    fake = _FakeNotion(db)
    notion_client_wrapper.create_page = fake.create_page
    notion_client_wrapper.update_page = fake.update_page
    notion_client_wrapper.query_database = fake.query_database
    sync.sync_once_locked_sync = lambda *a, **k: {}
    sync.sync_once = lambda *a, **k: {}


COUNTERS = {"reads": collections.Counter(), "writes": collections.Counter()}
_ORIG_EXECUTE = agent_tools.execute_tool
_ORIG_PREPARE = agent_tools.prepare_write
_ORIG_RUN = agent_tools.run_prepared_write
_TEST_DB: Path | None = None


def _force_execute(name, arguments, *, chat_id="", db_path=None):
    """Force the test db: agent_tools' defaults are bound at definition time."""
    COUNTERS["reads"][name] += 1
    return _ORIG_EXECUTE(name, arguments, chat_id=chat_id, db_path=db_path or _TEST_DB)


def _force_prepare(name, arguments, *, chat_id="", db_path=None):
    COUNTERS["writes"][name] += 1
    return _ORIG_PREPARE(name, arguments, chat_id=chat_id, db_path=db_path or _TEST_DB)


def _force_run(name, run, *, chat_id="", db_path=None):
    return _ORIG_RUN(name, run, chat_id=chat_id, db_path=db_path or _TEST_DB)


# ---------------------------------------------------------------------------
# Part A: data-biased question battery (sql_query_flow, real router)
# ---------------------------------------------------------------------------

def _ask_battery(db: Path, facts: dict) -> bool:
    ok = True

    def qcheck(label: str, question: str, *conds) -> bool:
        nonlocal ok
        answer = sql_query_flow.answer_question(
            question, db_path=str(db), max_iterations=4, chat_id=CHAT_ID
        )
        conds = tuple(conds) + (
            lambda a: not a.startswith(sql_query_flow.ANSWER_ERROR_PREFIX),
        )
        ok_here = all(c(answer) for c in conds)
        ok = _check(ok, label, ok_here,
                    f"Q={question[:60]!r} A={answer[:110]!r}")
        return ok_here

    f = facts
    qcheck("total minutes on Physics", 
           "Which subject did I spend the most total study time on across the last two years? Give the subject and total minutes.",
           lambda a: "phys" in a.lower(),
           lambda a: _near(f["subject_minutes"]["Physics"], a, 0.15))
    qcheck("year1 vs year2 accuracy",
           "What was my average accuracy in year 1 (before Aug 2025) and year 2?",
           lambda a: _near(f["y1_avg_acc"], a, 0.12),
           lambda a: _near(f["y2_avg_acc"], a, 0.12))
    qcheck("most improved subject",
           "Which subject improved the most in accuracy from year 1 to year 2?",
           lambda a: "phys" in a.lower())
    qcheck("2025 session count",
           "How many study sessions did I log in calendar year 2025?",
           lambda a: _near(f["y2025_sessions"], a, 0.1))
    qcheck("top 2025 month",
           "Which month in 2025 did I study the most total minutes?",
           lambda a: "may" in a.lower() or "05" in a)
    qcheck("total Chem minutes (stored as 'Chem')",
           "How many minutes did I spend on subject 'Chem' in total across both years?",
           lambda a: _near(f["subject_minutes"]["Chem"], a, 0.1))
    qcheck("doubts resolved year2",
           "How many doubts did I resolve in year 2 (from Aug 2025 onward)?",
           lambda a: _near(f["resolved_y2"], a, 0.2))
    qcheck("mock scores trend",
           "What were my average mock exam scores in year 1 and year 2?",
           lambda a: _near(f["exam_y1_avg"], a, 0.15),
           lambda a: _near(f["exam_y2_avg"], a, 0.15))
    qcheck("active goals",
           "List my active goals and their targets.",
           lambda a: any(k in a.lower() for k in ("syllabus", "accuracy", "pyq", "goal")))
    qcheck("anomaly 2025-05-20",
           "What stands out about my session on 2025-05-20?",
           lambda a: _near(300, a, 0.1) or "long" in a.lower())
    qcheck("overdue revision",
           "How many revision chapters are overdue (next execution date before 2026-07-31)?",
           lambda a: _near(f["overdue_revision"], a, 0.3))
    qcheck("anti-hallucination: Sanskrit",
           "What do you know about my Sanskrit studies?",
           lambda a: any(k in a.lower() for k in ("no ", "nothing", "not found", "didn't find", "no data", "no record")))
    return ok


# ---------------------------------------------------------------------------
# Part B: multi-tool agent battery (agent.run / continue_run)
# ---------------------------------------------------------------------------

def _db_q(db: Path, sql: str, params: tuple = ()):
    with sqlite3.connect(str(db)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, params)]


async def _agent_task(label: str, prompt: str, db: Path) -> dict:
    async def on_status(s: str):
        print(f"      {s}", flush=True)
    try:
        result = await asyncio.wait_for(
            agent.run(CHAT_ID, prompt, on_status=on_status), timeout=420
        )
    except asyncio.TimeoutError:
        print(f"      [TIMEOUT] agent.run for {label}", flush=True)
        return {"final": {"type": "timeout"}, "had_preview": False}
    if result["type"] == "preview":
        print(f"      PREVIEW: {result['preview'][:140]!r}", flush=True)
        try:
            confirmed = await asyncio.wait_for(
                agent.continue_run(result["state_id"], True, on_status=on_status),
                timeout=300,
            )
        except asyncio.TimeoutError:
            print(f"      [TIMEOUT] continue_run for {label}", flush=True)
            confirmed = {"type": "timeout"}
        return {"final": confirmed, "had_preview": True}
    return {"final": result, "had_preview": False}


async def _agent_battery(db: Path) -> bool:
    ok = True
    goal_count_before = len(_db_q(db, "SELECT * FROM op_goals"))
    job_count_before = len(_db_q(db, "SELECT * FROM user_jobs"))

    r = await _agent_task("A: read-only SQL compare",
        "Use SQL to compare my year-2 average accuracy for Physics vs Maths. "
        "Report both numbers and say which subject is better.",
        db)
    text = r["final"]["response"].text
    ok = _check(ok, "A used sql_select", COUNTERS["reads"]["sql_select"] >= 1,
                f"reads={dict(COUNTERS['reads'])}, writes={dict(COUNTERS['writes'])}")
    ok = _check(ok, "A answer names Physics", "phys" in text.lower(), text[:120])
    ok = _check(ok, "A had no preview", not r["had_preview"])

    r = await _agent_task("B: read-only goals+sessions",
        "Show my active goals and my 3 most recent study sessions.", db)
    text = r["final"]["response"].text
    ok = _check(ok, "B used reads", COUNTERS["reads"]["sql_select"] >= 1)
    ok = _check(ok, "B mentions a goal or session",
                any(k in text.lower() for k in ("syllabus", "goal", "kinematics", "session")), text[:120])

    r = await _agent_task("C: write log study session",
        "Log a study session: 60 minutes, Physics MLE on Kinematics, "
        "15 questions attempted, 12 correct, block EB-2.", db)
    text = r["final"].get("response", {}).text if isinstance(r["final"], dict) else ""
    rows = _db_q(db, "SELECT task, accuracy_ratio, questions_attempted FROM ledger WHERE notion_page_id LIKE 'n-%'")
    ok = _check(ok, "C had preview+confirm", r["had_preview"])
    ok = _check(ok, "C wrote ledger row", len(rows) >= 1, f"rows={rows}")
    ok = _check(ok, "C accuracy 0.8 in mirror",
                any(row["accuracy_ratio"] == 0.8 for row in rows), text[:140])

    r = await _agent_task("D: write create goal",
        "Create a goal: score 80 marks in a JEE Main mock by December 2026, "
        "priority 2, subject Physics.", db)
    text = r["final"].get("response", {}).text if isinstance(r["final"], dict) else ""
    goal_count_after = len(_db_q(db, "SELECT * FROM op_goals"))
    ok = _check(ok, "D had preview+confirm", r["had_preview"])
    ok = _check(ok, "D created op_goals row", goal_count_after == goal_count_before + 1,
                f"{goal_count_before} -> {goal_count_after} | {text[:140]}")

    r = await _agent_task("E: write schedule reminder",
        "Set a daily reminder at 20:00 to do 30 minutes of Maths revision.", db)
    text = r["final"].get("response", {}).text if isinstance(r["final"], dict) else ""
    job_count_after = len(_db_q(db, "SELECT * FROM user_jobs WHERE chat_id = ?", (CHAT_ID,)))
    ok = _check(ok, "E had preview+confirm", r["had_preview"])
    ok = _check(ok, "E created user_jobs row", job_count_after == job_count_before + 1,
                f"{job_count_before} -> {job_count_after} | {text[:140]}")
    jobs = _db_q(db, "SELECT schedule_kind, run_time, action_text FROM user_jobs WHERE chat_id = ?", (CHAT_ID,))
    ok = _check(ok, "E daily 20:00 job",
                any(j["schedule_kind"] == "daily" and j["run_time"] == "20:00" for j in jobs),
                str(jobs))
    return ok


def main() -> int:
    if os.environ.get("SKIP_REAL_LLM"):
        print("SKIP_REAL_LLM set — skipping real-LLM battery.")
        return 0
    part = os.environ.get("TEST_2YEAR_PART", "AB")
    try:
        db, facts = _build_two_year_db()
        print(f"Seeded test mirror at {db}", flush=True)
        for k, v in sorted(facts.items()):
            print(f"  fact {k}: {v}", flush=True)
        _install_stubs(db)
        global _TEST_DB
        _TEST_DB = db
        agent_tools.execute_tool = _force_execute
        agent_tools.prepare_write = _force_prepare
        agent_tools.run_prepared_write = _force_run

        ok = True
        if "A" in part:
            print("\n=== PART A: data-biased questions (real router) ===", flush=True)
            ok = _ask_battery(db, facts)
        if "B" in part:
            print("\n=== PART B: multi-tool agent (real router) ===", flush=True)
            ok = asyncio.run(_agent_battery(db)) and ok

        print(f"\nTool usage: reads={dict(COUNTERS['reads'])} writes={dict(COUNTERS['writes'])}", flush=True)
        print("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED", flush=True)
        return 0 if ok else 1
    except Exception:
        import traceback
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(main())