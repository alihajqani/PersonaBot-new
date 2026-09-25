"""Google Forms provider."""

from __future__ import annotations

import re
from typing import Any

from personabot.domain.models import Option, Question, QuestionType
from personabot.domain.text import normalize_for_match
from personabot.errors import ExtractionError, SubmissionError
from personabot.logging_setup import get_logger
from personabot.providers.base import FormSubmitter, SchemaExtractor
from personabot.providers.registry import register_extractor, register_submitter

log = get_logger(__name__)

_ITEM = "div[role='listitem']"
_RADIO = "div[role='radio']"
_NEXT = "div[jsname='OCpkoe']"
_SUBMIT = "div[jsname='M2UYVd']"
_SUCCESS = "div.vHW8K"
_ENTRY_ID = re.compile(r",\[\[(\d+),")

# Google Forms server-renders every choice control with ``aria-disabled="true"``
# and its JS clears the attribute once the page is hydrated. Playwright treats
# an aria-disabled element as *not enabled*, so a click issued before hydration
# waits out its whole timeout, and a ``force=True``/JS click is silently ignored
# by the (not yet installed) jsaction handler — verified: ``aria-checked`` stays
# false. Waiting for the attribute to clear is the only correct fix.
_UNHYDRATED = (
    "div[role='radio'][aria-disabled='true'], div[role='checkbox'][aria-disabled='true']"
)
_HYDRATED_JS = """
([itemSelector, unhydratedSelector]) =>
    document.querySelectorAll(itemSelector).length > 0 &&
    !document.querySelector(unhydratedSelector)
"""
# Generous on purpose: with CONCURRENT_SUBMISSIONS browsers on one machine,
# hydration can land well after ``networkidle``.
_HYDRATION_TIMEOUT_MS = 45_000

# Long enough for a busy page, short enough that 18 questions on one page do not
# add three minutes before the scripted fallback in `_click` gets its turn.
_CLICK_TIMEOUT_MS = 5_000

# Google Forms sets ``aria-checked`` from its own event handler, a frame or two
# after the click returns, so a value read immediately after clicking still says
# false. Verification polls instead of sampling once.
_ANSWER_SETTLE_MS = 5_000

# Clicking Next is a real navigation (to ``/formResponse``), and a rejected Next
# does not navigate at all. Both outcomes are therefore observable, which beats
# the old ``networkidle`` wait twice over: ``networkidle`` needed ~15s where
# hydration needs ~3s, and under CONCURRENT_SUBMISSIONS browsers it frequently
# never settled inside its 60s timeout at all.
_PAGE_TIMEOUT_MS = 60_000

# ``_ENTRY_ID``, in the browser.
_ENTRY_ID_JS = r"""(item) => {
    const params = item.querySelector('div[data-params]');
    if (!params) return null;
    const match = /,\[\[(\d+),/.exec(params.getAttribute('data-params') || '');
    return match ? 'entry.' + match[1] : null;
}"""

# Which questions on this page now hold an answer. One round trip for the whole
# page, and it reads the same DOM signals the fillers write.
_ANSWERED_JS = f"""
(itemSelector) => {{
    const entryId = {_ENTRY_ID_JS};
    const answered = {{}};
    document.querySelectorAll(itemSelector).forEach(item => {{
        const id = entryId(item);
        if (!id) return;
        const field = item.querySelector("input[type='text'], textarea");
        answered[id] = Boolean(
            item.querySelector("div[role='radio'][aria-checked='true'], "
                             + "div[role='checkbox'][aria-checked='true']")
        ) || Boolean(field && field.value.trim());
    }});
    return answered;
}}
"""
_ALL_ANSWERED_JS = f"""
([itemSelector, ids]) => {{
    const answered = ({_ANSWERED_JS})(itemSelector);
    return ids.every(id => answered[id]);
}}
"""

# Whether the first control on the page has any height. See `_has_layout`.
_HAS_LAYOUT_JS = """
(itemSelector) => {
    const control = document.querySelector(
        itemSelector + " div[role='radio'], " +
        itemSelector + " div[role='checkbox'], " +
        itemSelector + " input[type='text'], " +
        itemSelector + " textarea");
    return !control || control.getBoundingClientRect().height > 0;
}
"""

