"""Empirical capability-gates test: simulate 4 lifecycle stages and show how
the system personalizes responses based on what's unlocked.

Creates 4 SQLite databases with increasing data richness:
  Stage 1: Week 1   — fresh start, barely any data
  Stage 2: Week 4   — getting into the rhythm
  Stage 3: Week 16  — 3 months in, patterns forming
  Stage 4: 2 years  — full history, everything unlocked

Then runs capability_gates.check_all() on each and simulates how the bot
would respond to common user prompts at each stage.

Run: python test_capability_lifecycle.py
"""

from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from textwrap import indent

import capability_gates

# ---------------------------------------------------------------------------
# Stage builders — each creates a realistic SQLite mirror
# ---------------------------------------------------------------------------

SCHEMA_SCRIPT = """
-- Notion mirror tables
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT, task TEXT, exercise_type TEXT, chapter_text TEXT,
    questions_attempted INTEGER, questions_correct INTEGER,
    actual_time_min REAL, cognitive_yield REAL,
    created_time TEXT, archived INTEGER DEFAULT 0,
    page_content TEXT
);
CREATE TABLE IF NOT EXISTS doubts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT, core_concept TEXT, status TEXT DEFAULT 'open',
    created_time TEXT, archived INTEGER DEFAULT 0,
    page_content TEXT
);
CREATE TABLE IF NOT EXISTS revision (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT, core_concept TEXT, created_time TEXT,
    archived INTEGER DEFAULT 0
);

-- Operational tables
CREATE TABLE IF NOT EXISTS op_exams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT, exam_date TEXT, status TEXT DEFAULT 'Tentative',
    maximum_marks REAL, target_marks REAL,
    archived INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS op_work_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT, status TEXT, subject TEXT, title TEXT,
    created_time TEXT, archived INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS op_doubt_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doubt_title TEXT, subject TEXT, minutes INTEGER,
    outcome TEXT, created_time TEXT
);
CREATE TABLE IF NOT EXISTS op_goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_text TEXT, goal_type TEXT, active INTEGER DEFAULT 1,
    created_time TEXT
);

-- Execution discipline
CREATE TABLE IF NOT EXISTS execution_templates (
    template_key TEXT PRIMARY KEY, name TEXT, day_type TEXT
);
CREATE TABLE IF NOT EXISTS execution_blocks (
    block_key TEXT, template_key TEXT, seq INTEGER,
    start_hhmm TEXT, end_hhmm TEXT, kind TEXT, title TEXT, minutes INTEGER
);
CREATE TABLE IF NOT EXISTS block_confirmations (
    local_date TEXT, block_key TEXT, template_key TEXT,
    status TEXT, started_at TEXT, skipped_at TEXT,
    stopped_at TEXT, duration_min REAL, completion_source TEXT,
    PRIMARY KEY (local_date, block_key)
);
CREATE TABLE IF NOT EXISTS execution_day_types (
    local_date TEXT PRIMARY KEY, day_type TEXT, resolved_at TEXT
);

-- Learn formulas
CREATE TABLE IF NOT EXISTS learn_formulas (
    formula_id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT, chapter TEXT, topic TEXT, formula_text TEXT,
    source TEXT DEFAULT 'self', created_at TEXT, unlock_at TEXT,
    recalled_at TEXT, recall_count INTEGER DEFAULT 0,
    mastery TEXT DEFAULT 'new', notes TEXT DEFAULT ''
);

-- JEE analytics
CREATE TABLE IF NOT EXISTS op_jee_metadata (
    id INTEGER PRIMARY KEY CHECK(id=1), total_papers INTEGER,
    total_questions INTEGER, total_classified INTEGER,
    total_patterns INTEGER, total_chapters INTEGER, source_updated_at TEXT
);
CREATE TABLE IF NOT EXISTS op_jee_chapter_stats (
    subject TEXT, chapter TEXT, exam_type TEXT,
    total_questions INTEGER, repeating_questions INTEGER,
    unique_questions INTEGER, repeat_ratio REAL,
    easy_ratio REAL, medium_ratio REAL, hard_ratio REAL,
    importance_score REAL, needs_figure INTEGER,
    by_year_json TEXT, by_difficulty_json TEXT,
    by_question_type_json TEXT, sub_topics_json TEXT,
    PRIMARY KEY(subject, chapter, exam_type)
);
CREATE TABLE IF NOT EXISTS op_jee_patterns (
    pattern_id INTEGER PRIMARY KEY, subject TEXT, chapter TEXT,
    sub_topic TEXT, frequency INTEGER, years_json TEXT,
    exams_json TEXT, core_concept TEXT, key_formula TEXT,
    common_trap TEXT, difficulty TEXT, question_type TEXT
);

-- Commitment checks (for streaks)
CREATE TABLE IF NOT EXISTS commitment_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id TEXT, check_date TEXT, met INTEGER, target REAL, actual REAL
);

-- Onboarding
CREATE TABLE IF NOT EXISTS user_prefs (
    key TEXT PRIMARY KEY, value TEXT, active INTEGER DEFAULT 1
);
"""


