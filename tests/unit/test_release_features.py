"""Behaviour new in 3.0 that is not covered by a module-level suite:
schema snapshots, batch seeding, respondent numbering, reproducible careless
responders, survey scaffolding and the CLI's survey-less commands.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from personabot.application.generate_answers import careless_rng
from personabot.application.generate_personas import next_start_index
from personabot.application.scaffold import new_survey
from personabot.domain.models import FormSchema
from personabot.domain.sampling import PopulationSampler, batch_rng
from personabot.errors import ConfigError
from personabot.survey.loader import load_spec, save_schema

from conftest import LIVE_SPEC, SURVEYS, make_settings


def _schema(*questions: tuple[str, str]) -> FormSchema:
    return FormSchema.from_list(
        [{"question_id": qid, "question_text": text, "type": "TEXT_INPUT"} for qid, text in questions]
    )


class TestSchemaSnapshot:
    def test_the_first_extraction_is_just_written(self, tmp_path):
        target = tmp_path / "form_schema.json"
        change = save_schema(_schema(("1", "a")), target, previous_path=tmp_path / "prev.json")
        assert change.is_new
        assert json.loads(target.read_text("utf-8"))[0]["question_id"] == "1"
        assert not (tmp_path / "prev.json").exists()

    def test_an_identical_re_extraction_keeps_no_backup(self, tmp_path):
        target, previous = tmp_path / "form_schema.json", tmp_path / "prev.json"
        save_schema(_schema(("1", "a")), target, previous_path=previous)
        change = save_schema(_schema(("1", "a")), target, previous_path=previous)
        assert change.identical
        assert not previous.exists()

    def test_a_republished_form_is_reported_and_the_old_schema_kept(self, tmp_path):
        """The one event that silently invalidates a finished survey config."""
        target, previous = tmp_path / "form_schema.json", tmp_path / "prev.json"
        save_schema(_schema(("1", "a"), ("2", "b")), target, previous_path=previous)
        change = save_schema(_schema(("1", "a*"), ("3", "c")), target, previous_path=previous)

        assert change.added == ("3",)
        assert change.removed == ("2",)
        assert change.changed == ("1",)
        assert "1 added" in change.describe() and "1 removed" in change.describe()
        assert [q["question_id"] for q in json.loads(previous.read_text("utf-8"))] == ["1", "2"]


class TestBatchSeeding:
    def test_the_first_batch_reproduces_2x(self):
        assert batch_rng(7, 1).random() == random.Random(7).random()

    def test_a_later_seeded_batch_is_not_a_copy_of_the_first(self):
        """2.x seeded every batch with the seed alone: batch two replayed
        batch one's draws under new respondent numbers."""
        sampler = PopulationSampler(load_spec(LIVE_SPEC).population)
        first = sampler.sample_population(3, seed=7, start_index=1)
        second = sampler.sample_population(3, seed=7, start_index=4)
        assert [p.traits_z for p in first] != [p.traits_z for p in second]
        assert [p.id for p in second] == ["persona_4", "persona_5", "persona_6"]

    def test_a_seeded_batch_is_reproducible(self):
        sampler = PopulationSampler(load_spec(LIVE_SPEC).population)
        a = sampler.sample_population(3, seed=7, start_index=4)
        b = sampler.sample_population(3, seed=7, start_index=4)
        assert [p.traits_z for p in a] == [p.traits_z for p in b]


class TestRespondentNumbering:
    def test_an_empty_output_starts_at_one(self, tmp_path):
        assert next_start_index(tmp_path / "personas") == 1

    def test_numbering_continues_after_every_stage(self, tmp_path):
        personas = tmp_path / "personas"
        for folder, index in (("", 2), ("inprogress", 5), ("done", 3)):
            (personas / folder).mkdir(parents=True, exist_ok=True)
            (personas / folder / f"{index}.json").write_text(json.dumps({"id": f"persona_{index}"}))
        (personas / "junk.json").write_text("{not json")
        assert next_start_index(personas) == 6


class TestCarelessResponders:
    def test_the_straight_line_is_the_same_in_every_process(self):
        """`hash(str)` is salted per interpreter; crc32 is not."""
        assert careless_rng("persona_3").random() == careless_rng("persona_3").random()
        assert careless_rng("persona_3").random() != careless_rng("persona_4").random()


class TestNewSurvey:
    def _settings(self, tmp_path: Path):
        surveys = tmp_path / "surveys"
        (surveys / "_template").mkdir(parents=True)
        (surveys / "_template" / "survey.yaml").write_text(
            (SURVEYS / "_template" / "survey.yaml").read_text("utf-8"), encoding="utf-8"
        )
        (surveys / "donor").mkdir()
        (surveys / "donor" / "survey.yaml").write_text(
            "id: donor\nform:\n  provider: porsline\n  url: https://survey.porsline.ir/s/OLD\n",
            encoding="utf-8",
        )
        (surveys / "donor" / "form_schema.json").write_text("[]", encoding="utf-8")
        return make_settings(tmp_path, surveys_dir=surveys)

    def test_the_template_becomes_a_valid_survey_with_the_new_id(self, tmp_path):
        settings = self._settings(tmp_path)
        target = new_survey(settings, "job_stress_fa")
        spec = load_spec(target / "survey.yaml")
        assert spec.id == "job_stress_fa"

    def test_copying_a_survey_drops_its_schema_and_url(self, tmp_path):
        """The schema describes another form, and the URL would submit into it."""
        settings = self._settings(tmp_path)
        target = new_survey(settings, "clone", source="donor")
        text = (target / "survey.yaml").read_text("utf-8")
        assert "id: clone" in text
        assert "OLD" not in text
        assert not (target / "form_schema.json").exists()

    def test_an_existing_folder_is_never_overwritten(self, tmp_path):
        settings = self._settings(tmp_path)
        with pytest.raises(ConfigError, match="already exists"):
            new_survey(settings, "donor")

    def test_a_bad_id_is_refused(self, tmp_path):
        with pytest.raises(ConfigError, match="not a usable survey id"):
            new_survey(self._settings(tmp_path), "../escape")


class TestCli:
    def test_surveyless_commands_run_without_a_survey(self, monkeypatch, tmp_path, capsys):
        from personabot import cli

        monkeypatch.chdir(SURVEYS.parent)
        monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "out"))
        assert cli.main(["providers"]) == 0
        assert "porsline_v2" in capsys.readouterr().out
        assert cli.main(["surveys"]) == 0
        assert "tehran_urban_network_fa" in capsys.readouterr().out

    def test_validate_config_reports_full_coverage(self, monkeypatch, tmp_path, capsys):
        from personabot import cli

        monkeypatch.chdir(SURVEYS.parent)
        monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "out"))
        assert cli.main(["--survey", "teacher_innovation_km_ai_fa", "validate-config"]) == 0
        out = capsys.readouterr().out
        assert "Every question is accounted for" in out
        assert "porsa" in out

    def test_a_missing_survey_is_a_configuration_error(self, monkeypatch, tmp_path):
        from personabot import cli

        monkeypatch.chdir(SURVEYS.parent)
        monkeypatch.delenv("SURVEY", raising=False)
        monkeypatch.delenv("SURVEY_CONFIG", raising=False)
        monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "out"))
        assert cli.main(["--survey", "does_not_exist", "status"]) == 2