# Resolves to 'advanced' once a *different*, hydrated page is up, or to
# 'rejected' as soon as the form complains. Polling for the outcome — rather
# than waiting on a load state and then looking — is what makes this immune to
# the race where the click has not yet committed its navigation and the checks
# all pass against the page we are trying to leave.
_ADVANCE_STATE_JS = f"""
([itemSelector, unhydratedSelector, before]) => {{
    const entryId = {_ENTRY_ID_JS};
    // The alert container ships with every question and is empty until the form
    // has something to say, so only its *text* signals a rejection.
    const alerts = [...document.querySelectorAll(itemSelector + " [role='alert']")];
    if (alerts.some(alert => alert.textContent.trim())) return 'rejected';
    const items = [...document.querySelectorAll(itemSelector)];
    if (!items.length || document.querySelector(unhydratedSelector)) return null;
    const ids = items.map(entryId).filter(Boolean);
    return JSON.stringify(ids) === JSON.stringify(before) ? null : 'advanced';
}}
"""


def _entry_id(data_params: str | None) -> str | None:
    match = _ENTRY_ID.search(data_params or "")
    return f"entry.{match.group(1)}" if match else None


def _radio_by_value(value: str) -> str:
    """A ``data-value`` selector that survives Persian text.

    The previous ``f"...[data-value={value!r}]"`` used Python's ``repr``, which
    escapes format characters — so any option containing a ZWNJ (U+200C), which
    is most compound Persian words, became the literal ``\\u200c`` in the
    selector and never matched. CSS takes the character verbatim inside a quoted
    string; only backslash and the quote itself need escaping.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'{_RADIO}[data-value="{escaped}"]'


async def _find_radio(block: Any, value: str) -> Any | None:
    """The radio in ``block`` standing for ``value``, or None."""
    exact = block.locator(_radio_by_value(value)).first
    if await exact.count():
        return exact

    # Second chance for answers whose letters differ only by code point, e.g.
    # Arabic vs Persian yeh — the same fold Phase 3 validation applies.
    wanted = normalize_for_match(value)
    for radio in await block.locator(_RADIO).all():
        if normalize_for_match(await radio.get_attribute("data-value") or "") == wanted:
            return radio
    return None


async def _answered(page: Any, expected: list[str]) -> dict[str, bool]:
    """Wait for ``expected`` to register, then report what actually did."""
    from playwright.async_api import TimeoutError as PlaywrightTimeout

    if expected:
        try:
            await page.wait_for_function(
                _ALL_ANSWERED_JS, arg=[_ITEM, expected], timeout=_ANSWER_SETTLE_MS
            )
        except PlaywrightTimeout:
            pass  # The map below reports precisely which ones are missing.
    return await page.evaluate(_ANSWERED_JS, _ITEM)


async def _entry_ids(page: Any) -> list[str]:
    """Entry ids of the questions on the page, in DOM order — the page's identity."""
    ids: list[str] = []
    for container in await page.locator(f"{_ITEM} div[data-params]").all():
        question_id = _entry_id(await container.get_attribute("data-params"))
        if question_id:
            ids.append(question_id)
    return ids


async def _wait_hydrated(page: Any) -> None:
    """Block until the page's choice controls accept clicks.

    Raises ``playwright.async_api.TimeoutError`` if hydration never completes.
    """
    await page.wait_for_function(
        _HYDRATED_JS, arg=[_ITEM, _UNHYDRATED], timeout=_HYDRATION_TIMEOUT_MS
    )


async def _has_layout(page: Any) -> bool:
    """Whether the page's controls have been laid out with any height.

    When one of Google's stylesheets fails to arrive — routine on a slow link —
    the DOM and its jsaction handlers are fine but every control collapses to
    zero height, so Playwright calls it invisible and refuses to click. Asking
    once per page turns 18 questions × a 5s timeout into a single probe.
    """
    return bool(await page.evaluate(_HAS_LAYOUT_JS, _ITEM))


async def _click(locator: Any, *, scripted: bool = False) -> None:
    """Click, falling back to a scripted click when the element has no layout.

    A scripted click still reaches the hydrated jsaction handler and does check
    the option (verified against a page loaded with its stylesheet blocked).
    Either path is checked afterwards by :func:`_answered`, so a click that fails
    to register is still reported rather than assumed.

    ``no_wait_after`` keeps a click that starts a navigation from waiting on it:
    that wait used to exceed the click's own timeout on a slow link and lose a
    submission whose page was already on its way. Outcomes are confirmed by
    :func:`_advance_state` and ``detect_success`` instead — which is where
    Playwright is heading anyway, the flag being documented as the future default.
    """
    from playwright.async_api import TimeoutError as PlaywrightTimeout

    if scripted:
        await locator.evaluate("element => element.click()")
        return
    try:
        await locator.click(timeout=_CLICK_TIMEOUT_MS, no_wait_after=True)
    except PlaywrightTimeout:
        await locator.evaluate("element => element.click()")


