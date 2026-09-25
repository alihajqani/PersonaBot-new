"""The instrument interpreter, bound to the real 51-question form.

These are the regression tests for the discovery that motivated the rewrite:
`core/instrument.py` targeted a different questionnaire, so `classify()`
returned `other` for all 51 questions, `static_answers()` returned `{}`, and
`is_reverse()` never fired — silently, while the pipeline reported success.
"""

from __future__ import annotations

import copy
import random

import pytest

from personabot.domain.instrument import Instrument
from personabot.domain.sampling import PopulationSampler
from personabot.errors import SchemaCoverageError


class TestCoverage:
    def test_every_question_is_accounted_for(self, instrument):
        report = instrument.require_full_coverage()
        assert not report.unassigned
        assert report.total == 51
        assert report.static == 12
        assert sum(report.by_construct.values()) == 39

    def test_no_rule_matches_nothing(self, instrument):
        report = instrument.coverage()
        assert report.unused_scales == []
        assert report.unused_constructs == []
        assert report.unused_static == []

    def test_questions_are_not_double_owned(self, instrument):
        """A static binding wins and removes the question from every chunk."""
        static_ids = {
            qid for qid, b in instrument.bindings.items() if b.is_static
        }
        chunk_ids = {
            q.question_id
            for _c, questions in instrument.grouped_by_construct()
            for q in questions
        }
        assert static_ids.isdisjoint(chunk_ids)
        assert len(static_ids) + len(chunk_ids) == 51


class TestCoverageGuardCatchesStaleSignatures:
    """The check that would have caught the original failure on day one."""

    def test_stale_signature_raises(self, spec, schema):
        stale = copy.deepcopy(spec)
        # The signature the legacy code looked for; absent from this form.
        stale.scales[0].match = {"options_include_any": ["همیشه توافق داریم"]}
        stale.constructs[0].match = {"scale": "freq5"}

        with pytest.raises(SchemaCoverageError) as excinfo:
            Instrument(stale, schema).require_full_coverage()

        message = str(excinfo.value)
        assert "UNASSIGNED" in message
        assert "MATCHING NOTHING" in message

    def test_error_names_the_orphaned_questions(self, spec, schema):
        stale = copy.deepcopy(spec)
        stale.constructs[0].match = {"question_id_in": ["does-not-exist"]}
        with pytest.raises(SchemaCoverageError, match="39021295"):
            Instrument(stale, schema).require_full_coverage()


class TestConstructAssignment:
    EXPECTED_SIZES = {
        "sexual_satisfaction": 13,
        "partner_responsiveness": 9,
        "sexual_communication": 13,
        "attachment_avoidance": 2,
        "attachment_anxiety": 2,
    }

    def test_chunks_are_not_one_bucket(self, instrument):
        """The legacy behaviour was a single 'other' bucket of all 51 items."""
        groups = instrument.grouped_by_construct()
        assert len(groups) == 5
        assert {c.key: len(qs) for c, qs in groups} == self.EXPECTED_SIZES

    def test_construct_order_follows_the_spec(self, instrument, spec):
        ordered = [c.key for c, _ in instrument.grouped_by_construct()]
        assert ordered == [c.key for c in spec.constructs if c.key in self.EXPECTED_SIZES]

    def test_every_construct_maps_to_a_declared_trait(self, instrument, spec):
        dimensions = set(spec.population.latent_model.dimensions)
        for construct, _questions in instrument.grouped_by_construct():
            assert set(construct.traits) <= dimensions

    @pytest.mark.parametrize(
        ("question_id", "construct"),
        [
            ("39021312", "attachment_avoidance"),   # coerced into sex
            ("39021330", "attachment_anxiety"),     # fears he will stray
            ("39021449", "partner_responsiveness"), # satisfied with foreplay
            ("39021475", "sexual_satisfaction"),    # satisfied with frequency
            ("39022057", "sexual_communication"),   # discusses frequency
            ("39021466", "sexual_communication"),   # Scale A, communication content
        ],
    )
    def test_representative_items_land_where_intended(self, instrument, question_id, construct):
        assert instrument.bindings[question_id].construct.key == construct


class TestReverseKeying:
    def test_reverse_items_are_tagged(self, instrument):
        reverse = {
            qid for qid in instrument.psychometric_ids if instrument.is_reverse(qid)
        }
        assert reverse == {
            "39021302", "39021359", "39021403", "39021498", "39021513",  # satisfaction
            "39021369", "39021471", "39021482",                          # responsiveness
        }

    def test_legacy_fired_zero_times_here(self, instrument):
        """Contrast: the old keyword lists matched nothing in this form."""
        assert any(instrument.is_reverse(q) for q in instrument.psychometric_ids)


