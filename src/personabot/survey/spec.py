"""Pydantic models for the survey YAML.

These models *are* the config schema. A malformed spec fails here, at load, with
a field path — never silently at question 47 of a run.

The layering rule: this file describes an **instrument** (population, scales,
constructs, reverse keys, static bindings). It holds nothing about credentials,
endpoints or concurrency — those live in :mod:`personabot.settings` — and
nothing about wording, which lives in the Jinja2 prompt preset. Keeping the
three apart is what makes a new survey a config change rather than a code change.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from personabot.domain.expressions import Expression
from personabot.errors import SurveySpecError

# A YAML scalar that may be a literal number or an expression string such as
# "min(age - 1, 29)". Validated at load; evaluated per respondent.
NumberOrExpr = Union[float, int, str]


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _check_expression(value: Any, where: str) -> Any:
    """Compile a string field now so a typo surfaces at load time."""
    if isinstance(value, str):
        try:
            Expression(value)
        except Exception as exc:
            raise SurveySpecError(f"{where}: {exc}") from exc
    return value


# ===== DISTRIBUTIONS =====

class ConstantDist(_Base):
    dist: Literal["constant"]
    value: Any


class UniformIntDist(_Base):
    dist: Literal["uniform_int"]
    low: NumberOrExpr
    high: NumberOrExpr

    @field_validator("low", "high")
    @classmethod
    def _valid_expr(cls, v: Any) -> Any:
        return _check_expression(v, "uniform_int bound")


class CategoricalDist(_Base):
    """A weighted choice over labels.

    Weights are normalized on use. Audit finding A7: the legacy occupation frame
    summed to 1.03, and the cumulative walk returned before reaching the final
    entry, so "unemployed" was drawn at 0.01 instead of its nominal 0.04. The
    loader warns whenever weights do not sum to 1 rather than quietly truncating.
    """

    dist: Literal["categorical"]
    choices: list[tuple[Any, float]]

    @field_validator("choices")
    @classmethod
    def _positive_and_nonempty(cls, choices: list[tuple[Any, float]]) -> list[tuple[Any, float]]:
        if not choices:
            raise ValueError("categorical needs at least one choice")
        if any(weight < 0 for _, weight in choices):
            raise ValueError("categorical weights must be non-negative")
        if sum(weight for _, weight in choices) <= 0:
            raise ValueError("categorical weights sum to zero")
        return choices

    @property
    def weight_sum(self) -> float:
        return sum(weight for _, weight in self.choices)


class NormalDist(_Base):
    dist: Literal["normal"]
    mu: NumberOrExpr
    sigma: NumberOrExpr
    round: Literal["int"] | None = None
    clip: tuple[NumberOrExpr, NumberOrExpr] | None = None
    # Audit finding A8: clipping piles probability mass onto a boundary. For the
    # legacy frame, ~24% of 25-year-olds landed on exactly one marriage age.
    # Resampling draws from the truncated distribution instead.
    on_out_of_range: Literal["resample", "clip"] = "resample"
    max_resample_attempts: int = Field(default=64, ge=1, le=10_000)

    @field_validator("mu", "sigma")
    @classmethod
    def _valid_expr(cls, v: Any) -> Any:
        return _check_expression(v, "normal parameter")

    @model_validator(mode="after")
    def _clip_needed_for_resample(self) -> "NormalDist":
        if self.on_out_of_range == "resample" and self.clip is None:
            # Nothing to resample against; harmless, but say so.
            object.__setattr__(self, "on_out_of_range", "clip")
        if self.clip is not None:
            for bound in self.clip:
                _check_expression(bound, "normal clip bound")
        return self


class DerivedDist(_Base):
    """A deterministic function of variables already drawn."""

    dist: Literal["derived"]
    expr: str

    @field_validator("expr")
    @classmethod
    def _valid_expr(cls, v: str) -> str:
        return _check_expression(v, "derived expr")


class BernoulliDist(_Base):
    dist: Literal["bernoulli"]
    p: NumberOrExpr
    labels: dict[str, Any] | None = None

    @field_validator("p")
    @classmethod
    def _valid_expr(cls, v: Any) -> Any:
        return _check_expression(v, "bernoulli p")

    @field_validator("labels")
    @classmethod
    def _true_false_keys(cls, labels: dict[str, Any] | None) -> dict[str, Any] | None:
        if labels is None:
            return None
        keys = {str(k).lower() for k in labels}
        if not keys <= {"true", "false"}:
            raise ValueError("bernoulli labels must use only 'true' and 'false' keys")
        return {str(k).lower(): v for k, v in labels.items()}


class ConditionalCase(_Base):
    """One branch of a conditional draw. Evaluated in order, first hit wins."""

    when: str | None = None
    default: bool = False
    body: "AnyDist"

    @model_validator(mode="before")
    @classmethod
    def _lift_inline_dist(cls, data: Any) -> Any:
        """Allow the branch's distribution to be written inline.

        So ``{when: "x > 2", dist: categorical, choices: [...]}`` rather than
        forcing a nested ``body:`` key.
        """
        if not isinstance(data, dict) or "body" in data:
            return data
        control = {k: data[k] for k in ("when", "default") if k in data}
        body = {k: v for k, v in data.items() if k not in ("when", "default")}
        return {**control, "body": body}

    @model_validator(mode="after")
    def _needs_a_condition(self) -> "ConditionalCase":
        if not self.default and self.when is None:
            raise ValueError("a conditional case needs either 'when' or 'default: true'")
        if self.when is not None:
            _check_expression(self.when, "conditional when")
        return self


class ConditionalDist(_Base):
    dist: Literal["conditional"]
    cases: list[ConditionalCase]

    @field_validator("cases")
    @classmethod
    def _has_default_last(cls, cases: list[ConditionalCase]) -> list[ConditionalCase]:
        if not cases:
            raise ValueError("conditional needs at least one case")
        if not any(case.default for case in cases):
            raise ValueError(
                "conditional needs a final 'default: true' case, otherwise a "
                "respondent can fall through with no value"
            )
        if not cases[-1].default:
            raise ValueError("the 'default: true' case must be last")
        return cases


AnyDist = Annotated[
    Union[
        ConstantDist, UniformIntDist, CategoricalDist,
        NormalDist, DerivedDist, BernoulliDist, ConditionalDist,
    ],
    Field(discriminator="dist"),
]

ConditionalCase.model_rebuild()


class VariableSpec(_Base):
    """A named variable plus how to draw it.

    The YAML is flat — ``{name: age, dist: uniform_int, low: 40, high: 65}`` —
    so the distribution fields are lifted into ``body`` before validation.
    """

    name: str
    persist: bool = True
    body: AnyDist

    @model_validator(mode="before")
    @classmethod
    def _lift_inline_dist(cls, data: Any) -> Any:
        if not isinstance(data, dict) or "body" in data:
            return data
        control = {k: data[k] for k in ("name", "persist") if k in data}
        body = {k: v for k, v in data.items() if k not in ("name", "persist")}
        return {**control, "body": body}


# ===== POPULATION =====

class DimensionSpec(_Base):
    loadings: dict[str, float]
    label: str = ""


class LatentModelSpec(_Base):
    """A factor model: ``theta = L·f + u·e``, with ``f ~ N(0, I)``.

    Squared loadings must sum to < 1 so uniqueness ``u = sqrt(1 - sum)`` is real
    and every trait has unit variance. That constraint is what makes the implied
    covariance positive-semidefinite by construction, with no matrix algebra —
    the one piece of the legacy code the audit found sound and worth preserving.
    """

    common_factors: list[str] = Field(min_length=1)
    dimensions: dict[str, DimensionSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_loadings(self) -> "LatentModelSpec":
        factors = set(self.common_factors)
        for name, dimension in self.dimensions.items():
            unknown = set(dimension.loadings) - factors
            if unknown:
                raise ValueError(
                    f"dimension '{name}' loads on unknown factor(s) {sorted(unknown)}; "
                    f"declared factors are {sorted(factors)}"
                )
            ssq = sum(value * value for value in dimension.loadings.values())
            if ssq >= 1.0:
                raise ValueError(
                    f"dimension '{name}': squared loadings sum to {ssq:.4f}, which must "
                    f"be < 1 so uniqueness stays real. Scale the loadings down."
                )
        return self

    def communality(self, dimension: str) -> float:
        """Share of a dimension's variance explained by the common factors."""
        loadings = self.dimensions[dimension].loadings
        return sum(value * value for value in loadings.values())

    def implied_correlation(self, a: str, b: str) -> float:
        """Correlation the model implies between two dimensions.

        Off-diagonal is the loading cross-product. The diagonal is exactly 1:
        traits carry unit variance because uniqueness fills the gap left by the
        common factors (``ssq + u**2 == 1``), so returning the communality there
        would understate a trait's correlation with itself.
        """
        if a == b:
            return 1.0
        first = self.dimensions[a].loadings
        second = self.dimensions[b].loadings
        return sum(
            first.get(factor, 0.0) * second.get(factor, 0.0)
            for factor in self.common_factors
        )


