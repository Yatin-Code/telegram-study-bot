"""Phase 8 core tests — deterministic coaching plan suggestions.

Covers the read-only ``coaching_planner`` module:
  * tomorrow schedule combining coaching classes + pre/post-class blocks
  * priority ordering and reason/evidence strings
  * conflicts and capacity (over-capacity → warnings + unplaced)
  * teacher-window doubt prep ("doubts where useful")
  * weekly plans and per-day capacity
  * determinism, no writes, no LLM

Usage:
    python test_coaching_planner.py
    .venv-test/bin/python -m pytest -q test_coaching_planner.py
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

import pytest

import coaching_planner as cp
import ntsc_coaching
import operational_store
import sync


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "coach.db"
    with sync.connect(path) as conn:
        sync.init_db(conn)
        operational_store.init_db(conn)
        ntsc_coaching.init_db(conn)
    return path


def insert(path, table, **values):
    physical = {
        "goals": "op_goals",
        "work_items": "op_work_items",
        "exams": "op_exams",
        "exam_questions": "op_exam_questions",
        "doubt_attempts": "op_doubt_attempts",
        "timetable": "op_timetable",
        "daily_plan": "op_daily_plan",
    }.get(table, table)
    base = {
        "notion_page_id": values.pop("notion_page_id", f"{table}-id"),
        "archived": 0,
        "last_synced_at": "2026-07-20T00:00:00+00:00",
        "raw_json": "{}",
    }
    if physical.startswith("op_"):
        base.setdefault("id", base["notion_page_id"])
        base.setdefault("created_time", "2026-07-20T00:00:00+00:00")
        base.setdefault("last_edited_time", "2026-07-20T00:00:00+00:00")
    base.update(values)
    with sqlite3.connect(path) as conn:
        if physical.startswith("op_"):
            operational_store.init_db(conn)
        cols = ",".join(f'"{key}"' for key in base)
        marks = ",".join("?" for _ in base)
        conn.execute(
            f'INSERT INTO "{physical}" ({cols}) VALUES ({marks})', tuple(base.values())
        )
        conn.commit()


def add_class(path, date, start, duration_min=60, class_type="Lecture", subjects="Physics"):
    with sqlite3.connect(path) as conn:
        ntsc_coaching.init_db(conn)
        conn.execute(
            "INSERT INTO coaching_classes "
            "(source_id,class_date,start_time,duration_min,class_type,subjects,source_updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (f"{date}|{start}", date, start, duration_min, class_type, subjects, "2026-07-20"),
        )
        conn.commit()


def timed_blocks(plan):
    return [
        b for b in plan["blocks"]
        if b["placed"] and b.get("start") and b.get("end")
    ]


def assert_no_overlaps(plan):
    blocks = timed_blocks(plan)
    by_date: dict[str, list] = {}
    for b in blocks:
        by_date.setdefault(b["date"], []).append(b)
    for date, day in by_date.items():
        ordered = sorted(day, key=lambda b: (b["start"], b["id"]))
        for first, second in zip(ordered, ordered[1:]):
            assert cp._to_min(second["start"]) >= cp._to_min(first["end"]), (
                f"overlap on {date}: {first['title']} and {second['title']}"
            )


def test_tomorrow_schedule_combines_classes_prepost_and_work(db):
    add_class(db, "2026-08-03", "09:00", subjects="Physics")
    add_class(db, "2026-08-03", "11:00", duration_min=90, subjects="Maths")
    insert(
        db, "revision", notion_page_id="r1", chapter_module="Kinematics",
        status="Pending", next_execution_date="2026-08-01",
    )
    insert(
        db, "work_items", notion_page_id="hw1", title="DPP Sheet 3",
        kind="Coaching Homework", status="Planned", due_date="2026-08-03",
        estimated_min=60, priority=90,
    )
    insert(
        db, "work_items", notion_page_id="b1", title="Old PYQs",
        kind="Backlog", status="Backlog", estimated_min=45, priority=40,
    )

    plan = cp.build_plan(target_date="2026-08-03", days=1, db_path=db)

    assert plan["plan_type"] == "daily"
    assert plan["dates"] == ["2026-08-03"]
    assert plan["generated_with"] == "deterministic"
    assert plan["llm_involved"] is False

    classes = [b for b in plan["blocks"] if b["kind"] == "Coaching Class"]
    assert len(classes) == 2
    physics = next(b for b in classes if b["title"].startswith("Physics"))
    assert (physics["start"], physics["end"], physics["duration_min"]) == ("09:00", "10:00", 60)
    assert physics["priority"] == cp.PRIO_FIXED
    maths = next(b for b in classes if b["title"].startswith("Maths"))
    assert (maths["start"], maths["end"], maths["duration_min"]) == ("11:00", "12:30", 90)

    pre = [b for b in plan["blocks"] if b["kind"] == "Pre-Class Prep"]
    assert pre and (pre[0]["start"], pre[0]["end"]) == ("08:30", "09:00")
    post = [b for b in plan["blocks"] if b["kind"] == "Post-Class Consolidation"]
    assert post and (post[-1]["start"], post[-1]["end"]) == ("12:30", "13:00")

    revision = [b for b in plan["blocks"] if b["kind"] == "Revision"]
    assert revision and (revision[0]["start"], revision[0]["end"]) == ("08:00", "08:30")
    assert "Kinematics" in revision[0]["title"]
    assert "Revision is due" in revision[0]["reason"]

    homework = [b for b in plan["blocks"] if b["kind"] == "Coaching Homework"]
    assert homework and (homework[0]["start"], homework[0]["end"]) == ("13:00", "14:00")
    backlog = [b for b in plan["blocks"] if b["kind"] == "Backlog"]
    assert backlog and backlog[0]["start"] == "14:00"

    assert plan["unplaced"] == []
    assert plan["warnings"] == []
    assert_no_overlaps(plan)

    cap = plan["capacity"]["2026-08-03"]
    assert cap["fixed_minutes"] == 150
    assert cap["committed_minutes"] == 0
    assert cap["planned_minutes"] == 405
    assert cap["budget_minutes"] == 600
    assert cap["minutes_headroom"] == 600 - 405
    assert cap["cy_capacity"] == 300

    assert plan["sources"]["classes"] == 2
    assert plan["sources"]["revision"] == 1
    assert plan["sources"]["homework"] == 1
    assert plan["sources"]["backlog"] == 1

    # every block is validated
    for block in plan["blocks"] + plan["unplaced"]:
        cp._validate_block(block)


def add_test(path, title, test_date, syllabus=""):
    with sqlite3.connect(path) as conn:
        ntsc_coaching.init_db(conn)
        conn.execute(
            "INSERT INTO coaching_tests "
            "(source_id,title,test_date,course_id,batch,goal,syllabus,source_updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (f"test-{title}", title, test_date, "", "", "", syllabus, "2026-07-20"),
        )
        conn.commit()


def test_priority_order_and_reasons_drive_placement(db):
    add_class(db, "2026-08-03", "09:00", subjects="Physics")
    insert(
        db, "revision", notion_page_id="r1", chapter_module="Thermodynamics",
        status="Pending", next_execution_date="2026-08-01",
    )
    insert(
        db, "work_items", notion_page_id="hw1", title="Maths DPP",
        kind="Coaching Homework", status="Planned", due_date="2026-08-03",
        estimated_min=60,
    )
    insert(
        db, "work_items", notion_page_id="b1", title="Old Kinematics sheets",
        kind="Backlog", status="Backlog", estimated_min=45, priority=30,
    )
    add_test(db, "Test 1", "2026-08-06", syllabus="Thermo; Kinematics")

    plan = cp.build_plan(target_date="2026-08-03", days=1, db_path=db)

    generic_kinds = [
        b["kind"] for b in timed_blocks(plan)
        if b["kind"] not in ("Coaching Class", "Pre-Class Prep", "Post-Class Consolidation")
    ]
    assert generic_kinds == ["Revision", "Coaching Homework", "Test Prep", "Backlog"]

    by_kind = {b["kind"]: b for b in plan["blocks"]}
    assert by_kind["Revision"]["priority"] == cp.PRIO_REVISION
    assert by_kind["Coaching Homework"]["priority"] >= cp.PRIO_HOMEWORK
    assert by_kind["Test Prep"]["priority"] == cp.PRIO_TEST_PREP
    assert by_kind["Backlog"]["priority"] == cp.PRIO_BACKLOG

    assert "Revision is due" in by_kind["Revision"]["reason"]
    assert "homework" in by_kind["Coaching Homework"]["reason"].lower()
    assert by_kind["Coaching Homework"]["evidence"]["due_date"] == "2026-08-03"
    assert "on 2026-08-06" in by_kind["Test Prep"]["reason"]
    assert "Thermo" in by_kind["Test Prep"]["reason"]
    assert by_kind["Test Prep"]["evidence"]["days_to_test"] == 3
    assert "backlog" in by_kind["Backlog"]["reason"].lower()


def test_over_capacity_warns_and_unplaces_candidates(db):
    insert(
        db, "daily_plan", notion_page_id="p1", title="Planned block 1",
        plan_date="2026-08-03", sequence=1, status="Planned",
        expected_cy=250, estimated_min=300,
    )
    insert(
        db, "daily_plan", notion_page_id="p2", title="Planned block 2",
        plan_date="2026-08-03", sequence=2, status="Planned",
        expected_cy=100, estimated_min=300,
    )
    insert(
        db, "revision", notion_page_id="r1", chapter_module="Kinematics",
        status="Pending", next_execution_date="2026-08-01",
    )

    plan = cp.build_plan(target_date="2026-08-03", days=1, db_path=db)

    committed = [b for b in plan["blocks"] if b["kind"] == "Planned Item"]
    assert len(committed) == 2
    assert all(b["start"] is None and b["end"] is None for b in committed)
    assert all(b["placed"] for b in committed)

    cap = plan["capacity"]["2026-08-03"]
    assert cap["planned_cy"] == 350.0
    assert cap["cy_capacity"] == 300
    assert cap["cy_headroom"] == 0
    assert cap["planned_minutes"] == 600
    assert cap["minutes_headroom"] == 0

    assert any("over the 300 CY ceiling" in w for w in plan["warnings"])
    revision_unplaced = [u for u in plan["unplaced"] if u["kind"] == "Revision"]
    assert revision_unplaced
    assert revision_unplaced[0]["skip_reason"] == "capacity exceeded"
    assert revision_unplaced[0]["placed"] is False
    assert any(
        "could not place 'Revision: Kinematics' on 2026-08-03" in w for w in plan["warnings"]
    )


def test_teacher_window_doubt_prep_is_anchored(db):
    add_class(db, "2026-08-03", "09:00", subjects="Physics")
    insert(
        db, "timetable", notion_page_id="window-1", title="Teacher doubts",
        weekday="Monday", start_time="15:00", end_time="16:00",
        kind="Doubt Window", subject="Physics", active=1, questions_allowed=1,
    )
    insert(
        db, "doubts", notion_page_id="d1", core_concept="relative velocity sign",
        subject="Physics", status="Unresolved", workflow_state="Attempting",
    )
    for n in (1, 2):
        insert(
            db, "doubt_attempts", notion_page_id=f"a{n}", title=f"Attempt {n}",
            doubt="d1", valid=1, outcome="Unsolved", attempt_no=n,
            attempted_at=f"2026-08-0{n}T09:00:00+00:00",
        )

    plan = cp.build_plan(target_date="2026-08-03", days=1, db_path=db)

    window = [b for b in plan["blocks"] if b["kind"] == "Doubt Window"]
    assert window and (window[0]["start"], window[0]["end"]) == ("15:00", "16:00")
    doubt_work = [b for b in plan["blocks"] if b["kind"] == "Doubt Work"]
    assert doubt_work
    assert (doubt_work[0]["start"], doubt_work[0]["end"]) == ("14:40", "15:00")
    assert "Teacher window at 15:00" in doubt_work[0]["reason"]
    assert "d1" in doubt_work[0]["evidence"]["doubts"]
    assert doubt_work[0]["priority"] == cp.PRIO_DOUBT_WINDOW
    assert_no_overlaps(plan)


def test_weekly_plan_has_per_day_capacity(db):
    add_class(db, "2026-08-03", "09:00", subjects="Physics")
    add_class(db, "2026-08-05", "09:00", subjects="Maths")
    insert(
        db, "revision", notion_page_id="r1", chapter_module="Kinematics",
        status="Pending", next_execution_date="2026-08-03",
    )

    plan = cp.build_plan(target_date="2026-08-03", days=7, db_path=db)

    assert plan["plan_type"] == "weekly"
    assert plan["dates"] == [
        (dt.date(2026, 8, 3) + dt.timedelta(days=i)).isoformat() for i in range(7)
    ]
    class_dates = {b["date"] for b in plan["blocks"] if b["kind"] == "Coaching Class"}
    assert class_dates == {"2026-08-03", "2026-08-05"}
    assert set(plan["capacity"].keys()) == set(plan["dates"])
    for day in plan["dates"]:
        cap = plan["capacity"][day]
        assert cap["cy_capacity"] == 300
        assert cap["planned_minutes"] <= cap["budget_minutes"]
    assert plan["totals"]["days"] == 7
    assert_no_overlaps(plan)


def test_deterministic_no_writes_no_llm(db):
    add_class(db, "2026-08-03", "09:00", subjects="Physics")
    insert(
        db, "revision", notion_page_id="r1", chapter_module="Kinematics",
        status="Pending", next_execution_date="2026-08-01",
    )
    with sqlite3.connect(db) as conn:
        before = conn.execute("SELECT COUNT(*) FROM op_daily_plan").fetchone()[0]

    first = cp.build_plan(target_date="2026-08-03", days=1, db_path=db)
    second = cp.build_plan(target_date="2026-08-03", days=1, db_path=db)

    with sqlite3.connect(db) as conn:
        after = conn.execute("SELECT COUNT(*) FROM op_daily_plan").fetchone()[0]
    assert before == after == 0
    assert first == second
    assert first["llm_involved"] is False
    assert first["generated_with"] == "deterministic"


def test_no_classes_cached_warns(db):
    insert(
        db, "revision", notion_page_id="r1", chapter_module="Kinematics",
        status="Pending", next_execution_date="2026-08-01",
    )
    plan = cp.build_plan(target_date="2026-08-03", days=1, db_path=db)
    assert any("no coaching classes cached" in w for w in plan["warnings"])
    assert plan["sources"]["classes"] == 0


def test_capacity_override_is_bounded(db):
    add_class(db, "2026-08-03", "09:00", subjects="Physics")
    insert(
        db, "revision", notion_page_id="r1", chapter_module="Kinematics",
        status="Pending", next_execution_date="2026-08-01",
    )
    plan = cp.build_plan(
        target_date="2026-08-03", days=1, db_path=db,
        capacity={"max_daily_minutes": 150, "cy_capacity": 100},
    )
    cap = plan["capacity"]["2026-08-03"]
    assert cap["budget_minutes"] == 150
    assert cap["cy_capacity"] == 100
    # fixed class (60) + pre (30) + post (30) = 120 ≤ 150, but the revision's
    # 30 min would push to 150... exactly at budget → still fits, backlog none.
    assert plan["totals"]["planned_minutes"] == 150
    # nothing may exceed the budget
    assert cap["planned_minutes"] <= 150


def test_class_times_are_normalized_from_flexible_portal_clocks(db):
    """Portal start times like '9:30' or '04:00 PM' must not crash the plan."""
    add_class(db, "2026-08-03", "9:30", subjects="Physics")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE coaching_classes SET start_time='04:00 PM' "
            "WHERE start_time='9:30' AND class_date='2026-08-03'"
        )
        conn.execute(
            "INSERT INTO coaching_classes "
            "(source_id,class_date,start_time,duration_min,class_type,subjects,source_updated_at) "
            "VALUES ('x|2026-08-03|09:30', '2026-08-03', '09:30', 60, 'Lecture', 'Maths', '2026-07-20')"
        )
        conn.commit()
    plan = cp.build_plan(target_date="2026-08-03", days=1, db_path=db)
    times = sorted(
        (b["start"], b["end"]) for b in plan["blocks"]
        if b["kind"] == "Coaching Class"
    )
    assert ("04:00", "05:00") in times
    assert ("09:30", "10:30") in times
    assert all(
        cp._HHMM_RE.match(str(b.get("start") or "")) for b in plan["blocks"]
        if b["placed"] and b.get("start") is not None
    )
    assert_no_overlaps(plan)


def test_build_plan_rejects_bad_range(db):
    with pytest.raises(ValueError):
        cp.build_plan(target_date="2026-08-03", days=0, db_path=db)


def test_no_free_gap_marks_candidate_unplaced(db):
    add_class(db, "2026-08-03", "09:00", subjects="Physics")
    add_class(db, "2026-08-03", "11:00", subjects="Maths")
    add_class(db, "2026-08-03", "20:00", subjects="Chem")
    insert(
        db, "work_items", notion_page_id="hw1", title="Long homework",
        kind="Coaching Homework", status="Planned", due_date="2026-08-03",
        estimated_min=600,
    )
    plan = cp.build_plan(
        target_date="2026-08-03", days=1, db_path=db,
        capacity={"max_daily_minutes": 1500},
    )
    long_hw = [u for u in plan["unplaced"] if u["title"] == "Long homework"]
    assert long_hw
    assert long_hw[0]["skip_reason"] == "no free gap of 600 minutes"
    assert any("no free gap of 600 minutes" in w for w in plan["warnings"])
    assert_no_overlaps(plan)


def test_plan_tomorrow_uses_local_tomorrow(db, monkeypatch):
    monkeypatch.setattr(
        cp.session_context, "local_today_iso", lambda: "2026-08-02"
    )
    add_class(db, "2026-08-03", "09:00", subjects="Physics")
    plan = cp.plan_tomorrow(db_path=db)
    assert plan["dates"] == ["2026-08-03"]
    assert any(b["kind"] == "Coaching Class" for b in plan["blocks"])


def test_two_day_plan_uses_mock_prep_on_t2_and_t1(db):
    """A 2-day plan before a test 2 days out carries Mock Prep, not Test Prep."""
    add_test(db, "Mock 1", "2026-08-08", syllabus="Thermo; Kinematics")
    plan = cp.build_plan(target_date="2026-08-06", days=2, db_path=db)
    kinds_by_date = {
        date: [b["kind"] for b in plan["blocks"] if b["date"] == date]
        for date in plan["dates"]
    }
    for date in ("2026-08-06", "2026-08-07"):
        assert kinds_by_date[date] == ["Mock Prep"], f"Mock Prep on {date}"
        title = [b["title"] for b in plan["blocks"] if b["date"] == date]
        assert all("Mock 1" in t for t in title)
    assert plan["sources"]["mock_prep_blocks"] == 2
    assert plan["sources"]["test_prep_blocks"] == 0


def test_mock_prep_block_replaces_test_prep_on_t2_and_t1(db):
    """T-2 and T-1 get a dedicated Mock Prep block; T-3 and T-0 keep generic Test Prep."""
    add_test(db, "Mock 1", "2026-08-08", syllabus="Thermo; Kinematics")
    plan = cp.build_plan(target_date="2026-08-05", days=4, db_path=db)
    blocks = {
        date: [b for b in plan["blocks"] if b["date"] == date]
        for date in plan["dates"]
    }
    t3, t2, t1, t0 = "2026-08-05", "2026-08-06", "2026-08-07", "2026-08-08"

    for date in (t2, t1):
        mock = [b for b in blocks[date] if b["kind"] == "Mock Prep"]
        assert mock, f"expected a Mock Prep block on {date}"
        assert "Mock 1" in mock[0]["title"]
        assert mock[0]["duration_min"] == 60
        assert mock[0]["priority"] == cp.PRIO_MOCK_PREP
        assert not [b for b in blocks[date] if b["kind"] == "Test Prep"], (
            f"generic Test Prep must not appear on {date}"
        )

    for date in (t3, t0):
        assert [b for b in blocks[date] if b["kind"] == "Test Prep"], (
            f"generic Test Prep expected on {date}"
        )
        assert not [b for b in blocks[date] if b["kind"] == "Mock Prep"]

    assert plan["sources"]["mock_prep_blocks"] == 2
    assert plan["sources"]["test_prep_blocks"] == 2


def test_mock_prep_drops_when_no_free_gap(db):
    """A Mock Prep block is dropped with a skip_reason when the day has no free gap."""
    add_test(db, "Mock 2", "2026-08-06", syllabus="Thermo")
    # T-2 (2026-08-04): pack the whole 08:00-22:00 window with back-to-back
    # fixed classes so no 60-minute gap remains for the Mock Prep block.
    for hour in range(8, 22, 2):
        add_class(
            db, "2026-08-04", f"{hour:02d}:00", duration_min=120, subjects="Physics",
        )
    plan = cp.build_plan(
        target_date="2026-08-04", days=1, db_path=db,
        capacity={"max_daily_minutes": 1500},
    )
    unplaced = [u for u in plan["unplaced"] if u["kind"] == "Mock Prep"]
    assert unplaced, "Mock Prep should be dropped when the day has no free gap"
    assert unplaced[0]["skip_reason"] == "no free gap of 60 minutes"
    assert unplaced[0]["placed"] is False
    assert any("could not place 'Mock Prep: Mock 2'" in w for w in plan["warnings"])


def main() -> int:
    import sys

    import pytest

    return pytest.main([__file__, "-q"])


if __name__ == "__main__":
    sys.exit(main())
