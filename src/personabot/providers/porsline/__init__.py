"""Porsline provider — the live platform for this study.

Written against the DOM of ``survey.porsline.ir`` in its *classic* layout, which
was walked end to end (241 steps, 237 questions, 3 statement pages) to establish
the facts below. Selectors are CSS-module build hashes and will rot whenever the
vendor redeploys; they are kept in one place so the rot is cheap to repair.

How this form actually behaves
------------------------------

* **A welcome page gates everything.** The form opens on a statement with a
  single ``<button><span>شروع</span></button>``. Until it is clicked no question
  exists in the DOM, so an extractor that starts parsing immediately finds
  nothing at all.

* **One question per page**, navigated by an *icon* button carrying
  ``aria-label="بعدی"`` and **no text**. Any locator that looks for button text
  will never find it.

* **Statement pages.** Three pages carry a ``label[for^="question-"]`` but
  nothing answerable — they are section dividers ("لطفا با دقت به سوالات زیر
  پاسخ دهید."). They advance only via their own
  ``button[class*="statement_button"]`` ("ادامه"). They are *not* questions and
  are kept out of the schema.

* **The choices are not inside the label's nearest question div.** The label
  lives in ``questionIntro_question_intro_wrapper__…``, and the
  ``[role="radio"]`` choices are that wrapper's *siblings*. Climbing to the
  nearest ``div[class*="question"]`` therefore lands on a subtree with zero
  options — which is why every question has to be resolved against
  ``div[class*="classic_question_wrapper__"]`` instead.

* **Selecting a choice auto-advances.** All 237 questions advanced on their own
  when answered; the next button was never needed. So :meth:`fill_page` records
  what it acted on and :meth:`advance` checks whether the SPA already moved,
  otherwise it would click "next" over an unanswered question and skip it.

* **Clicks made during the page transition are silently dropped.** Walking the
  form on a fixed delay produced each question twice, half the clicks doing
  nothing. Navigation therefore waits for the visible question id to *change*
  and retries, rather than trusting a sleep.

* **Pagination is unanswered-friendly, except at the end.** "بعدی" advances past
  an unanswered required question, so extraction never has to write into a live
  form. The last question is the exception: it will not advance until answered,
  which is exactly the terminal condition the extractor stops on.

Note the form is ~241 pages long, so ``MAX_PAGES`` must be well above the
default of 40 or both phases stop a fifth of the way in.
"""

from __future__ import annotations

import asyncio
import weakref
from typing import Any

from personabot.domain.models import Option, Question, QuestionType
from personabot.domain.text import normalize_for_match
from personabot.errors import ExtractionError, SubmissionError
from personabot.logging_setup import get_logger
from personabot.providers.base import FormSubmitter, SchemaExtractor
from personabot.providers.registry import register_extractor, register_submitter

log = get_logger(__name__)

# --- selectors ---
_QUESTION_BOX = 'div[class*="classic_question_wrapper__"]'
_START_BUTTON = 'button:has-text("شروع")'
_STATEMENT_BUTTON = 'button[class*="statement_button"], button:has-text("ادامه")'
_NEXT_BUTTON = 'button[aria-label="بعدی"]'
_SUBMIT_BUTTON = (
    'button[class*="shared_submit__"], button[class*="shared_send_button__"], '
    'button:has-text("ارسال")'
)

# --- timings ---
_SETTLE_MS = 700          # after the question changes, before we touch anything
_CHANGE_TIMEOUT_MS = 8_000
_POLL_MS = 300
_NAV_ATTEMPTS = 3
_FUZZY_MIN_SCORE = 85

_SUCCESS_SELECTORS = (
    'h1:has-text("سپاس‌گزاریم")',
    'a:has-text("ساخت پرسشنامه در پُرس‌لاین")',
    'div[class*="styles_info_box"]',
    "text=سپاس‌گزاریم",
    "text=سپاسگزاریم",
    "text=با تشکر",
    "text=ثبت شد",
    "[class*='thank']",
)

