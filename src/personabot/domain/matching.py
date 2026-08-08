"""The question-matcher DSL.

One mechanism replaces every hard-coded content check that used to live in
``core/instrument.py``: scale detection, construct assignment, attachment-pole
splitting, reverse keying, and demographic binding. A matcher is a small tree of
predicates over a :class:`Question`, written declaratively in the survey YAML::

    match:
      all:
        - {scale: attachment}
        - {text_contains_any: ["نگرانم", "دوست ندارند"]}

Two design choices are load-bearing:

* **Text is normalized on both sides** (see :mod:`personabot.domain.text`), so a
  spec written with Persian yeh still matches a schema extracted with Arabic yeh.
* **Everything is compiled at load time.** Regexes, unknown keys and empty
  option lists all fail when the config is read, not on question 47 of a run.

Leaves
------
``question_id``          exact id, or ``question_id_in`` for a list
``type``                 question type, or a list of types
``text_matches``         regex searched against the question text
``text_contains_any``    any substring present
``text_contains_all``    all substrings present
``options_equal``        the option list equals this list (order-insensitive)
``options_include_any``  at least one of these options is present
``options_include_all``  all of these options are present
``options_regex``        regex matching at least one option
``option_count``         exact int, or ``{min: n, max: m}``
``scale``                the scale already assigned to this question
``always`` / ``never``   constant predicates

Combinators
-----------
``all`` (AND), ``any`` (OR), ``not`` (negation)
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from personabot.domain.models import Question, QuestionType
from personabot.domain.text import normalize_for_match
from personabot.errors import SurveySpecError


@dataclass(frozen=True, slots=True)
class MatchContext:
    """Facts about a question that are derived rather than intrinsic.

    ``scale`` is resolved before constructs are assigned, which is what lets a
    construct rule say ``{scale: attachment}`` instead of restating the option
    signature.
    """

    scale: str | None = None


class Matcher(ABC):
    """A compiled predicate over a question."""

    @abstractmethod
    def matches(self, question: Question, context: MatchContext | None = None) -> bool: ...

    @abstractmethod
    def describe(self) -> str:
        """Human-readable form, used in coverage diagnostics."""


# ===== COMBINATORS =====

@dataclass(frozen=True, slots=True)
class AllOf(Matcher):
    children: tuple[Matcher, ...]

    def matches(self, question: Question, context: MatchContext | None = None) -> bool:
        return all(child.matches(question, context) for child in self.children)

    def describe(self) -> str:
        return "(" + " AND ".join(c.describe() for c in self.children) + ")"


@dataclass(frozen=True, slots=True)
class AnyOf(Matcher):
    children: tuple[Matcher, ...]

    def matches(self, question: Question, context: MatchContext | None = None) -> bool:
        return any(child.matches(question, context) for child in self.children)

    def describe(self) -> str:
        return "(" + " OR ".join(c.describe() for c in self.children) + ")"


@dataclass(frozen=True, slots=True)
class Not(Matcher):
    child: Matcher

    def matches(self, question: Question, context: MatchContext | None = None) -> bool:
        return not self.child.matches(question, context)

    def describe(self) -> str:
        return f"NOT {self.child.describe()}"


@dataclass(frozen=True, slots=True)
class Constant(Matcher):
    value: bool

    def matches(self, question: Question, context: MatchContext | None = None) -> bool:
        return self.value

    def describe(self) -> str:
        return "always" if self.value else "never"


# ===== LEAVES =====

@dataclass(frozen=True, slots=True)
class QuestionIdIn(Matcher):
    ids: frozenset[str]

    def matches(self, question: Question, context: MatchContext | None = None) -> bool:
        return question.question_id in self.ids

    def describe(self) -> str:
        shown = sorted(self.ids)
        preview = ", ".join(shown[:3]) + ("…" if len(shown) > 3 else "")
        return f"id in [{preview}] ({len(shown)})"


@dataclass(frozen=True, slots=True)
class TypeIs(Matcher):
    types: frozenset[QuestionType]

    def matches(self, question: Question, context: MatchContext | None = None) -> bool:
        return question.type in self.types

    def describe(self) -> str:
        return f"type in {sorted(str(t) for t in self.types)}"


@dataclass(frozen=True, slots=True)
class TextMatches(Matcher):
    pattern: re.Pattern[str]

    def matches(self, question: Question, context: MatchContext | None = None) -> bool:
        return bool(self.pattern.search(normalize_for_match(question.question_text)))

    def describe(self) -> str:
        return f"text ~ /{self.pattern.pattern}/"


@dataclass(frozen=True, slots=True)
class TextContains(Matcher):
    needles: tuple[str, ...]
    require_all: bool = False

    def matches(self, question: Question, context: MatchContext | None = None) -> bool:
        haystack = normalize_for_match(question.question_text)
        check = all if self.require_all else any
        return check(needle in haystack for needle in self.needles)

    def describe(self) -> str:
        joiner = "all" if self.require_all else "any"
        return f"text contains {joiner} of {len(self.needles)} phrases"


@dataclass(frozen=True, slots=True)
class OptionsEqual(Matcher):
    """The option list is exactly this set.

    Order-insensitive on purpose: extractors are not guaranteed to preserve DOM
    order across providers, and a scale's identity is its option *set*. Ordinal
    coding direction is declared separately via the scale's ``coding`` field.
    """

    expected: frozenset[str]

    def matches(self, question: Question, context: MatchContext | None = None) -> bool:
        actual = frozenset(normalize_for_match(v) for v in question.option_values)
        return actual == self.expected

    def describe(self) -> str:
        return f"options == set of {len(self.expected)}"


@dataclass(frozen=True, slots=True)
class OptionsInclude(Matcher):
    needles: frozenset[str]
    require_all: bool = False

    def matches(self, question: Question, context: MatchContext | None = None) -> bool:
        actual = {normalize_for_match(v) for v in question.option_values}
        if self.require_all:
            return self.needles <= actual
        return bool(self.needles & actual)

    def describe(self) -> str:
        joiner = "all" if self.require_all else "any"
        return f"options include {joiner} of {sorted(self.needles)}"


@dataclass(frozen=True, slots=True)
class OptionsRegex(Matcher):
    pattern: re.Pattern[str]

    def matches(self, question: Question, context: MatchContext | None = None) -> bool:
        return any(
            self.pattern.search(normalize_for_match(v)) for v in question.option_values
        )

    def describe(self) -> str:
        return f"any option ~ /{self.pattern.pattern}/"


@dataclass(frozen=True, slots=True)
class OptionCount(Matcher):
    minimum: int | None = None
    maximum: int | None = None

    def matches(self, question: Question, context: MatchContext | None = None) -> bool:
        count = question.scale_size
        if self.minimum is not None and count < self.minimum:
            return False
        if self.maximum is not None and count > self.maximum:
            return False
        return True

    def describe(self) -> str:
        return f"option_count in [{self.minimum}, {self.maximum}]"


@dataclass(frozen=True, slots=True)
class ScaleIs(Matcher):
    names: frozenset[str]

    def matches(self, question: Question, context: MatchContext | None = None) -> bool:
        return context is not None and context.scale in self.names

    def describe(self) -> str:
        return f"scale in {sorted(self.names)}"


# ===== COMPILATION =====

_LEAF_KEYS = frozenset({
    "question_id", "question_id_in", "type", "text_matches", "text_contains_any",
    "text_contains_all", "options_equal", "options_include_any",
    "options_include_all", "options_regex", "option_count", "scale",
    "always", "never",
})
_COMBINATOR_KEYS = frozenset({"all", "any", "not"})
_VALID_KEYS = _LEAF_KEYS | _COMBINATOR_KEYS


def build_matcher(spec: Mapping[str, Any], *, path: str = "match") -> Matcher:
    """Compile a matcher spec into a predicate tree.

    Raises:
        SurveySpecError: on unknown keys, bad regexes, or empty lists — all at
            config-load time so the run never starts with a broken rule.
    """
    if not isinstance(spec, Mapping):
        raise SurveySpecError(f"{path}: expected a mapping, got {type(spec).__name__}")
    if not spec:
        raise SurveySpecError(f"{path}: empty matcher. Use {{always: true}} to match everything.")

    unknown = set(spec) - _VALID_KEYS
    if unknown:
        raise SurveySpecError(
            f"{path}: unknown matcher key(s) {sorted(unknown)}. "
            f"Valid keys: {sorted(_VALID_KEYS)}"
        )

    children = [_build_one(key, value, path) for key, value in spec.items()]
    return children[0] if len(children) == 1 else AllOf(tuple(children))


def _build_one(key: str, value: Any, path: str) -> Matcher:
    where = f"{path}.{key}"

    match key:
        case "all" | "any":
            items = _as_sequence(value, where)
            if not items:
                raise SurveySpecError(f"{where}: needs at least one sub-matcher")
            compiled = tuple(
                build_matcher(item, path=f"{where}[{i}]") for i, item in enumerate(items)
            )
            return AllOf(compiled) if key == "all" else AnyOf(compiled)

        case "not":
            return Not(build_matcher(value, path=where))

        case "always":
            return Constant(bool(value))
        case "never":
            return Constant(not bool(value))

        case "question_id":
            return QuestionIdIn(frozenset({str(value)}))
        case "question_id_in":
            ids = {str(v) for v in _as_sequence(value, where)}
            if not ids:
                raise SurveySpecError(f"{where}: empty id list")
            return QuestionIdIn(frozenset(ids))

        case "type":
            raw_types = _as_sequence(value, where) if not isinstance(value, str) else [value]
            resolved = set()
            for raw in raw_types:
                question_type = QuestionType.from_raw(str(raw))
                if question_type is QuestionType.UNKNOWN and str(raw).upper() != "UNKNOWN":
                    raise SurveySpecError(
                        f"{where}: unknown question type {raw!r}. Valid: "
                        f"{[str(t) for t in QuestionType]}"
                    )
                resolved.add(question_type)
            return TypeIs(frozenset(resolved))

        case "text_matches":
            return TextMatches(_compile_regex(value, where))
        case "options_regex":
            return OptionsRegex(_compile_regex(value, where))

        case "text_contains_any" | "text_contains_all":
            needles = tuple(
                normalize_for_match(str(v)) for v in _as_sequence(value, where)
            )
            if not needles:
                raise SurveySpecError(f"{where}: empty phrase list")
            return TextContains(needles, require_all=key.endswith("_all"))

        case "options_equal":
            expected = {normalize_for_match(str(v)) for v in _as_sequence(value, where)}
            if not expected:
                raise SurveySpecError(f"{where}: empty option list")
            return OptionsEqual(frozenset(expected))

        case "options_include_any" | "options_include_all":
            needles = {normalize_for_match(str(v)) for v in _as_sequence(value, where)}
            if not needles:
                raise SurveySpecError(f"{where}: empty option list")
            return OptionsInclude(frozenset(needles), require_all=key.endswith("_all"))

        case "option_count":
            if isinstance(value, int):
                return OptionCount(minimum=value, maximum=value)
            if isinstance(value, Mapping):
                return OptionCount(minimum=value.get("min"), maximum=value.get("max"))
            raise SurveySpecError(f"{where}: expected an int or {{min, max}}")

        case "scale":
            names = {str(v) for v in (
                [value] if isinstance(value, str) else _as_sequence(value, where)
            )}
            if not names:
                raise SurveySpecError(f"{where}: empty scale list")
            return ScaleIs(frozenset(names))

    raise SurveySpecError(f"{where}: unhandled matcher key")


def _as_sequence(value: Any, where: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise SurveySpecError(f"{where}: expected a list, got {type(value).__name__}")
    return value


def _compile_regex(value: Any, where: str) -> re.Pattern[str]:
    if not isinstance(value, str):
        raise SurveySpecError(f"{where}: expected a regex string")
    try:
        # The pattern is written against normalized text, so normalize it too.
        return re.compile(normalize_for_match(value))
    except re.error as exc:
        raise SurveySpecError(f"{where}: invalid regex {value!r}: {exc}") from exc