class BandsSpec(_Base):
    """Cut points turning a z-score into a word. Only words reach the LLM."""

    cuts: list[float] = Field(min_length=1)
    labels: list[str] = Field(min_length=2)

    @model_validator(mode="after")
    def _shapes_agree(self) -> "BandsSpec":
        if len(self.labels) != len(self.cuts) + 1:
            raise ValueError(
                f"bands: {len(self.cuts)} cuts need {len(self.cuts) + 1} labels, "
                f"got {len(self.labels)}"
            )
        if list(self.cuts) != sorted(self.cuts):
            raise ValueError("bands: cuts must be in ascending order")
        return self


class PopulationSpec(_Base):
    seed: int | None = None
    latent_model: LatentModelSpec
    bands: BandsSpec
    response_style: dict[str, AnyDist] = Field(default_factory=dict)
    demographics: list[VariableSpec] = Field(default_factory=list)
    derived_fields: list[VariableSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_names(self) -> "PopulationSpec":
        seen: set[str] = set()
        for variable in [*self.demographics, *self.derived_fields]:
            if variable.name in seen:
                raise ValueError(f"duplicate variable name '{variable.name}'")
            seen.add(variable.name)
        return self


# ===== INSTRUMENT =====

class ScaleSpec(_Base):
    """A response format, plus the direction its options run in.

    ``coding`` decides ordinal scoring: ``low_first`` means index 0 is the low
    end (score = index + 1); ``high_first`` means index 0 is the high end
    (score = K - index). Declared per scale because different scales in the same
    instrument can share option strings with opposite meanings.
    """

    name: str
    match: dict[str, Any]
    coding: Literal["low_first", "high_first"] = "low_first"


class ConstructSpec(_Base):
    key: str
    label: str
    traits: list[str] = Field(min_length=1)
    match: dict[str, Any]
    reverse_when: dict[str, Any] | None = None


class StaticAnswerSpec(_Base):
    """A question answered from the persona record, never by the LLM.

    This is what removes the hard-coded question-ID table from the answer
    prompt. Values are Jinja2 expressions over ``persona``, so derived facts
    (spouse age, menopause age) are computed deterministically by the sampler
    instead of being left to the model's arithmetic.
    """

    name: str
    match: dict[str, Any]
    value: str
    exclude_from_psychometrics: bool = False


# ===== ANSWERING =====

class NormalizationSpec(_Base):
    unicode: str = "NFKC"
    fold_arabic_to_persian: bool = True
    zwnj: Literal["strip", "space", "keep"] = "strip"
    # Includes the Persian comma (U+060C) and full stop (U+06D4) alongside the
    # ASCII marks. Omitting «،» pushed otherwise-exact answers down the fuzzy
    # path — harmless at 91%, but it burns threshold headroom for no reason.
    strip_trailing: str = ".,؛،۔"


class FuzzySpec(_Base):
    algorithm: Literal["ratio", "partial_ratio", "token_sort_ratio"] = "ratio"
    min_score: int = Field(default=86, ge=0, le=100)


class CompletenessSpec(_Base):
    min_ratio: float = Field(default=0.95, gt=0, le=1)
    denominator: Literal["answerable", "all"] = "answerable"
    max_retries: int = Field(default=2, ge=0)
    retry_scope: Literal["failed_constructs_only", "whole_persona"] = "failed_constructs_only"


class CarelessSpec(_Base):
    """Inattentive responders, simulated in code because models cannot fake it."""

    enabled: bool = True
    anchor_stickiness: float = Field(default=0.70, ge=0, le=1)
    drift: list[int] = Field(default_factory=lambda: [-1, 1])
    # Audit finding A10: the legacy version skipped every question without
    # options, so careless personas received no text answers at all.
    apply_static_answers: bool = True


class ReverseTagsSpec(_Base):
    direct: str = "[direct]"
    reverse: str = "[reverse]"


class TemperatureSpec(_Base):
    min: float = Field(default=0.55, ge=0, le=2)
    max: float = Field(default=0.85, ge=0, le=2)

    @model_validator(mode="after")
    def _ordered(self) -> "TemperatureSpec":
        if self.min > self.max:
            raise ValueError("temperature.min must be <= temperature.max")
        return self


class AnsweringSpec(_Base):
    chunk_by: Literal["construct", "none"] = "construct"
    # Presenting items out of scale order is a deliberate guard against the
    # self-consistency that inflates Cronbach's alpha.
    shuffle_within_chunk: bool = True
    temperature: TemperatureSpec = Field(default_factory=TemperatureSpec)
    normalization: NormalizationSpec = Field(default_factory=NormalizationSpec)
    fuzzy: FuzzySpec = Field(default_factory=FuzzySpec)
    completeness: CompletenessSpec = Field(default_factory=CompletenessSpec)
    careless: CarelessSpec = Field(default_factory=CarelessSpec)
    reverse_tags: ReverseTagsSpec = Field(default_factory=ReverseTagsSpec)


class NudgeRule(_Base):
    when: str
    text: str

    @field_validator("when")
    @classmethod
    def _valid_expr(cls, v: str) -> str:
        return _check_expression(v, "nudge when")


class NudgesSpec(_Base):
    rules: list[NudgeRule] = Field(default_factory=list)
    default: str = ""


# ===== ANALYSIS =====

class StraightliningSpec(_Base):
    scope: Literal["within_construct", "pooled"] = "within_construct"
    sd_threshold: float = Field(default=0.5, gt=0)


class SanitySpec(_Base):
    alpha_max: float = 0.95
    mean_inter_item_r_max: float = 0.6


class AnalysisSpec(_Base):
    # Audit finding A3: auto-reversing any item with a negative item-total
    # correlation capitalizes on chance and inflates alpha — the very artifact
    # this project exists to detect. The config is the source of truth; the
    # harness reports disagreement rather than silently flipping.
    reverse_keys_from: Literal["config", "auto", "both"] = "config"
    warn_on_sign_disagreement: bool = True
    # Audit finding A4: two-option demographics always score as extreme
    # responses, which structurally inflates the response-style indices.
    response_style_min_scale_points: int = Field(default=3, ge=2)
    straightlining: StraightliningSpec = Field(default_factory=StraightliningSpec)
    sanity: SanitySpec = Field(default_factory=SanitySpec)
    min_items_for_alpha: int = Field(default=3, ge=2)


# ===== ROOT =====

class SurveySpec(_Base):
    version: int = 1
    id: str
    locale: str = "en"
    prompt_preset: str

    population: PopulationSpec
    scales: list[ScaleSpec] = Field(default_factory=list)
    constructs: list[ConstructSpec] = Field(min_length=1)
    static_answers: list[StaticAnswerSpec] = Field(default_factory=list)
    skip: list[dict[str, Any]] = Field(default_factory=list)

    answering: AnsweringSpec = Field(default_factory=AnsweringSpec)
    response_style_nudges: NudgesSpec = Field(default_factory=NudgesSpec)
    analysis: AnalysisSpec = Field(default_factory=AnalysisSpec)

    @model_validator(mode="after")
    def _cross_references_resolve(self) -> "SurveySpec":
        dimensions = set(self.population.latent_model.dimensions)

        for construct in self.constructs:
            unknown = set(construct.traits) - dimensions
            if unknown:
                raise ValueError(
                    f"construct '{construct.key}' references unknown trait(s) "
                    f"{sorted(unknown)}; declared dimensions are {sorted(dimensions)}"
                )

        for collection, label in ((self.constructs, "construct"), (self.scales, "scale")):
            keys = [getattr(item, "key", None) or item.name for item in collection]
            duplicates = {k for k in keys if keys.count(k) > 1}
            if duplicates:
                raise ValueError(f"duplicate {label} name(s) {sorted(duplicates)}")

        names = [entry.name for entry in self.static_answers]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise ValueError(f"duplicate static_answers name(s) {sorted(duplicates)}")

        return self
