"""Phases 2 and 3 end to end against a stub LLM.

No network, no browser, no API key. Proves the wiring the legacy system never
had: chunking actually happens, static bindings are applied, Persian
normalization rescues answers, and the completeness gate measures against the
live schema rather than a hard-coded constant.
"""

from __future__ import annotations

import asyncio
import json
import random
from pathlib import Path

import pytest

from personabot.application import generate_answers, generate_personas
from personabot.infrastructure.llm.client import LLMClient
from personabot.infrastructure.prompts.renderer import build_renderer
from personabot.infrastructure.storage.json_store import read_json
from personabot.settings import Settings

ARABIC_YEH = "ي"


class StubLLM(LLMClient):
    """Answers from the schema itself, recording every call it receives."""

    def __init__(self, *, arabic_spelling: bool = False, fail_constructs: set[str] | None = None):
        self.calls: list[tuple[str, str]] = []
        self.arabic_spelling = arabic_spelling
        self.fail_constructs = fail_constructs or set()
        self._seen_failures: set[str] = set()
        self._rng = random.Random(0)

    async def generate(self, system_prompt: str, user_prompt: str, temperature: float) -> str:
        self.calls.append((system_prompt, user_prompt))
        await asyncio.sleep(0)

        # The persona prompt lists respondents; the answer prompt lists items.
        if "--- پاسخ‌دهنده" in user_prompt:
            return self._personas(user_prompt)
        return self._answers(system_prompt, user_prompt)

    def _personas(self, user_prompt: str) -> str:
        ids = [
            line.removeprefix("--- پاسخ‌دهنده").removesuffix("---").strip()
            for line in user_prompt.splitlines()
            if line.startswith("--- پاسخ‌دهنده")
        ]
        assert ids, "stub could not find any persona ids in the prompt"
        return json.dumps(
            [
                {
                    "id": persona_id,
                    "details": {
                        "background_and_physical_status": "زندگی من ...",
                        "marital_and_sexual_satisfaction": "رابطه ما ...",
                    },
                }
                for persona_id in ids
            ],
            ensure_ascii=False,
        )

    def _answers(self, system_prompt: str, user_prompt: str) -> str:
        for construct in self.fail_constructs:
            if construct in system_prompt and construct not in self._seen_failures:
                self._seen_failures.add(construct)
                return "this is not json"

        answers: dict[str, str] = {"_reasoning": "چون خسته‌ام"}
        blocks = user_prompt.split("--- گویه ")[1:]
        for block in blocks:
            question_id = None
            options: list[str] = []
            for line in block.splitlines():
                line = line.strip()
                if line.startswith("شناسه:"):
                    question_id = line.split(":", 1)[1].strip()
                elif line.startswith('• "'):
                    options.append(line[3:].rstrip('"'))
            if question_id and options:
                choice = self._rng.choice(options)
                if self.arabic_spelling:
                    choice = choice.replace("ی", ARABIC_YEH)
                answers[question_id] = choice
        return json.dumps(answers, ensure_ascii=False)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    settings = Settings(
        _env_file=None,
        output_dir=tmp_path / "output",
        survey_config=Path("configs/surveys/sexual_intimacy_fa.yaml"),
        concurrent_llm_tasks=4,
        persona_batch_size=3,
        ai_call_delay_seconds=0,
        gemini_api_keys=(),
    )
    settings.ensure_directories()
    return settings


@pytest.fixture
def renderer():
    return build_renderer("sexual_intimacy_fa")


class TestPersonaGeneration:
    async def test_writes_one_file_per_persona(self, settings, spec, renderer):
        client = StubLLM()
        result = await generate_personas.run(
            settings=settings, spec=spec, client=client,
            renderer=renderer, count=6, seed=1,
        )
        assert result.succeeded == 6
        assert result.ok
        assert len(list(settings.personas_dir.glob("*.json"))) == 6

    async def test_batches_run_concurrently(self, settings, spec, renderer):
        """6 personas at batch size 3 must be 2 calls, not 6."""
        client = StubLLM()
        await generate_personas.run(
            settings=settings, spec=spec, client=client,
            renderer=renderer, count=6, seed=1,
        )
        assert len(client.calls) == 2

    async def test_model_cannot_alter_the_sampled_ground_truth(self, settings, spec, renderer):
        """The merge takes `details` only; demographics stay as sampled."""
        client = StubLLM()
        await generate_personas.run(
            settings=settings, spec=spec, client=client,
            renderer=renderer, count=3, seed=7,
        )
        for path in settings.personas_dir.glob("*.json"):
            record = read_json(path)
            assert 40 <= record["demographics"]["age"] <= 65
            assert record["details"]
            assert record["latent_traits_z"]

    async def test_raw_z_scores_never_reach_the_prompt(self, settings, spec, renderer):
        client = StubLLM()
        await generate_personas.run(
            settings=settings, spec=spec, client=client,
            renderer=renderer, count=3, seed=1,
        )
        assert client.calls, "no prompts were rendered"
        for _system, user in client.calls:
            assert "latent_traits_z" not in user
            # Band words are expected; the numeric z-scores behind them are not.
            assert any(band in user for band in spec.population.bands.labels)
            personas = json.loads(client._personas(user))
            assert personas