class TestStaticAnswers:
    @pytest.fixture
    def persona(self, spec):
        return PopulationSampler(spec.population).sample_population(1, seed=1)[0]

    def test_produces_an_answer_for_every_bound_question(self, instrument, persona):
        answers = instrument.static_answers(persona)
        assert len(answers) == 12  # legacy produced 0

    def test_values_come_from_the_persona_not_the_model(self, instrument, persona):
        answers = instrument.static_answers(persona)
        assert answers["39020952"] == str(persona.demographics["age"])
        assert answers["39020953"] == str(persona.derived["spouse_age"])
        assert answers["39021262"] == str(persona.demographics["years_married"])

    def test_choice_bindings_yield_real_option_strings(self, instrument, persona):
        """A static answer for a RADIO must be submittable as-is."""
        answers = instrument.static_answers(persona)
        for question_id, value in answers.items():
            question = instrument.schema[question_id]
            if question.is_choice:
                assert value in question.option_values

    def test_instruction_blocks_get_their_literal(self, instrument, persona):
        answers = instrument.static_answers(persona)
        assert answers["39020853"] == "خواندم"
        assert answers["44413635"] == "خواندم"

    def test_derived_arithmetic_is_coherent(self, instrument, spec):
        """Menopause age and duration used to be the model's arithmetic."""
        for persona in PopulationSampler(spec.population).sample_population(200, seed=3):
            answers = instrument.static_answers(persona)
            age = int(answers["39020952"])
            assert int(answers["39021251"]) < age            # menopause age
            assert int(answers["39021257"]) >= 1             # duration
            assert int(answers["39020953"]) > age            # spouse older


class TestCarelessResponders:
    def test_covers_every_psychometric_item(self, instrument):
        answers = instrument.careless_answers(random.Random(42))
        assert set(answers) == set(instrument.psychometric_ids)

    def test_with_static_answers_reaches_full_completeness(self, instrument, spec):
        """Audit A10: the legacy version skipped free text and landed at 82.4%."""
        persona = PopulationSampler(spec.population).sample_population(1, seed=5)[0]
        combined = instrument.static_answers(persona) | instrument.careless_answers(
            random.Random(42)
        )
        assert set(combined) == set(instrument.answerable_ids)

    def test_straightlines_within_a_construct(self, instrument, spec):
        """Most answers in a construct sit on one anchor, with light drift."""
        answers = instrument.careless_answers(random.Random(7))
        stickiness = spec.answering.careless.anchor_stickiness
        for construct, questions in instrument.grouped_by_construct():
            if len(questions) < 5:
                continue
            chosen = [answers[q.question_id] for q in questions]
            modal = max(set(chosen), key=chosen.count)
            assert chosen.count(modal) / len(chosen) >= stickiness - 0.25

    def test_only_emits_real_options(self, instrument):
        answers = instrument.careless_answers(random.Random(11))
        for question_id, value in answers.items():
            assert value in instrument.schema[question_id].option_values


class TestOrdinalCoding:
    def test_low_first_scale_scores_ascending(self, instrument):
        question = instrument.schema["39021295"]  # هرگز … همیشه
        codes = [instrument.numeric_code(question, v) for v in question.option_values]
        assert codes == [1, 2, 3, 4, 5]

    def test_unknown_value_returns_none(self, instrument):
        question = instrument.schema["39021295"]
        assert instrument.numeric_code(question, "not an option") is None

    def test_high_first_scale_reverses(self, spec, schema):
        flipped = copy.deepcopy(spec)
        for scale in flipped.scales:
            if scale.name == "freq5":
                scale.coding = "high_first"
        instrument = Instrument(flipped, schema)
        question = instrument.schema["39021295"]
        codes = [instrument.numeric_code(question, v) for v in question.option_values]
        assert codes == [5, 4, 3, 2, 1]

    def test_every_psychometric_answer_is_codable(self, instrument):
        """Guards the analysis harness against silent listwise deletion."""
        for question_id in instrument.psychometric_ids:
            question = instrument.schema[question_id]
            for value in question.option_values:
                assert instrument.numeric_code(question, value) is not None


class TestResponseStyle:
    def test_high_acquiescence_produces_a_nudge(self, instrument):
        text = instrument.response_style_text(
            {"acquiescence": 0.5, "extreme_response": 0.0}
        )
        assert text and text != instrument.spec.response_style_nudges.default

    def test_neutral_style_falls_back_to_default(self, instrument):
        text = instrument.response_style_text(
            {"acquiescence": 0.0, "extreme_response": 0.0}
        )
        assert text == instrument.spec.response_style_nudges.default

    def test_only_band_words_reach_the_prompt(self, instrument, spec):
        """Raw z-scores are persisted but deliberately withheld from the model."""
        persona = PopulationSampler(spec.population).sample_population(1, seed=2)[0]
        construct = spec.constructs[0]
        bands = instrument.trait_bands_for(persona, construct)
        assert bands
        for value in bands.values():
            assert value in spec.population.bands.labels
