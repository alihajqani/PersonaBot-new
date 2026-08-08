"""The generic population sampler.

The factor model is carried over verbatim from `core/trait_sampler.py`, which
the audit found mathematically sound. These tests pin its statistical
properties so a future config edit cannot quietly change the psychometrics.
"""

from __future__ import annotations

import math
import random
from collections import Counter

import pytest

from personabot.domain.sampling import PopulationSampler, band_for, draw_value
from personabot.errors import ConfigError
from personabot.survey.spec import CategoricalDist, DerivedDist, NormalDist

# Monte-Carlo sample large enough that a 0.02 tolerance is meaningful.
N = 20_000
SEED = 42


def correlation(xs, ys) -> float:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    return sxy / math.sqrt(sxx * syy)


@pytest.fixture(scope="module")
def population(spec):
    return PopulationSampler(spec.population).sample_population(N, seed=SEED)


class TestFactorModel:
    def test_traits_have_unit_variance(self, population, spec):
        """Var(z) = sum(loadings^2) + u^2 = 1 by construction."""
        for dimension in spec.population.latent_model.dimensions:
            values = [p.traits_z[dimension] for p in population]
            mean = sum(values) / N
            variance = sum((v - mean) ** 2 for v in values) / (N - 1)
            assert variance == pytest.approx(1.0, abs=0.05)
            assert mean == pytest.approx(0.0, abs=0.05)

    def test_empirical_correlations_match_the_design(self, population, spec):
        model = spec.population.latent_model
        dimensions = list(model.dimensions)
        columns = {d: [p.traits_z[d] for p in population] for d in dimensions}

        for i, a in enumerate(dimensions):
            for b in dimensions[i + 1:]:
                empirical = correlation(columns[a], columns[b])
                designed = model.implied_correlation(a, b)
                assert empirical == pytest.approx(designed, abs=0.02), (
                    f"r({a}, {b}): sampled {empirical:+.3f}, design {designed:+.3f}"
                )

    def test_self_correlation_is_one(self, spec):
        model = spec.population.latent_model
        for dimension in model.dimensions:
            assert model.implied_correlation(dimension, dimension) == 1.0


