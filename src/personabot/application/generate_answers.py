"""Phase 3 — answer generation.

Structure, and why each part exists:

* **Static bindings first.** Demographics and derived facts are written straight
  from the persona, guaranteeing identity consistency and keeping arithmetic out
  of the model's hands.
* **Careless short-circuit.** Inattentive respondents straightline in code —
  models cannot convincingly fake inattention. They still receive every static
  answer, so unlike the predecessor (A10) they no longer fail the gate.
* **One request per construct, items shuffled.** The deliberate guard against
  the self-consistency that inflates Cronbach's alpha. Consolidating these would
  defeat the methodological point of the project.
* **Completeness gate with targeted retry.** Only the constructs that actually
  failed are re-requested, rather than regenerating the whole persona.

Personas are processed concurrently under a semaphore, and constructs within one
persona run sequentially — they share a persona's narrative, and interleaving
them would multiply peak key demand for no benefit.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any, Sequence

from personabot.domain.answer_validation import AnswerValidator, check_completeness
from personabot.domain.instrument import Instrument
from personabot.domain.models import Persona, PhaseResult, Question
from personabot.errors import PersonaBotError
from personabot.infrastructure.llm.client import LLMClient
from personabot.infrastructure.llm.factory import parse_json_response
from personabot.infrastructure.prompts.renderer import PromptRenderer
from personabot.infrastructure.storage.json_store import read_json_or_none, write_json
from personabot.infrastructure.storage.workqueue import WorkQueue
from personabot.logging_setup import get_logger
from personabot.settings import Settings
from personabot.survey.spec import ConstructSpec

log = get_logger(__name__)


def _questions_for_prompt(
    questions: Sequence[Question], instrument: Instrument, construct: ConstructSpec
) -> list[dict[str, Any]]:
    tags = instrument.spec.answering.reverse_tags
    has_mixed_direction = construct.reverse_when is not None

    return [
        {
            "id": question.question_id,
            "text": question.question_text.replace("\n*", "").strip(),
            "options": list(question.option_values),
            "direction": (
                (tags.reverse if instrument.is_reverse(question.question_id) else tags.direct)
                if has_mixed_direction and question.is_choice
                else None
            ),
        }
        for question in questions
    ]


async def _answer_construct(
    construct: ConstructSpec,
    questions: Sequence[Question],
    persona: Persona,
    *,
    instrument: Instrument,
    client: LLMClient,
    renderer: PromptRenderer,
    validator: AnswerValidator,
) -> dict[str, str]:
    """Ask for one construct's answers and validate them."""
    items = list(questions)
    if instrument.spec.answering.shuffle_within_chunk:
        random.shuffle(items)

    variables = {
        "construct": {"key": construct.key, "label": construct.label},
        "persona": {
            "demographics": persona.demographics,
            "derived": persona.derived,
            "details": persona.details,
        },
        "trait_bands": instrument.trait_bands_for(persona, construct),
        "response_style_text": instrument.response_style_text(persona.response_style),
        "reverse_tags": {
            "direct": instrument.spec.answering.reverse_tags.direct,
            "reverse": instrument.spec.answering.reverse_tags.reverse,
        },
        "questions": _questions_for_prompt(items, instrument, construct),
    }

    system_prompt, user_prompt = renderer.render_pair("answers", **variables)
    temperature_range = instrument.spec.answering.temperature
    temperature = random.uniform(temperature_range.min, temperature_range.max)

    raw = await client.generate(system_prompt, user_prompt, temperature)
    payload = parse_json_response(raw)
    if not isinstance(payload, dict):
        raise PersonaBotError(f"Expected a JSON object, got {type(payload).__name__}")

    outcome = validator.validate_chunk(payload, items)

    for correction in outcome.corrections:
        log.info(
            "  [%s/%s] corrected %s: %r -> %r (%d%%)",
            persona.id, construct.key, correction.question_id,
            correction.raw, correction.corrected, correction.score,
        )
    for question_id, value in outcome.discarded.items():
        log.warning(
            "  [%s/%s] discarded %s: %r matched no option.",
            persona.id, construct.key, question_id, value,
        )
    if outcome.rogue_ids:
        log.warning(
            "  [%s/%s] ignored %d id(s) not asked for.",
            persona.id, construct.key, len(outcome.rogue_ids),
        )

    return outcome.answers


