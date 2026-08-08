"""Avalform provider — classic full-page-load forms."""

from __future__ import annotations

from typing import Any

from bs4 import BeautifulSoup

from personabot.domain.models import Option, Question, QuestionType
from personabot.logging_setup import get_logger
from personabot.providers.base import FormSubmitter, SchemaExtractor
from personabot.providers.registry import register_extractor, register_submitter

log = get_logger(__name__)

_NEXT = "input[name='submit_primary']"
_SUCCESS = "div.form_success"


def _parse(html: str) -> list[Question]:
    soup = BeautifulSoup(html, "html.parser")
    questions: list[Question] = []
    seen: set[str] = set()

    for field in soup.select("input[name], select[name], textarea[name]"):
        name = field.get("name")
        if not name or name in seen or field.get("type") in ("submit", "hidden"):
            continue

        block = field.find_parent("li") or field.find_parent("div") or field.parent
        label = block.find("label") if block else None
        text = label.get_text(" ", strip=True) if label else name

        if field.name == "select":
            options = [
                Option(text=o.get_text(strip=True), value=o.get("value") or o.get_text(strip=True))
                for o in field.select("option")
                if o.get("value")
            ]
            question_type = QuestionType.SELECT
        elif field.get("type") == "radio":
            group = block.select(f"input[name='{name}']") if block else []
            options = []
            for radio in group:
                radio_label = radio.find_next("label")
                value = radio.get("value") or (radio_label.get_text(strip=True) if radio_label else "")
                if value:
                    options.append(Option(text=value, value=value))
            question_type = QuestionType.RADIO
        else:
            options = []
            question_type = QuestionType.TEXT

        seen.add(name)
        questions.append(
            Question(question_id=name, question_text=text,
                     type=question_type, options=tuple(options))
        )
    return questions


@register_extractor("avalform")
class AvalformExtractor(SchemaExtractor):
    async def parse_page(self, page: Any) -> list[Question]:
        return _parse(await page.content())

    async def fill_placeholders(self, page: Any, questions: list[Question]) -> None:
        from playwright.async_api import Error as PlaywrightError

        for question in questions:
            try:
                if question.type is QuestionType.RADIO and question.options:
                    # Dual strategy: labels work for simple radios, .check() for
                    # matrix rows whose labels are visually hidden.
                    radio = page.locator(f"input[name={question.question_id!r}]").first
                    if await radio.count():
                        await radio.check(force=True, timeout=5_000)
                elif question.type is QuestionType.SELECT and question.options:
                    await page.select_option(
                        f"select[name={question.question_id!r}]",
                        question.options[0].value, timeout=5_000,
                    )
                else:
                    field = page.locator(
                        f"input[name={question.question_id!r}], "
                        f"textarea[name={question.question_id!r}]"
                    ).first
                    if await field.count():
                        await field.fill("25", timeout=5_000)
            except PlaywrightError as exc:
                log.debug("Placeholder skipped for %s: %s", question.question_id, exc)

    async def advance(self, page: Any) -> bool:
        button = page.locator(_NEXT)
        if not await button.count():
            return False
        value = (await button.first.get_attribute("value")) or ""
        await button.first.click(timeout=15_000)
        await page.wait_for_load_state("networkidle", timeout=30_000)
        return "ارسال" not in value and "پایان" not in value


@register_submitter("avalform")
class AvalformSubmitter(FormSubmitter):
    async def fill_page(self, page: Any, answers: dict[str, str]) -> int:
        from playwright.async_api import Error as PlaywrightError

        filled = 0
        # This provider is the one that genuinely benefits from the schema:
        # field type drives the interaction, so dispatch on it.
        by_id = self.schema.by_id if self.schema else {}

        for question in _parse(await page.content()):
            value = answers.get(question.question_id)
            if value is None:
                continue
            known = by_id.get(question.question_id, question)

            try:
                if known.type is QuestionType.SELECT:
                    await page.select_option(
                        f"select[name={question.question_id!r}]", label=value, timeout=10_000
                    )
                elif known.type is QuestionType.RADIO:
                    radio = page.locator(
                        f"input[name={question.question_id!r}][value={value!r}]"
                    ).first
                    if await radio.count():
                        await radio.check(force=True, timeout=10_000)
                    else:
                        continue
                else:
                    await page.fill(
                        f"input[name={question.question_id!r}], "
                        f"textarea[name={question.question_id!r}]",
                        value, timeout=10_000,
                    )
                filled += 1
            except PlaywrightError as exc:
                log.warning("Could not fill %s: %s", question.question_id, exc)
        return filled

    async def advance(self, page: Any) -> bool:
        button = page.locator(_NEXT)
        if not await button.count():
            return False
        value = (await button.first.get_attribute("value")) or ""
        is_final = "ارسال" in value or "پایان" in value
        await button.first.click(timeout=15_000)
        await page.wait_for_load_state("networkidle", timeout=60_000)
        return not is_final

    async def detect_success(self, page: Any) -> bool:
        from playwright.async_api import Error as PlaywrightError

        try:
            await page.wait_for_selector(_SUCCESS, timeout=30_000)
            return True
        except PlaywrightError:
            return False