def _seed_execution_blocks(conn: sqlite3.Connection) -> None:
    """Seed the coaching-day template with 10 blocks (verbatim from the PDF)."""
    conn.executescript("""
        INSERT INTO execution_templates VALUES
            ('coaching', 'Coaching Day', 'coaching'),
            ('non_coaching', 'Non-Coaching Day', 'non_coaching');
        INSERT INTO execution_blocks VALUES
            ('sleep', 'coaching', 1, '00:00', '06:00', 'sleep', 'Sleep', 360),
            ('morning', 'coaching', 2, '06:00', '08:00', 'break', 'Morning Routine', 120),
            ('exec_a', 'coaching', 3, '08:00', '10:30', 'study', 'Execution Block A', 150),
            ('break_a', 'coaching', 4, '10:30', '10:45', 'break', 'Break', 15),
            ('exec_b', 'coaching', 5, '10:45', '12:30', 'study', 'Execution Block B', 105),
            ('lunch', 'coaching', 6, '12:30', '14:00', 'break', 'Lunch + Rest', 90),
            ('exec_c', 'coaching', 7, '14:00', '17:00', 'study', 'Execution Block C', 180),
            ('sports', 'coaching', 8, '17:00', '18:00', 'break', 'Sports/Exercise', 60),
            ('exec_d', 'coaching', 9, '18:00', '22:15', 'study', 'Execution Block D', 255),
            ('exec_e', 'coaching', 10, '22:15', '01:00', 'study', 'Execution Block E', 165);
    """)


def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _days_ago(days: int) -> str:
    return (datetime.utcnow() - timedelta(days=days)).isoformat() + "Z"


def _days_from_now(days: int) -> str:
    return (datetime.utcnow() + timedelta(days=days)).isoformat() + "Z"


