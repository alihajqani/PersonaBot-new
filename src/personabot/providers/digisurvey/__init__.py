"""DigiSurvey provider — SPA with late rendering."""

from __future__ import annotations

import asyncio
from typing import Any

from bs4 import BeautifulSoup

from personabot.domain.models import Option, Question, QuestionType
from personabot.logging_setup import get_logger
from personabot.providers.base import FormSubmitter, SchemaExtractor, exact_text
from personabot.providers.registry import register_extractor, register_submitter

log = get_logger(__name__)

_NEXT = "button.btn.next.green"
_SUBMIT_TEXT = "ارسال"
_FINISH_MARKERS = ("finish", "done", "result", "thank")


def _parse(html: str) -> list[Question]:
    soup = BeautifulSoup(html, "html.parser")
    questions: list[Question] = []

    for block in soup.select("div.question[data-id]"):
        question_id = block.get("data-id")
        if not question_id:
            continue
        heading = block.find(["h3", "h4", "label", "p"])
        text = heading.get_text(" ", strip=True) if heading else ""

        radios = block.select("input[type='radio']")
        checkboxes = block.select("input[type='checkbox']")
        inputs = radios or checkboxes

        options = []
        for element in inputs:
            label = element.find_next("label")
            value = element.get("value") or (label.get_text(strip=True) if label else "")
            if value:
                options.append(Option(text=value, value=value))

        if radios:
            question_type = QuestionType.RADIO
        elif checkboxes:
            question_type = QuestionType.CHECKBOX
        elif block.find("textarea") or block.select("input[type='text']"):
            question_type = QuestionType.TEXT
        else:
            # The legacy extractor dropped these silently, so no answer was ever
            # generated for them and nothing warned.
            log.warning("Question %s has an unrecognized type; recording as UNKNOWN.", question_id)
            question_type = QuestionType.UNKNOWN

        questions.append(
            Question(question_id=question_id, question_text=text,
                     type=question_type, options=tuple(options))
        )
    return questions


@register_extractor("digisurvey")
class DigiSurveyExtractor(SchemaExtractor):
    async def parse_page(self, page: Any) -> list[Question]:
        questions = _parse(await page.content())
        if not questions:
            # SPA content can arrive after networkidle.
            await page.wait_for_timeout(8_000)
            questions = _parse(await page.content())
        return questions

    async def fill_placeholders(self, page: Any, questions: list[Question]) -> None:
        from playwright.async_api import Error as PlaywrightError

        for question in questions:
            try:
                scope = page.locator(f"div.question[data-id={question.question_id!r}]")
                if question.options:
                    await scope.locator("input[type='radio'], input[type='checkbox']").first.check(
                        force=True, timeout=5_000
                    )
                elif question.type is QuestionType.TEXT:
                    await scope.locator("input[type='text'], textarea").first.fill(
                        "25", timeout=5_000
                    )
            except PlaywrightError as exc:
                log.debug("Placeholder skipped for %s: %s", question.question_id, exc)

    async def advance(self, page: Any) -> bool:
        button = page.locator(_NEXT)
        if not await button.count():
            return False
        if _SUBMIT_TEXT in ((await button.first.inner_text()) or ""):
            return False
        await button.first.click(timeout=15_000)
        await page.wait_for_timeout(1_500)
        return True


@register_submitter("digisurvey")
class DigiSurveySubmitter(FormSubmitter):
    async def fill_page(self, page: Any, answers: dict[str, str]) -> int:
        from playwright.async_api import Error as PlaywrightError

        filled = 0
        for question in _parse(await page.content()):
            value = answers.get(question.question_id)
            if value is None:
                continue
            scope = page.locator(f"div.question[data-id={question.question_id!r}]")
            try:
                choices = scope.locator("label")
                if question.options and await choices.count():
                    target = choices.filter(has_text=exact_text(value)).first
                    if await target.count():
                        await target.click(force=True, timeout=10_000)
                        filled += 1
                    continue
                field = scope.locator("input[type='text'], textarea").first
                if await field.count():
                    await field.fill(value, timeout=10_000)
                    filled += 1
            except PlaywrightError as exc:
                log.warning("Could not fill %s: %s", question.question_id, exc)
        return filled

    async def advance(self, page: Any) -> bool:
        from playwright.async_api import Error as PlaywrightError

        button = page.locator(_NEXT)
        if not await button.count():
            return False
        is_final = _SUBMIT_TEXT in ((await button.first.inner_text()) or "")
        try:
            await button.first.evaluate("element => element.click()")
        except PlaywrightError:
            await button.first.click(force=True, timeout=10_000)
        await page.wait_for_timeout(2_000)
        return not is_final

    async def detect_success(self, page: Any) -> bool:
        for _ in range(15):
            url = (page.url or "").lower()
            if any(marker in url for marker in _FINISH_MARKERS):
                return True
            if not await page.locator("div.question[data-id]").count():
                return True
            await asyncio.sleep(1)
        return False
