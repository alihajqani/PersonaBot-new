"""Persian normalization and answer validation — audit finding A2.

The legacy pipeline compared model output to schema options with only whitespace
stripping. Several Persian letters have a visually identical Arabic code point,
so a model emitting Arabic yeh mismatched on every occurrence of that letter,
pushed the similarity below threshold, and had a *correct* answer discarded.
"""

from __future__ import annotations

import pytest
from thefuzz import fuzz

from personabot.domain.answer_validation import AnswerValidator, check_completeness
from personabot.domain.models import Option, Question, QuestionType
from personabot.domain.text import normalize, normalize_for_match

ARABIC_YEH, PERSIAN_YEH = "ي", "ی"
ARABIC_KAF, PERSIAN_KAF = "ك", "ک"
ZWNJ = "‌"


class TestNormalization:
    def test_folds_arabic_yeh_and_kaf_to_persian(self):
        assert normalize_for_match(f"هم{ARABIC_YEH}شه") == normalize_for_match(f"هم{PERSIAN_YEH}شه")
        assert normalize_for_match(f"{ARABIC_KAF}م") == normalize_for_match(f"{PERSIAN_KAF}م")

    def test_folds_persian_and_arabic_digits_to_ascii(self):
        assert normalize_for_match("۵۲") == "52"
        assert normalize_for_match("٥٢") == "52"

    def test_strips_zwnj_by_default(self):
        assert normalize_for_match(f"می{ZWNJ}شود") == normalize_for_match("میشود")

    def test_keeps_zwnj_when_asked(self):
        assert ZWNJ in normalize(f"می{ZWNJ}شود", zwnj="keep")

    def test_collapses_whitespace_and_trims(self):
        assert normalize_for_match("  به   ندرت  ") == "به ندرت"

    def test_strips_configured_trailing_punctuation(self):
        assert normalize("همیشه.", strip_trailing=".,؛") == normalize("همیشه")

    def test_empty_input_is_safe(self):
        assert normalize_for_match("") == ""


@pytest.fixture
def question() -> Question:
    values = ["هرگز", "به ندرت", "گاهی", "اکثرا", "همیشه"]
    return Question(
        question_id="q1",
        question_text="نمونه",
        type=QuestionType.RADIO,
        options=tuple(Option(text=v, value=v) for v in values),
    )


@pytest.fixture
def validator(spec) -> AnswerValidator:
    return AnswerValidator(spec.answering)


class TestAnswerMatching:
    def test_exact_match(self, validator, question):
        result = validator.validate_chunk({"q1": "همیشه"}, [question])
        assert result.answers == {"q1": "همیشه"}
        assert not result.corrections

    @pytest.mark.parametrize("raw", ["همیشه.", "  همیشه  ", "همیشه،"])
    def test_cosmetic_variation_still_matches_exactly(self, validator, question, raw):
        result = validator.validate_chunk({"q1": raw}, [question])
        assert result.answers == {"q1": "همیشه"}
        assert not result.corrections

    def test_arabic_yeh_answer_matches_persian_option(self, validator, question):
        """The A2 regression. Must be an exact match, not a fuzzy rescue."""
        arabic_spelling = f"هم{ARABIC_YEH}شه"
        result = validator.validate_chunk({"q1": arabic_spelling}, [question])
        assert result.answers == {"q1": "همیشه"}
        assert not result.corrections
        assert not result.discarded

    def test_legacy_comparison_would_have_dropped_it(self, spec):
        """Documents why the fix matters: un-normalized, it scores below threshold."""
        score = fuzz.ratio(f"هم{ARABIC_YEH}شه", "همیشه")
        assert score < spec.answering.fuzzy.min_score

    def test_unrelated_answer_is_discarded_not_guessed(self, validator, question):
        result = validator.validate_chunk({"q1": "خیلی زیاد"}, [question])
        assert result.answers == {}
        assert "q1" in result.discarded

    def test_returns_the_canonical_option_not_the_normalized_form(self, validator, question):
        """The form must receive the exact string it rendered."""
        result = validator.validate_chunk({"q1": "  همیشه  "}, [question])
        assert result.answers["q1"] in question.option_values


class TestChunkBookkeeping:
    def test_captures_reasoning_instead_of_dropping_it(self, validator, question):
        result = validator.validate_chunk(
            {"_reasoning": "چون خسته‌ام", "q1": "گاهی"}, [question]
        )
        assert result.reasoning == "چون خسته‌ام"
        assert result.answers == {"q1": "گاهی"}

    def test_reports_rogue_ids(self, validator, question):
        result = validator.validate_chunk({"nope": "x", "q1": "گاهی"}, [question])
        assert result.rogue_ids == ["nope"]

    def test_missing_is_scoped_to_the_chunk(self, validator, question):
        other = Question(question_id="q2", question_text="x", type=QuestionType.TEXT)
        result = validator.validate_chunk({"q1": "گاهی"}, [question, other])
        assert result.missing_ids == ["q2"]

    def test_free_text_passes_through_stripped(self, validator):
        free = Question(question_id="t", question_text="سن", type=QuestionType.TEXT)
        result = validator.validate_chunk({"t": "  52 "}, [free])
        assert result.answers == {"t": "52"}


class TestCompleteness:
    """Replaces cleaner.py's fixed threshold of 90, which exceeded this form's
    51 questions — running it would have deleted every valid answer file."""

    def test_measures_against_the_live_schema(self):
        expected = [f"q{i}" for i in range(51)]
        check = check_completeness({q: "a" for q in expected}, expected, 0.95)
        assert check.passed and check.ratio == 1.0

    def test_fails_below_threshold_and_names_what_is_missing(self):
        expected = [f"q{i}" for i in range(51)]
        answers = {q: "a" for q in expected[:40]}
        check = check_completeness(answers, expected, 0.95)
        assert not check.passed
        assert len(check.missing_ids) == 11

    def test_a_full_51_answer_file_survives(self, instrument):
        answers = {qid: "a" for qid in instrument.answerable_ids}
        check = check_completeness(answers, instrument.answerable_ids, 0.95)
        assert check.passed
