"""The instrument interpreter.

Replaces ``core/instrument.py``, which was a configuration file for one specific
questionnaire written as Python: scale detection, construct assignment,
attachment-pole splitting, reverse keying and demographic binding were all
keyword matches against exact Persian string literals. Adding a second survey
meant editing the core.

Here the same behaviours are read from a :class:`SurveySpec`. This module
contains no survey content at all.

Resolution order, per question:

1. **Scale** — ordered, first match wins. Carries the response format and the
   ordinal coding direction.
2. **Static binding** — ordered, first match wins. A question bound here is
   answered from the persona and excluded from every LLM chunk.
3. **Construct** — ordered, first match wins, evaluated with the scale in
   context so a rule can say ``{scale: attachment}``. Ordering is what makes
   pole splitting fall out naturally: list the narrower rule first.

The catch-all that used to swallow everything is gone. A question matching no
construct is a **coverage error**, surfaced by :meth:`Instrument.coverage`.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping

from jinja2 import BaseLoader, Environment, StrictUndefined, TemplateError

from personabot.domain.matching import MatchContext, Matcher, build_matcher
from personabot.domain.models import FormSchema, Persona, Question
from personabot.errors import SchemaCoverageError, SurveySpecError
from personabot.logging_setup import get_logger
from personabot.survey.spec import ConstructSpec, ScaleSpec, StaticAnswerSpec, SurveySpec

log = get_logger(__name__)

# StrictUndefined: a static-answer template referencing a field the sampler does
# not produce fails loudly instead of rendering an empty string into a live form.
_JINJA = Environment(loader=BaseLoader(), undefined=StrictUndefined, autoescape=False)


@dataclass(frozen=True, slots=True)
class Binding:
    """How one question resolves against the spec."""

    question: Question
    scale: ScaleSpec | None = None
    construct: ConstructSpec | None = None
    static: StaticAnswerSpec | None = None
    is_reverse: bool = False
    skipped: bool = False

    @property
    def is_static(self) -> bool:
        return self.static is not None

    @property
    def counts_toward_psychometrics(self) -> bool:
        if self.skipped:
            return False
        if self.static is not None:
            return not self.static.exclude_from_psychometrics
        return self.construct is not None


@dataclass(slots=True)
class CoverageReport:
    """What the spec does and does not account for.

    This is the guard against the failure that made every pre-refactor run
    worthless: stale signatures matched nothing, everything fell into a
    catch-all bucket, and the pipeline reported success.
    """

    total: int = 0
    static: int = 0
    skipped: int = 0
    by_construct: dict[str, int] = field(default_factory=dict)
    by_scale: dict[str, int] = field(default_factory=dict)
    unassigned: list[Question] = field(default_factory=list)
    unused_scales: list[str] = field(default_factory=list)
    unused_constructs: list[str] = field(default_factory=list)
    unused_static: list[str] = field(default_factory=list)
    choice_without_scale: list[Question] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (
            self.unassigned
            or self.unused_scales
            or self.unused_constructs
            or self.unused_static
        )

    def render(self) -> str:
        lines = [
            f"Schema coverage: {self.total} questions "
            f"({self.static} static, {self.skipped} skipped, "
            f"{sum(self.by_construct.values())} psychometric)",
        ]
        if self.by_scale:
            lines.append("  scales:")
            lines += [f"    {name:28s} {count:3d}" for name, count in self.by_scale.items()]
        if self.by_construct:
            lines.append("  constructs:")
            lines += [f"    {name:28s} {count:3d}" for name, count in self.by_construct.items()]

        if self.unassigned:
            lines.append(f"  UNASSIGNED ({len(self.unassigned)}):")
            for question in self.unassigned[:10]:
                lines.append(
                    f"    {question.question_id}  {question.question_text[:60]!r}"
                )
            if len(self.unassigned) > 10:
                lines.append(f"    … and {len(self.unassigned) - 10} more")
        if self.unused_scales:
            lines.append(f"  SCALES MATCHING NOTHING: {self.unused_scales}")
        if self.unused_constructs:
            lines.append(f"  CONSTRUCTS MATCHING NOTHING: {self.unused_constructs}")
        if self.unused_static:
            lines.append(f"  STATIC BINDINGS MATCHING NOTHING: {self.unused_static}")
        if self.choice_without_scale:
            lines.append(
                f"  choice questions with no scale (not scoreable): "
                f"{[q.question_id for q in self.choice_without_scale]}"
            )
        return "\n".join(lines)


class Instrument:
    """A survey spec bound to a concrete form schema."""

    def __init__(self, spec: SurveySpec, schema: FormSchema) -> None:
        self.spec = spec
        self.schema = schema

        self._scale_matchers = [
            (scale, build_matcher(scale.match, path=f"scales[{i}].match"))
            for i, scale in enumerate(spec.scales)
        ]
        self._construct_matchers = [
            (construct, build_matcher(construct.match, path=f"constructs[{i}].match"))
            for i, construct in enumerate(spec.constructs)
        ]
        self._reverse_matchers: dict[str, Matcher | None] = {
            construct.key: (
                build_matcher(construct.reverse_when, path=f"constructs[{i}].reverse_when")
                if construct.reverse_when
                else None
            )
            for i, construct in enumerate(spec.constructs)
        }
        self._static_matchers = [
            (entry, build_matcher(entry.match, path=f"static_answers[{i}].match"))
            for i, entry in enumerate(spec.static_answers)
        ]
        self._skip_matchers = [
            build_matcher(rule, path=f"skip[{i}]") for i, rule in enumerate(spec.skip)
        ]

        self.bindings: dict[str, Binding] = self._resolve_all()

    # ------------------------------------------------------------- resolution

    def _resolve_all(self) -> dict[str, Binding]:
        bindings: dict[str, Binding] = {}
        for question in self.schema:
            bindings[question.question_id] = self._resolve(question)
        return bindings

    def _resolve(self, question: Question) -> Binding:
        if any(matcher.matches(question) for matcher in self._skip_matchers):
            return Binding(question=question, skipped=True)

        scale = next(
            (s for s, matcher in self._scale_matchers if matcher.matches(question)), None
        )
        context = MatchContext(scale=scale.name if scale else None)

        static = next(
            (e for e, matcher in self._static_matchers if matcher.matches(question, context)),
            None,
        )
        if static is not None:
            return Binding(question=question, scale=scale, static=static)

        construct = next(
            (c for c, matcher in self._construct_matchers if matcher.matches(question, context)),
            None,
        )
        is_reverse = False
        if construct is not None:
            reverse_matcher = self._reverse_matchers.get(construct.key)
            is_reverse = bool(reverse_matcher and reverse_matcher.matches(question, context))

        return Binding(
            question=question, scale=scale, construct=construct, is_reverse=is_reverse
        )

    # -------------------------------------------------------------- coverage

    def coverage(self) -> CoverageReport:
        """Audit the spec against the schema."""
        report = CoverageReport(total=len(self.schema))

        seen_scales: set[str] = set()
        seen_constructs: set[str] = set()
        seen_static: set[str] = set()

        for binding in self.bindings.values():
            if binding.skipped:
                report.skipped += 1
                continue
            if binding.scale:
                seen_scales.add(binding.scale.name)
                report.by_scale[binding.scale.name] = report.by_scale.get(binding.scale.name, 0) + 1
            if binding.static:
                report.static += 1
                seen_static.add(binding.static.name)
                continue
            if binding.construct:
                key = binding.construct.key
                seen_constructs.add(key)
                report.by_construct[key] = report.by_construct.get(key, 0) + 1
                if binding.question.is_choice and binding.scale is None:
                    report.choice_without_scale.append(binding.question)
                continue
            report.unassigned.append(binding.question)

        report.unused_scales = [s.name for s in self.spec.scales if s.name not in seen_scales]
        report.unused_constructs = [
            c.key for c in self.spec.constructs if c.key not in seen_constructs
        ]
        report.unused_static = [
            e.name for e in self.spec.static_answers if e.name not in seen_static
        ]
        return report

    def require_full_coverage(self) -> CoverageReport:
        """Raise unless every question is accounted for and every rule is used."""
        report = self.coverage()
        if not report.ok:
            raise SchemaCoverageError(
                "The survey spec does not match this form schema.\n"
                + report.render()
                + "\n\nEvery question must resolve to a static binding, a construct, or "
                "an explicit skip rule, and every declared rule must match something. "
                "A rule that matches nothing is almost always a stale signature."
            )
        return report

    # ------------------------------------------------------------- accessors

    @property
    def answerable_ids(self) -> tuple[str, ...]:
        """Questions that must appear in a finished answer set."""
        return tuple(
            qid for qid, binding in self.bindings.items() if not binding.skipped
        )

    @property
    def psychometric_ids(self) -> tuple[str, ...]:
        return tuple(
            qid for qid, binding in self.bindings.items()
            if binding.construct is not None
        )

    def binding_for(self, question_id: str) -> Binding:
        return self.bindings[question_id]

    def grouped_by_construct(self) -> list[tuple[ConstructSpec, list[Question]]]:
        """Psychometric questions bucketed by construct, in spec order.

        Chunking by construct — rather than asking for the whole questionnaire
        at once — is the deliberate guard against the self-consistency that
        inflates Cronbach's alpha. Consolidating these chunks would defeat the
        methodological point of the project.
        """
        buckets: dict[str, list[Question]] = {}
        for binding in self.bindings.values():
            if binding.construct is None or binding.skipped:
                continue
            buckets.setdefault(binding.construct.key, []).append(binding.question)
        return [
            (construct, buckets[construct.key])
            for construct in self.spec.constructs
            if construct.key in buckets
        ]

    def iter_constructs(self) -> Iterator[tuple[ConstructSpec, list[Question]]]:
        yield from self.grouped_by_construct()

    # -------------------------------------------------------- static answers

    def static_answers(self, persona: Persona) -> dict[str, str]:
        """Answer bound questions directly from the persona.

        Guarantees identity consistency across a long questionnaire and keeps
        arithmetic (spouse age, menopause age) out of the model's hands. The
        predecessor delegated this to a hard-coded question-ID table inside the
        prompt, which silently detached whenever the form was republished.
        """
        context = {"persona": persona.to_dict() | {
            "demographics": persona.demographics,
            "derived": persona.derived,
        }}

        answers: dict[str, str] = {}
        for question_id, binding in self.bindings.items():
            if binding.static is None:
                continue
            try:
                rendered = _JINJA.from_string(binding.static.value).render(context)
            except TemplateError as exc:
                raise SurveySpecError(
                    f"static_answers['{binding.static.name}'].value failed to render: "
                    f"{exc}. Template was {binding.static.value!r}. Available persona "
                    f"fields: demographics={sorted(persona.demographics)}, "
                    f"derived={sorted(persona.derived)}"
                ) from exc
            answers[question_id] = rendered.strip()
        return answers

    # ------------------------------------------------------- ordinal scoring

    def numeric_code(self, question: Question, value: str) -> int | None:
        """Score an option string on this question's scale, 1..K.

        Direction comes from the scale's ``coding`` field rather than a flat
        option-to-score dictionary, because different scales in one instrument
        can share option strings with opposite meanings.
        """
        options = question.option_values
        if value not in options:
            return None
        binding = self.bindings.get(question.question_id)
        index = options.index(value)
        size = len(options)
        if binding and binding.scale and binding.scale.coding == "high_first":
            return size - index
        return index + 1

    def is_reverse(self, question_id: str) -> bool:
        binding = self.bindings.get(question_id)
        return bool(binding and binding.is_reverse)

    # ------------------------------------------------------------- careless

    def careless_answers(self, rng: random.Random) -> dict[str, str]:
        """Straightlining-with-noise over the psychometric items.

        Simulated in code because models cannot convincingly fake inattention.
        Static bindings are applied separately by the caller, so — unlike the
        predecessor (audit A10) — a careless persona still receives every
        demographic and free-text answer and does not sink below the
        completeness gate.
        """
        config = self.spec.answering.careless
        answers: dict[str, str] = {}

        for _construct, questions in self.grouped_by_construct():
            anchor: int | None = None
            for question in questions:
                options = question.option_values
                if not options:
                    continue
                if anchor is None:
                    anchor = rng.randrange(len(options))
                base = min(anchor, len(options) - 1)
                if rng.random() < config.anchor_stickiness:
                    index = base
                else:
                    index = base + rng.choice(config.drift)
                answers[question.question_id] = options[max(0, min(len(options) - 1, index))]
        return answers

    # -------------------------------------------------------- response style

    def response_style_text(self, style: Mapping[str, Any]) -> str:
        """Turn sampled response-style parameters into prompt language.

        Injects realistic method variance — acquiescence, extreme vs midpoint
        preference — without telling the model what its trait answers should be.
        """
        nudges = self.spec.response_style_nudges
        from personabot.domain.expressions import Expression

        parts = [
            rule.text
            for rule in nudges.rules
            if Expression(rule.when).evaluate_bool(dict(style))
        ]
        return " ".join(parts) if parts else nudges.default

    def trait_bands_for(
        self, persona: Persona, construct: ConstructSpec
    ) -> dict[str, str]:
        """The band words for one construct's traits.

        Only words reach the prompt; the raw z-scores stay in the record. That
        boundary is deliberate — handing the model numbers invites it to
        reverse-engineer a target rather than answer in character.
        """
        labels = self.spec.population.latent_model.dimensions
        return {
            labels[trait].label or trait: persona.traits_band[trait]
            for trait in construct.traits
            if trait in persona.traits_band
        }