class TestAnswerGeneration:
    async def _make_personas(self, settings, spec, renderer, count=4):
        await generate_personas.run(
            settings=settings, spec=spec, client=StubLLM(),
            renderer=renderer, count=count, seed=11,
        )

    async def test_produces_complete_answer_sets(self, settings, spec, instrument, renderer):
        await self._make_personas(settings, spec, renderer)
        client = StubLLM()

        result = await generate_answers.run(
            settings=settings, instrument=instrument, client=client, renderer=renderer
        )

        assert result.ok, result.failures
        assert result.succeeded == 4, "no personas were processed"
        written = list(settings.answers_dir.glob("*.json"))
        assert len(written) == result.succeeded
        for path in written:
            answers = read_json(path)
            assert set(answers) == set(instrument.answerable_ids)

    async def test_one_request_per_construct_not_one_per_persona(
        self, settings, spec, instrument, renderer
    ):
        """Chunking is the guard against alpha inflation. If this collapses to
        one call per persona, the methodology is gone."""
        await self._make_personas(settings, spec, renderer, count=2)
        client = StubLLM()
        await generate_answers.run(
            settings=settings, instrument=instrument, client=client, renderer=renderer
        )
        produced = list(settings.answers_dir.glob("*.json"))
        assert produced, "no answer sets were produced"
        constructs = len(instrument.grouped_by_construct())
        assert constructs == 5
        # Careless personas short-circuit without any call, so the floor is one
        # full sweep of constructs for at least one LLM-answered persona.
        assert len(client.calls) >= constructs

    async def test_static_answers_come_from_the_persona(
        self, settings, spec, instrument, renderer
    ):
        await self._make_personas(settings, spec, renderer, count=2)
        await generate_answers.run(
            settings=settings, instrument=instrument, client=StubLLM(), renderer=renderer
        )

        done = {p.name for p in (settings.personas_dir / "done").glob("*.json")}
        assert done, "no personas reached done/"
        for name in done:
            persona = read_json(settings.personas_dir / "done" / name)
            answers = read_json(settings.answers_dir / name)
            assert answers["39020952"] == str(persona["demographics"]["age"])
            assert answers["39020853"] == "خواندم"

    async def test_arabic_spelled_answers_are_still_accepted(
        self, settings, spec, instrument, renderer
    ):
        """Audit A2 end to end: a model using Arabic yeh must not lose answers."""
        await self._make_personas(settings, spec, renderer, count=2)
        client = StubLLM(arabic_spelling=True)

        result = await generate_answers.run(
            settings=settings, instrument=instrument, client=client, renderer=renderer
        )

        assert result.ok, result.failures
        assert result.succeeded == 2, "no personas were processed"
        for path in settings.answers_dir.glob("*.json"):
            answers = read_json(path)
            assert set(answers) == set(instrument.answerable_ids)
            # Stored values are the schema's canonical strings, not the model's.
            for question_id, value in answers.items():
                question = instrument.schema[question_id]
                if question.is_choice:
                    assert value in question.option_values

    async def test_failed_construct_is_retried(self, settings, spec, instrument, renderer):
        await self._make_personas(settings, spec, renderer, count=1)
        client = StubLLM(fail_constructs={"اضطراب جنسی"})

        result = await generate_answers.run(
            settings=settings, instrument=instrument, client=client, renderer=renderer
        )

        assert result.ok, result.failures
        assert result.succeeded == 1, "the persona was not processed"
        for path in settings.answers_dir.glob("*.json"):
            assert set(read_json(path)) == set(instrument.answerable_ids)

    async def test_personas_move_to_done_only_on_success(
        self, settings, spec, instrument, renderer
    ):
        await self._make_personas(settings, spec, renderer, count=3)
        await generate_answers.run(
            settings=settings, instrument=instrument, client=StubLLM(), renderer=renderer
        )
        assert not list(settings.personas_dir.glob("*.json"))
        assert len(list((settings.personas_dir / "done").glob("*.json"))) == 3
        assert not list((settings.personas_dir / "inprogress").glob("*.json"))


class TestStatusCommand:
    def test_classifies_complete_partial_and_corrupt(self, settings, instrument):
        from personabot.application.status import build_report
        from personabot.infrastructure.storage.json_store import write_json

        ids = list(instrument.answerable_ids)
        write_json(settings.answers_dir / "good.json", {q: "x" for q in ids})
        write_json(settings.answers_dir / "partial.json", {q: "x" for q in ids[:30]})
        (settings.answers_dir / "broken.json").write_text("{", encoding="utf-8")

        report = build_report(settings, instrument)
        answers = next(g for g in report.groups if g.label == "answers ready")

        assert answers.total == 3
        assert answers.valid == 1
        assert [name for name, _ in answers.incomplete] == ["partial.json"]
        assert answers.corrupt == ["broken.json"]

    def test_renders_without_a_schema(self, settings):
        from personabot.application.status import build_report

        text = build_report(settings, None).render()
        assert "MISSING" in text
