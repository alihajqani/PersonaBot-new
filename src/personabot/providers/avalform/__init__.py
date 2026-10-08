"""Avalform provider — form.avalform.com (a MachForm derivative).

Server-rendered, one HTML page per form page, paginated by POSTing the page
back (``submit_primary``). Established against a live five-page form
(2026-10-08); the 2.x version of this module had never met one and got three
things wrong, each fixed here:

* **It submitted the form while reading it.** ``advance`` clicked the button
  first and only then asked whether it was the last page, so extracting a
  schema posted a placeholder response into the client's data. The page
  position is now read *before* clicking — from the hidden ``page_number`` and
  the title's «صفحه N از M» — and the last page is never submitted during
  extraction. Intermediate pages are only held server-side until the final
  page is posted; no response is recorded for them.
* **Options were their numeric codes.** ``<option value="1">مادر</option>``
  was recorded as ``"1"``, so the model saw «1، 2، 3» instead of the choices,
  and the submitter then looked for an option *labelled* «1». Options are now
  the visible labels; the submitter maps a label back to its value.
* **Matrices and checkbox groups were misread.** A matrix is one ``<li>`` with
  a table whose every row is its own radio group (``element_N``) sharing the
  header's columns; a checkbox group is one question spread over inputs named
  ``element_N_1 … element_N_k``. Each matrix row is now a question, and a
  checkbox group is one ``CHECKBOX`` question whose answer lists the chosen
  options separated by ``" | "``.

Required fields are validated on the server: a page that comes back unchanged
carries MachForm's ``li.error`` messages, which are reported.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from bs4 import BeautifulSoup, Tag

from personabot.domain.models import Option, Question, QuestionType
from personabot.domain.text import normalize_for_match
from personabot.errors import ExtractionError, SubmissionError
from personabot.logging_setup import get_logger
from personabot.providers.base import FormSubmitter, SchemaExtractor
from personabot.providers.registry import register_extractor, register_submitter

log = get_logger(__name__)

_NEXT = "#submit_primary"
_SUCCESS = "div.form_success, #form_success, .success_message"
_FINAL_LABELS = ("ارسال", "ثبت", "پایان", "submit")
_PAGE_OF = re.compile(r"صفحه\s*([0-9۰-۹]+)\s*از\s*([0-9۰-۹]+)")
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
_FUZZY_MIN_SCORE = 90

#: Separator between the options of one checkbox answer.
MULTI_SEPARATOR = " | "


# ================================================================ parsing

@dataclass(slots=True)
class Field:
    """One answerable thing on a page, with what the browser needs to fill it."""

    question: Question
    kind: str                                   # select | radio | checkbox | text
    name: str = ""                              # input/select name (radio, select, text)
    values: list[str] = field(default_factory=list)   # option index -> value attribute
    inputs: list[str] = field(default_factory=list)   # checkbox: option index -> input name


@dataclass(slots=True)
class PageInfo:
    number: int | None
    total: int | None
    button: str
    errors: list[str]

    @property
    def is_last(self) -> bool:
        if self.number is not None and self.total is not None:
            return self.number >= self.total
        return any(word in self.button for word in _FINAL_LABELS)


def page_info(html: str) -> PageInfo:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    match = _PAGE_OF.search(title)
    number = total = None
    if match:
        number, total = (int(g.translate(_DIGITS)) for g in match.groups())
    hidden = soup.select_one("input[name=page_number]")
    if hidden and (hidden.get("value") or "").isdigit():
        number = int(hidden["value"])
    button = soup.select_one(_NEXT)
    errors = [
        " ".join(e.get_text(" ", strip=True).split())
        for e in soup.select("li.error p.error, #error_message, li.error .error_message")
    ]
    return PageInfo(number, total, (button.get("value") or "") if button else "", errors)


def _clean(element: Tag | None) -> str:
    if element is None:
        return ""
    text = " ".join(element.get_text(" ", strip=True).split())
    return re.sub(r"\s*\*\s*$", "", text).strip()   # the required-field star


def parse_fields(html: str) -> list[Field]:
    """Every answerable field on one page, in page order."""
    soup = BeautifulSoup(html, "html.parser")
    fields: list[Field] = []
    for li in soup.select("form li[id^=li_]"):
        if li.get("id") == "li_buttons":
            continue
        classes = li.get("class") or []
        label = _clean(li.find(class_="description"))

        if "matrix" in classes:
            fields.extend(_matrix_rows(li, label))
            continue

        checkboxes = li.select("input[type=checkbox][name]")
        radios = li.select("input[type=radio][name]")
        select = li.select_one("select[name]")
        text = li.select_one("textarea[name], input[name]:not([type=hidden]):not([type=radio])"
                             ":not([type=checkbox]):not([type=submit])")

        if checkboxes:
            base = "element_" + li["id"].removeprefix("li_")
            labels = [_choice_label(li, box) for box in checkboxes]
            fields.append(Field(
                Question(base, label, QuestionType.CHECKBOX, _options(labels)),
                "checkbox", inputs=[box["name"] for box in checkboxes],
            ))
        elif radios:
            labels = [_choice_label(li, radio) for radio in radios]
            fields.append(Field(
                Question(radios[0]["name"], label, QuestionType.RADIO, _options(labels)),
                "radio", name=radios[0]["name"], values=[r.get("value", "") for r in radios],
            ))
        elif select is not None:
            choices = [o for o in select.find_all("option") if o.get("value")]
            fields.append(Field(
                Question(select["name"], label, QuestionType.SELECT,
                         _options([_clean(o) for o in choices])),
                "select", name=select["name"], values=[o["value"] for o in choices],
            ))
        elif text is not None:
            fields.append(Field(Question(text["name"], label, QuestionType.TEXT), "text",
                                name=text["name"]))
        else:
            log.warning("%s has no input this provider understands; skipping it.", li.get("id"))
    return fields


def _matrix_rows(li: Tag, heading: str) -> list[Field]:
    columns = [_clean(th) for th in li.select("thead th[id]")]
    rows: list[Field] = []
    for tr in li.select("tbody tr"):
        radios = tr.select("input[type=radio][name]")
        if not radios:
            log.warning("A row of matrix %s has no radio buttons; skipping it.", li.get("id"))
            continue
        labels = columns or [_choice_label(tr, radio) for radio in radios]
        if len(labels) != len(radios):
            labels = [_choice_label(tr, radio) for radio in radios]
        text = _clean(tr.find(["td", "th"]))
        rows.append(Field(
            Question(radios[0]["name"], text or heading, QuestionType.RADIO, _options(labels)),
            "radio", name=radios[0]["name"], values=[r.get("value", "") for r in radios],
        ))
    return rows


def _choice_label(scope: Tag, element: Tag) -> str:
    own = scope.find("label", attrs={"for": element.get("id")}) if element.get("id") else None
    if own is None:
        own = element.find_next("label")
    return _clean(own) or element.get("value", "")


def _options(labels: list[str]) -> tuple[Option, ...]:
    return tuple(Option(text=label, value=label) for label in labels)


def _index_of(answer: str, labels: list[str]) -> int | None:
    """Which option an answer names: normalized equality, then a close match."""
    target = normalize_for_match(answer)
    normalized = [normalize_for_match(label) for label in labels]
    for index, label in enumerate(normalized):
        if label and label == target:
            return index
    from thefuzz import fuzz

    best, best_score = None, _FUZZY_MIN_SCORE - 1
    for index, label in enumerate(normalized):
        score = int(fuzz.ratio(target, label)) if label else 0
        if score > best_score:
            best, best_score = index, score
    return best


def _css(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


# ============================================================ extraction

@register_extractor("avalform")
class AvalformExtractor(SchemaExtractor):
    nav_wait_until = "load"

    async def parse_page(self, page: Any) -> list[Question]:
        return [f.question for f in parse_fields(await page.content())]

    async def fill_placeholders(self, page: Any, questions: list[Question]) -> None:
        """Satisfy the server's required-field check so the next page loads.

        Never reaches the database: MachForm keeps intermediate pages aside
        until the last page is posted, and :meth:`advance` never posts it.
        """
        from playwright.async_api import Error as PlaywrightError

        info = page_info(await page.content())
        if info.is_last:
            return
        for item in parse_fields(await page.content()):
            try:
                await _fill(page, item, _placeholder(item))
            except PlaywrightError as exc:
                log.debug("Placeholder skipped for %s: %s", item.question.question_id, exc)

    async def advance(self, page: Any) -> bool:
        before = page_info(await page.content())
        if before.is_last:
            log.info("Page %s of %s is the last; not submitting it.", before.number, before.total)
            return False
        await page.locator(_NEXT).first.click(timeout=15_000)
        await page.wait_for_load_state("load", timeout=60_000)
        after = page_info(await page.content())
        if after.number == before.number:
            raise ExtractionError(
                f"Avalform stayed on page {before.number}: "
                f"{'; '.join(after.errors) or 'no error message shown'}."
            )
        return True


def _placeholder(item: Field) -> str:
    if item.question.options:
        return item.question.options[0].value
    return "25"


async def _fill(page: Any, item: Field, answer: str) -> bool:
    """Put ``answer`` into one field. False when it matches no option."""
    labels = [o.value for o in item.question.options]

    if item.kind == "text":
        await page.locator(f'[name="{_css(item.name)}"]').first.fill(answer, timeout=10_000)
        return True

    if item.kind == "select":
        index = _index_of(answer, labels)
        if index is None:
            return False
        await page.locator(f'select[name="{_css(item.name)}"]').select_option(
            value=item.values[index], timeout=10_000
        )
        return True

    if item.kind == "radio":
        index = _index_of(answer, labels)
        if index is None:
            return False
        await page.locator(
            f'input[name="{_css(item.name)}"][value="{_css(item.values[index])}"]'
        ).first.check(force=True, timeout=10_000)
        return True

    # checkbox: every listed option
    chosen = [part for part in (p.strip() for p in answer.split(MULTI_SEPARATOR.strip())) if part]
    indexes = [_index_of(part, labels) for part in chosen]
    if not chosen or None in indexes:
        return False
    for index in indexes:
        await page.locator(f'input[name="{_css(item.inputs[index])}"]').first.check(
            force=True, timeout=10_000
        )
    return True


# ============================================================ submission

@register_submitter("avalform")
class AvalformSubmitter(FormSubmitter):
    nav_wait_until = "load"

    async def fill_page(self, page: Any, answers: dict[str, str]) -> int:
        from playwright.async_api import Error as PlaywrightError

        filled = 0
        for item in parse_fields(await page.content()):
            question_id = item.question.question_id
            answer = answers.get(question_id)
            if answer is None:
                continue
            try:
                if await _fill(page, item, answer):
                    filled += 1
                else:
                    log.warning("Answer %r matches none of %s's options %s.",
                                answer, question_id, [o.value for o in item.question.options])
            except PlaywrightError as exc:
                log.warning("Could not fill %s: %s", question_id, exc)
        return filled

    async def advance(self, page: Any) -> bool:
        before = page_info(await page.content())
        await page.locator(_NEXT).first.click(timeout=15_000)
        await page.wait_for_load_state("load", timeout=60_000)
        if before.is_last:
            return False
        after = page_info(await page.content())
        if after.number == before.number and after.total is not None:
            raise SubmissionError(
                f"Avalform refused page {before.number}: "
                f"{'; '.join(after.errors) or 'no error message shown'}."
            )
        return True

    async def detect_success(self, page: Any) -> bool:
        """The confirmation block, or the form gone with no error left."""
        from playwright.async_api import Error as PlaywrightError

        try:
            confirmed = bool(await page.locator(_SUCCESS).count())
            html = await page.content()
        except PlaywrightError:
            return False
        if not confirmed:
            confirmed = "submit_primary" not in html and not page_info(html).errors
        if confirmed:
            # What the form said, so a run's log shows each confirmation.
            text = " ".join(BeautifulSoup(html, "html.parser").get_text(" ", strip=True).split())
            log.info("Avalform confirmation: %s", text[:160])
        return confirmed
