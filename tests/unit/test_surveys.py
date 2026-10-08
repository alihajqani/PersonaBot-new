"""Every survey shipped under surveys/ must be runnable as it stands.

These are the questionnaires the 2.x forks were built for, migrated into one
tree. A survey that stops validating — a matcher gone stale, a prompt that no
longer renders — would otherwise be discovered on the next live run, which is
exactly the failure the forks kept rediscovering one copy at a time.
"""

from __future__ import annotations

import random

import pytest

from personabot.domain.instrument import Instrument
from personabot.domain.sampling import PopulationSampler
from personabot.infrastructure.prompts.renderer import build_renderer
from personabot.providers import registry
from personabot.survey.loader import load_schema
from personabot.workspace import available_surveys

from conftest import SURVEYS, make_settings, make_workspace

TEMPLATES = ("persona.system.j2", "persona.user.j2", "answers.system.j2", "answers.user.j2")


def _surveys() -> list[str]:
    return available_surveys(make_settings(SURVEYS))


MIGRATED_FROM_2X = {
    "emotional_suppression_somatic_fa", "internet_addiction_parenting_fa",
    "marital_adjustment_sexual_karaj_fa", "marital_conflict_couples_fa",
    "marital_relationship_battery_fa", "parental_conflict_attachment_delinquency_fa",
    "pay_autonomy_climate_vignette_fa", "psychopathology_battery_fa", "sexual_intimacy_fa",
    "sleep_fomo_digital_burnout_fa", "student_stress_internet_erq_fa",
    "teacher_innovation_km_ai_fa", "tehran_urban_network_fa", "worry_metacognition_gad_fa",
}


def test_the_fourteen_migrated_surveys_are_still_present():
    """Delivered client surveys join these; none of the originals may vanish."""
    assert MIGRATED_FROM_2X <= set(_surveys())


@pytest.fixture(params=_surveys())
def survey(request, tmp_path):
    return make_workspace(tmp_path, request.param)


class TestEverySurvey:
    def test_the_folder_name_is_the_survey_id(self, survey):
        assert survey.survey_dir.name == survey.spec.id

    def test_it_names_a_registered_provider(self, survey):
        assert survey.provider in registry.available()

    def test_it_accounts_for_every_question_of_its_form(self, survey):
        """The coverage check is the guard against stale signatures."""
        report = Instrument(survey.spec, load_schema(survey.schema_file)).coverage()
        assert report.ok, report.render()

    def test_every_template_resolves(self, survey):
        renderer = build_renderer(survey)
        for name in TEMPLATES:
            assert renderer.has(name) or (survey.base_prompt_dir / name).is_file(), name

    def test_its_prompts_render_for_a_sampled_respondent(self, survey):
        """Renders both phases' prompts with the survey's own population.

        StrictUndefined makes a template that references a field the sampler
        no longer produces fail here instead of on a live run.
        """
        spec = survey.spec
        instrument = Instrument(spec, load_schema(survey.schema_file))
        renderer = build_renderer(survey)

        persona = PopulationSampler(spec.population).sample(1, random.Random(0))
        persona.details = {"daily_life": "…"}
        if spec.scenarios:
            persona.scenario = spec.scenarios[0].key
        dimensions = spec.population.latent_model.dimensions

        system, user = renderer.render_pair(
            "persona",
            dimensions=[{"name": n, "label": d.label or n} for n, d in dimensions.items()],
            bands={"labels": spec.population.bands.labels},
            profiles=[{
                "id": persona.id,
                "demographics": persona.demographics,
                "derived": persona.derived,
                "bands": {(dimensions[n].label or n): b for n, b in persona.traits_band.items()},
            }],
        )
        assert persona.id in user and system

        scenario = instrument.scenario_for(persona)
        for construct, questions in instrument.grouped_by_construct():
            system, user = renderer.render_pair(
                "answers",
                construct={"key": construct.key, "label": construct.label},
                persona={
                    "demographics": persona.demographics,
                    "derived": persona.derived,
                    "details": persona.details,
                },
                trait_bands=instrument.trait_bands_for(persona, construct),
                response_style_text=instrument.response_style_text(persona.response_style),
                reverse_tags={
                    "direct": spec.answering.reverse_tags.direct,
                    "reverse": spec.answering.reverse_tags.reverse,
                },
                questions=[
                    {"id": q.question_id, "text": q.question_text,
                     "options": list(q.option_values), "direction": None}
                    for q in questions
                ],
                scenario={"key": scenario.key, "text": scenario.text} if scenario else None,
            )
            assert questions[0].question_id in user

    def test_static_answers_render_for_a_sampled_respondent(self, survey):
        instrument = Instrument(survey.spec, load_schema(survey.schema_file))
        persona = PopulationSampler(survey.spec.population).sample(1, random.Random(1))
        answers = instrument.static_answers(persona)
        assert all(isinstance(value, str) for value in answers.values())


class TestTemplate:
    """`new-survey` copies surveys/_template — it must stay valid."""

    def test_the_template_loads_and_renders_with_the_base_pack(self, tmp_path):
        workspace = make_workspace(tmp_path, "_template")
        spec = workspace.spec
        renderer = build_renderer(workspace)

        # No overrides: every template comes from the Persian base pack.
        for name in TEMPLATES:
            assert renderer.origin(name) == workspace.base_prompt_dir

        system, _ = renderer.render_pair(
            "persona",
            dimensions=[{"name": "t", "label": "صفت"}],
            bands={"labels": spec.population.bands.labels},
            profiles=[],
        )
        for key in spec.prompt.persona_sections:
            assert f"`{key}`" in system

    def test_the_template_is_not_listed_as_a_survey(self):
        assert "_template" not in _surveys()