def _date_days_ago(days: int) -> str:
    return (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")


def _date_days_from_now(days: int) -> str:
    return (datetime.utcnow() + timedelta(days=days)).strftime("%Y-%m-%d")


SUBJECTS = ["Physics", "Chemistry", "Mathematics"]
CHAPTERS = {
    "Physics": ["Kinematics", "Laws of Motion", "Rotational Dynamics",
                "Gravitation", "Thermodynamics", "Electrostatics",
                "Current Electricity", "Magnetism", "EM Induction",
                "Optics", "Modern Physics", "Waves"],
    "Chemistry": ["Mole Concept", "Atomic Structure", "Chemical Bonding",
                  "Thermodynamics", "Equilibrium", "Redox",
                  "p-Block", "d-Block", "Coordination Compounds",
                  "Organic Basics", "Hydrocarbons", "Aldehydes & Ketones"],
    "Mathematics": ["Sets", "Complex Numbers", "Quadratic Equations",
                    "Sequences & Series", "Permutations", "Binomial Theorem",
                    "Trigonometry", "Coordinate Geometry", "Limits",
                    "Differentiation", "Integration", "Differential Equations"],
}


def _build_stage_1_week1(tmpdir: Path) -> Path:
    """Week 1: fresh start — barely any data, most capabilities locked."""
    db = tmpdir / "week1.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(SCHEMA_SCRIPT)
    # 2 ledger rows (studied twice)
    for subj in ["Physics", "Chemistry"]:
        conn.execute(
            "INSERT INTO ledger (subject, task, questions_attempted, questions_correct, "
            "actual_time_min, cognitive_yield, created_time, archived) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
            (subj, "practice problems", 20, 15, 30, 3.5, _days_ago(1), )
        )
    # 1 doubt
    conn.execute(
        "INSERT INTO doubts (subject, core_concept, status, created_time, archived) "
        "VALUES (?, ?, 'open', ?, 0)",
        ("Physics", "why does friction oppose motion", _days_ago(2))
    )
    # No exams, no work_items, no formulas, no execution blocks
    # 1 onboarding pref
    conn.execute("INSERT INTO user_prefs VALUES ('timezone', 'Asia/Kolkata', 1)")
    conn.commit()
    conn.close()
    return db


def _build_stage_2_week4(tmpdir: Path) -> Path:
    """Week 4: getting into the rhythm — ~20 sessions, 1 exam, some chapters."""
    db = tmpdir / "week4.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(SCHEMA_SCRIPT)
    _seed_execution_blocks(conn)
    # ~20 ledger rows across 4 weeks
    for day in range(28):
        if day % 3 == 0:  # ~every 3rd day
            continue  # skip some days (not every day)
        subj = SUBJECTS[day % 3]
        chap = CHAPTERS[subj][day % len(CHAPTERS[subj])]
        correct = 15 + (day % 5)
        conn.execute(
            "INSERT INTO ledger (subject, task, chapter_text, questions_attempted, "
            "questions_correct, actual_time_min, cognitive_yield, created_time, archived) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (subj, "practice", chap, 20, correct, 30 + day % 15, 2.5 + day % 2,
             _days_ago(day))
        )
    # 1 exam (JEE Main mock in 10 days)
    conn.execute(
        "INSERT INTO op_exams (title, exam_date, status, maximum_marks, target_marks, archived) "
        "VALUES (?, ?, 'Tentative', 300, 220, 0)",
        ("JEE Main Mock 1", _date_days_from_now(10))
    )
    # 3 Current Syllabus work items (not completed)
    for subj in SUBJECTS:
        chap = CHAPTERS[subj][0]
        conn.execute(
            "INSERT INTO op_work_items (kind, status, subject, title, created_time, archived) "
            "VALUES ('Current Syllabus', 'Inbox', ?, ?, ?, 0)",
            (subj, chap, _days_ago(20))
        )
    # 3 doubts
    for i, (subj, concept) in enumerate([
        ("Physics", "Newton's 3rd law edge cases"),
        ("Chemistry", "hybridization vs resonance"),
        ("Mathematics", "why L'Hopital fails"),
    ]):
        conn.execute(
            "INSERT INTO doubts (subject, core_concept, status, created_time, archived) "
            "VALUES (?, ?, 'open', ?, 0)",
            (subj, concept, _days_ago(27 - i * 3))
        )
    # 1 doubt attempt (not enough for teacher escalation)
    conn.execute(
        "INSERT INTO op_doubt_attempts (doubt_title, subject, minutes, outcome, created_time) "
        "VALUES (?, ?, ?, 'stuck', ?)",
        ("Newton's 3rd law edge cases", "Physics", 15, _days_ago(20))
    )
    # 1 goal
    conn.execute(
        "INSERT INTO op_goals (goal_text, goal_type, active, created_time) "
        "VALUES ('300 CY every day', 'daily_cy', 1, ?)",
        (_days_ago(25),)
    )
    # A few block confirmations
    for day in [1, 2, 3, 5, 7, 10, 12, 14, 17, 20, 22, 25]:
        status = "completed" if day % 3 != 0 else "started"
        started = _days_ago(day) + "T08:00:00Z"
        stopped = _days_ago(day) + "T08:30:00Z" if status == "completed" else None
        conn.execute(
            "INSERT INTO block_confirmations "
            "(local_date, block_key, template_key, status, started_at, "
            " skipped_at, stopped_at, duration_min, completion_source) "
            "VALUES (?, 'exec_a', 'coaching', ?, ?, NULL, ?, NULL, NULL)",
            (_date_days_ago(day), status, started, stopped)
        )
    # 2 formulas saved
    conn.execute(
        "INSERT INTO learn_formulas (subject, chapter, topic, formula_text, source, created_at, unlock_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("Physics", "Kinematics", "v = u + at", "v² = u² + 2as", "Mr. Sharma",
         _days_ago(5), _days_from_now(30))
    )
    conn.execute(
        "INSERT INTO learn_formulas (subject, chapter, topic, formula_text, source, created_at, unlock_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("Chemistry", "Mole Concept", "mole = mass/molar mass", "n = m/M", "self",
         _days_ago(3), _days_from_now(32))
    )
    conn.execute("INSERT INTO user_prefs VALUES ('timezone', 'Asia/Kolkata', 1)")
    conn.commit()
    conn.close()
    return db


