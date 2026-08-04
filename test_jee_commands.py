"""JEE bot command handlers + agent tools: offline tests on tmp mirrors.

Handlers in bot.py read the MODULE-LEVEL ``DEFAULT_DB_PATH`` of agent_tools /
jee_data_loader (not bot.DEFAULT_DB_PATH), so both are monkeypatched to a tmp
SQLite mirror seeded from a tiny synthetic final_data.json. No real LLM, no
network, no real sqlite_mirror.db.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import types

import pytest

import agent_tools
import bot
import config.settings
import jee_data_loader


# ---------------------------------------------------------------------------
# Synthetic fixture (duplicated here — never import from test_jee_data_loader)
# ---------------------------------------------------------------------------

def _synthetic_data() -> dict:
    return {
        "metadata": {
            "total_papers": 24,
            "total_questions": 1200,
            "total_classified": 1180,
            "total_patterns": 3,
            "total_chapters": 4,
        },
        "chapter_stats": {
            "physics": {
                "Electric Charges and Fields": {
                    "total_questions": 10,
                    "repeating_questions": 4,
                    "unique_questions": 6,
                    "by_exam": {"mains": 6, "advanced": 4},
                    "by_difficulty": {"Easy": 3, "Medium": 4, "Hard": 3},
                    "by_year": {"2020": 3, "2021": 4, "2022": 3},
                    "by_question_type": {"MCQ": 7, "Numerical": 3},
                    "sub_topics": ["Coulomb's law"],
                },
                "Kinematics": {
                    "total_questions": 20,
                    "repeating_questions": 10,
                    "unique_questions": 10,
                    "by_exam": {"mains": 12, "advanced": 8},
                    "by_difficulty": {"Easy": 10, "Medium": 8, "Hard": 2},
                    "by_year": {"2021": 10, "2022": 10},
                    "by_question_type": {"MCQ": 15, "Numerical": 5},
                    "sub_topics": ["1D motion", "2D motion"],
                },
                "Unclassified": {
                    "total_questions": 8,
                    "repeating_questions": 8,
                    "unique_questions": 0,
                    "by_exam": {"mains": 8},
                    "by_difficulty": {"Easy": 0, "Medium": 0, "Hard": 8},
                },
            },
            "chemistry": {
                "Chemical Bonding": {
                    "total_questions": 15,
                    "repeating_questions": 6,
                    "unique_questions": 9,
                    "by_exam": {"mains": 15},
                    "by_difficulty": {"Easy": 5, "Medium": 5, "Hard": 5},
                    "by_year": {"2019": 15},
                    "by_question_type": {"MCQ": 15},
                    "sub_topics": ["VSEPR"],
                },
            },
        },
        "chapter_rankings": [
            {
                "subject": "physics",
                "chapter": "Kinematics",
                "total": 20,
                "roi_score": 0.95,
                "repeat_ratio": 0.5,
                "easy_ratio": 0.5,
            },
        ],
        "patterns": [
            {
                "cluster_id": 1,
                "subject": "physics",
                "chapter": "Electric Charges and Fields",
                "sub_topic": "Coulomb's law",
                "frequency": 8,
                "years": [2020, 2021, 2022],
                "exams": ["mains"],
                "core_concept": "Inverse square law",
                "key_formula": "F = kq1q2/r^2",
                "common_trap": "Sign errors",
                "difficulty": "Easy",
                "question_type": "MCQ",
            },
            {
                "cluster_id": 2,
                "subject": "physics",
                "chapter": "Kinematics",
                "sub_topic": "Projectile motion",
                "frequency": 5,
                "years": [2021, 2022],
                "exams": ["mains", "advanced"],
                "core_concept": "Range equation",
                "key_formula": "R = v^2 sin(2θ)/g",
                "common_trap": "Angle doubling",
                "difficulty": "Medium",
                "question_type": "Numerical",
            },
            {
                "cluster_id": 3,
                "subject": "chemistry",
                "chapter": "Chemical Bonding",
                "sub_topic": "VSEPR geometry",
                "frequency": 3,
                "years": [2019],
                "exams": ["mains"],
                "core_concept": "Geometry prediction",
                "key_formula": "AXE notation",
                "common_trap": "Lone pairs",
                "difficulty": "Hard",
                "question_type": "MCQ",
            },
        ],
        "trends": [
            {
                "subject": "physics",
                "chapter": "Electric Charges and Fields",
                "year_counts": {"2020": 3, "2021": 4, "2022": 3},
            },
            {
                "subject": "chemistry",
                "chapter": "Chemical Bonding",
                "year_counts": {"2019": 15},
            },
        ],
        "questions": [
            {
                "subject": "physics",
                "chapter": "Electric Charges and Fields",
                "sub_topic": "Coulomb's law",
                "difficulty": "Easy",
                "exam_type": "mains",
                "exam": "mains",
                "year": 2020,
                "question_type": "MCQ",
                "cluster_id": 1,
                "cluster_size": 8,
                "cluster_years": [2020, 2021, 2022],
            },
            {
                "subject": "physics",
                "chapter": "Kinematics",
                "sub_topic": "Projectile motion",
                "difficulty": "Medium",
                "exam_type": "advanced",
                "exam": "advanced",
                "year": 2022,
                "question_type": "Numerical",
                "cluster_id": 2,
                "cluster_size": 5,
                "cluster_years": [2021, 2022],
            },
        ],
    }


# ---------------------------------------------------------------------------
# Fake telegram plumbing (repo convention: SimpleNamespace update + fake msg)
# ---------------------------------------------------------------------------

async def _allow_all(_update) -> bool:
    return False


class FakeMessage:
    def __init__(self, text: str = ""):
        self.text = text
        self.replies: list[str] = []

    async def reply_text(self, text: str, **kwargs):
        self.replies.append(text)


def _make_update(text: str = ""):
    return types.SimpleNamespace(
        effective_message=FakeMessage(text=text),
        effective_chat=types.SimpleNamespace(id=42),
        effective_user=types.SimpleNamespace(id=42),
    )


def _run_handler(handler, text: str = "") -> str:
    update = _make_update(text)
    asyncio.run(handler(update, types.SimpleNamespace()))
    assert update.effective_message.replies, "handler sent no reply"
    return update.effective_message.replies[0]


def _patch_defaults(monkeypatch, db):
    monkeypatch.setattr(agent_tools, "DEFAULT_DB_PATH", db)
    monkeypatch.setattr(jee_data_loader, "DEFAULT_DB_PATH", db)
    monkeypatch.setattr(bot, "_reject_if_unauthorized", _allow_all)


@pytest.fixture()
def jee_db(tmp_path, monkeypatch):
    db = tmp_path / "jee_loaded.db"
    data = tmp_path / "final_data.json"
    data.write_text(json.dumps(_synthetic_data()), encoding="utf-8")
    jee_data_loader.load(data_path=data, db_path=db)
    _patch_defaults(monkeypatch, db)
    return db


@pytest.fixture()
def empty_db(tmp_path, monkeypatch):
    db = tmp_path / "jee_empty.db"
    conn = sqlite3.connect(str(db))
    jee_data_loader.init_db(conn)
    conn.close()
    _patch_defaults(monkeypatch, db)
    return db


# ---------------------------------------------------------------------------
# Handlers with data loaded — reply contains seeded values
# ---------------------------------------------------------------------------

def test_pattern_command_replies_with_patterns(jee_db):
    reply = _run_handler(
        bot.pattern_command, "/pattern Physics Electric Charges and Fields"
    )
    assert "Top JEE repeating patterns" in reply
    assert "Coulomb" in reply
    assert "8×" in reply


def test_chapter_ranking_command_replies(jee_db):
    reply = _run_handler(bot.chapter_ranking_command, "/ranking mains Physics")
    assert "Chapter ranking" in reply
    assert "Kinematics" in reply
    assert "0.95" in reply


def test_jee_stats_command_replies(jee_db):
    reply = _run_handler(bot.jee_stats_command, "/jee_stats")
    assert "24 papers" in reply
    assert "1200 questions" in reply
    assert "3 repeating patterns" in reply


def test_roi_plan_command_replies(jee_db):
    reply = _run_handler(bot.roi_plan_command, "/roi Physics")
    assert "ROI plan" in reply
    assert "Kinematics" in reply


def test_dashboard_command_replies_with_url(jee_db, monkeypatch):
    monkeypatch.setattr(
        config.settings, "jee_dashboard_url", lambda: "https://example.test/dash"
    )
    reply = _run_handler(bot.dashboard_command, "/dashboard")
    assert "JEE dashboard" in reply
    assert "example.test/dash" in reply


# ---------------------------------------------------------------------------
# Handlers with empty tables — exact _JEE_NOT_LOADED fallback
# ---------------------------------------------------------------------------

def test_pattern_command_empty_tables_fallback(empty_db):
    reply = _run_handler(bot.pattern_command, "/pattern Physics")
    assert reply == bot._JEE_NOT_LOADED


def test_chapter_ranking_command_empty_tables_fallback(empty_db):
    reply = _run_handler(bot.chapter_ranking_command, "/ranking mains")
    assert reply == bot._JEE_NOT_LOADED


def test_jee_stats_command_empty_tables_fallback(empty_db):
    reply = _run_handler(bot.jee_stats_command, "/jee_stats")
    assert reply == bot._JEE_NOT_LOADED


def test_roi_plan_command_empty_tables_fallback(empty_db):
    reply = _run_handler(bot.roi_plan_command, "/roi")
    assert reply == bot._JEE_NOT_LOADED


def test_dashboard_command_empty_tables_fallback(empty_db, monkeypatch):
    monkeypatch.setattr(
        config.settings, "jee_dashboard_url", lambda: "https://example.test/dash"
    )
    reply = _run_handler(bot.dashboard_command, "/dashboard")
    assert reply == bot._JEE_NOT_LOADED


# ---------------------------------------------------------------------------
# Agent tools — direct calls against the temp mirror
# ---------------------------------------------------------------------------

def test_get_jee_patterns_returns_seeded_rows(jee_db):
    result = agent_tools.get_jee_patterns(
        "Physics", "Electric Charges and Fields", db_path=jee_db
    )
    assert "error" not in result
    assert len(result["patterns"]) == 1
    assert result["patterns"][0]["chapter"] == "Electric Charges and Fields"
    assert result["patterns"][0]["frequency"] == 8


def test_get_chapter_roi_ordered_by_importance(jee_db):
    result = agent_tools.get_chapter_roi("mains", "Physics", db_path=jee_db)
    chapters = result["chapters"]
    assert chapters
    assert chapters[0]["chapter"] == "Kinematics"
    assert chapters[0]["importance_score"] == pytest.approx(0.95)
    assert all(c["chapter"].lower() != "unclassified" for c in chapters)


def test_get_exam_weightage_returns_entries(jee_db):
    result = agent_tools.get_exam_weightage(db_path=jee_db)
    assert "weightage" in result
    assert len(result["weightage"]) == 3
    assert result["weightage"][0]["chapter"] == "Kinematics"


def test_get_jee_patterns_limit_is_bounded(jee_db):
    result = agent_tools.get_jee_patterns("Physics", limit=999, db_path=jee_db)
    assert "error" not in result
    assert len(result["patterns"]) <= 20


def test_get_jee_patterns_injection_safe(jee_db):
    result = agent_tools.get_jee_patterns(subject="' OR 1=1 --", db_path=jee_db)
    assert "error" not in result
    assert result["patterns"] == []


def test_jee_tools_registered_in_agent_surface():
    names = {"get_jee_patterns", "get_chapter_roi", "get_exam_weightage"}
    assert names <= set(agent_tools.READ_TOOLS)
    spec_names = {s["name"] for s in agent_tools.TOOL_SPECS}
    assert names <= spec_names


def test_execute_tool_dispatches_jee_patterns(jee_db):
    result = agent_tools.execute_tool(
        "get_jee_patterns",
        {"subject": "Physics", "chapter": "Electric Charges and Fields", "limit": 3},
        db_path=jee_db,
    )
    assert "patterns" in result
    assert result["patterns"]


def test_execute_tool_unknown_name_returns_error(jee_db):
    result = agent_tools.execute_tool("nonexistent_jee_tool", {}, db_path=jee_db)
    assert result.get("error") is True


# ---------------------------------------------------------------------------
# /learn and /insights handlers (personalization wiring)
# ---------------------------------------------------------------------------

def _seed_ledger_rows(db, rows):
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ledger ("
            " notion_page_id TEXT, archived INTEGER DEFAULT 0, subject TEXT,"
            " chapter_text TEXT, actual_time_min REAL, questions_attempted INTEGER,"
            " questions_correct INTEGER)"
        )
        for i, (subject, chapter, minutes, attempted, correct) in enumerate(rows):
            conn.execute(
                "INSERT INTO ledger (notion_page_id, archived, subject, chapter_text,"
                " actual_time_min, questions_attempted, questions_correct) "
                "VALUES (?,0,?,?,?,?,?)",
                (f"l{i}", subject, chapter, minutes, attempted, correct),
            )
        conn.commit()


def test_learn_command_saves_formula(jee_db, monkeypatch):
    import learn_formulas
    monkeypatch.setattr(learn_formulas, "DEFAULT_DB_PATH", jee_db)
    reply = _run_handler(bot.learn_command, "/learn Physics | Kinematics | v = u + at")
    assert "Saved formula" in reply
    assert "v = u + at" in reply
    rows = learn_formulas.list(db_path=jee_db)
    assert len(rows) == 1
    assert rows[0]["subject"] == "Physics"
    assert rows[0]["chapter"] == "Kinematics"


def test_learn_command_stats_reply(jee_db, monkeypatch):
    import learn_formulas
    monkeypatch.setattr(learn_formulas, "DEFAULT_DB_PATH", jee_db)
    reply = _run_handler(bot.learn_command, "/learn")
    assert "Formula memory" in reply
    assert "Due for recall" in reply


def test_learn_command_bad_format_returns_usage(jee_db, monkeypatch):
    import learn_formulas
    monkeypatch.setattr(learn_formulas, "DEFAULT_DB_PATH", jee_db)
    reply = _run_handler(bot.learn_command, "/learn Physics")
    assert "Format:" in reply


def test_insights_command_all_replies(jee_db, monkeypatch):
    import jee_insights
    monkeypatch.setattr(jee_insights, "DEFAULT_DB_PATH", jee_db)
    _seed_ledger_rows(jee_db, [
        ("Physics", "Electric Charges and Fields", 300, 20, 15),
        ("Physics", "Electric Charges and Fields", 200, 20, 15),
        ("Physics", "Electric Charges and Fields", 250, 20, 14),
        ("Physics", "Kinematics", 10, 5, 4),
        ("Physics", "Kinematics", 8, 5, 3),
    ])
    reply = _run_handler(bot.insights_command, "/insights")
    assert "Personalized Insights" in reply
    assert "ok" or "over-allocated" or "under-allocated" in reply.lower() or "roi" in reply.lower()


def test_insights_command_doubts_replies(jee_db, monkeypatch):
    import jee_insights
    monkeypatch.setattr(jee_insights, "DEFAULT_DB_PATH", jee_db)
    with sqlite3.connect(str(jee_db)) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS doubts ("
            " notion_page_id TEXT, archived INTEGER DEFAULT 0, subject TEXT,"
            " core_concept TEXT, status TEXT, created_time TEXT)"
        )
        conn.execute(
            "INSERT INTO doubts (notion_page_id, archived, subject, core_concept,"
            " status, created_time) VALUES ('d1',0,'Physics',"
            " 'Electric Charges and Fields confusion','open','2026-07-10')"
        )
        conn.commit()
    reply = _run_handler(bot.insights_command, "/insights doubts")
    assert "Doubt prioritization" in reply


def test_insights_command_no_jee_data_fallback(empty_db, monkeypatch):
    import jee_insights
    monkeypatch.setattr(jee_insights, "DEFAULT_DB_PATH", empty_db)
    reply = _run_handler(bot.insights_command, "/insights")
    lowered = reply.lower()
    assert "not loaded" in lowered or "sessions" in lowered or "unavailable" in lowered
