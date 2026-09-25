"""Core data structures.

The predecessor passed untyped ``Dict[str, Any]`` between every function, with
shape enforced only by ``KeyError`` at runtime. These are the shapes that were
implicit; making them explicit is what lets the matcher, sampler and validator
be tested without a browser or an API key.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable, Mapping


class QuestionType(StrEnum):
    """Normalized question types.

    Providers emit divergent vocabularies (``TEXT_AREA``, ``MATRIX_RADIO``,
    ``UNKNOWN``…). Extractors map onto this set so downstream code has one thing
    to reason about. ``from_raw`` keeps old schema files loadable.
    """

    TEXT = "TEXT_INPUT"
    RADIO = "RADIO"
    SELECT = "SELECT"
    CHECKBOX = "CHECKBOX"
    MATRIX = "MATRIX"
    # A free-text question the platform renders as an audio recorder. It is
    # answered in prose like TEXT, but no submitter can type into it — the
    # transcript is exported to a file instead (see `transcripts` in the spec).
    VOICE = "VOICE"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def from_raw(cls, raw: str) -> "QuestionType":
        normalized = (raw or "").strip().upper()
        aliases = {
            "TEXT_INPUT": cls.TEXT,
            "TEXT_AREA": cls.TEXT,
            "TEXTAREA": cls.TEXT,
            "TEXT": cls.TEXT,
            "RADIO": cls.RADIO,
            "SELECT": cls.SELECT,
            "DROPDOWN": cls.SELECT,
            "CHECKBOX": cls.CHECKBOX,
            "MATRIX": cls.MATRIX,
            "MATRIX_RADIO": cls.MATRIX,
            "VOICE": cls.VOICE,
            "AUDIO": cls.VOICE,
            "FILE_UPLOAD": cls.VOICE,
        }
        return aliases.get(normalized, cls.UNKNOWN)


@dataclass(frozen=True, slots=True)
class Option:
    """One selectable choice. ``value`` is what gets submitted and matched."""

    text: str
    value: str

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Option":
        # Schemas in the wild carry `text` only, `value` only, or both.
        text = str(data.get("text", data.get("value", "")))
        value = str(data.get("value", data.get("text", "")))
        return cls(text=text, value=value)


@dataclass(frozen=True, slots=True)
class Question:
    """A single form field, as extracted in phase 1."""

    question_id: str
    question_text: str
    type: QuestionType
    options: tuple[Option, ...] = ()

    @property
    def option_values(self) -> tuple[str, ...]:
        return tuple(option.value for option in self.options)

    @property
    def scale_size(self) -> int:
        """Number of choices, or 0 for free-text. ``K`` in psychometric terms."""
        return len(self.options)

    @property
    def is_choice(self) -> bool:
        return bool(self.options)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Question":
        return cls(
            question_id=str(data["question_id"]),
            question_text=str(data.get("question_text", "")),
            type=QuestionType.from_raw(str(data.get("type", ""))),
            options=tuple(Option.from_dict(o) for o in data.get("options", ())),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "question_text": self.question_text,
            "type": str(self.type),
            "options": [{"text": o.text, "value": o.value} for o in self.options],
        }


@dataclass(frozen=True, slots=True)
class FormSchema:
    """The whole questionnaire. Ordered, and unique by ``question_id``."""

    questions: tuple[Question, ...]

    def __iter__(self) -> Iterable[Question]:
        return iter(self.questions)

    def __len__(self) -> int:
        return len(self.questions)

    def __getitem__(self, question_id: str) -> Question:
        return self.by_id[question_id]

    @property
    def by_id(self) -> dict[str, Question]:
        return {q.question_id: q for q in self.questions}

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(q.question_id for q in self.questions)

    @classmethod
    def from_list(cls, raw: Iterable[Mapping[str, Any]]) -> "FormSchema":
        questions, seen = [], set()
        for entry in raw:
            question = Question.from_dict(entry)
            # Paginated extraction can revisit a page; keep the first sighting.
            if question.question_id in seen:
                continue
            seen.add(question.question_id)
            questions.append(question)
        return cls(questions=tuple(questions))

    def to_list(self) -> list[dict[str, Any]]:
        return [q.to_dict() for q in self.questions]


@dataclass(slots=True)
class Persona:
    """One synthetic respondent.

    ``traits_z`` is the sampled ground truth and is *never* shown to the LLM;
    only ``traits_band`` (Persian words) reaches a prompt. ``derived`` holds
    deterministic facts — spouse age, weight, menopause age — that used to be
    left to the model to invent via a hard-coded question-ID table in the prompt.
    """

    id: str
    demographics: dict[str, Any] = field(default_factory=dict)
    derived: dict[str, Any] = field(default_factory=dict)
    traits_z: dict[str, float] = field(default_factory=dict)
    traits_band: dict[str, str] = field(default_factory=dict)
    response_style: dict[str, Any] = field(default_factory=dict)
    details: dict[str, str] = field(default_factory=dict)
    # Key of the between-subjects scenario this respondent answers under, for
    # surveys that declare `scenarios`. None everywhere else.
    scenario: str | None = None

    @property
    def is_careless(self) -> bool:
        return bool(self.response_style.get("careless", False))

    def to_dict(self) -> dict[str, Any]:
        record = {
            "id": self.id,
            "demographics": self.demographics,
            "derived": self.derived,
            "latent_traits_z": self.traits_z,
            "latent_traits_band": self.traits_band,
            "response_style": self.response_style,
            "details": self.details,
        }
        if self.scenario is not None:
            record["scenario"] = self.scenario
        return record

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Persona":
        return cls(
            id=str(data.get("id", "unknown")),
            demographics=dict(data.get("demographics", {})),
            derived=dict(data.get("derived", {})),
            traits_z=dict(data.get("latent_traits_z", {})),
            traits_band=dict(data.get("latent_traits_band", {})),
            response_style=dict(data.get("response_style", {})),
            details=dict(data.get("details", {})),
            scenario=str(data["scenario"]) if data.get("scenario") is not None else None,
        )


@dataclass(slots=True)
class ItemFailure:
    """One unit of work that did not succeed, kept for the summary and exit code."""

    item: str
    reason: str
    retryable: bool = False


@dataclass(slots=True)
class PhaseResult:
    """Outcome of a pipeline phase.

    Replaces the old pattern of logging an error, continuing, and always
    reporting ``"All selected phases have been executed."`` — which meant a run
    could produce nothing at all and still exit 0, so `make loop` could not tell
    a working run from a broken one.
    """

    phase: str
    succeeded: int = 0
    skipped: int = 0
    failures: list[ItemFailure] = field(default_factory=list)

    @property
    def failed(self) -> int:
        return len(self.failures)

    @property
    def ok(self) -> bool:
        return not self.failures

    def record_success(self, count: int = 1) -> None:
        self.succeeded += count

    def record_failure(self, item: str, reason: str, *, retryable: bool = False) -> None:
        self.failures.append(ItemFailure(item=item, reason=reason, retryable=retryable))

    def summary(self) -> str:
        parts = [f"{self.succeeded} ok"]
        if self.skipped:
            parts.append(f"{self.skipped} skipped")
        if self.failures:
            parts.append(f"{self.failed} failed")
        return f"{self.phase}: " + ", ".join(parts)