async def _fill(locator: Any, value: str, *, scripted: bool = False) -> None:
    """Type ``value``, falling back to a scripted assignment. See :func:`_click`."""
    from playwright.async_api import TimeoutError as PlaywrightTimeout

    if not scripted:
        try:
            await locator.fill(value, timeout=_CLICK_TIMEOUT_MS)
            return
        except PlaywrightTimeout:
            pass
    await locator.evaluate(
        """(element, value) => {
            element.value = value;
            element.dispatchEvent(new Event('input', {bubbles: true}));
            element.dispatchEvent(new Event('change', {bubbles: true}));
        }""",
        value,
    )


async def _advance_state(page: Any, before: list[str]) -> str:
    """``'advanced'`` or ``'rejected'`` after a Next click.

    Raises ``playwright.async_api.TimeoutError`` when the form does neither.
    """
    handle = await page.wait_for_function(
        _ADVANCE_STATE_JS, arg=[_ITEM, _UNHYDRATED, before], timeout=_PAGE_TIMEOUT_MS
    )
    return str(await handle.json_value())


async def _validation_detail(page: Any) -> str:
    """Whatever the form is complaining about, ready to append to an error message."""
    from playwright.async_api import Error as PlaywrightError

    try:
        texts = await page.locator(f"{_ITEM} [role='alert']").all_inner_texts()
    except PlaywrightError:
        return ""
    messages = list(dict.fromkeys(t.strip() for t in texts if t.strip()))
    return f" Form says: {'; '.join(messages)}" if messages else ""


@register_extractor("google_forms")
class GoogleFormsExtractor(SchemaExtractor):
    # Hydration, not the load state, is this provider's readiness signal.
    nav_wait_until = "domcontentloaded"

    async def prepare(self, page: Any) -> None:
        await _wait_hydrated(page)

    async def parse_page(self, page: Any) -> list[Question]:
        questions: list[Question] = []
        for block in await page.locator(_ITEM).all():
            container = block.locator("div[data-params]").first
            if not await container.count():
                continue
            question_id = _entry_id(await container.get_attribute("data-params"))
            if not question_id:
                continue

            heading = block.locator("div[role='heading']").first
            text = (await heading.inner_text()).strip() if await heading.count() else ""

            # Built with a loop, not a comprehension: an `await` inside a nested
            # generator expression makes it an *async* generator, which the outer
            # comprehension cannot iterate.
            options: list[Option] = []
            for item in await block.locator(_RADIO).all():
                value = await item.get_attribute("data-value")
                if value:
                    options.append(Option(text=value, value=value))

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
        """Answer every question on the page so Google Forms will let us past it.

        Each field must be located *inside its own* ``listitem``. Scoping to the
        page instead answered question #1 once per question and left the rest
        blank, so a multi-question page failed required-field validation and
        `advance` clicked Next forever against a page that never changed.
        """
        from playwright.async_api import Error as PlaywrightError

        for block in await page.locator(_ITEM).all():
            container = block.locator("div[data-params]").first
            if not await container.count():
                continue
            question_id = _entry_id(await container.get_attribute("data-params"))
            if not question_id:
                continue

            try:
                radios = block.locator(_RADIO)
                if await radios.count():
                    await _click(radios.first)
                    continue

                checkboxes = block.locator("div[role='checkbox']")
                if await checkboxes.count():
                    await _click(checkboxes.first)
                    continue

                field = block.locator("input[type='text'], textarea").first
                if await field.count():
                    await _fill(field, "25")
            except PlaywrightError as exc:
                log.debug("Placeholder skipped for %s: %s", question_id, exc)

    async def advance(self, page: Any) -> bool:
        from playwright.async_api import TimeoutError as PlaywrightTimeout

        if await page.locator(_SUBMIT).count():
            return False
        nxt = page.locator(_NEXT)
        if not await nxt.count():
            return False

        before = await _entry_ids(page)
        await _click(nxt.first)

        # A rejected page looks exactly like a successful one apart from the
        # questions still being the same. Without this check the extractor burned
        # through MAX_PAGES silently.
        try:
            state = await _advance_state(page, before)
        except PlaywrightTimeout as exc:
            raise ExtractionError(
                f"Clicked Next but the form neither advanced nor reported an error "
                f"within {_PAGE_TIMEOUT_MS // 1000}s."
            ) from exc
        if state == "rejected":
            raise ExtractionError(
                "The form rejected the placeholder answers."
                f"{await _validation_detail(page)}"
            )
        return True


