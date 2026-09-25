"""Validation and self-correction of model-produced answers.

Three defects from the predecessor are fixed here.

**A2 — Persian normalization.** Matching ran ``fuzz.ratio`` over text that had
only been whitespace-stripped. Arabic yeh/kaf against Persian yeh/kaf mismatches
on every occurrence of the letter, dropping a long option's similarity below the
acceptance threshold. A correct answer was then discarded, counted as missing,
and could sink the persona below the completeness gate — invisibly. Both sides
are now normalized before comparison (:mod:`personabot.domain.text`).

**Misleading completeness warnings.** ``validate_and_clean_answers`` was handed
the *chunk* but logged "missing N questions" as though it had the whole schema,
so the number meant nothing. Chunk-level validation and run-level completeness
are now separate operations.

**Silent loss of reasoning.** The prompt mandates a ``_reasoning`` key and the
old validator dropped every underscore-prefixed key without recording it. It is
now captured on the result so callers can log or persist it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from personabot.domain.models import Question
from personabot.domain.text import normalize
from personabot.logging_setup import get_logger
from personabot.survey.spec import AnsweringSpec

log = get_logger(__name__)


@dataclass(slots=True)
class Correction:
    question_id: str
    raw: str
    corrected: str
    score: int


@dataclass(slots=True)
class ValidationResult:
    """Outcome of validating one chunk of answers."""

    answers: dict[str, str] = field(default_factory=dict)
    corrections: list[Correction] = field(default_factory=list)
    discarded: dict[str, str] = field(default_factory=dict)
    rogue_ids: list[str] = field(default_factory=list)
    missing_ids: list[str] = field(default_factory=list)
    reasoning: str | None = None

    @property
    def accepted(self) -> int:
        return len(self.answers)


class AnswerValidator:
    """Validates raw model output against the questions actually asked."""

    def __init__(self, answering: AnsweringSpec) -> None:
        self.config = answering
        self._normalization = answering.normalization
        self._fuzzy = answering.fuzzy

    def normalize(self, text: str) -> str:
        return normalize(
            text,
            unicode_form=self._normalization.unicode,
            fold_arabic=self._normalization.fold_arabic_to_persian,
            zwnj=self._normalization.zwnj,
            strip_trailing=self._normalization.strip_trailing,
        )

    def validate_chunk(
        self,
        raw_answers: Mapping[str, Any],
        questions: Sequence[Question],
    ) -> ValidationResult:
        """Clean one chunk against the questions that chunk asked about."""
        result = ValidationResult()
        by_id = {q.question_id: q for q in questions}

        for key, raw_value in raw_answers.items():
            if key.startswith("_"):
                if key == "_reasoning" and isinstance(raw_value, str):
                    result.reasoning = raw_value.strip()
                continue

            question = by_id.get(str(key))
            if question is None:
                result.rogue_ids.append(str(key))
                continue

            if question.is_choice:
                matched = self._match_option(question, raw_value, result)
                if matched is not None:
                    result.answers[question.question_id] = matched
            else:
                result.answers[question.question_id] = str(raw_value).strip()

        result.missing_ids = [
            q.question_id for q in questions if q.question_id not in result.answers
        ]
        return result

    def _match_option(
        self, question: Question, raw_value: Any, result: ValidationResult
    ) -> str | None:
        """Resolve a model's answer to one of the question's real options.

        Returns the **canonical** option string from the schema, never the
        normalized comparison form — the form still has to receive the exact
        text it rendered.
        """
        candidate = self.normalize(str(raw_value))

        # Exact match after normalization is the common case.
        for option in question.option_values:
            if self.normalize(option) == candidate:
                return option

        best_option: str | None = None
        best_score = self._fuzzy.min_score - 1
        for option in question.option_values:
            score = self._similarity(candidate, self.normalize(option))
            if score > best_score:
                best_score, best_option = score, option

        if best_option is not None and best_score >= self._fuzzy.min_score:
            result.corrections.append(
                Correction(
                    question_id=question.question_id,
                    raw=str(raw_value),
                    corrected=best_option,
                    score=best_score,
                )
            )
            return best_option

        result.discarded[question.question_id] = str(raw_value)
        return None

    def _similarity(self, left: str, right: str) -> int:
        from thefuzz import fuzz

        match self._fuzzy.algorithm:
            case "partial_ratio":
                return int(fuzz.partial_ratio(left, right))
            case "token_sort_ratio":
                return int(fuzz.token_sort_ratio(left, right))
            case _:
                return int(fuzz.ratio(left, right))


@dataclass(slots=True)
class CompletenessCheck:
    """Whether an answer set is finished enough to submit."""

    answered: int
    expected: int
    missing_ids: tuple[str, ...]
    threshold: float

    @property
    def ratio(self) -> float:
        return self.answered / self.expected if self.expected else 0.0

    @property
    def passed(self) -> bool:
        return self.ratio >= self.threshold

    def describe(self) -> str:
        return (
            f"{self.answered}/{self.expected} answered ({self.ratio:.1%}, "
            f"need {self.threshold:.0%})"
        )


def check_completeness(
    answers: Mapping[str, Any],
    expected_ids: Iterable[str],
    threshold: float,
) -> CompletenessCheck:
    """Compare an answer set against the questions this form actually has.

    Replaces ``cleaner.py``, whose ``MIN_ENTRIES_THRESHOLD = 90`` was a
    module-level constant unrelated to any live schema. The current form has 51
    questions, so a *perfect* answer file scored 51 and the cleaner deleted it —
    running it would have destroyed the entire corpus.
    """
    expected = tuple(expected_ids)
    missing = tuple(qid for qid in expected if qid not in answers)
    return CompletenessCheck(
        answered=len(expected) - len(missing),
        expected=len(expected),
        missing_ids=missing,
        threshold=threshold,
    )
