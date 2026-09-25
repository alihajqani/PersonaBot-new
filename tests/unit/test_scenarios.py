"""Between-subjects scenarios, from assignment in phase 2 to routing in phase 4.

The live form (survey.porsline.ir/s/Yiz8N63e) draws one of 12 vignettes per
session and prints its number first: «4.در سازمان شما ساختار پرداخت…». Answers
are generated long before that draw, so an answer set is only worth anything if
it is submitted in a session that drew the scenario it was written for.
"""

from __future__ import annotations

import json
import random
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from personabot.application import generate_answers, generate_personas
from personabot.domain.instrument import Instrument
from personabot.domain.models import FormSchema, Persona
from personabot.domain.scenarios import (
    SCENARIO_FIELD,
    assign_scenarios,
    identify_scenario,
    split_answer_file,
)
from personabot.errors import SurveySpecError
from personabot.infrastructure.prompts.renderer import build_renderer
from personabot.infrastructure.storage.json_store import read_json, write_json
from types import SimpleNamespace

from personabot.providers.base import FormSubmitter
from personabot.survey.loader import load_spec
from personabot.survey.spec import ScenarioSpec, SurveySpec
from personabot.workspace import Workspace

from conftest import SURVEYS, make_workspace
from test_pipeline_e2e import StubLLM

VIGNETTE = "pay_autonomy_climate_vignette_fa"
SPEC_PATH = SURVEYS / VIGNETTE / "survey.yaml"
SCHEMA_PATH = SURVEYS / VIGNETTE / "form_schema.json"

SCENARIOS = [
    ScenarioSpec(key="1", text="در سازمان شما ساختار پرداخت به‌گونه‌ای است که نیمی از دریافتی شما ثابت است. شما استقلال کاری بالایی دارید. محیط سازمان مبتنی بر کنترل است."),
    ScenarioSpec(key="4", text="در سازمان شما ساختار پرداخت به‌گونه‌ای است که نیمی از دریافتی شما ثابت است. شما استقلال کاری پایینی دارید. فضای سازمانی حمایتگر است."),
    ScenarioSpec(key="10", text="در این سازمان شما استقلال کاری بالایی در نحوه انجام کار دارید. فضای سازمانی حمایتگر است."),
]


class TestIdentifyScenario:
    def test_number_the_form_prints_is_authoritative(self):
        title = "4.در سازمان شما ساختار پرداخت  به‌گونه‌ای است که نیمی از دریافتی شما ثابت است."
        assert identify_scenario(title, SCENARIOS) == "4"

    def test_two_digit_key_is_not_read_as_its_first_digit(self):
        assert identify_scenario("10.در این سازمان شما استقلال کاری", SCENARIOS) == "10"

    def test_persian_digits_and_spacing(self):
        assert identify_scenario(" ۱۰ . در این سازمان", SCENARIOS) == "10"

    def test_instruction_pages_are_not_scenarios(self):
        for text in (
            "فرض کنید در یک سازمان مشغول به کار هستید و مسئولیت انجام یک پروژه مهم به شما سپرده شده است.",
            "لطفا با توجه به ویژگیهای توضیح داده شده در مورد شغل و ویژگی های سازمان، به سوالات زیر پاسخ دهید.",
        ):
            assert identify_scenario(text, SCENARIOS) is None

    def test_unnumbered_text_falls_back_to_a_clear_fuzzy_match(self):
        assert identify_scenario(SCENARIOS[1].text, SCENARIOS) == "4"

    def test_number_outside_the_design_is_not_guessed(self):
        assert identify_scenario("7.متنی که با هیچ سناریویی نمی‌خواند", SCENARIOS) is None


class TestAssignment:
    def test_cells_differ_by_at_most_one(self):
        personas = [Persona(id=f"p{i}") for i in range(29)]
        twelve = [ScenarioSpec(key=str(k), text="x") for k in range(1, 13)]
        assign_scenarios(personas, twelve, random.Random(3))

        counts = Counter(p.scenario for p in personas)
        assert set(counts) == {str(k) for k in range(1, 13)}
        assert max(counts.values()) - min(counts.values()) <= 1

    def test_persona_record_round_trips_its_scenario(self):
        persona = Persona(id="p", scenario="7")
        assert Persona.from_dict(persona.to_dict()).scenario == "7"

    def test_surveys_without_scenarios_keep_their_record_shape(self):
        assert "scenario" not in Persona(id="p").to_dict()


class TestAnswerFile:
    def test_tag_is_separated_from_form_answers(self):
        scenario, answers = split_answer_file({SCENARIO_FIELD: "4", "45898989": "موافقم", "age": 30})
        assert scenario == "4"
        assert answers == {"45898989": "موافقم", "age": "30"}


class TestSpec:
    def test_duplicate_keys_are_rejected(self):
        raw = load_spec(SPEC_PATH).model_dump()
        raw["scenarios"].append(dict(raw["scenarios"][0]))
        with pytest.raises(ValueError, match="duplicate scenario"):
            SurveySpec.model_validate(raw)

    def test_live_spec_declares_the_twelve_cell_design(self):
        spec = load_spec(SPEC_PATH)
        assert [s.key for s in spec.scenarios] == [str(k) for k in range(1, 13)]
        cells = {tuple(sorted(s.factors.items())) for s in spec.scenarios}
        assert len(cells) == 12

    def test_persona_without_scenario_is_refused(self):
        spec = load_spec(SPEC_PATH)
        schema = FormSchema.from_list(json.loads(SCHEMA_PATH.read_text("utf-8")))
        with pytest.raises(SurveySpecError, match="regenerated"):
            Instrument(spec, schema).scenario_for(Persona(id="old"))


