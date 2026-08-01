"""Deterministic fast-path: whole-message regex routing before the agent."""

from __future__ import annotations

import bot


def test_execution_log_matches():
    intent = bot._try_pattern_match("solved 20 questions 15 correct 30 mins")
    assert intent is not None
    assert intent.action == "log_execution"
    assert intent.fields["questions_attempted"] == 20
    assert intent.fields["questions_correct"] == 15
    assert intent.fields["actual_time_min"] == 30


def test_execution_log_variants():
    for text in (
        "did 25 qs, 18 correct, 40 min",
        "20 questions 15 correct 30 minutes",
        "completed 15 questions 12 correct 20 minutes",
        "attempted 10 q 7 right 15 m",
    ):
        intent = bot._try_pattern_match(text)
        assert intent is not None, text
        assert intent.action == "log_execution"


def test_compound_message_falls_through_to_agent():
    """A message that starts like a log but adds more must NOT be fast-pathed —
    the second half would be silently dropped."""
    assert bot._try_pattern_match("solved 20 questions 15 correct 30 mins and remind me tomorrow") is None
    assert bot._try_pattern_match("20 qs 15 correct 30 min, also had a doubt") is None


def test_incorrect_gt_attempted_not_matched():
    assert bot._try_pattern_match("20 qs 25 correct 30 min") is None


def test_doubt_prefix():
    intent = bot._try_pattern_match("doubt: sign of relative velocity")
    assert intent is not None
    assert intent.action == "log_doubt"
    assert intent.fields["core_concept"] == "sign of relative velocity"


def test_doubt_query():
    intent = bot._try_pattern_match("list doubts")
    assert intent is not None
    assert intent.action == "query"
    assert intent.database == "doubts"
    assert intent.filters.subject is None

    intent = bot._try_pattern_match("show physics doubts")
    assert intent is not None
    assert intent.filters.subject == "physics"


def test_doubt_query_compound_falls_through():
    assert bot._try_pattern_match("list doubts and my goals too") is None


def test_revision_query():
    intent = bot._try_pattern_match("show revisions")
    assert intent is not None
    assert intent.action == "query"
    assert intent.database == "revision"


def test_revision_log():
    intent = bot._try_pattern_match("revised wave optics")
    assert intent is not None
    assert intent.action == "log_revision"
    assert intent.fields["chapter_module"] == "wave optics"


def test_context_set_subject_and_chapter():
    intent = bot._try_pattern_match("starting physics wave optics")
    assert intent is not None
    assert intent.action == "set_context"
    assert intent.filters.subject == "Physics"
    assert intent.filters.chapter == "wave optics"


def test_context_set_full_phrase():
    intent = bot._try_pattern_match("starting eb-1 physics kinematics ex 2a")
    assert intent is not None
    assert intent.action == "set_context"
    assert intent.filters.block == "EB-1"
    assert intent.filters.subject == "Physics"
    assert intent.filters.chapter == "kinematics"
    assert intent.filters.exercise == "Ex 2A"


def test_context_set_subject_only():
    intent = bot._try_pattern_match("studying maths")
    assert intent is not None
    assert intent.action == "set_context"
    assert intent.filters.subject == "Maths"


def test_unknown_single_word_falls_through():
    """One unrecognized word after the verb is too vague — let the agent decide."""
    assert bot._try_pattern_match("starting xyzzy") is None


def test_unrelated_text_falls_through():
    assert bot._try_pattern_match("how am I doing this week?") is None
    assert bot._try_pattern_match("hello") is None
    assert bot._try_pattern_match("what is cognitive yield") is None
