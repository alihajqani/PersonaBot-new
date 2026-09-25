"""The layered prompt loader and the base packs.

In 2.x every survey carried four full templates, and eleven of fourteen user
templates were byte-identical. The base pack holds the shared wording; a
survey supplies variables, a block, or a whole file — in that order of effort.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from personabot.errors import ConfigError
from personabot.infrastructure.prompts.renderer import PromptRenderer, build_renderer

from conftest import PROMPTS, make_workspace

ANSWER_VARS = dict(
    construct={"key": "c", "label": "رضایت شغلی"},
    persona={"demographics": {"سن": 40}, "derived": {}, "details": {}},
    trait_bands={"رضایت": "بالا"},
    response_style_text="سبک عادی",
    reverse_tags={"direct": "[مستقیم]", "reverse": "[معکوس]"},
    questions=[{"id": "q1", "text": "از کارم راضی‌ام", "options": ["بله", "خیر"], "direction": None}],
    scenario=None,
)


def _survey(prompt: dict | None = None) -> dict:
    return {"id": "s", "locale": "fa", "title": "", "prompt": {
        "base": None, "role": "", "study_context": "", "persona_sections": {},
        "persona_rules": [], "answer_rules": [], "scenario_preamble": "", "vars": {},
        **(prompt or {}),
    }}


def _renderer(tmp_path: Path, files: dict[str, str] | None = None, **prompt) -> PromptRenderer:
    override = tmp_path / "prompts"
    override.mkdir(exist_ok=True)
    for name, text in (files or {}).items():
        (override / name).write_text(text, encoding="utf-8")
    base = PROMPTS / "base" / "fa"
    return PromptRenderer([override, base], base_dir=base, globals={"survey": _survey(prompt)})


class TestLayering:
    def test_with_no_overrides_everything_comes_from_the_base_pack(self, tmp_path):
        renderer = _renderer(tmp_path)
        assert renderer.origin("answers.system.j2") == PROMPTS / "base" / "fa"

    def test_a_whole_file_in_the_survey_wins(self, tmp_path):
        renderer = _renderer(tmp_path, {"answers.user.j2": "custom {{ construct.label }}"})
        assert renderer.render("answers.user.j2", **ANSWER_VARS) == "custom رضایت شغلی"

    def test_one_block_can_be_replaced_without_copying_the_file(self, tmp_path):
        renderer = _renderer(tmp_path, {
            "answers.system.j2":
                '{% extends "base/answers.system.j2" %}{% block role %}نقش ویژه{% endblock %}',
        })
        text = renderer.render("answers.system.j2", **ANSWER_VARS)
        assert text.startswith("نقش ویژه")
        assert "چگونه پاسخ بدهی" in text          # the rest of the base survives

    def test_a_missing_template_names_where_it_looked(self, tmp_path):
        with pytest.raises(ConfigError, match="Searched"):
            _renderer(tmp_path).render("nope.j2")

    def test_an_undefined_variable_fails_loudly(self, tmp_path):
        with pytest.raises(ConfigError, match="failed"):
            _renderer(tmp_path).render("answers.user.j2")

    def test_no_template_directory_at_all_is_a_config_error(self, tmp_path):
        with pytest.raises(ConfigError, match="No prompt template directory"):
            PromptRenderer([tmp_path / "missing"])


class TestPersianBasePack:
    def test_the_role_comes_from_the_survey(self, tmp_path):
        text = _renderer(tmp_path, role="یک معلم شاغل").render("answers.system.j2", **ANSWER_VARS)
        assert "در نقش یک معلم شاغل" in text

    def test_survey_rules_continue_the_numbering(self, tmp_path):
        text = _renderer(tmp_path, answer_rules=["قاعده‌ی ویژه"]).render(
            "answers.system.j2", **ANSWER_VARS
        )
        assert "11. قاعده‌ی ویژه" in text

    def test_reverse_tags_reach_the_rules(self, tmp_path):
        text = _renderer(tmp_path).render("answers.system.j2", **ANSWER_VARS)
        assert "[معکوس]" in text and "[مستقیم]" in text

    def test_a_scenario_is_shown_with_its_preamble(self, tmp_path):
        variables = {**ANSWER_VARS, "scenario": {"key": "3", "text": "متن سناریو"}}
        text = _renderer(tmp_path, scenario_preamble="فرض کنید").render(
            "answers.system.j2", **variables
        )
        assert "«فرض کنید»" in text and "«متن سناریو»" in text

    def test_persona_sections_come_from_the_survey(self, tmp_path):
        text = _renderer(
            tmp_path,
            study_context="معلمان شاغل",
            persona_sections={"teaching": "کلاس و درس"},
            persona_rules=["سابقه با سن بخواند"],
        ).render(
            "persona.system.j2",
            dimensions=[{"name": "x", "label": "نوآوری"}],
            bands={"labels": ["کم", "زیاد"]},
        )
        assert "`teaching`: کلاس و درس" in text
        assert "معلمان شاغل" in text
        assert "- سابقه با سن بخواند" in text

    def test_generic_sections_when_the_survey_declares_none(self, tmp_path):
        text = _renderer(tmp_path).render(
            "persona.system.j2",
            dimensions=[{"name": "x", "label": "صفت"}],
            bands={"labels": ["کم", "زیاد"]},
        )
        assert "`daily_life`" in text


class TestBuildRenderer:
    def test_the_survey_is_a_global_in_both_phases(self, tmp_path):
        workspace = make_workspace(tmp_path, "_template")
        renderer = build_renderer(workspace)
        text = renderer.render("answers.system.j2", **ANSWER_VARS)
        assert workspace.spec.prompt.role in text
