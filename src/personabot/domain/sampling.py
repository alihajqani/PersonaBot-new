"""Generic population sampler driven entirely by the survey spec.

Replaces ``core/trait_sampler.py``, whose demographic frames, factor loadings,
band labels and Persian strings were Python constants. Nothing here knows what
a survey is about; the numbers all come from :class:`PopulationSpec`.

The factor model is preserved verbatim from the legacy implementation, which the
audit found mathematically sound::

    theta_dim = L[dim] · f + u_dim · e_dim,   f ~ N(0, I_k),  e ~ N(0, 1)
    u_dim     = sqrt(1 - Σ L[dim]²)

so ``Var(theta) = Σ L² + u² = 1`` exactly and ``Σ = LL' + diag(u²)`` is
positive-semidefinite by construction — no matrix decomposition needed.

Two behaviours changed deliberately, both audit findings:

* **A7** — categorical weights are normalized rather than walked as a raw
  cumulative sum, so a frame summing to 1.03 no longer truncates its last
  category from 0.04 down to 0.01.
* **A8** — out-of-range normal draws are resampled from the truncated
  distribution by default instead of clipped, which used to pile ~24% of the
  youngest cohort onto a single boundary value.
"""

from __future__ import annotations

import math
import random
from typing import Any, Iterator, Mapping

from personabot.domain.expressions import Expression, resolve
from personabot.domain.models import Persona
from personabot.errors import ConfigError, ExpressionError
from personabot.logging_setup import get_logger
from personabot.survey.spec import (
    AnyDist,
    BandsSpec,
    BernoulliDist,
    CategoricalDist,
    ConditionalDist,
    ConstantDist,
    DerivedDist,
    LatentModelSpec,
    NormalDist,
    PopulationSpec,
    UniformIntDist,
    VariableSpec,
)

log = get_logger(__name__)


def band_for(z: float, bands: BandsSpec) -> str:
    """Map a z-score to its band label."""
    index = 0
    for cut in bands.cuts:
        if z >= cut:
            index += 1
    return bands.labels[index]


def draw_traits(
    rng: random.Random, model: LatentModelSpec
) -> dict[str, float]:
    """Draw one respondent's standing on every latent dimension."""
    factors = {name: rng.gauss(0.0, 1.0) for name in model.common_factors}

    traits: dict[str, float] = {}
    for name, dimension in model.dimensions.items():
        ssq = sum(value * value for value in dimension.loadings.values())
        uniqueness = math.sqrt(max(1e-9, 1.0 - ssq))
        common = sum(
            loading * factors[factor]
            for factor, loading in dimension.loadings.items()
        )
        traits[name] = round(common + uniqueness * rng.gauss(0.0, 1.0), 3)
    return traits


# ===== DISTRIBUTION DISPATCH =====

def draw_value(
    body: AnyDist,
    rng: random.Random,
    context: Mapping[str, Any],
    *,
    where: str,
) -> Any:
    """Draw one value from any distribution kind."""
    match body:
        case ConstantDist():
            return body.value

        case UniformIntDist():
            low = int(resolve(body.low, context, rng=rng))
            high = int(resolve(body.high, context, rng=rng))
            if low > high:
                raise ConfigError(
                    f"{where}: uniform_int low ({low}) exceeds high ({high}) for this "
                    f"respondent. Check the bound expressions against the sampled values."
                )
            return rng.randint(low, high)

        case CategoricalDist():
            return _weighted_choice(body, rng, where)

        case NormalDist():
            return _draw_normal(body, rng, context, where)

        case DerivedDist():
            return Expression(body.expr).evaluate(context, rng=rng)

        case BernoulliDist():
            probability = float(resolve(body.p, context, rng=rng))
            outcome = rng.random() < probability
            if body.labels:
                return body.labels.get("true" if outcome else "false", outcome)
            return outcome

        case ConditionalDist():
            for index, case in enumerate(body.cases):
                if case.default:
                    return draw_value(case.body, rng, context, where=f"{where}.cases[{index}]")
                if Expression(case.when or "False").evaluate_bool(context, rng=rng):
                    return draw_value(case.body, rng, context, where=f"{where}.cases[{index}]")
            raise ConfigError(f"{where}: no conditional case matched and no default exists")

    raise ConfigError(f"{where}: unsupported distribution {type(body).__name__}")


def _weighted_choice(body: CategoricalDist, rng: random.Random, where: str) -> Any:
    """Weighted draw over normalized weights.

    Audit A7. The legacy version walked a raw cumulative sum and returned on
    ``r <= cum``. When weights summed above 1 the cumulative crossed 1.0 before
    the final entry, so the tail category became nearly unreachable — silently,
    with no warning and no error.
    """
    total = body.weight_sum
    threshold = rng.random() * total
    cumulative = 0.0
    for label, weight in body.choices:
        cumulative += weight
        if threshold < cumulative:
            return label
    return body.choices[-1][0]


