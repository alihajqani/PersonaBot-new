"""The Porsline provider, bound to the real DOM contract.

These are the regression tests for the discovery that the refactored provider
could not read this form at all: it never clicked «شروع», it looked for
navigation by button *text* when the next button is an icon carrying only
``aria-label="بعدی"``, and it resolved options against the label's nearest
``div[class*="question"]`` — a wrapper that holds the title and none of the
choices. Extraction produced either nothing or 237 optionless TEXT questions.

The page states below are verbatim shapes returned by the provider's DOM probe
during a full walk of the live form.
"""

from __future__ import annotations

import pytest

from personabot.domain.models import QuestionType
from personabot.providers.porsline import PorslineExtractor, _match_option


class FakePage:
    """Stands in for a Playwright page by replaying one probe result."""

    def __init__(self, state: dict) -> None:
        self.state = state

    async def evaluate(self, _script, *args):
        return self.state


def _state(**questions) -> dict:
    return {"signature": "", "questions": [], "hasStart": False,
            "hasSubmit": False, "hasStatement": False, "nextEnabled": True, **questions}


RADIO_PAGE = _state(
    signature="51531905",
    questions=[{
        "id": "51531905",
        "title": "جنسیت",
        "options": ["آقا", "خانم"],
        "answered": False,
        "inputs": [],
        "hasTable": False,
    }],
)

# A section divider. It owns a question id and a label, but asks nothing and
# advances only via its own «ادامه» button.
STATEMENT_PAGE = _state(
    signature="51536339",
    hasStatement=True,
    questions=[{
        "id": "51536339",
        "title": "لطفا با دقت به سوالات زیر پاسخ دهید.",
        "options": [],
        "answered": False,
        "inputs": [],
        "hasTable": False,
    }],
)


class TestParsePage:
    @pytest.mark.asyncio
    async def test_choices_are_extracted(self, extractor):
        questions = await extractor.parse_page(FakePage(RADIO_PAGE))

        assert len(questions) == 1
        question = questions[0]
        assert question.question_id == "51531905"
        assert question.type is QuestionType.RADIO
        assert question.option_values == ("آقا", "خانم")

    @pytest.mark.asyncio
    async def test_title_and_options_drop_the_numbering(self, extractor):
        """The DOM renders «* ۱ جنسیت جنسیت» and «۱ آقا»; neither may survive.

        A numbered option never equals what the LLM answers, so every answer
        would fall to the fuzzy tier or be discarded outright.
        """
        question = (await extractor.parse_page(FakePage(RADIO_PAGE)))[0]

        assert question.question_text == "جنسیت"
        assert question.option_values == ("آقا", "خانم")

    @pytest.mark.asyncio
    async def test_statement_pages_are_not_questions(self, extractor):
        """Emitting these would send a divider to the LLM and count it against
        the completeness gate, which no persona could then pass."""
        assert await extractor.parse_page(FakePage(STATEMENT_PAGE)) == []


class TestMatchOption:
    AGREEMENT = ["کاملا مخالفم", "مخالفم", "نظری ندارم", "موافقم", "کاملا موافقم"]

    def test_exact_answer(self):
        assert _match_option("موافقم", self.AGREEMENT) == 3

    def test_exact_wins_over_a_longer_option_containing_it(self):
        """«موافقم» is a substring of «کاملا موافقم».

        The predecessor matched by substring and could select the wrong point on
        a Likert scale — a silent, systematic data error rather than a crash.
        """
        assert _match_option("موافقم", self.AGREEMENT) == 3
        assert _match_option("کاملا موافقم", self.AGREEMENT) == 4

    def test_arabic_and_persian_letter_variants_are_folded(self):
        # Arabic yeh/kaf for the Persian ones, as models routinely emit.
        assert _match_option("نظري ندارم", self.AGREEMENT) == 2

    def test_near_miss_falls_back_to_fuzzy(self):
        assert _match_option("موافق", self.AGREEMENT) == 3

    def test_unrelated_answer_matches_nothing(self):
        assert _match_option("سیب زمینی سرخ کرده", self.AGREEMENT) is None

    def test_empty_answer_matches_nothing(self):
        assert _match_option("", self.AGREEMENT) is None


@pytest.fixture
def extractor():
    return PorslineExtractor(settings=None, browser=None)
