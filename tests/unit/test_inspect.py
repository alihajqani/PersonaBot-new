"""The schema inventory that bootstraps writing a config for a new form."""

from __future__ import annotations

import yaml

from personabot.application.inspect import summarize
from personabot.survey.spec import ScaleSpec


class TestSummary:
    def test_groups_questions_by_option_signature(self, schema):
        summary = summarize(schema)
        assert len(summary.groups) == 5
        assert [len(g.questions) for g in summary.groups] == [27, 12, 1, 1, 1]

    def test_largest_group_first(self, schema):
        """Psychometric scales lead; single-item signatures are demographics."""
        sizes = [len(g.questions) for g in summary_groups(schema)]
        assert sizes == sorted(sizes, reverse=True)

    def test_free_text_is_separated(self, schema):
        summary = summarize(schema)
        assert len(summary.free_text) == 9
        assert all(not q.is_choice for q in summary.free_text)

    def test_every_question_appears_exactly_once(self, schema):
        summary = summarize(schema)
        seen = [q.question_id for g in summary.groups for q in g.questions]
        seen += [q.question_id for q in summary.free_text]
        assert len(seen) == len(schema)
        assert len(set(seen)) == len(schema)


class TestDraftScalesBlock:
    def test_emits_parseable_yaml(self, schema):
        block = summarize(schema).render_scales()
        parsed = yaml.safe_load(block)
        assert isinstance(parsed["scales"], list)
        assert len(parsed["scales"]) == 5

    def test_draft_validates_as_real_scale_specs(self, schema):
        """What it prints must actually be pasteable into a config."""
        parsed = yaml.safe_load(summarize(schema).render_scales())
        for entry in parsed["scales"]:
            ScaleSpec.model_validate(entry)

    def test_draft_scales_match_the_questions_they_came_from(self, schema):
        """Round trip: the emitted matcher must select its own group."""
        from personabot.domain.matching import build_matcher

        summary = summarize(schema)
        parsed = yaml.safe_load(summary.render_scales())

        for group, entry in zip(summary.groups, parsed["scales"]):
            matcher = build_matcher(entry["match"])
            hits = [q.question_id for q in schema if matcher.matches(q)]
            assert hits == [q.question_id for q in group.questions]

    def test_quotes_survive_special_characters(self):
        from personabot.domain.models import FormSchema, Option, Question, QuestionType

        awkward = Question(
            question_id="q1",
            question_text="x",
            type=QuestionType.RADIO,
            options=(
                Option(text='has "quotes"', value='has "quotes"'),
                Option(text="back\\slash", value="back\\slash"),
            ),
        )
        parsed = yaml.safe_load(summarize(FormSchema((awkward,))).render_scales())
        assert parsed["scales"][0]["match"]["options_equal"] == [
            'has "quotes"', "back\\slash",
        ]


class TestRendering:
    def test_report_mentions_counts_and_ids(self, schema):
        text = summarize(schema).render()
        assert "51 questions" in text
        assert "39021295" in text
        assert "DRAFT `scales:` BLOCK" in text

    def test_id_checklist_lists_every_choice_question(self, schema):
        summary = summarize(schema)
        checklist = summary.render_id_checklist()
        for group in summary.groups:
            for question in group.questions:
                assert question.question_id in checklist


def summary_groups(schema):
    return summarize(schema).groups


class TestShippedConfigs:
    """Every config in configs/surveys/ must load and build.

    A shipped template that fails validation blocks step 6 of the new-survey
    guide for everyone. `_template.yaml` once carried `question_id_in: []`,
    which the matcher rejects as an empty list.
    """

    def test_all_shipped_configs_load(self):
        from pathlib import Path

        from personabot.survey.loader import load_spec

        configs = sorted(Path("configs/surveys").glob("*.yaml"))
        assert configs, "no shipped configs found"
        for path in configs:
            spec = load_spec(path)
            assert spec.id and spec.prompt_preset

    def test_all_shipped_configs_build_an_instrument(self, schema):
        """Building compiles every matcher, which is where empty lists fail."""
        from pathlib import Path

        from personabot.domain.instrument import Instrument
        from personabot.survey.loader import load_spec

        for path in sorted(Path("configs/surveys").glob("*.yaml")):
            Instrument(load_spec(path), schema)

    def test_every_config_has_its_prompt_preset(self):
        from pathlib import Path

        from personabot.infrastructure.prompts.renderer import build_renderer
        from personabot.survey.loader import load_spec

        for path in sorted(Path("configs/surveys").glob("*.yaml")):
            spec = load_spec(path)
            renderer = build_renderer(spec.prompt_preset)
            for template in (
                "persona.system.j2", "persona.user.j2",
                "answers.system.j2", "answers.user.j2",
            ):
                assert renderer.has(template), f"{spec.prompt_preset} lacks {template}"
