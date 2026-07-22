"""
Tests for formulas.py — the local replication of Notion's computed columns.

Pure-Python, no LLM/SQLite/Notion needed. Run: python3 test_formulas.py

Verifies the t_q lookup table, the cognitive yield / theory yield formulas
(including the velocity cap difference), and the simpler accuracy/minutes
formulas against hand-computed expected values.
"""

from __future__ import annotations

import sys

import formulas


def _check(ok, label, cond, extra=""):
    print(f"[{'OK ' if cond else 'BAD'}] {label}{(' -> ' + extra) if extra else ''}")
    return ok and cond


def test_t_q_lookup() -> bool:
    ok = True
    print("=== t_q lookup table ===")
    cases = [
        ("Chem", "Ex 1A", 2.0),
        ("Physics", "Ex 1A", 2.5),
        ("Maths", "Ex 1A", 4.5),
        ("Chem", "Ex 1B", 2.5),
        ("Physics", "Ex 1B", 3.5),
        ("Maths", "Ex 1B", 6.0),
        ("Chem", "Ex 2A", 2.5),
        ("Physics", "Ex 2A", 4.5),
        ("Maths", "Ex 2A", 6.5),
        ("Chem", "Ex 2B", 2.5),
        ("Physics", "Ex 2B", 4.5),
        ("Maths", "Ex 2B", 6.5),
        ("Chem", "MLE", 3.0),
        ("Physics", "MLE", 5.0),
        ("Maths", "MLE", 5.5),
        ("Chem", "Ex 4A", 10.0),
        ("Physics", "Ex 4A", 13.0),
        ("Maths", "Ex 4A", 15.0),
        ("Chem", "Ex 4B", 10.0),
        ("Physics", "Ex 4B", 13.0),
        ("Maths", "Ex 4B", 15.0),
        ("Chem", "Ex 3A", 12.0),
        ("Physics", "Ex 3A", 15.0),
        ("Maths", "Ex 3A", 18.0),
        ("Chem", "Ex 3B", 12.0),
        ("Physics", "Ex 3B", 15.0),
        ("Maths", "Ex 3B", 18.0),
        ("Chem", "JMYL", 4.0),
        ("Physics", "JMYL", 4.0),
        ("Maths", "JMYL", 4.0),
        ("Chem", "JAYL", 8.0),
        ("Physics", "JAYL", 8.0),
        ("Maths", "JAYL", 8.0),
        ("Chem", "PYQs", 4.5),
        ("Physics", "PYQs", 4.5),
        ("Maths", "PYQs", 4.5),
    ]
    for subject, ex_type, expected in cases:
        got = formulas.t_q_for(subject, ex_type)
        ok = _check(ok, f"t_q({subject}, {ex_type})", got == expected, f"got {got}")

    # Default fallbacks
    ok = _check(ok, "t_q(Chem, Unknown) -> 4.0", formulas.t_q_for("Chem", "Unknown") == 4.0)
    ok = _check(ok, "t_q(None, None) -> 4.0", formulas.t_q_for(None, None) == 4.0)
    ok = _check(ok, "t_q('', '') -> 4.0", formulas.t_q_for("", "") == 4.0)
    # Default per-type fallback for non-uniform types
    ok = _check(ok, "t_q(Chem, Ex 1A) default subject", formulas.t_q_for("Biology", "Ex 1A") == 3.0)
    assert ok


def test_accuracy_ratio() -> bool:
    ok = True
    print("\n=== accuracy_ratio ===")
    ok = _check(ok, "8/10 = 0.8", formulas.accuracy_ratio(10, 8) == 0.8)
    ok = _check(ok, "0/0 = 0", formulas.accuracy_ratio(0, 0) == 0.0)
    ok = _check(ok, "10/0 = 0 (no attempts)", formulas.accuracy_ratio(0, 10) == 0.0)
    ok = _check(ok, "15/10 = 1.0 (capped)", formulas.accuracy_ratio(10, 15) == 1.0)
    ok = _check(ok, "None/None = 0", formulas.accuracy_ratio(None, None) == 0.0)
    ok = _check(ok, "10/10 = 1.0", formulas.accuracy_ratio(10, 10) == 1.0)
    assert ok


def test_mins_per_question() -> bool:
    ok = True
    print("\n=== mins_per_question ===")
    ok = _check(ok, "20/10 = 2.0", formulas.mins_per_question(20, 10) == 2.0)
    ok = _check(ok, "0/0 = None", formulas.mins_per_question(0, 0) is None)
    ok = _check(ok, "20/0 = None", formulas.mins_per_question(20, 0) is None)
    ok = _check(ok, "None/None = None", formulas.mins_per_question(None, None) is None)
    assert ok