# ----------------------------------------------------------- phases 2 and 3


@pytest.fixture
def vignette_settings(tmp_path: Path) -> Workspace:
    workspace = make_workspace(
        tmp_path, VIGNETTE,
        concurrent_llm_tasks=4,
        persona_batch_size=6,
        ai_call_delay_seconds=0,
        gemini_api_keys=(),
    )
    workspace.ensure_directories()
    return workspace


class TestPipeline:
    async def test_each_answer_set_is_written_for_its_personas_scenario(self, vignette_settings):
        spec = load_spec(SPEC_PATH)
        schema = FormSchema.from_list(json.loads(SCHEMA_PATH.read_text("utf-8")))
        instrument = Instrument(spec, schema)
        renderer = build_renderer(vignette_settings)
        client = StubLLM()

        await generate_personas.run(
            workspace=vignette_settings, client=client,
            renderer=renderer, count=12, seed=5,
        )
        personas = {
            path.name: read_json(path) for path in vignette_settings.personas_dir.glob("*.json")
        }
        assert sorted(p["scenario"] for p in personas.values()) == sorted(
            str(k) for k in range(1, 13)
        )

        client.calls.clear()
        result = await generate_answers.run(
            workspace=vignette_settings, instrument=instrument,
            client=client, renderer=renderer,
        )
        assert result.succeeded == 12

        for name, persona in personas.items():
            answers = read_json(vignette_settings.answers_dir / name)
            assert answers[SCENARIO_FIELD] == persona["scenario"]

        # Careless respondents straightline in code and never reach the model.
        attentive = {p["scenario"] for p in personas.values() if not p["response_style"]["careless"]}
        texts = {s.key: s.text for s in spec.scenarios}
        prompted = {
            key for key, text in texts.items()
            if any(text in system for system, _ in client.calls)
        }
        assert attentive and prompted == attentive
        # The design cell never reaches the model.
        assert not any("emphasized" in system or "climate" in system for system, _ in client.calls)


# ------------------------------------------------------------------ phase 4


class FakeTor:
    enabled = False

    def burn(self, proxy) -> None:
        pass


class FakeBrowser:
    def __init__(self) -> None:
        self.opened = 0
        self.tor = FakeTor()

    @asynccontextmanager
    async def circuit(self, *, label: str = "ctx"):
        self.opened += 1
        yield SimpleNamespace(context=FakeContext(), proxy=None)


class FakeContext:
    async def new_page(self):
        return FakePage()


class FakePage:
    def set_default_timeout(self, _ms):
        pass

    async def goto(self, *_args, **_kwargs):
        pass


class ScriptedSubmitter(FormSubmitter):
    """Draws scenarios from a fixed script and records what it submitted."""

    name = "scripted"

    def __init__(self, settings, draws, **kwargs) -> None:
        super().__init__(settings, FakeBrowser(), None, SCENARIOS, **kwargs)
        self.draws = list(draws)
        self.submitted: list[tuple[str, dict[str, str]]] = []
        self._current: str | None = None

    async def detect_scenario(self, page):
        self._current = self.draws.pop(0) if self.draws else "1"
        return self._current

    async def _fill_and_submit(self, page, answers, label):
        self.submitted.append((self._current, answers))

    async def fill_page(self, page, answers):
        return 0

    async def advance(self, page):
        return False

    async def detect_success(self, page):
        return True


@pytest.fixture
def submit_settings(tmp_path: Path) -> Workspace:
    workspace = make_workspace(
        tmp_path, VIGNETTE,
        concurrent_submissions=1,
        submission_delay_seconds=0,
        gemini_api_keys=(),
    )
    workspace.ensure_directories()
    return workspace


def _answer_file(settings: Workspace, name: str, scenario: str) -> None:
    write_json(settings.answers_dir / f"{name}.json",
               {SCENARIO_FIELD: scenario, "45898989": f"answer-for-{scenario}"})


class TestScenarioRouting:
    async def test_answer_sets_only_go_to_sessions_that_drew_their_scenario(self, submit_settings):
        _answer_file(submit_settings, "a", "4")
        _answer_file(submit_settings, "b", "10")
        submitter = ScriptedSubmitter(submit_settings, draws=["1", "10", "10", "4"])

        result = await submitter.run()

        assert result.succeeded == 2 and result.ok
        assert [draw for draw, _ in submitter.submitted] == ["10", "4"]
        for draw, answers in submitter.submitted:
            assert answers == {"45898989": f"answer-for-{draw}"}  # tag stripped
        assert submitter.browser.opened == 4  # two sessions closed unsent
        assert sorted(p.name for p in (submit_settings.answers_dir / "done").glob("*.json")) == [
            "a.json", "b.json"
        ]

    async def test_stops_when_the_form_never_draws_the_remaining_scenario(self, submit_settings):
        _answer_file(submit_settings, "a", "4")
        submitter = ScriptedSubmitter(submit_settings, draws=[])  # always draws "1"

        result = await submitter.run()

        assert result.succeeded == 0
        assert [f.item for f in result.failures] == ["a.json"]
        assert submitter.browser.opened == FormSubmitter.SCENARIO_MISSES_PER_SCENARIO * len(SCENARIOS)
        assert (submit_settings.answers_dir / "a.json").exists()  # still pending

    async def test_files_without_a_known_scenario_are_not_submitted(self, submit_settings):
        write_json(submit_settings.answers_dir / "old.json", {"45898989": "موافقم"})
        _answer_file(submit_settings, "a", "4")
        submitter = ScriptedSubmitter(submit_settings, draws=["4"])

        result = await submitter.run()

        assert result.succeeded == 1
        assert [f.item for f in result.failures] == ["old.json"]
