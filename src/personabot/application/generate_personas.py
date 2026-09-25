"""Phase 2 — persona generation.

Traits and demographics are drawn in pure Python; the model only writes prose.
The merge keeps the **sampled** record authoritative and takes nothing but
``details`` from the model, so a model that ignores its instructions cannot
corrupt the population's statistical structure.

Concurrency: batches run under a semaphore inside a ``TaskGroup``. The
predecessor awaited inside a ``for`` loop, so the whole phase was serial despite
being ``async`` throughout.

Respondent numbering continues across runs. A spec may derive an identifier
from ``index`` (a serial phone number that ties a submitted response to its
offline transcript), so a second batch must not restart at 1. 2.x made the
operator pass ``--start-index`` by hand and remember the right value; here the
next free number is read off the personas already on disk.
"""

from __future__ import annotations

import asyncio
import random
import re
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from personabot.domain.models import Persona, PhaseResult
from personabot.domain.sampling import PopulationSampler, batch_rng
from personabot.domain.scenarios import assign_scenarios
from personabot.errors import LLMError, PersonaBotError
from personabot.infrastructure.llm.client import LLMClient
from personabot.infrastructure.llm.factory import parse_json_response
from personabot.infrastructure.prompts.renderer import PromptRenderer
from personabot.infrastructure.storage.json_store import read_json_or_none, write_json
from personabot.logging_setup import get_logger
from personabot.survey.spec import SurveySpec

if TYPE_CHECKING:
    from personabot.workspace import Workspace

log = get_logger(__name__)

_PERSONA_ID = re.compile(r"^persona_(\d+)$")


def _profile_for_prompt(persona: Persona, spec: SurveySpec) -> dict[str, Any]:
    """What the model is allowed to see.

    Band words only — never the raw z-scores. Handing the model numbers invites
    it to reverse-engineer a target instead of writing in character.
    """
    dimensions = spec.population.latent_model.dimensions
    return {
        "id": persona.id,
        "demographics": persona.demographics,
        "derived": persona.derived,
        "bands": {
            (dimensions[name].label or name): band
            for name, band in persona.traits_band.items()
        },
    }


def next_start_index(personas_dir: Path) -> int:
    """The first respondent number not yet used in this survey's output.

    Looks at pending, claimed and finished personas alike: a number handed to a
    persona that has already been answered is just as taken.
    """
    highest = 0
    for directory in (personas_dir, personas_dir / "inprogress", personas_dir / "done"):
        if not directory.is_dir():
            continue
        for path in directory.glob("*.json"):
            payload = read_json_or_none(path)
            if not isinstance(payload, dict):
                continue
            match = _PERSONA_ID.match(str(payload.get("id", "")))
            if match:
                highest = max(highest, int(match.group(1)))
    return highest + 1


async def _generate_batch(
    batch: Sequence[Persona],
    *,
    spec: SurveySpec,
    client: LLMClient,
    renderer: PromptRenderer,
) -> dict[str, dict[str, str]]:
    """Ask for prose for one batch. Returns ``{persona_id: details}``."""
    dimensions = [
        {"name": name, "label": dimension.label or name}
        for name, dimension in spec.population.latent_model.dimensions.items()
    ]
    variables = {
        # `survey` is a renderer global (id, locale, title, prompt).
        "dimensions": dimensions,
        "bands": {"labels": spec.population.bands.labels},
        "profiles": [_profile_for_prompt(persona, spec) for persona in batch],
    }

    system_prompt, user_prompt = renderer.render_pair("persona", **variables)
    temperature = random.uniform(0.75, 0.95)

    raw = await client.generate(system_prompt, user_prompt, temperature)
    payload = parse_json_response(raw)

    if isinstance(payload, dict):
        payload = payload.get("personas", [])
    if not isinstance(payload, list):
        raise LLMError(f"Expected a JSON array of personas, got {type(payload).__name__}")

    details: dict[str, dict[str, str]] = {}
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        persona_id = str(entry.get("id", ""))
        prose = entry.get("details")
        if persona_id and isinstance(prose, dict):
            details[persona_id] = {str(k): str(v) for k, v in prose.items()}
    return details


async def run(
    *,
    workspace: "Workspace",
    client: LLMClient,
    renderer: PromptRenderer,
    count: int,
    seed: int | None = None,
    start_index: int | None = None,
) -> PhaseResult:
    result = PhaseResult(phase="persona generation")
    spec, settings = workspace.spec, workspace.settings

    if start_index is None:
        start_index = next_start_index(workspace.personas_dir)

    sampler = PopulationSampler(spec.population)
    effective_seed = seed if seed is not None else spec.population.seed
    population = sampler.sample_population(count, seed=effective_seed, start_index=start_index)
    log.info(
        "Sampled %d respondents (index %d..%d) from the configured population frame.",
        count, start_index, start_index + count - 1,
    )

    if spec.scenarios:
        # A separate generator from the sampler's, so adding scenarios to a spec
        # does not reshuffle the traits of an otherwise identical seeded run.
        assign_scenarios(population, spec.scenarios, batch_rng(effective_seed, start_index))
        log.info("Assigned %d scenario(s) across the sample, balanced.", len(spec.scenarios))

    size = settings.persona_batch_size
    batches = [population[i : i + size] for i in range(0, len(population), size)]

    semaphore = asyncio.Semaphore(settings.concurrent_llm_tasks)
    collected: dict[str, dict[str, str]] = {}
    lock = asyncio.Lock()

    async def process(index: int, batch: Sequence[Persona]) -> None:
        async with semaphore:
            label = f"batch {index + 1}/{len(batches)}"
            try:
                details = await _generate_batch(
                    batch, spec=spec, client=client, renderer=renderer
                )
            except (PersonaBotError, ValueError) as exc:
                # One failed batch must not sink the others.
                log.error("%s failed: %s", label, exc)
                result.record_failure(label, str(exc), retryable=getattr(exc, "retryable", False))
                return

            async with lock:
                collected.update(details)

            missing = [p.id for p in batch if p.id not in details]
            if missing:
                log.warning("%s: model returned no prose for %d persona(s).", label, len(missing))
            log.info("%s: %d/%d personas written.", label, len(details), len(batch))

            if settings.ai_call_delay_seconds:
                await asyncio.sleep(settings.ai_call_delay_seconds)

    async with asyncio.TaskGroup() as group:
        for index, batch in enumerate(batches):
            group.create_task(process(index, batch))

    for persona in population:
        prose = collected.get(persona.id)
        if not prose:
            result.skipped += 1
            continue
        # The sampled record stays authoritative; only `details` comes from the model.
        persona.details = prose
        write_json(workspace.personas_dir / f"{uuid.uuid4()}.json", persona.to_dict())
        result.record_success()

    log.info(
        "Persona generation finished: %d saved, %d without prose, %d batch failure(s).",
        result.succeeded, result.skipped, result.failed,
    )
    return result