# One round trip returns everything both phases need about the current page.
# Doing this in the page beats N Playwright `is_visible()` calls, and — unlike
# parsing `page.content()` with BeautifulSoup — it can tell a rendered question
# from one the SPA is merely keeping in the DOM.
_PROBE = """
() => {
  const visible = (el) => {
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return rect.width > 0 && rect.height > 0
        && style.visibility !== 'hidden' && style.display !== 'none';
  };
  const clean = (text) => (text || '').trim().replace(/\\s+/g, ' ');

  const questions = [...document.querySelectorAll('label[for^="question-"]')]
    .filter(visible)
    .map((label) => {
      const id = label.getAttribute('for').slice('question-'.length);
      const box = label.closest('div[class*="classic_question_wrapper__"]')
               || label.parentElement;
      const title = label.querySelector('[class*="title_text"]');
      const choices = [...box.querySelectorAll('[role="radio"]')];
      const inputs = [...box.querySelectorAll(
        'input:not([type=hidden]):not([type=radio]):not([type=checkbox]), textarea')];
      return {
        id,
        title: clean(title ? title.innerText : label.innerText),
        options: choices.map((choice) => {
          const label = choice.querySelector('[class*="choice_label"]');
          return clean(label ? label.innerText : choice.innerText);
        }),
        answered: choices.some((c) => c.getAttribute('aria-checked') === 'true'),
        inputs: inputs.map((i) => ({ id: i.id, tag: i.tagName.toLowerCase() })),
        hasTable: !!box.querySelector('table'),
      };
    });

  const buttons = [...document.querySelectorAll('button')].filter(visible);
  const byClass = (re) => buttons.find((b) => re.test(String(b.className)));
  const submit = byClass(/shared_submit__|shared_send_button__/)
              || buttons.find((b) => /^(ارسال|ثبت|پایان)$/.test(clean(b.innerText)));
  const statement = byClass(/statement_button/)
                 || buttons.find((b) => clean(b.innerText) === 'ادامه');
  const next = buttons.find((b) => b.getAttribute('aria-label') === 'بعدی');

  return {
    signature: questions.map((q) => q.id).join(','),
    questions,
    hasStart: buttons.some((b) => clean(b.innerText) === 'شروع'),
    hasSubmit: !!submit && !submit.disabled,
    hasStatement: !!statement,
    nextEnabled: !!next && !next.disabled,
  };
}
"""


async def _read_state(page: Any) -> dict[str, Any]:
    return await page.evaluate(_PROBE)