class TestBands:
    def test_maps_z_scores_to_labels(self, spec):
        bands = spec.population.bands
        assert band_for(-5.0, bands) == bands.labels[0]
        assert band_for(5.0, bands) == bands.labels[-1]
        assert band_for(0.0, bands) == bands.labels[len(bands.cuts) // 2]

    def test_every_persona_gets_a_band_for_every_trait(self, population, spec):
        dimensions = set(spec.population.latent_model.dimensions)
        labels = set(spec.population.bands.labels)
        for persona in population[:200]:
            assert set(persona.traits_band) == dimensions
            assert set(persona.traits_band.values()) <= labels


class TestCategoricalWeights:
    """Audit A7: the legacy occupation frame summed to 1.03 and its final
    category was drawn at 0.01 instead of its nominal 0.04."""

    def test_respects_declared_proportions(self, population):
        counts = Counter(p.demographics["occupation"] for p in population)
        assert counts["خانه دار"] / N == pytest.approx(0.62, abs=0.02)
        assert counts["شاغل"] / N == pytest.approx(0.38, abs=0.02)

    def test_normalizes_weights_that_do_not_sum_to_one(self):
        """A frame summing to 1.03 must not truncate its tail."""
        body = CategoricalDist(
            dist="categorical",
            choices=[("a", 0.99), ("b", 0.04)],  # sums to 1.03
        )
        rng = random.Random(0)
        counts = Counter(
            draw_value(body, rng, {}, where="test") for _ in range(20_000)
        )
        # 0.04 / 1.03 = 0.0388 — not the 0.01 the legacy walk produced.
        assert counts["b"] / 20_000 == pytest.approx(0.0388, abs=0.01)

    def test_every_declared_category_is_reachable(self, population):
        seen = {p.demographics["education_level"] for p in population}
        assert len(seen) == 3


class TestTruncatedNormal:
    """Audit A8: clipping piles mass onto a boundary; resampling does not."""

    def test_resample_avoids_boundary_pileup(self):
        body = NormalDist(
            dist="normal", mu=22, sigma=2.8, round="int",
            clip=(16, 24), on_out_of_range="resample",
        )
        rng = random.Random(1)
        values = [draw_value(body, rng, {}, where="t") for _ in range(20_000)]
        share_at_bound = values.count(24) / len(values)
        assert share_at_bound < 0.15

    def test_clip_mode_does_pile_up(self):
        """Documents the legacy behaviour the default now avoids."""
        body = NormalDist(
            dist="normal", mu=22, sigma=2.8, round="int",
            clip=(16, 24), on_out_of_range="clip",
        )
        rng = random.Random(1)
        values = [draw_value(body, rng, {}, where="t") for _ in range(20_000)]
        assert values.count(24) / len(values) > 0.15

    def test_all_draws_respect_the_bounds(self):
        body = NormalDist(
            dist="normal", mu=22, sigma=2.8, round="int", clip=(16, 24),
        )
        rng = random.Random(2)
        assert all(
            16 <= draw_value(body, rng, {}, where="t") <= 24 for _ in range(5_000)
        )

    def test_inverted_bounds_are_reported_clearly(self):
        body = NormalDist(dist="normal", mu=20, sigma=2, clip=("high", "low"))
        with pytest.raises(ConfigError, match="exceeds upper bound"):
            draw_value(body, random.Random(0), {"high": 30, "low": 10}, where="t")


class TestDerivedFields:
    def test_derived_values_are_internally_consistent(self, population):
        for persona in population[:2_000]:
            age = persona.demographics["age"]
            assert persona.derived["menopause_age"] < age
            assert persona.derived["menopause_duration_years"] >= 1
            assert persona.derived["spouse_age"] > age
            assert persona.demographics["years_married"] >= 1

    def test_non_persisted_intermediates_stay_out_of_the_record(self, population):
        """`marriage_age` exists only to compute years_married."""
        assert "marriage_age" not in population[0].demographics

    def test_forward_reference_is_rejected_at_construction(self, spec):
        """Declaration order is evaluation order, so this is checkable up front
        rather than surfacing as 'Unknown name' midway through a run."""
        broken = spec.population.model_copy(deep=True)
        broken.demographics[0].body = DerivedDist(
            dist="derived", expr="later_variable + 1"
        )
        with pytest.raises(ConfigError, match="not defined before it"):
            PopulationSampler(broken)


class TestResponseStyle:
    def test_careless_rate_matches_the_spec(self, population, spec):
        rate = sum(p.is_careless for p in population) / N
        declared = spec.population.response_style["careless"].p
        assert rate == pytest.approx(float(declared), abs=0.01)

    def test_style_parameters_are_centred(self, population):
        for key in ("acquiescence", "extreme_response"):
            values = [p.response_style[key] for p in population]
            assert sum(values) / N == pytest.approx(0.0, abs=0.02)


class TestReproducibility:
    def test_same_seed_gives_identical_output(self, spec):
        sampler = PopulationSampler(spec.population)
        first = [p.to_dict() for p in sampler.sample_population(10, seed=7)]
        second = [p.to_dict() for p in sampler.sample_population(10, seed=7)]
        assert first == second

    def test_different_seed_gives_different_output(self, spec):
        sampler = PopulationSampler(spec.population)
        first = [p.to_dict() for p in sampler.sample_population(10, seed=7)]
        third = [p.to_dict() for p in sampler.sample_population(10, seed=8)]
        assert first != third

    def test_ids_are_sequential(self, spec):
        people = PopulationSampler(spec.population).sample_population(5, seed=1)
        assert [p.id for p in people] == [f"persona_{i}" for i in range(1, 6)]
