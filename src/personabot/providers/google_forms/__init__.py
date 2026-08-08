"""Google Forms provider."""

from __future__ import annotations

import re
from typing import Any

from personabot.domain.models import Option, Question, QuestionType
from personabot.logging_setup import get_logger
from personabot.providers.base import FormSubmitter, SchemaExtractor, exact_text
from personabot.providers.registry import register_extractor, register_submitter

log = get_logger(__name__)

_NEXT = "div[jsname='OCpkoe']"
_SUBMIT = "div[jsname='M2UYVd']"
_SUCCESS = "div.vHW8K"
_ENTRY_ID = re.compile(r",\[\[(\d+),")


def _entry_id(data_params: str | None) -> str | None:
    match = _ENTRY_ID.search(data_params or "")
    return f"entry.{match.group(1)}" if match else None


@register_extractor("google_forms")
class GoogleFormsExtractor(SchemaExtractor):
    async def parse_page(self, page: Any) -> list[Question]:
        questions: list[Question] = []
        for block in await page.locator("div[role='listitem']").all():
            container = block.locator("div[data-params]").first
            if not await container.count():
                continue
            question_id = _entry_id(await container.get_attribute("data-params"))
            if not question_id:
                continue

            heading = block.locator("div[role='heading']").first
            text = (await heading.inner_text()).strip() if await heading.count() else ""

            options = [
                Option(text=value, value=value)
                for value in (
                    (await item.get_attribute("data-value")) or ""
                    for item in await block.locator("div[role='radio']").all()
                )
                if value
            ]

            if options:
                question_type = QuestionType.RADIO
            elif await block.locator("textarea").count():
                question_type = QuestionType.TEXT
            elif await block.locator("input[type='text']").count():
                question_type = QuestionType.TEXT
            else:
                question_type = QuestionType.UNKNOWN

            questions.append(
                Question(question_id=question_id, question_text=text,
                         type=question_type, options=tuple(options))
            )
        return questions

    async def fill_placeholders(self, page: Any, questions: list[Question]) -> None:
        from playwright.async_api import Error as PlaywrightError

        for question in questions:
            try:
                if question.options:
                    await page.locator("div[role='radio']").first.click(timeout=5_000)
                elif question.type is QuestionType.TEXT:
                    field = page.locator("input[type='text'], textarea").first
                    if await field.count():
                        await field.fill("25", timeout=5_000)
            except PlaywrightError as exc:
                log.debug("Placeholder skipped for %s: %s", question.question_id, exc)

    async def advance(self, page: Any) -> bool:
        if await page.locator(_SUBMIT).count():
            return False
        nxt = page.locator(_NEXT)
        if not await nxt.count():
            return False
        await nxt.first.click(timeout=15_000)
        await page.wait_for_load_state("networkidle", timeout=30_000)
        return True


@register_submitter("google_forms")
class GoogleFormsSubmitter(FormSubmitter):
    async def fill_page(self, page: Any, answers: dict[str, str]) -> int:
        from playwright.async_api import Error as PlaywrightError

        filled = 0
        for block in await page.locator("div[role='listitem']").all():
            container = block.locator("div[data-params]").first
            if not await container.count():
                continue
            question_id = _entry_id(await container.get_attribute("data-params"))
            value = answers.get(question_id or "")
            if value is None:
                continue

            try:
                radios = block.locator("div[role='radio']")
                if await radios.count():
                    target = radios.filter(has_text=exact_text(value)).first
                    if not await target.count():
                        target = block.locator(f"div[role='radio'][data-value={value!r}]").first
                    if await target.count():
                        await target.click(timeout=10_000)
                        filled += 1
                    continue

                field = block.locator("input[type='text'], textarea").first
                if await field.count():
                    await field.fill(value, timeout=10_000)
                    filled += 1
            except PlaywrightError as exc:
                log.warning("Could not fill %s: %s", question_id, exc)
        return filled

    async def advance(self, page: Any) -> bool:
        submit = page.locator(_SUBMIT)
        if await submit.count():
            await submit.first.click(timeout=15_000)
            await page.wait_for_load_state("networkidle", timeout=60_000)
            return False
        nxt = page.locator(_NEXT)
        if not await nxt.count():
            return False
        await nxt.first.click(timeout=15_000)
        await page.wait_for_load_state("networkidle", timeout=30_000)
        return True

    async def detect_success(self, page: Any) -> bool:
        from playwright.async_api import Error as PlaywrightError

        try:
            await page.wait_for_selector(_SUCCESS, timeout=60_000)
            return True
        except PlaywrightError:
            return False
