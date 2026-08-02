"""Chapter auto-classification (todo 10) — baseline + classify tests.

Covers the chapter mastery/revision/hard classification system:
  * two new local tables: chapter_classifications + chapter_lifecycle_meta
  * classify_candidates eligibility (tracked-from-start gate, no retroactive)
  * LLM proposal → 'proposed' row (deterministic 'revision' default on failure)
  * Confirm / Dismiss callback handlers updating status
  * the chapter_classify scan (claim-deduped, one proposal per chapter_key)
  * bot.py job + callback registrations

The baseline characterization tests at the top pin the PRE-change behavior
(no tables, no scan, callbacks unchanged) and pass on untouched code; the
new-API tests below them are the failing-first proof.

Usage:
    .venv-test/bin/python -m pytest -q -p no:cacheprovider -m "not live" test_chapter_classification.py
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import sqlite3
import types
from pathlib import Path

import pytest

import bot
import operational_store
import study_domain as sd
import sync
import session_context

TZ = session_context.local_now().tzinfo


def _at(hour, minute):
    return dt.datetime(2026, 8, 2, hour, minute, tzinfo=TZ)


# ---------------------------------------------------------------------------
# Baseline characterization — pins the CURRENT (pre-todo-10) behavior.
# These must PASS on unchanged code and are flipped by this todo.
# ---------------------------------------------------------------------------

def test_baseline_no_chapter_classification_tables(tmp_path):
    """Before todo 10: neither classification table exists in a fresh db."""
    db = tmp_path / "discipline.db"
    import execution_discipline as ed
    ed._connect(db)
    with sqlite3.connect(db) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    assert "chapter_classifications" not in tables
    assert "chapter_lifecycle_meta" not in tables


def test_baseline_no_classify_scan_in_bot_source():
    """Before todo 10: no chapter_classify scan and no classify: callback.

    Flipped by this todo — the scan, job name and callback handler now exist.
    """
    source = Path(bot.__file__).read_text(encoding="utf-8")
    assert "_chapter_classify_scan" in source
    assert 'pattern=r"^classify:"' in source
    assert 'name="chapter_classify"' in source


def test_baseline_discipline_and_agent_callbacks_unchanged():
    """Before todo 10: the ^discipline: and ^agent: handlers are present once."""
    source = Path(bot.__file__).read_text(encoding="utf-8")
    assert source.count(
        'CallbackQueryHandler(on_discipline_callback, pattern=r"^discipline:")'
    ) == 1
    assert source.count(
        'CallbackQueryHandler(on_agent_callback, pattern=r"^agent:")'
    ) == 1


# ---------------------------------------------------------------------------
# New-API tests (todo 10). These FAIL before the implementation lands and
# must be green after it. The module is imported inside the functions so the
# baseline tests above keep passing against the pre-change tree.
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    """Fresh tmp SQLite file per test (classification + op + ledger tables)."""
    import chapter_classification as cc
    path = tmp_path / "cc.db"
    with sqlite3.connect(str(path)) as conn:
        conn.row_factory = sqlite3.Row
        operational_store.init_db(conn)
        sync.init_db(conn)
        cc.init_db(conn)
    return path


def _cc():
    import chapter_classification as cc
    return cc


def _query(db, sql, params=()):
    """Row-factory select helper for direct assertion reads."""
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql, params).fetchall()


def _seed_activation(db, activated_at):
    cc = _cc()
    with sqlite3.connect(db) as conn:
        conn.execute(
            f"INSERT OR IGNORE INTO {cc.LIFECYCLE_TABLE} (singleton, activated_at) "
            "VALUES (1, ?)",
            (activated_at,),
        )
        conn.commit()


def _seed_work_item(db, *, title="Physics: Kinematics", subject="Physics",
                    chapter="Kinematics", created_time, status="Completed",
                    kind="Current Syllabus"):
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO op_work_items (id, notion_page_id, archived, title, kind, status, "
            "subject, chapter, created_time, last_edited_time, last_synced_at, raw_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                title, title, 0, title, kind, status, subject, chapter,
                created_time, created_time, created_time, "{}",
            ),
        )
        conn.commit()


def _seed_ledger(db, *, notion_page_id, subject, chapter_text, date,
                 accuracy_ratio, cognitive_yield, actual_time_min=30,
                 created_time=None, archived=0):
    created_time = created_time or f"{date}T09:00:00.000+00:00"
    with sync.connect(db) as conn:
        sync.init_db(conn)
        conn.execute(
            "INSERT INTO ledger (notion_page_id, created_time, date, subject, "
            "chapter_text, accuracy_ratio, cognitive_yield, actual_time_min, "
            "questions_attempted, questions_correct, last_synced_at, raw_json, archived) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                notion_page_id, created_time, date, subject, chapter_text,
                accuracy_ratio, cognitive_yield, actual_time_min,
                10, 7, "2026-07-20T00:00:00+00:00", "{}", archived,
            ),
        )
        conn.commit()


def _completed_chapter_env(db):
    """Lifecycle activated 07-01; Kinematics completed 07-15 with 2 ledger rows."""
    _seed_activation(db, "2026-07-01T00:00:00.000+00:00")
    _seed_work_item(
        db,
        title="Physics: Kinematics", subject="Physics", chapter="Kinematics",
        created_time="2026-07-15T00:00:00.000+00:00",
    )
    _seed_ledger(db, notion_page_id="k1", subject="Physics", chapter_text="Kinematics",
                 date="2026-07-16", accuracy_ratio=0.5, cognitive_yield=40)
    _seed_ledger(db, notion_page_id="k2", subject="Physics", chapter_text="Kinematics",
                 date="2026-07-18", accuracy_ratio=0.9, cognitive_yield=80)


# --- tables / identity ------------------------------------------------------

def test_classify_tables_exist_with_correct_columns(db):
    cc = _cc()
    with sqlite3.connect(db) as conn:
        cc_info = {r[1] for r in conn.execute(
            f"PRAGMA table_info({cc.CLASSIFICATIONS_TABLE})"
        )}
        meta_info = {r[1] for r in conn.execute(
            f"PRAGMA table_info({cc.LIFECYCLE_TABLE})"
        )}
    assert cc_info == {
        "chapter_key", "subject", "chapter", "tag", "accuracy_ratio",
        "cognitive_yield", "evidence_count", "reason", "decided_at", "status",
    }
    assert meta_info == {"singleton", "activated_at"}


def test_classify_tables_registered_in_local_sql_tables():
    from config import ownership
    cc = _cc()
    assert cc.CLASSIFICATIONS_TABLE in ownership.LOCAL_SQL_TABLES
    assert cc.LIFECYCLE_TABLE in ownership.LOCAL_SQL_TABLES


def test_chapter_key_for_matches_sha1_style():
    cc = _cc()
    expected = hashlib.sha1(b"Physics||Kinematics").hexdigest()[:16]
    assert cc.chapter_key_for("Physics", "Kinematics") == expected
    assert len(cc.chapter_key_for("Physics", "Kinematics")) == 16


def test_ensure_activated_writes_once_insert_or_ignore(db):
    cc = _cc()
    cc.ensure_activated(_at(8, 0), db_path=db)
    cc.ensure_activated(_at(9, 0), db_path=db)
    rows = _query(db, f"SELECT * FROM {cc.LIFECYCLE_TABLE}")
    assert len(rows) == 1
    assert rows[0]["singleton"] == 1


# --- classify_candidates eligibility -----------------------------------------

def test_classify_candidates_finds_eligible_completed_chapter(db):
    _completed_chapter_env(db)
    cc = _cc()
    candidates = cc.classify_candidates(_at(8, 0), db_path=db)
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand["subject"] == "Physics"
    assert cand["chapter"] == "Kinematics"
    assert cand["chapter_key"] == cc.chapter_key_for("Physics", "Kinematics")
    assert cand["metrics"]["sessions"] == 2
    assert cand["metrics"]["avg_accuracy"] == pytest.approx(0.7)


def test_classify_candidates_skips_pre_activation_chapter(db):
    """A chapter tracked before activation is never retroactively classified."""
    _seed_activation(db, "2026-07-20T00:00:00.000+00:00")
    _seed_work_item(
        db, subject="Physics", chapter="Kinematics",
        created_time="2026-07-10T00:00:00.000+00:00",
    )
    _seed_ledger(db, notion_page_id="k1", subject="Physics", chapter_text="Kinematics",
                 date="2026-07-12", accuracy_ratio=0.7, cognitive_yield=60)
    cc = _cc()
    assert cc.classify_candidates(_at(8, 0), db_path=db) == []


def test_classify_candidates_skips_already_classified(db):
    _completed_chapter_env(db)
    cc = _cc()
    cc._write_proposal({
        "chapter_key": cc.chapter_key_for("Physics", "Kinematics"),
        "subject": "Physics", "chapter": "Kinematics",
        "metrics": {"avg_accuracy": 0.7, "avg_cy": 60.0, "sessions": 2},
    }, tag="revision", reason="seeded", db_path=db)
    assert cc.classify_candidates(_at(8, 0), db_path=db) == []


def test_classify_candidates_skips_chapter_with_no_metrics(db):
    _seed_activation(db, "2026-07-01T00:00:00.000+00:00")
    _seed_work_item(
        db, subject="Physics", chapter="Kinematics",
        created_time="2026-07-15T00:00:00.000+00:00",
    )
    cc = _cc()
    assert cc.classify_candidates(_at(8, 0), db_path=db) == []


def test_classify_candidates_skips_when_metrics_predate_work_item(db):
    """Ledger first_date before the work item creation → not eligible."""
    _seed_activation(db, "2026-07-01T00:00:00.000+00:00")
    _seed_work_item(
        db, subject="Physics", chapter="Kinematics",
        created_time="2026-07-18T00:00:00.000+00:00",
    )
    _seed_ledger(db, notion_page_id="k1", subject="Physics", chapter_text="Kinematics",
                 date="2026-07-16", accuracy_ratio=0.7, cognitive_yield=60)
    cc = _cc()
    assert cc.classify_candidates(_at(8, 0), db_path=db) == []


def test_classify_candidates_empty_without_activation(db):
    _seed_work_item(
        db, subject="Physics", chapter="Kinematics",
        created_time="2026-07-15T00:00:00.000+00:00",
    )
    cc = _cc()
    assert cc.classify_candidates(_at(8, 0), db_path=db) == []


def test_classify_candidates_not_completed_is_skipped(db):
    _seed_activation(db, "2026-07-01T00:00:00.000+00:00")
    _seed_work_item(
        db, subject="Physics", chapter="Kinematics",
        created_time="2026-07-15T00:00:00.000+00:00", status="Active",
    )
    _seed_ledger(db, notion_page_id="k1", subject="Physics", chapter_text="Kinematics",
                 date="2026-07-16", accuracy_ratio=0.7, cognitive_yield=60)
    cc = _cc()
    assert cc.classify_candidates(_at(8, 0), db_path=db) == []


# --- propose_chapter_classification ------------------------------------------

def test_propose_writes_proposed_row_with_mocked_tag(db, monkeypatch):
    _completed_chapter_env(db)
    cc = _cc()

    def fake_llm(messages):
        return "mastery — strong accuracy and yield"

    monkeypatch.setattr(cc, "_classify_llm_complete", fake_llm)
    candidate = cc.classify_candidates(_at(8, 0), db_path=db)[0]
    proposal = cc.propose_chapter_classification(candidate, db_path=db)
    assert proposal["tag"] == "mastery"
    assert proposal["status"] == "proposed"
    assert proposal["chapter_key"] == candidate["chapter_key"]
    rows = _query(
        db,
        f"SELECT * FROM {cc.CLASSIFICATIONS_TABLE} WHERE chapter_key=?",
        (candidate["chapter_key"],),
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["tag"] == "mastery"
    assert row["status"] == "proposed"
    assert row["subject"] == "Physics"
    assert row["chapter"] == "Kinematics"
    assert row["evidence_count"] == 2
    assert row["accuracy_ratio"] == pytest.approx(0.7)


def test_propose_defaults_revision_when_llm_raises(db, monkeypatch):
    _completed_chapter_env(db)
    cc = _cc()

    def boom(messages):
        raise RuntimeError("quota")

    monkeypatch.setattr(cc, "_classify_llm_complete", boom)
    candidate = cc.classify_candidates(_at(8, 0), db_path=db)[0]
    proposal = cc.propose_chapter_classification(candidate, db_path=db)
    assert proposal["tag"] == "revision"
    assert proposal["status"] == "proposed"
    rows = _query(
        db,
        f"SELECT status, tag FROM {cc.CLASSIFICATIONS_TABLE} WHERE chapter_key=?",
        (candidate["chapter_key"],),
    )
    assert (rows[0]["tag"], rows[0]["status"]) == ("revision", "proposed")


def test_propose_defaults_revision_for_invalid_tag(db, monkeypatch):
    _completed_chapter_env(db)
    cc = _cc()
    monkeypatch.setattr(cc, "_classify_llm_complete", lambda m: "genius — nope")
    candidate = cc.classify_candidates(_at(8, 0), db_path=db)[0]
    proposal = cc.propose_chapter_classification(candidate, db_path=db)
    assert proposal["tag"] == "revision"


def test_propose_redacts_metrics_payload(db, monkeypatch):
    _completed_chapter_env(db)
    cc = _cc()
    captured = {}

    def fake_llm(messages):
        captured["system"] = messages[0]["content"]
        return "revision — ok"

    monkeypatch.setattr(cc, "_classify_llm_complete", fake_llm)
    candidate = cc.classify_candidates(_at(8, 0), db_path=db)[0]
    cc.propose_chapter_classification(candidate, db_path=db)
    assert "You are the study coach of a JEE aspirant" in captured["system"]
    assert "Kinematics" in captured["system"]
    assert "mastery, revision, or hard" in captured["system"]


# --- confirm / dismiss --------------------------------------------------------

def test_confirm_callback_updates_status_to_confirmed(db):
    _completed_chapter_env(db)
    cc = _cc()
    key = cc.chapter_key_for("Physics", "Kinematics")
    cc._write_proposal({
        "chapter_key": key, "subject": "Physics", "chapter": "Kinematics",
        "metrics": {"avg_accuracy": 0.7, "avg_cy": 60.0, "sessions": 2},
    }, tag="mastery", reason="ok", db_path=db)
    row = cc.confirm_classification(key, db_path=db)
    assert row is not None
    assert row["status"] == "confirmed"
    assert row["tag"] == "mastery"


def test_dismiss_callback_updates_status_to_dismissed(db):
    _completed_chapter_env(db)
    cc = _cc()
    key = cc.chapter_key_for("Physics", "Kinematics")
    cc._write_proposal({
        "chapter_key": key, "subject": "Physics", "chapter": "Kinematics",
        "metrics": {"avg_accuracy": 0.7, "avg_cy": 60.0, "sessions": 2},
    }, tag="hard", reason="ok", db_path=db)
    row = cc.dismiss_classification(key, db_path=db)
    assert row is not None
    assert row["status"] == "dismissed"
    assert row["tag"] == "hard"


def test_confirm_unknown_chapter_returns_none(db):
    cc = _cc()
    assert cc.confirm_classification("nope", db_path=db) is None
    assert cc.dismiss_classification("nope", db_path=db) is None


# --- bot wiring: scan + callback ---------------------------------------------

def _send_capture(context, sent):
    async def fake_send(_bot, chat_id, text, **_kw):
        sent.append((chat_id, text, _kw.get("reply_markup")))

    return fake_send


async def _run_classify_scan(db, now, monkeypatch, sent):
    import chapter_classification as cc
    cc.ensure_activated(now, db_path=db)
    sent.clear()
    monkeypatch.setattr(bot, "_send_markdown", _send_capture(None, sent))
    monkeypatch.setattr(bot.sync, "sync_once_locked", lambda **_kw: {})
    monkeypatch.setattr(bot, "telegram_allowed_user_id", lambda: 1)
    monkeypatch.setattr(cc, "_classify_llm_complete", lambda messages: "mastery — good")
    monkeypatch.setattr(session_context, "local_now", lambda: now)
    monkeypatch.setattr(cc, "DEFAULT_DB_PATH", db)
    context = types.SimpleNamespace(bot=types.SimpleNamespace())
    await bot._chapter_classify_scan(context)
    return sent


def test_classify_scan_sends_one_proposal_claim_dedup(db, monkeypatch):
    _completed_chapter_env(db)
    sent = []
    asyncio.run(_run_classify_scan(db, _at(8, 0), monkeypatch, sent))
    assert len(sent) == 1
    text = sent[0][1]
    assert "Finished Kinematics" in text
    assert "accuracy 70%" in text
    assert "CY 60" in text
    assert "propose: **mastery**" in text
    markup = sent[0][2]
    assert markup is not None
    buttons = [
        b.callback_data for row in markup.inline_keyboard for b in row
    ]
    key = _cc().chapter_key_for("Physics", "Kinematics")
    assert f"classify:confirm:{key}" in buttons
    assert f"classify:dismiss:{key}" in buttons
    # Claim-dedup: the second scan sends nothing.
    sent2 = []
    asyncio.run(_run_classify_scan(db, _at(8, 5), monkeypatch, sent2))
    assert sent2 == []


def test_classify_scan_claims_and_releases_on_failure(db, monkeypatch):
    _completed_chapter_env(db)
    import chapter_classification as cc
    import reminders
    sent = []

    async def failing_send(_bot, chat_id, text, **_kw):
        raise RuntimeError("network")

    async def run_scan():
        monkeypatch.setattr(bot, "_send_markdown", failing_send)
        monkeypatch.setattr(bot.sync, "sync_once_locked", lambda **_kw: {})
        monkeypatch.setattr(bot, "telegram_allowed_user_id", lambda: 1)
        monkeypatch.setattr(cc, "_classify_llm_complete", lambda messages: "revision — ok")
        monkeypatch.setattr(session_context, "local_now", lambda: _at(8, 0))
        monkeypatch.setattr(cc, "DEFAULT_DB_PATH", db)
        context = types.SimpleNamespace(bot=types.SimpleNamespace())
        await bot._chapter_classify_scan(context)

    asyncio.run(run_scan())
    key = cc.chapter_key_for("Physics", "Kinematics")
    # The failed-send claim is released so a later scan can retry.
    assert reminders.claim(f"classify:{key}", db_path=db) is True


def test_classify_callback_confirm_edits_and_confirms(db, monkeypatch):
    _completed_chapter_env(db)
    import chapter_classification as cc
    key = cc.chapter_key_for("Physics", "Kinematics")
    cc._write_proposal({
        "chapter_key": key, "subject": "Physics", "chapter": "Kinematics",
        "metrics": {"avg_accuracy": 0.7, "avg_cy": 60.0, "sessions": 2},
    }, tag="mastery", reason="ok", db_path=db)
    monkeypatch.setattr(cc, "DEFAULT_DB_PATH", db)
    edits = []
    query = _fake_query(f"classify:confirm:{key}", edits)
    update = types.SimpleNamespace(callback_query=query)
    asyncio.run(bot.on_classify_callback(update, types.SimpleNamespace()))
    assert edits and edits[0] == "✅ Kinematics tagged mastery."
    assert cc.confirm_classification(key, db_path=db)["status"] == "confirmed"


def test_classify_callback_dismiss_edits_and_dismisses(db, monkeypatch):
    _completed_chapter_env(db)
    import chapter_classification as cc
    key = cc.chapter_key_for("Physics", "Kinematics")
    cc._write_proposal({
        "chapter_key": key, "subject": "Physics", "chapter": "Kinematics",
        "metrics": {"avg_accuracy": 0.7, "avg_cy": 60.0, "sessions": 2},
    }, tag="revision", reason="ok", db_path=db)
    monkeypatch.setattr(cc, "DEFAULT_DB_PATH", db)
    edits = []
    query = _fake_query(f"classify:dismiss:{key}", edits)
    update = types.SimpleNamespace(callback_query=query)
    asyncio.run(bot.on_classify_callback(update, types.SimpleNamespace()))
    assert edits and edits[0] == "Okay, Kinematics left untagged."
    assert cc.dismiss_classification(key, db_path=db)["status"] == "dismissed"


def _fake_query(data, edits):
    class Query:
        def __init__(self):
            self.data = data

        async def answer(self):
            pass

        async def edit_message_text(self, text, **_kw):
            edits.append(text)

    return Query()


def test_classify_job_registered_once_in_post_init():
    source = Path(bot.__file__).read_text(encoding="utf-8")
    assert source.count('name="chapter_classify"') == 1
    assert source.count("_guard_scheduled(_chapter_classify_scan)") == 1
    assert "interval=600" in source
    job_guard = source.index("if application.job_queue is not None:")
    job_site = source.index("_guard_scheduled(_chapter_classify_scan)")
    assert job_site > job_guard


def test_classify_callback_registered_once():
    source = Path(bot.__file__).read_text(encoding="utf-8")
    assert source.count(
        'CallbackQueryHandler(on_classify_callback, pattern=r"^classify:")'
    ) == 1
    # Still registered right after the discipline handler.
    discipline_site = source.index(
        'CallbackQueryHandler(on_discipline_callback, pattern=r"^discipline:")'
    )
    classify_site = source.index(
        'CallbackQueryHandler(on_classify_callback, pattern=r"^classify:")'
    )
    assert classify_site > discipline_site
