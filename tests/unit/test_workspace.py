"""Workspace: one survey bound to one deployment.

What the 2.x forks got from being separate copies — their own URL, their own
output, their own survey selection — now has to come from resolution rules.
These tests pin those rules down, because getting one wrong is how answers for
one questionnaire end up submitted into another.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from personabot.errors import ConfigError
from personabot.workspace import available_surveys, open_workspace, resolve_spec_path

from conftest import SURVEYS, make_settings, make_workspace

TEHRAN = "tehran_urban_network_fa"
TEACHER = "teacher_innovation_km_ai_fa"


class TestSurveyResolution:
    def test_config_beats_survey_beats_environment(self, tmp_path):
        explicit = SURVEYS / TEACHER / "survey.yaml"
        settings = make_settings(tmp_path, survey=TEHRAN)

        assert resolve_spec_path(settings, config=explicit, survey=TEHRAN) == explicit
        assert resolve_spec_path(settings, survey=TEACHER) == SURVEYS / TEACHER / "survey.yaml"
        assert resolve_spec_path(settings) == SURVEYS / TEHRAN / "survey.yaml"

    def test_survey_config_beats_survey(self, tmp_path):
        explicit = SURVEYS / TEACHER / "survey.yaml"
        settings = make_settings(tmp_path, survey=TEHRAN, survey_config=explicit)
        assert resolve_spec_path(settings) == explicit

    def test_nothing_selected_lists_what_exists(self, tmp_path):
        with pytest.raises(ConfigError, match=TEHRAN):
            resolve_spec_path(make_settings(tmp_path))

    def test_an_unknown_id_says_how_to_create_it(self, tmp_path):
        with pytest.raises(ConfigError, match="new-survey no_such_survey"):
            resolve_spec_path(make_settings(tmp_path), survey="no_such_survey")

    def test_templates_are_not_surveys(self, tmp_path):
        surveys = available_surveys(make_settings(tmp_path))
        assert "_template" not in surveys
        assert TEHRAN in surveys


class TestOutputIsolation:
    def test_each_survey_writes_under_its_own_folder(self, tmp_path):
        first = make_workspace(tmp_path, TEHRAN)
        second = make_workspace(tmp_path, TEACHER)

        assert first.answers_dir == tmp_path / "output" / TEHRAN / "answers"
        assert second.answers_dir == tmp_path / "output" / TEACHER / "answers"
        assert first.answers_dir != second.answers_dir

    def test_the_schema_lives_with_the_survey(self, tmp_path):
        workspace = make_workspace(tmp_path, TEHRAN)
        assert workspace.schema_file == SURVEYS / TEHRAN / "form_schema.json"

    def test_ensure_directories_creates_every_queue(self, tmp_path):
        workspace = make_workspace(tmp_path, TEHRAN)
        workspace.ensure_directories()
        for queue in (workspace.personas_dir, workspace.answers_dir):
            for state in ("", "inprogress", "done"):
                assert (queue / state).is_dir()
        assert workspace.receipts_dir.is_dir()


class TestFormResolution:
    def test_the_surveys_own_url_beats_base_form_url(self, tmp_path):
        """A URL shared by every survey must never redirect one survey's
        answers into another survey's form."""
        workspace = make_workspace(tmp_path, TEHRAN, base_form_url="https://example.com/other")
        assert workspace.form_url == workspace.spec.form.url

    def test_base_form_url_is_a_fallback_for_a_survey_without_one(self, tmp_path):
        workspace = make_workspace(tmp_path, "sexual_intimacy_fa", base_form_url="https://x.test/s/1")
        assert workspace.spec.form.url is None
        assert workspace.form_url == "https://x.test/s/1"

    def test_a_cli_url_beats_everything(self, tmp_path):
        settings = make_settings(tmp_path, base_form_url="https://x.test/s/env")
        workspace = open_workspace(settings, survey=TEHRAN, form_url="https://x.test/s/cli")
        assert workspace.form_url == "https://x.test/s/cli"

    def test_a_disagreeing_base_form_url_is_warned_about(self, tmp_path, caplog, monkeypatch):
        settings = make_settings(tmp_path, base_form_url="https://example.com/other")
        monkeypatch.setattr(logging.getLogger("personabot"), "propagate", True)
        with caplog.at_level(logging.WARNING, logger="personabot"):
            open_workspace(settings, survey=TEHRAN)
        assert any("BASE_FORM_URL" in record.message for record in caplog.records)

    def test_no_url_anywhere_names_the_field_to_set(self, tmp_path):
        workspace = make_workspace(tmp_path, "sexual_intimacy_fa")
        with pytest.raises(ConfigError, match="form.url"):
            workspace.require_form_url()

    def test_provider_comes_from_the_survey_unless_given(self, tmp_path):
        assert make_workspace(tmp_path, TEHRAN).provider == "porsline_v2"
        settings = make_settings(tmp_path)
        assert open_workspace(settings, survey=TEHRAN, provider="porsline").provider == "porsline"

    def test_page_budget_comes_from_the_survey_before_the_deployment(self, tmp_path):
        assert make_workspace(tmp_path, TEHRAN, max_pages=7).max_pages == 400

    def test_page_budget_falls_back_to_max_pages(self, tmp_path, monkeypatch):
        workspace = make_workspace(tmp_path, TEHRAN, max_pages=7)
        monkeypatch.setattr(workspace.spec.form, "max_pages", None)
        assert workspace.max_pages == 7


class TestPromptResolution:
    def test_a_persian_survey_uses_the_persian_base_pack(self, tmp_path):
        workspace = make_workspace(tmp_path, TEHRAN)
        assert workspace.base_prompt_pack == "fa"
        assert workspace.prompt_search_path() == [
            workspace.prompts_dir, workspace.settings.prompts_dir / "base" / "fa",
        ]

    def test_a_locale_without_a_pack_falls_back_to_english(self, tmp_path, monkeypatch):
        workspace = make_workspace(tmp_path, TEHRAN)
        monkeypatch.setattr(workspace.spec, "locale", "de")
        assert workspace.base_prompt_pack == "en"

    def test_a_legacy_preset_joins_the_search_path(self, tmp_path, monkeypatch):
        workspace = make_workspace(tmp_path, TEHRAN)
        monkeypatch.setattr(workspace.spec, "prompt_preset", "old_preset")
        assert workspace.settings.prompts_dir / "old_preset" in workspace.prompt_search_path()