def _draw_normal(
    body: NormalDist,
    rng: random.Random,
    context: Mapping[str, Any],
    where: str,
) -> float | int:
    mu = float(resolve(body.mu, context, rng=rng))
    sigma = float(resolve(body.sigma, context, rng=rng))

    def finish(value: float) -> float | int:
        return int(round(value)) if body.round == "int" else value

    if body.clip is None:
        return finish(rng.gauss(mu, sigma))

    low = float(resolve(body.clip[0], context, rng=rng))
    high = float(resolve(body.clip[1], context, rng=rng))
    if low > high:
        raise ConfigError(
            f"{where}: clip lower bound ({low}) exceeds upper bound ({high}) for this "
            f"respondent. Check the bound expressions."
        )

    if body.on_out_of_range == "resample":
        for _ in range(body.max_resample_attempts):
            value = rng.gauss(mu, sigma)
            if low <= value <= high:
                return finish(value)
        # Bounds may sit far into a tail; fall back rather than loop forever.
        log.debug(
            "%s: truncated resampling exhausted after %d attempts "
            "(mu=%.2f sigma=%.2f range=[%.2f, %.2f]); clipping instead.",
            where, body.max_resample_attempts, mu, sigma, low, high,
        )

    return finish(max(low, min(high, rng.gauss(mu, sigma))))


# ===== SAMPLER =====

class PopulationSampler:
    """Draws synthetic respondents according to a :class:`PopulationSpec`."""

    def __init__(self, population: PopulationSpec) -> None:
        self.population = population
        self._validate_no_forward_references()

    def _validate_no_forward_references(self) -> None:
        """Catch a variable referencing one declared after it.

        Declaration order *is* the evaluation order, so this is checkable up
        front rather than surfacing as an "Unknown name" mid-run.
        """
        available = {"z"}
        for variable in [*self.population.demographics, *self.population.derived_fields]:
            for source in _expression_sources(variable):
                try:
                    names = _referenced_names(source)
                except ExpressionError:
                    continue  # already reported by the spec validator
                unknown = names - available - {"z"}
                if unknown:
                    raise ConfigError(
                        f"variable '{variable.name}' references {sorted(unknown)}, which "
                        f"is not defined before it. Variables are drawn in declaration "
                        f"order; move it earlier or rename. Available here: "
                        f"{sorted(available - {'z'}) or '(none)'}"
                    )
            available.add(variable.name)

    def sample(self, index: int, rng: random.Random) -> Persona:
        """Draw one respondent."""
        traits = draw_traits(rng, self.population.latent_model)
        bands = {
            name: band_for(value, self.population.bands)
            for name, value in traits.items()
        }

        # `z` exposes trait scores to expressions, e.g. sigmoid(z.anxiety).
        context: dict[str, Any] = {"z": traits}

        response_style: dict[str, Any] = {}
        for name, body in self.population.response_style.items():
            value = draw_value(body, rng, context, where=f"response_style.{name}")
            response_style[name] = value
            context[name] = value

        demographics: dict[str, Any] = {}
        for variable in self.population.demographics:
            value = draw_value(variable.body, rng, context, where=f"demographics.{variable.name}")
            context[variable.name] = value
            if variable.persist:
                demographics[variable.name] = value

        derived: dict[str, Any] = {}
        for variable in self.population.derived_fields:
            value = draw_value(variable.body, rng, context, where=f"derived_fields.{variable.name}")
            context[variable.name] = value
            if variable.persist:
                derived[variable.name] = value

        return Persona(
            id=f"persona_{index}",
            demographics=demographics,
            derived=derived,
            traits_z=traits,
            traits_band=bands,
            response_style=response_style,
        )

    def sample_population(self, count: int, seed: int | None = None) -> list[Persona]:
        """Draw ``count`` respondents.

        The seed comes from the caller or the spec. Supplying one makes a whole
        run reproducible, which the legacy sampler could not do.
        """
        effective_seed = seed if seed is not None else self.population.seed
        rng = random.Random(effective_seed)
        return [self.sample(i + 1, rng) for i in range(count)]

    def iter_population(
        self, count: int, seed: int | None = None
    ) -> Iterator[Persona]:
        effective_seed = seed if seed is not None else self.population.seed
        rng = random.Random(effective_seed)
        for i in range(count):
            yield self.sample(i + 1, rng)


def _expression_sources(variable: VariableSpec) -> list[str]:
    """Every expression string a variable can evaluate."""
    sources: list[str] = []

    def collect(body: Any) -> None:
        match body:
            case UniformIntDist():
                sources.extend(s for s in (body.low, body.high) if isinstance(s, str))
            case NormalDist():
                sources.extend(s for s in (body.mu, body.sigma) if isinstance(s, str))
                if body.clip:
                    sources.extend(s for s in body.clip if isinstance(s, str))
            case DerivedDist():
                sources.append(body.expr)
            case BernoulliDist():
                if isinstance(body.p, str):
                    sources.append(body.p)
            case ConditionalDist():
                for case in body.cases:
                    if case.when:
                        sources.append(case.when)
                    collect(case.body)

    collect(variable.body)
    return sources


def _referenced_names(source: str) -> set[str]:
    """Free variable names in an expression, excluding function names."""
    import ast

    tree = ast.parse(source, mode="eval")
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    return {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id not in called
    }