def test_cognitive_yield() -> bool:
    ok = True
    print("\n=== cognitive_yield ===")

    # Case: Chem/Ex1A, T=20, A=10, C=8
    # t_q=2.0, T_target=20, accuracy=0.8, ratio=20/20=1.0, velocity=1.0
    # round(1.666 * 20 * 0.8^5 * 1.0) = round(1.666 * 20 * 0.32768) = round(10.916) = 11
    cy = formulas.cognitive_yield("Chem", "Ex 1A", 20, 10, 8)
    ok = _check(ok, "Chem/Ex1A T=20 A=10 C=8 -> 11", cy == 11, f"got {cy}")

    # No time -> 0
    ok = _check(ok, "no time -> 0", formulas.cognitive_yield("Chem", "Ex 1A", 0, 10, 8) == 0)
    # No questions -> 0
    ok = _check(ok, "no questions -> 0", formulas.cognitive_yield("Chem", "Ex 1A", 20, 0, 0) == 0)

    # Velocity capped at 1.5: Chem/Ex1A, T=5, A=10, C=8
    # T_target=20, ratio=20/5=4.0 -> capped to 1.5
    # round(1.666 * 20 * 0.8^5 * 1.5) = round(1.666 * 20 * 0.32768 * 1.5) = round(16.37) = 16
    cy_fast = formulas.cognitive_yield("Chem", "Ex 1A", 5, 10, 8)
    ok = _check(ok, "velocity capped at 1.5 -> 16", cy_fast == 16, f"got {cy_fast}")

    # Physics/Ex2A, T=45, A=10, C=7
    # t_q=4.5, T_target=45, accuracy=0.7, ratio=45/45=1.0, velocity=1.0
    # round(1.666 * 45 * 0.7^(10/4.5) * 1.0) = round(1.666 * 45 * 0.7^2.222...)
    # 0.7^2.222 = e^(2.222 * ln(0.7)) = e^(2.222 * -0.3567) = e^(-0.7924) = 0.4527
    # round(1.666 * 45 * 0.4527) = round(33.90) = 34
    cy_physics = formulas.cognitive_yield("Physics", "Ex 2A", 45, 10, 7)
    ok = _check(ok, "Physics/Ex2A T=45 A=10 C=7 -> 34", cy_physics == 34, f"got {cy_physics}")

    # Perfect accuracy
    # Chem/Ex1A, T=20, A=10, C=10
    # round(1.666 * 20 * 1.0^5 * 1.0) = round(33.32) = 33
    cy_perfect = formulas.cognitive_yield("Chem", "Ex 1A", 20, 10, 10)
    ok = _check(ok, "perfect accuracy -> 33", cy_perfect == 33, f"got {cy_perfect}")

    # C > A capped to 1.0
    cy_overcap = formulas.cognitive_yield("Chem", "Ex 1A", 20, 10, 15)
    ok = _check(ok, "C>A capped to same as perfect", cy_overcap == cy_perfect, f"got {cy_overcap}")

    assert ok


def test_theory_yield() -> bool:
    ok = True
    print("\n=== theory_yield ===")

    # At target velocity, theory_yield == cognitive_yield
    ty = formulas.theory_yield("Chem", "Ex 1A", 20, 10, 8)
    cy = formulas.cognitive_yield("Chem", "Ex 1A", 20, 10, 8)
    ok = _check(ok, "at target velocity, TY == CY", ty == cy, f"TY={ty} CY={cy}")

    # At high velocity, theory_yield > cognitive_yield (uncapped)
    ty_fast = formulas.theory_yield("Chem", "Ex 1A", 5, 10, 8)
    cy_fast = formulas.cognitive_yield("Chem", "Ex 1A", 5, 10, 8)
    ok = _check(ok, "fast: TY > CY (uncapped velocity)", ty_fast > cy_fast,
                f"TY={ty_fast} CY={cy_fast}")
    # ty_fast = round(1.666 * 20 * 0.8^5 * 4.0) = round(1.666 * 20 * 0.32768 * 4.0) = round(43.67) = 44
    ok = _check(ok, "fast theory yield -> 44", ty_fast == 44, f"got {ty_fast}")

    # No time / no questions -> 0
    ok = _check(ok, "no time -> 0", formulas.theory_yield("Chem", "Ex 1A", 0, 10, 8) == 0)
    ok = _check(ok, "no questions -> 0", formulas.theory_yield("Chem", "Ex 1A", 20, 0, 0) == 0)

    assert ok


def test_none_inputs() -> bool:
    ok = True
    print("\n=== None/missing inputs ===")
    ok = _check(ok, "all None -> 0", formulas.cognitive_yield(None, None, None, None, None) == 0)
    ok = _check(ok, "theory all None -> 0", formulas.theory_yield(None, None, None, None, None) == 0)
    ok = _check(ok, "accuracy None -> 0", formulas.accuracy_ratio(None, None) == 0.0)
    ok = _check(ok, "mins None -> None", formulas.mins_per_question(None, None) is None)
    assert ok


def main() -> int:
    test_t_q_lookup()
    test_accuracy_ratio()
    test_mins_per_question()
    test_cognitive_yield()
    test_theory_yield()
    test_none_inputs()
    print("\n" + "=" * 70)
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
