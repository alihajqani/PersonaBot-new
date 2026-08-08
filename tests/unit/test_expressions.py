"""The config expression sandbox.

A survey spec is data that someone will accept from a colleague without reading
every line, so it must not be able to execute code.
"""

from __future__ import annotations

import random

import pytest

from personabot.domain.expressions import Expression, resolve
from personabot.errors import ExpressionError

CONTEXT = {
    "age": 52,
    "marriage_age": 21,
    "acquiescence": 0.45,
    "z": {"attachment_anxiety": 1.2, "marital_satisfaction": -0.4},
}


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("max(1, age - marriage_age)", 31),
        ("min(age - 1, 29)", 29),
        ("age >= 40", True),
        ("age >= 55", False),
        ("acquiescence > 0.30", True),
        ("clamp(5, 0, 3)", 3),
        ("age if age > 40 else 40", 52),
        ("-age", -52),
        ("age >= 40 and age <= 65", True),
        ("40 <= age <= 65", True),  # chained comparison
    ],
)
def test_evaluates_supported_expressions(source, expected):
    assert Expression(source).evaluate(CONTEXT) == expected


def test_nested_namespace_access():
    assert Expression("z.attachment_anxiety").evaluate(CONTEXT) == 1.2


def test_sigmoid_does_not_overflow_on_extremes():
    assert Expression("sigmoid(1000)").evaluate({}) == pytest.approx(1.0)
    assert Expression("sigmoid(-1000)").evaluate({}) == pytest.approx(0.0)


@pytest.mark.parametrize(
    "source",
    [
        "__import__('os').system('id')",
        "open('/etc/passwd').read()",
        "(1).__class__.__bases__",
        "age.__class__",
        "[x for x in range(10)]",
        "{'a': 1}",
        "lambda: 1",
        "globals()",
        "eval('1+1')",
        "age := 5",
    ],
)
def test_rejects_anything_outside_the_allowlist(source):
    with pytest.raises(ExpressionError):
        Expression(source).evaluate(CONTEXT)


def test_rejects_unknown_names_and_functions():
    with pytest.raises(ExpressionError, match="Unknown name"):
        Expression("nonexistent + 1").evaluate(CONTEXT)
    with pytest.raises(ExpressionError, match="Unknown function"):
        Expression("frobnicate(1)").evaluate(CONTEXT)


def test_caps_exponent_so_a_typo_cannot_hang_the_process():
    with pytest.raises(ExpressionError, match="Exponent"):
        Expression("2 ** 10**9").evaluate(CONTEXT)


def test_division_by_zero_is_reported_with_the_source():
    with pytest.raises(ExpressionError, match="Division by zero"):
        Expression("age / 0").evaluate(CONTEXT)


class TestRandomnessIsGated:
    """Stochastic functions require an explicit rng.

    A context meant to be deterministic must not be able to draw randomness by
    accident — otherwise a "derived" field silently becomes non-reproducible.
    """

    def test_available_with_rng(self):
        value = Expression("randint(2, 8)").evaluate({}, rng=random.Random(0))
        assert 2 <= value <= 8

    def test_unavailable_without_rng(self):
        with pytest.raises(ExpressionError, match="unavailable"):
            Expression("randint(2, 8)").evaluate({})


def test_syntax_errors_surface_at_construction_not_evaluation():
    with pytest.raises(ExpressionError, match="Cannot parse"):
        Expression("age + (")


def test_resolve_passes_literals_through_untouched():
    assert resolve(42, CONTEXT) == 42
    assert resolve(3.5, CONTEXT) == 3.5
    assert resolve("age", CONTEXT) == 52
