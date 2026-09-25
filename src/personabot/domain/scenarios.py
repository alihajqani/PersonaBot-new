"""Between-subjects scenarios: assignment, recognition, and the answer-file tag.

The form, not PersonaBot, decides which scenario a respondent sees — it draws
one at random per session. Answers are generated long before that draw, so the
two are reconciled at submission time: every answer set carries the key of the
scenario it was written for, and the submitter only spends an answer set on a
session that drew the same scenario.
"""

from __future__ import annotations

import random
import re
from typing import Any, Mapping, Sequence

from personabot.domain.models import Persona
from personabot.domain.text import normalize_for_match
from personabot.survey.spec import ScenarioSpec

# The key under which an answer file records its scenario. Question ids are
# numeric strings, so a leading underscore can never collide with one.
SCENARIO_FIELD = "_scenario"
# Which persona an answer file was written for — metadata, never submitted.
PERSONA_FIELD = "_persona"

# «4.در سازمان…», «۴. در سازمان…», «4) …». Digits are folded to ASCII first.
_LEADING_NUMBER = re.compile(r"^\s*(\d{1,3})\s*[.\-)٫۔]")

# Scenarios of a factorial design share most of their sentences, so a text-only
# match has to be both close and clearly better than the runner-up.
_FUZZY_MIN_SCORE = 90
_FUZZY_MIN_MARGIN = 3


def assign_scenarios(
    personas: Sequence[Persona], scenarios: Sequence[ScenarioSpec], rng: random.Random
) -> None:
    """Give each persona a scenario, as evenly as the count allows.

    The form draws uniformly, so a balanced pool is what lets phase 4 spend
    every answer set without waiting on an unlucky scenario. Cells differ in
    size by at most one; which cells get the extra personas is random.
    """
    if not scenarios:
        return
    keys = [scenario.key for scenario in scenarios]
    order: list[str] = []
    while len(order) < len(personas):
        cycle = keys[:]
        rng.shuffle(cycle)
        order.extend(cycle)
    for persona, key in zip(personas, order):
        persona.scenario = key


def identify_scenario(text: str, scenarios: Sequence[ScenarioSpec]) -> str | None:
    """The key of the scenario this page text shows, or ``None``.

    The number the form prints in front of the scenario is authoritative. The
    text is only consulted when there is no number, so an instruction page
    («لطفا با دقت مطالعه کنید») is never mistaken for a scenario.
    """
    if not scenarios or not text:
        return None

    normalized = normalize_for_match(text)
    number = _LEADING_NUMBER.match(normalized)
    if number:
        wanted = number.group(1).lstrip("0") or "0"
        for scenario in scenarios:
            if normalize_for_match(scenario.key).lstrip("0") == wanted:
                return scenario.key

    from thefuzz import fuzz

    scored = sorted(
        (
            (int(fuzz.ratio(normalized, normalize_for_match(_strip_number(s.text)))), s.key)
            for s in scenarios
        ),
        reverse=True,
    )
    best_score, best_key = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0
    if best_score >= _FUZZY_MIN_SCORE and best_score - runner_up >= _FUZZY_MIN_MARGIN:
        return best_key
    return None


def _strip_number(text: str) -> str:
    return _LEADING_NUMBER.sub("", normalize_for_match(text), count=1)


def split_answer_file(payload: Mapping[str, Any]) -> tuple[str | None, dict[str, str]]:
    """Separate the scenario tag from the answers that go into the form."""
    scenario = payload.get(SCENARIO_FIELD)
    answers = {
        str(key): str(value)
        for key, value in payload.items()
        if not str(key).startswith("_")
    }
    return (str(scenario) if scenario is not None else None), answers