def _build_stage_3_week16(tmpdir: Path) -> Path:
    """Week 16: 3 months in — ~100 sessions, multiple exams, completed chapters."""
    db = tmpdir / "week16.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(SCHEMA_SCRIPT)
    _seed_execution_blocks(conn)
    # ~100 ledger rows across 16 weeks
    for day in range(112):
        if day % 7 == 6:  # skip most Sundays
            continue
        subj = SUBJECTS[day % 3]
        chaps = CHAPTERS[subj]
        chap = chaps[(day // 7) % len(chaps)]
        correct = 12 + (day % 8)
        attempted = 20 + (day % 5)
        conn.execute(
            "INSERT INTO ledger (subject, task, chapter_text, questions_attempted, "
            "questions_correct, actual_time_min, cognitive_yield, created_time, archived) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (subj, "practice" if day % 4 != 0 else "mock",
             chap, attempted, correct, 30 + day % 30, 2.0 + day % 3,
             _days_ago(day))
        )
    # 5 exams (3 past with results, 2 upcoming)
    for i, (title, days_offset, is_past) in enumerate([
        ("JEE Main Mock 1", -60, True),
        ("JEE Main Mock 2", -30, True),
        ("JEE Advanced Mock 1", -15, True),
        ("JEE Main Mock 3", 14, False),
        ("JEE Full Mock", 45, False),
    ]):
        exam_date = _date_days_from_now(days_offset) if days_offset > 0 else _date_days_ago(-days_offset)
        conn.execute(
            "INSERT INTO op_exams (title, exam_date, status, maximum_marks, target_marks, archived) "
            "VALUES (?, ?, 'Official', 300, 220, 0)",
            (title, exam_date)
        )
    # 6 completed chapters + 6 in-progress
    for subj in SUBJECTS:
        for i in range(4):
            chap = CHAPTERS[subj][i]
            status = "Completed" if i < 2 else "Inbox"
            conn.execute(
                "INSERT INTO op_work_items (kind, status, subject, title, created_time, archived) "
                "VALUES ('Current Syllabus', ?, ?, ?, ?, 0)",
                (status, subj, chap, _days_ago(100 - i * 10))
            )
    # 15 doubts, 8 attempts
    for i in range(15):
        subj = SUBJECTS[i % 3]
        concept = f"doubt #{i+1} in {CHAPTERS[subj][i % len(CHAPTERS[subj])]}"
        conn.execute(
            "INSERT INTO doubts (subject, core_concept, status, created_time, archived) "
            "VALUES (?, ?, 'open', ?, 0)",
            (subj, concept, _days_ago(100 - i * 5))
        )
    for i in range(8):
        conn.execute(
            "INSERT INTO op_doubt_attempts (doubt_title, subject, minutes, outcome, created_time) "
            "VALUES (?, ?, ?, ?, ?)",
            (f"doubt #{i+1}", SUBJECTS[i % 3], 10 + i * 5,
             "solved" if i % 3 == 0 else "stuck", _days_ago(90 - i * 7))
        )
    # 1 daily goal
    conn.execute(
        "INSERT INTO op_goals (goal_text, goal_type, active, created_time) "
        "VALUES ('300 CY every day', 'daily_cy', 1, ?)",
        (_days_ago(100),)
    )
    # Block confirmations (most completed)
    for day in range(1, 112, 2):
        status = "completed" if day % 5 != 0 else "skipped"
        started = _days_ago(day) + "T08:00:00Z"
        stopped = _days_ago(day) + "T08:30:00Z" if status == "completed" else None
        conn.execute(
            "INSERT INTO block_confirmations "
            "(local_date, block_key, template_key, status, started_at, "
            "skipped_at, stopped_at, duration_min, completion_source) "
            "VALUES (?, 'exec_a', 'coaching', ?, ?, NULL, ?, NULL, NULL)",
            (_date_days_ago(day), status, started, stopped)
        )
    # 10 formulas (some past unlock)
    for i in range(10):
        subj = SUBJECTS[i % 3]
        chap = CHAPTERS[subj][i % len(CHAPTERS[subj])]
        unlock_days = 35
        created_day = 70 - i * 5  # created over the past 70 days
        unlock_at = _days_ago(created_day - unlock_days) if created_day > unlock_days else _days_from_now(unlock_days - created_day)
        conn.execute(
            "INSERT INTO learn_formulas (subject, chapter, topic, formula_text, source, created_at, unlock_at, mastery) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (subj, chap, f"formula {i+1}", f"expr_{i+1} = {i}*x + {i**2}",
             "Mr. Sharma" if i % 2 == 0 else "self",
             _days_ago(created_day), unlock_at,
             "mastered" if created_day > unlock_days else "new")
        )
    # JEE analytics loaded
    conn.execute(
        "INSERT INTO op_jee_metadata VALUES (1, 414, 10051, 4233, 1118, 64, '2026-07-15')"
    )
    # A few JEE chapter stats
    for subj in SUBJECTS:
        for chap in CHAPTERS[subj][:3]:
            conn.execute(
                "INSERT INTO op_jee_chapter_stats VALUES "
                "(?, ?, 'mains', 100, 30, 70, 0.3, 0.4, 0.4, 0.2, 85.5, 0, '{}', '{}', '{}', '{}')",
                (subj, chap)
            )
    conn.execute("INSERT INTO user_prefs VALUES ('timezone', 'Asia/Kolkata', 1)")
    conn.commit()
    conn.close()
    return db