@register_submitter("google_forms")
class GoogleFormsSubmitter(FormSubmitter):
    # Hydration, not the load state, is this provider's readiness signal, and
    # `domcontentloaded` under load lands in ~15s. A hang is retried once on a
    # fresh connection by `on_circuit` (see Settings.effective_browser_attempts).
    nav_wait_until = "domcontentloaded"

    async def prepare(self, page: Any) -> None:
        await self._require_hydration(page)

    async def _require_hydration(self, page: Any) -> None:
        """Fail loudly rather than clicking dead controls for the whole budget."""
        from playwright.async_api import TimeoutError as PlaywrightTimeout

        try:
            await _wait_hydrated(page)
        except PlaywrightTimeout as exc:
            raise SubmissionError(
                f"The form's controls were still disabled after "
                f"{_HYDRATION_TIMEOUT_MS // 1000}s — Google Forms never finished "
                f"loading. Lower CONCURRENT_SUBMISSIONS if this is frequent."
            ) from exc

    async def fill_page(self, page: Any, answers: dict[str, str]) -> int:
        from playwright.async_api import Error as PlaywrightError

        await self._require_hydration(page)

        scripted = not await _has_layout(page)
        if scripted:
            log.warning(
                "This page has no layout — a stylesheet did not load. Filling it "
                "by script instead of by click."
            )

        attempted: list[str] = []
        for block in await page.locator(_ITEM).all():
            container = block.locator("div[data-params]").first
            if not await container.count():
                continue
            question_id = _entry_id(await container.get_attribute("data-params"))
            value = answers.get(question_id or "")
            if question_id is None or value is None:
                continue

            try:
                if await block.locator(_RADIO).count():
                    target = await _find_radio(block, value)
                    if target is None:
                        log.warning("No option matching %r for %s.", value, question_id)
                        continue
                    await _click(target, scripted=scripted)
                    attempted.append(question_id)
                    continue

                field = block.locator("input[type='text'], textarea").first
                if await field.count():
                    await _fill(field, value, scripted=scripted)
                    attempted.append(question_id)
            except PlaywrightError as exc:
                log.warning("Could not fill %s: %s", question_id, exc)

        # A click Google ignored leaves the question empty. Counting it as filled
        # would hide the reason the next Next gets rejected.
        answered = await _answered(page, attempted)
        missed = [question_id for question_id in attempted if not answered.get(question_id)]
        if missed:
            log.warning(
                "%d of %d field(s) did not take the answer: %s",
                len(missed), len(attempted), ", ".join(missed[:5]),
            )
        return len(attempted) - len(missed)

    async def advance(self, page: Any) -> bool:
        from playwright.async_api import TimeoutError as PlaywrightTimeout

        submit = page.locator(_SUBMIT)
        if await submit.count():
            # No load-state wait here: ``detect_success`` waits for the
            # confirmation element and auto-waits across the navigation. The old
            # ``networkidle`` wait could time out *after* the response landed,
            # turning a successful submission into a retryable failure.
            await _click(submit.first)
            return False
        nxt = page.locator(_NEXT)
        if not await nxt.count():
            return False

        before = await _entry_ids(page)
        await _click(nxt.first)

        # Same trap the extractor hit: a rejected Next leaves the page identical,
        # so the loop refilled page 1 for the entire page budget (~121 pages)
        # before failing with a message that said nothing about the real cause.
        try:
            state = await _advance_state(page, before)
        except PlaywrightTimeout as exc:
            raise SubmissionError(
                f"Clicked Next but the form neither advanced nor reported an error "
                f"within {_PAGE_TIMEOUT_MS // 1000}s."
            ) from exc
        if state == "rejected":
            answered = await page.evaluate(_ANSWERED_JS, _ITEM)
            empty = [question_id for question_id, ok in answered.items() if not ok]
            detail = f" Still empty: {', '.join(empty[:5])}." if empty else ""
            raise SubmissionError(
                f"The form rejected this page.{detail}{await _validation_detail(page)}"
            )
        return True

    async def detect_success(self, page: Any) -> bool:
        from playwright.async_api import Error as PlaywrightError

        try:
            await page.wait_for_selector(_SUCCESS, timeout=60_000)
            return True
        except PlaywrightError:
            return False