async def generate_for_persona(
    persona: Persona,
    *,
    instrument: Instrument,
    client: LLMClient,
    renderer: PromptRenderer,
    validator: AnswerValidator,
    settings: Settings,
) -> dict[str, str]:
    """Produce a complete answer set for one persona."""
    answers: dict[str, str] = dict(instrument.static_answers(persona))

    careless_config = instrument.spec.answering.careless
    if careless_config.enabled and persona.is_careless:
        log.info("[%s] careless responder — straightlining in code.", persona.id)
        answers.update(instrument.careless_answers(random.Random(hash(persona.id) & 0xFFFFFFFF)))
        return answers

    completeness = instrument.spec.answering.completeness
    pending = instrument.grouped_by_construct()

    for attempt in range(completeness.max_retries + 1):
        failed: list[tuple[ConstructSpec, list[Question]]] = []

        for construct, questions in pending:
            try:
                produced = await _answer_construct(
                    construct, questions, persona,
                    instrument=instrument, client=client,
                    renderer=renderer, validator=validator,
                )
            except PersonaBotError as exc:
                log.error("[%s] construct %r failed: %s", persona.id, construct.key, exc)
                failed.append((construct, questions))
                continue

            answers.update(produced)
            missing = [q for q in questions if q.question_id not in answers]
            if missing:
                log.warning(
                    "[%s] construct %r: %d/%d answered.",
                    persona.id, construct.key, len(questions) - len(missing), len(questions),
                )
                failed.append((construct, missing))
            else:
                log.info(
                    "[%s] construct %r: %d/%d answered.",
                    persona.id, construct.key, len(questions), len(questions),
                )

            if settings.ai_call_delay_seconds:
                await asyncio.sleep(settings.ai_call_delay_seconds)

        # Retry whenever anything is still missing and budget remains — not just
        # when the overall ratio is below the gate. A set sitting at 96% with a
        # construct that failed outright is two cheap requests away from being
        # complete, and those two items are exactly the ones a required-field
        # form will refuse to submit without.
        if not failed:
            break
        if attempt < completeness.max_retries:
            check = check_completeness(
                answers, instrument.answerable_ids, completeness.min_ratio
            )
            log.warning(
                "[%s] %s — retrying %d construct(s) (attempt %d/%d).",
                persona.id, check.describe(), len(failed),
                attempt + 2, completeness.max_retries + 1,
            )
            # Targeted retry: only what failed, not the whole persona.
            pending = failed if completeness.retry_scope == "failed_constructs_only" else \
                instrument.grouped_by_construct()

    return answers


async def run(
    *,
    settings: Settings,
    instrument: Instrument,
    client: LLMClient,
    renderer: PromptRenderer,
    limit: int | None = None,
) -> PhaseResult:
    result = PhaseResult(phase="answer generation")
    validator = AnswerValidator(instrument.spec.answering)
    completeness = instrument.spec.answering.completeness

    queue = WorkQueue(settings.personas_dir)
    queue.ensure()
    names = queue.pending_names()[: limit or None]

    if not names:
        log.warning("No personas awaiting answers in %s.", settings.personas_dir)
        return result

    log.info(
        "Generating answers for %d persona(s) with concurrency %d.",
        len(names), settings.concurrent_llm_tasks,
    )

    semaphore = asyncio.Semaphore(settings.concurrent_llm_tasks)

    async def process(name: str) -> None:
        async with semaphore:
            try:
                claimed = queue.claim(name)
            except PersonaBotError:
                result.skipped += 1
                return

            with claimed:
                payload = read_json_or_none(claimed.path)
                if not isinstance(payload, dict) or "details" not in payload:
                    log.warning("Skipping %s: not a usable persona file.", name)
                    result.record_failure(name, "malformed persona file")
                    return

                persona = Persona.from_dict(payload)
                try:
                    answers = await generate_for_persona(
                        persona, instrument=instrument, client=client,
                        renderer=renderer, validator=validator, settings=settings,
                    )
                except PersonaBotError as exc:
                    log.error("[%s] generation failed: %s", persona.id, exc)
                    result.record_failure(
                        name, str(exc), retryable=getattr(exc, "retryable", False)
                    )
                    return

                check = check_completeness(
                    answers, instrument.answerable_ids, completeness.min_ratio
                )
                if not check.passed:
                    log.error(
                        "[%s] incomplete: %s. Persona kept for a later retry.",
                        persona.id, check.describe(),
                    )
                    result.record_failure(name, f"incomplete: {check.describe()}", retryable=True)
                    return

                write_json(settings.answers_dir / name, answers)
                claimed.complete()
                result.record_success()
                log.info("[%s] %s -> answers/%s", persona.id, check.describe(), name)

    async with asyncio.TaskGroup() as group:
        for name in names:
            group.create_task(process(name))

    log.info("Answer generation finished: %s", result.summary())
    return result