def _build_stage_4_two_years(tmpdir: Path) -> Path:
    """2 years: full history — 500+ sessions, 20+ exams, all chapters done."""
    db = tmpdir / "two_years.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(SCHEMA_SCRIPT)
    _seed_execution_blocks(conn)
    # 500+ ledger rows across 2 years (730 days)
    for day in range(730):
        if day % 7 in (5, 6):  # skip most weekends
            continue
        subj = SUBJECTS[day % 3]
        chaps = CHAPTERS[subj]
        chap = chaps[(day // 30) % len(chaps)]
        correct = 14 + (day % 6)
        attempted = 20 + (day % 5)
        conn.execute(
            "INSERT INTO ledger (subject, task, chapter_text, questions_attempted, "
            "questions_correct, actual_time_min, cognitive_yield, created_time, archived) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (subj, "practice" if day % 5 != 0 else "mock",
             chap, attempted, correct, 30 + day % 20, 2.5 + day % 3,
             _days_ago(day))
        )
    # 20 exams (15 past with results, 5 upcoming)
    for i in range(20):
        days_offset = -700 + i * 40
        title = f"JEE {'Main' if i % 2 == 0 else 'Advanced'} Mock {i+1}"
        exam_date = _date_days_from_now(days_offset) if days_offset > 0 else _date_days_ago(-days_offset)
        conn.execute(
            "INSERT INTO op_exams (title, exam_date, status, maximum_marks, target_marks, archived) "
            "VALUES (?, ?, 'Official', 300, 220, 0)",
            (title, exam_date)
        )
    # All chapters completed
    for subj in SUBJECTS:
        for chap in CHAPTERS[subj]:
            conn.execute(
                "INSERT INTO op_work_items (kind, status, subject, title, created_time, archived) "
                "VALUES ('Current Syllabus', 'Completed', ?, ?, ?, 0)",
                (subj, chap, _days_ago(700))
            )
    # 50 doubts, 30+ attempts
    for i in range(50):
        subj = SUBJECTS[i % 3]
        concept = f"deep doubt #{i+1}: {CHAPTERS[subj][i % len(CHAPTERS[subj])]}"
        conn.execute(
            "INSERT INTO doubts (subject, core_concept, status, created_time, archived) "
            "VALUES (?, ?, ?, ?, 0)",
            (subj, concept, "open" if i % 4 == 0 else "resolved", _days_ago(700 - i * 10))
        )
    for i in range(35):
        conn.execute(
            "INSERT INTO op_doubt_attempts (doubt_title, subject, minutes, outcome, created_time) "
            "VALUES (?, ?, ?, ?, ?)",
            (f"doubt #{i+1}", SUBJECTS[i % 3], 10 + i * 3,
             "solved" if i % 2 == 0 else "stuck", _days_ago(680 - i * 15))
        )
    # 1 daily goal
    conn.execute(
        "INSERT INTO op_goals (goal_text, goal_type, active, created_time) "
        "VALUES ('300 CY every day', 'daily_cy', 1, ?)",
        (_days_ago(720),)
    )
    # Many block confirmations
    for day in range(1, 730, 3):
        status = "completed" if day % 7 != 0 else "skipped"
        started = _days_ago(day) + "T08:00:00Z"
        stopped = _days_ago(day) + "T08:30:00Z" if status == "completed" else None
        conn.execute(
            "INSERT INTO block_confirmations "
            "(local_date, block_key, template_key, status, started_at, "
            "skipped_at, stopped_at, duration_min, completion_source) "
            "VALUES (?, 'exec_a', 'coaching', ?, ?, NULL, ?, NULL, NULL)",
            (_date_days_ago(day), status, started, stopped)
        )
    # 40 formulas (most past unlock, some mastered)
    for i in range(40):
        subj = SUBJECTS[i % 3]
        chap = CHAPTERS[subj][i % len(CHAPTERS[subj])]
        created_day = 700 - i * 15
        unlock_at = _days_ago(created_day - 35)
        conn.execute(
            "INSERT INTO learn_formulas (subject, chapter, topic, formula_text, source, created_at, unlock_at, mastery, recall_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (subj, chap, f"formula {i+1}", f"expr_{i+1} = {i}*x + {i**2}",
             "Mr. Sharma" if i % 2 == 0 else "self",
             _days_ago(created_day), unlock_at,
             "mastered" if i < 30 else "learning",
             i // 5)
        )
    # Full JEE analytics
    conn.execute(
        "INSERT INTO op_jee_metadata VALUES (1, 414, 10051, 4233, 1118, 64, '2026-07-15')"
    )
    for subj in SUBJECTS:
        for chap in CHAPTERS[subj]:
            conn.execute(
                "INSERT INTO op_jee_chapter_stats VALUES "
                "(?, ?, 'mains', 100, 30, 70, 0.3, 0.4, 0.4, 0.2, 85.5, 0, '{}', '{}', '{}', '{}')",
                (subj, chap)
            )
            conn.execute(
                "INSERT INTO op_jee_chapter_stats VALUES "
                "(?, ?, 'advanced', 50, 20, 30, 0.4, 0.3, 0.4, 0.3, 70.0, 0, '{}', '{}', '{}', '{}')",
                (subj, chap)
            )
    # 50 patterns
    for i in range(50):
        conn.execute(
            "INSERT INTO op_jee_patterns (pattern_id, subject, chapter, sub_topic, "
            "frequency, years_json, exams_json, core_concept, key_formula, "
            "common_trap, difficulty, question_type) "
            "VALUES (?, ?, ?, ?, ?, '{}', '{}', ?, ?, ?, ?, ?)",
            (i + 1, SUBJECTS[i % 3], CHAPTERS[SUBJECTS[i % 3]][i % 12],
             f"sub_topic_{i}", 5 + i, f"concept_{i}", f"formula_{i}",
             f"trap_{i}", "Medium" if i % 2 == 0 else "Hard",
             "MCQ" if i % 3 == 0 else "Numerical")
        )
    conn.execute("INSERT INTO user_prefs VALUES ('timezone', 'Asia/Kolkata', 1)")
    conn.commit()
    conn.close()
    return db


# ---------------------------------------------------------------------------
# Prompt simulation — how the bot responds at each stage
# ---------------------------------------------------------------------------

PROMPTS = [
    ("What should I study today?", "planning"),
    ("How am I doing?", "progress_query"),
    ("What are my weak areas?", "weakness_query"),
    ("Give me a 2-day plan before my mock", "mock_prep_request"),
    ("What patterns repeat most in JEE?", "jee_analytics_query"),
    ("Quiz me on my formulas", "active_recall_request"),
    ("I'm stuck on Electrostatics, should I ask my teacher?", "teacher_escalation"),
    ("Start my study block", "discipline_start"),
    ("What did I do this week?", "weekly_report"),
    ("Tag my completed chapters", "chapter_classify"),
]


def _simulate_response(prompt: str, stage_name: str, gates: dict, db_path: Path) -> str:
    """Simulate how the bot would respond given unlocked/locked capabilities."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Count data for context
    ledger_count = conn.execute("SELECT COUNT(*) FROM ledger WHERE archived=0").fetchone()[0]
    doubt_count = conn.execute("SELECT COUNT(*) FROM doubts WHERE archived=0").fetchone()[0] \
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='doubts'").fetchone() else 0
    exam_count = conn.execute("SELECT COUNT(*) FROM op_exams WHERE archived=0").fetchone()[0] \
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='op_exams'").fetchone() else 0
    formula_count = conn.execute("SELECT COUNT(*) FROM learn_formulas").fetchone()[0] \
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='learn_formulas'").fetchone() else 0
    completed_chapters = conn.execute(
        "SELECT COUNT(*) FROM op_work_items WHERE archived=0 AND status='Completed'"
    ).fetchone()[0] if conn.execute("SELECT 1 FROM sqlite_master WHERE name='op_work_items'").fetchone() else 0

    conn.close()

    responses = {
        "What should I study today?": _resp_planning(gates, ledger_count, completed_chapters),
        "How am I doing?": _resp_progress(gates, ledger_count, doubt_count, exam_count),
        "What are my weak areas?": _resp_weakness(gates, ledger_count, doubt_count),
        "Give me a 2-day plan before my mock": _resp_mock_prep(gates, ledger_count, exam_count),
        "What patterns repeat most in JEE?": _resp_jee(gates),
        "Quiz me on my formulas": _resp_recall(gates, formula_count),
        "I'm stuck on Electrostatics, should I ask my teacher?": _resp_teacher(gates),
        "Start my study block": _resp_discipline(gates),
        "What did I do this week?": _resp_weekly(gates, ledger_count),
        "Tag my completed chapters": _resp_classify(gates, completed_chapters),
    }
    return responses.get(prompt, "(no simulation for this prompt)")


def _resp_planning(gates, ledger, chapters):
    if not gates["discipline"]["unlocked"]:
        return ("📋 I don't have your timetable set up yet. "
                "Once you sync your coaching portal or set up your chapters, "
                "I'll know exactly which study block you're in and what to study.\n\n"
                "For now, just log what you study (e.g. 'physics kinematics 20q 15c 30min') "
                "and I'll start building your profile.")
    if gates["mock_prep"]["unlocked"]:
        return ("🎯 You have a study block running right now. Based on your recent "
                "sessions and upcoming mock, I'd suggest focusing on your weakest chapter. "
                "I can propose a 2-day plan before your mock — just ask!")
    return ("⏰ You're in a study block! Log your session when done "
            "(e.g. '20 questions 15 correct 30 mins'). I need a few more sessions "
            "and an exam date before I can build a focused plan for you.")


def _resp_progress(gates, ledger, doubts, exams):
    if ledger < 3:
        return (f"📊 You've logged {ledger} session(s) so far. Too early for trends — "
                "keep studying and I'll start showing patterns after ~7 sessions.")
    if gates["weekly_report"]["unlocked"]:
        return (f"📊 You've logged {ledger} sessions across {exams} exams. "
                "Your weekly report is available — I can show streaks, "
                "subject breakdowns, and your CY trend over time.")
    return (f"📊 You've logged {ledger} sessions. You're building momentum! "
            "Once you hit 7 sessions, I'll start generating weekly reports.")


def _resp_weakness(gates, ledger, doubts):
    if ledger < 5:
        return (f"🔍 I need more data to identify weak areas. You've logged {ledger} "
                "sessions — keep going and I'll pinpoint which subjects/chapters "
                "need the most work after ~10 sessions.")
    if doubts > 0:
        return (f"🔍 You have {doubts} open doubts. Your weakest areas are where "
                "your doubts cluster. I can rank them by frequency and JEE weightage "
                "to tell you exactly what to fix first.")
    return "🔍 You're doing well across all subjects. Keep practicing!"


def _resp_mock_prep(gates, ledger, exams):
    if not gates["mock_prep"]["unlocked"]:
        reason = gates["mock_prep"]["reason"]
        return (f"🎯 I can't build a 2-day pre-mock plan yet — {reason}. "
                "Keep logging sessions and add your exam date, and I'll "
                "propose a focused plan once I know your patterns.")
    return ("🎯 I'm ready to build your 2-day pre-mock plan! I'll look at "
            "your recent sessions, weakest chapters, and JEE patterns to "
            "propose exactly what to study. Just confirm and I'll write the plan.")


def _resp_jee(gates):
    if not gates["jee_analytics"]["unlocked"]:
        return ("📊 JEE analytics not loaded yet. Once the data is available, "
                "I can tell you which chapters repeat most, what patterns to expect, "
                "and which chapters give the easiest marks.")
    return ("📊 JEE analytics loaded! I have data from 414 papers and 10,051 questions. "
            "I can rank chapters by ROI, show repeating patterns, and tell you "
            "exactly where to focus. Use /pattern, /chapter_ranking, or /roi_plan.")


def _resp_recall(gates, formulas):
    if not gates["active_recall"]["unlocked"]:
        return ("🧠 No formulas saved yet. Use /learn to store formulas your teacher "
                "teaches you — I'll unlock them for active recall after 35 days, "
                "so you retain them long-term.")
    due = gates.get("active_recall", {})
    return (f"🧠 You have {formulas} formula(s) stored. Those past their 35-day "
            "unlock window are available for active recall. Ready for a quiz?")


def _resp_teacher(gates):
    if not gates["teacher_window"]["unlocked"]:
        reason = gates["teacher_window"]["reason"]
        return (f"👨‍🏫 I'd hold off on asking your teacher — {reason}. "
                "Try working through it yourself first (at least 2 attempts, 30 min apart). "
                "Once you've logged 2 attempts, I'll flag it for teacher escalation.")
    return ("👨‍🏫 You've logged enough attempts on this doubt. It's time to escalate "
            "to your teacher — I'll prepare a summary of what you tried and where you're stuck.")


def _resp_discipline(gates):
    if not gates["discipline"]["unlocked"]:
        return ("⏰ Your timetable isn't set up yet. Once you sync your portal or "
                "I seed your execution blocks, I'll nudge you at the start of each "
                "study block with a 'Time to start' message.")
    return ("⏰ Your study block is active! Tap 'Start' to begin tracking time, "
            "and I'll keep you accountable with push reminders if you haven't started.")


def _resp_weekly(gates, ledger):
    if not gates["weekly_report"]["unlocked"]:
        return (f"📈 I need at least 7 sessions for a meaningful weekly report. "
                f"You have {ledger} — keep going!")
    return ("📈 Your weekly report is ready! I'll show your streak, "
            "subject breakdown, CY trend, block adherence, and what to focus on next week.")


def _resp_classify(gates, chapters):
    if not gates["chapter_classify"]["unlocked"]:
        return ("🏷️ I'll tag your chapters as mastery/revision/hard once you complete "
                "your first 'Current Syllabus' chapter. Keep studying and I'll analyze "
                "your accuracy + effort to propose the right tag.")
    return (f"🏷️ You have {chapters} completed chapter(s) eligible for classification. "
            "I can analyze your accuracy and cognitive yield to tag each as "
            "mastery, revision, or hard. Want me to propose tags?")


# ---------------------------------------------------------------------------
# Main test runner
# ---------------------------------------------------------------------------

def run() -> None:
    print("=" * 80)
    print("CAPABILITY GATES — LIFECYCLE SIMULATION")
    print("Testing how the system personalizes responses across 4 user journey stages")
    print("=" * 80)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        stages = [
            ("STAGE 1: Week 1 (Fresh Start)", _build_stage_1_week1(tmpdir)),
            ("STAGE 2: Week 4 (Getting Into Rhythm)", _build_stage_2_week4(tmpdir)),
            ("STAGE 3: Week 16 (3 Months In)", _build_stage_3_week16(tmpdir)),
            ("STAGE 4: 2 Years (Full History)", _build_stage_4_two_years(tmpdir)),
        ]

        for stage_name, db_path in stages:
            print("\n" + "=" * 80)
            print(f"  {stage_name}")
            print(f"  Database: {db_path.name}")
            print("=" * 80)

            # Get data counts
            conn = sqlite3.connect(str(db_path))
            ledger_count = conn.execute("SELECT COUNT(*) FROM ledger WHERE archived=0").fetchone()[0]
            doubt_count = conn.execute("SELECT COUNT(*) FROM doubts WHERE archived=0").fetchone()[0] \
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name='doubts'").fetchone() else 0
            exam_count = conn.execute("SELECT COUNT(*) FROM op_exams WHERE archived=0").fetchone()[0] \
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name='op_exams'").fetchone() else 0
            formula_count = conn.execute("SELECT COUNT(*) FROM learn_formulas").fetchone()[0] \
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name='learn_formulas'").fetchone() else 0
            block_count = conn.execute("SELECT COUNT(*) FROM execution_blocks").fetchone()[0] \
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name='execution_blocks'").fetchone() else 0
            completed_ch = conn.execute(
                "SELECT COUNT(*) FROM op_work_items WHERE archived=0 AND status='Completed'"
            ).fetchone()[0] if conn.execute("SELECT 1 FROM sqlite_master WHERE name='op_work_items'").fetchone() else 0
            doubt_attempts = conn.execute("SELECT COUNT(*) FROM op_doubt_attempts").fetchone()[0] \
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name='op_doubt_attempts'").fetchone() else 0
            jee_meta = conn.execute("SELECT COUNT(*) FROM op_jee_metadata").fetchone()[0] \
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name='op_jee_metadata'").fetchone() else 0
            conn.close()

            print(f"\n  📦 Data: {ledger_count} sessions | {doubt_count} doubts | "
                  f"{exam_count} exams | {completed_ch} completed chapters | "
                  f"{formula_count} formulas | {block_count} blocks | "
                  f"{doubt_attempts} doubt attempts | JEE={'loaded' if jee_meta else 'empty'}")

            # Check all gates
            gates = capability_gates.check_all(db_path=db_path)
            summary = capability_gates.progress_summary(db_path=db_path)

            print(f"\n  🔓 Unlocked ({summary['unlocked_count']}/{summary['total']}):")
            for cap in summary["unlocked"]:
                print(f"     ✅ {cap} — {gates[cap]['reason']}")

            print(f"\n  🔒 Locked ({len(summary['locked'])}/{summary['total']}):")
            for cap, reason in summary["locked"].items():
                print(f"     ❌ {cap} — {reason}")

            # Simulate prompts
            print(f"\n  💬 Prompt Simulation:")
            print("-" * 80)
            for prompt, _tag in PROMPTS:
                response = _simulate_response(prompt, stage_name, gates, db_path)
                print(f"\n  👤 User: \"{prompt}\"")
                print(f"  🤖 Bot:  {response}")

        print("\n" + "=" * 80)
        print("SUMMARY: How capability gates personalize the experience")
        print("=" * 80)
        print("""
  Week 1:  1/8 unlocked. The bot is a simple log + chat assistant.
           It doesn't pretend to know things it doesn't. It tells the user
           exactly what to do next (log sessions, set chapters, add exams).

  Week 4:  3/8 unlocked (agent_chat, discipline, maybe active_recall).
           The bot starts enforcing the timetable, tracking block starts,
           and saving formulas. It can see the user's pattern forming.

  Week 16: 7/8 unlocked. The bot is a full JEE coach — mock prep proposals,
           chapter classification, weekly reports, JEE analytics, teacher
           escalation. It knows the user's strengths, weaknesses, and
           what to study before each mock.

  2 years: 8/8 unlocked. The bot is a complete study companion — every
           capability available, full JEE analytics, 40+ formulas for
           active recall, deep doubt history, and 500+ sessions of pattern
           data. It can give precise, data-backed advice on every prompt.

  KEY INSIGHT: The bot NEVER pretends to know things it doesn't. At week 1,
  it doesn't generate a fake plan or pretend to understand JEE patterns.
  It tells the user exactly what's needed to unlock the next capability.
  This builds trust — the bot grows with the user.
""")


if __name__ == "__main__":
    run()