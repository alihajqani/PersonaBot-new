"""Phase 2 — persona generation.

Traits and demographics are drawn in pure Python; the model only writes prose.
The merge keeps the **sampled** record authoritative and takes nothing but
``details`` from the model, so a model that ignores its instructions cannot
corrupt the population's statistical structure.

Concurrency: batches run under a semaphore inside a ``TaskGroup``. The
predecessor awaited inside a ``for`` loop, so the whole phase was serial despite
being ``async`` throughout.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from typing import Any, Sequence

from personabot.domain.models import Persona, PhaseResult
from personabot.domain.sampling import PopulationSampler
from personabot.errors import LLMError, PersonaBotError
from personabot.infrastructure.llm.client import LLMClient
from personabot.infrastructure.llm.factory import parse_json_response
from personabot.infrastructure.prompts.renderer import PromptRenderer
from personabot.infrastructure.storage.json_store import write_json
from personabot.logging_setup import get_logger
from personabot.settings import Settings
from personabot.survey.spec import SurveySpec

log = get_logger(__name__)


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


async def _generate_batch(
    batch: Sequence[Persona],
    *,
    spec: SurveySpec,
    client: LLMClient,
    renderer: PromptRenderer,
    settings: Settings,
) -> dict[str, dict[str, str]]:
    """Ask for prose for one batch. Returns ``{persona_id: details}``."""
    dimensions = [
        {"name": name, "label": dimension.label or name}
        for name, dimension in spec.population.latent_model.dimensions.items()
    ]
    variables = {
        "survey": {"id": spec.id, "locale": spec.locale},
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
    settings: Settings,
    spec: SurveySpec,
    client: LLMClient,
    renderer: PromptRenderer,
    count: int,
    seed: int | None = None,
) -> PhaseResult:
    result = PhaseResult(phase="persona generation")

    sampler = PopulationSampler(spec.population)
    population = sampler.sample_population(count, seed=seed)
    log.info("Sampled %d respondents from the configured population frame.", count)

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
                    batch, spec=spec, client=client, renderer=renderer, settings=settings
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
        write_json(settings.personas_dir / f"{uuid.uuid4()}.json", persona.to_dict())
        result.record_success()

    log.info(
        "Persona generation finished: %d saved, %d without prose, %d batch failure(s).",
        result.succeeded, result.skipped, result.failed,
    )
    return result