async def _wait_for_change(page: Any, signature: str) -> dict[str, Any] | None:
    """Wait until the visible question set changes. Returns the settled state.

    ``None`` means the page never moved — which is how both the end of the form
    and a dropped click present themselves.
    """
    for _ in range(_CHANGE_TIMEOUT_MS // _POLL_MS):
        await page.wait_for_timeout(_POLL_MS)
        state = await _read_state(page)
        if state["signature"] != signature or state["hasSubmit"]:
            await page.wait_for_timeout(_SETTLE_MS)
            return await _read_state(page)
    return None


async def _navigate(page: Any, state: dict[str, Any]) -> dict[str, Any] | None:
    """Click continue/next until the page actually changes.

    Retries because Porsline drops clicks that land mid-transition. Returns the
    new state, or ``None`` when the form will not advance any further.
    """
    from playwright.async_api import Error as PlaywrightError

    signature = state["signature"]
    for attempt in range(1, _NAV_ATTEMPTS + 1):
        if state["hasStatement"]:
            target, what = page.locator(_STATEMENT_BUTTON).first, "ادامه"
        elif state["nextEnabled"]:
            target, what = page.locator(_NEXT_BUTTON).first, "بعدی"
        else:
            return None

        try:
            await target.click(force=True, timeout=10_000)
        except PlaywrightError as exc:
            log.debug("Could not click %s: %s", what, exc)

        changed = await _wait_for_change(page, signature)
        if changed is not None:
            return changed

        log.debug("Porsline ignored the %s click (attempt %d/%d).", what, attempt, _NAV_ATTEMPTS)
        state = await _read_state(page)

    return None


async def _click_start(page: Any) -> bool:
    """Get past the welcome page. True if a question is now showing."""
    state = await _read_state(page)
    if not state["hasStart"]:
        return bool(state["questions"]) or state["hasSubmit"]

    log.info("Welcome page: clicking «شروع».")
    for attempt in range(1, _NAV_ATTEMPTS + 1):
        from playwright.async_api import Error as PlaywrightError

        try:
            await page.locator(_START_BUTTON).first.click(force=True, timeout=15_000)
        except PlaywrightError as exc:
            log.debug("Could not click «شروع»: %s", exc)
        if await _wait_for_change(page, state["signature"]) is not None:
            return True
        log.debug("«شروع» did not take (attempt %d/%d).", attempt, _NAV_ATTEMPTS)
    return False


def _match_option(answer: str, options: list[str]) -> int | None:
    """Index of the choice this answer refers to, or ``None``.

    Phase 3 already snaps answers onto canonical schema strings, so normalized
    equality is the expected path; the fuzzy tier only covers schema drift.
    Exact is tried across *every* option before any looser tier, so "موافقم"
    cannot be captured by "کاملا موافقم".
    """
    target = normalize_for_match(answer)
    if not target:
        return None

    normalized = [normalize_for_match(option) for option in options]

    for index, option in enumerate(normalized):
        if option and option == target:
            return index

    from thefuzz import fuzz

    best_index, best_score = None, _FUZZY_MIN_SCORE - 1
    for index, option in enumerate(normalized):
        if not option:
            continue
        score = int(fuzz.ratio(target, option))
        if score > best_score:
            best_index, best_score = index, score

    if best_index is not None:
        log.debug(
            "Fuzzy-matched %r to %r (%d%%).", answer, options[best_index], best_score
        )
    return best_index


@register_extractor("porsline")
class PorslineExtractor(SchemaExtractor):
    async def prepare(self, page: Any) -> None:
        if not await _click_start(page):
            raise ExtractionError(
                "Clicked «شروع» but no question ever rendered. The welcome page "
                "markup has probably changed."
            )

    async def parse_page(self, page: Any) -> list[Question]:
        state = await _read_state(page)
        questions: list[Question] = []

        for entry in state["questions"]:
            options = tuple(
                Option(text=text, value=text) for text in entry["options"] if text
            )

            if options:
                question_type = QuestionType.RADIO
            elif entry["hasTable"]:
                question_type = QuestionType.MATRIX
                log.warning(
                    "Question %s looks like a matrix; its options are not "
                    "extracted, so it will not be answerable.", entry["id"],
                )
            elif entry["inputs"]:
                question_type = QuestionType.TEXT
            else:
                # A section divider. It owns a question id but asks nothing, so
                # letting it into the schema would send it to the LLM and count
                # it against the completeness gate.
                log.info(
                    "Skipping statement page %s — %r", entry["id"], entry["title"][:60]
                )
                continue

            questions.append(
                Question(
                    question_id=entry["id"],
                    question_text=entry["title"],
                    type=question_type,
                    options=options,
                )
            )

        return questions

    async def fill_placeholders(self, page: Any, questions: list[Question]) -> None:
        """Deliberately does nothing.

        "بعدی" paginates past unanswered required questions, so extraction can
        read the whole instrument without writing a single placeholder answer
        into the live form — no partial responses land in the researcher's data.
        """

    async def advance(self, page: Any) -> bool:
        state = await _read_state(page)
        if state["hasSubmit"]:
            log.info("Reached the submit page.")
            return False

        if await _navigate(page, state) is None:
            # The last question refuses to paginate until it is answered, which
            # is how a read-only walk discovers the end of the form.
            log.info(
                "Porsline stopped paginating at question %s — treating it as the "
                "last page.", state["signature"] or "(none)",
            )
            return False
        return True


@register_submitter("porsline")
class PorslineSubmitter(FormSubmitter):
    def __init__(self, settings: Any, browser: Any, schema: Any = None) -> None:
        super().__init__(settings, browser, schema)
        # Answering auto-advances, so `advance` has to know which page
        # `fill_page` acted on — otherwise it clicks "next" over the question
        # that just appeared and skips it. Keyed by page because one page serves
        # one submission, which keeps this correct under CONCURRENT_SUBMISSIONS.
        self._last_fill: weakref.WeakKeyDictionary[Any, tuple[str, int]] = (
            weakref.WeakKeyDictionary()
        )

    async def prepare(self, page: Any) -> None:
        if not await _click_start(page):
            raise SubmissionError("Clicked «شروع» but no question ever rendered.")

    async def fill_page(self, page: Any, answers: dict[str, str]) -> int:
        from playwright.async_api import Error as PlaywrightError

        state = await _read_state(page)
        filled = 0

        for entry in state["questions"]:
            question_id = entry["id"]
            answer = answers.get(question_id)

            if answer is None:
                if entry["options"] or entry["inputs"]:
                    log.warning("No answer for question %s; leaving it blank.", question_id)
                continue

            try:
                if entry["options"]:
                    index = _match_option(answer, entry["options"])
                    if index is None:
                        log.warning(
                            "Answer %r matches none of %s's options %s.",
                            answer, question_id, entry["options"],
                        )
                        continue
                    await self._select_choice(page, question_id, index)
                    filled += 1
                elif entry["inputs"]:
                    field_id = entry["inputs"][0]["id"]
                    field = (
                        page.locator(f"#{field_id}")
                        if field_id
                        else page.locator(f'{_QUESTION_BOX} input, {_QUESTION_BOX} textarea')
                    )
                    await field.first.fill(answer, timeout=10_000)
                    filled += 1
            except PlaywrightError as exc:
                log.warning("Could not fill %s: %s", question_id, exc)

        self._last_fill[page] = (state["signature"], filled)
        return filled

    @staticmethod
    async def _select_choice(page: Any, question_id: str, index: int) -> None:
        """Click the nth choice of a question.

        Clicking by index sidesteps text matching in the browser entirely, and a
        native ``element.click()`` is what the SPA's own auto-advance listens
        for.
        """
        await page.locator(f'label[for="question-{question_id}"]').first.evaluate(
            """(label, index) => {
                const box = label.closest('div[class*="classic_question_wrapper__"]')
                         || label.parentElement;
                const choices = box.querySelectorAll('[role="radio"]');
                if (!choices[index]) throw new Error('choice ' + index + ' is gone');
                choices[index].click();
            }""",
            index,
        )

    async def advance(self, page: Any) -> bool:
        from playwright.async_api import Error as PlaywrightError

        state = await _read_state(page)

        if state["hasSubmit"]:
            button = page.locator(_SUBMIT_BUTTON).first
            try:
                # React submit buttons routinely ignore a synthetic click.
                await button.evaluate("element => element.click()")
            except PlaywrightError:
                await button.click(force=True, timeout=10_000)
            await page.wait_for_timeout(2_000)
            return False

        filled_signature, filled = self._last_fill.get(page, (None, 0))

        if filled:
            if state["signature"] != filled_signature:
                # The SPA already advanced itself. Clicking anything now would
                # skip the question in front of us.
                return True
            if await _wait_for_change(page, state["signature"]) is not None:
                return True
            log.debug("Answered %s but it did not auto-advance; using «بعدی».",
                      filled_signature)

        # Either nothing was answerable here or the answer did not move the
        # form; page forward so the run does not stall on one question.
        if await _navigate(page, state) is None:
            log.warning(
                "Cannot advance past question %s and no submit button is "
                "showing.", state["signature"] or "(none)",
            )
            return False
        return True

    async def detect_success(self, page: Any) -> bool:
        from playwright.async_api import Error as PlaywrightError

        for _ in range(15):
            for selector in _SUCCESS_SELECTORS:
                try:
                    if await page.locator(selector).first.is_visible(timeout=500):
                        return True
                except PlaywrightError:
                    continue
            await asyncio.sleep(1)
        return False
