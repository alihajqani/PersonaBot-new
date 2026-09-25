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

* **…except when one wrapper holds several question labels.** Two layouts do
  this, and each breaks the other's fix. A *group header* shares the wrapper
  with its first question, whose radios are siblings of that question's own
  intro box. A *«single page display» group* renders a whole section — 30 items,
  150 radios — inside one wrapper, each item scoped by its own box. Resolving
  every label against the wrapper handed all 30 items all 150 radios. So
  :js:func:`scopeFor` uses the wrapper only when it owns exactly one label;
  otherwise the narrowest ancestor owning exactly one label, *if that box holds
  any controls*; otherwise the wrapper again, keeping only the controls that
  follow this label and precede the next one. The probe and the click share
  that one function, so what is read is what gets clicked.

* **A group renders its own «شروع» confirm button, and it looks exactly like a
  send button** (``shared_confirm_button__ shared_submit__``). Treated as the
  end of the form, it stopped one extraction at page 39 of 70 and made
  submissions "succeed" without sending. :js:func:`endButton` therefore excludes
  «شروع» and anything inside a group's wrapper before either tier runs. A
  grouped page also never auto-advances on being answered, so the submitter
  does not wait for it to.

* **The grouped renderer is a different provider.** Matrix questions, voice
  recorders and a ``shared_submit__`` confirm button belong to Porsline's newer
  renderer, handled by ``porsline_v2``. The tell-tale on this renderer is every
  question on a page reporting *all* of the page's radios as its options, and
  the extractor refuses to write such a schema rather than persist it silently.

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

* **Which button ends the form varies by survey.** Some surveys end on a real
  send button (``shared_submit__``/``shared_send_button__``, reading "ارسال");
  others end on a *statement* page whose ``styles_statement_button__`` reads
  "پایان" and carries no send class at all. Worse, a survey can show both: a
  closing statement page renders its «پایان» *above* the send button, so
  matching on button text alone picks the statement button and posts nothing.
  Because ``partial_submit_is_enabled`` is false on these surveys, nothing is
  recorded until the right button is clicked: a run that fills every question
  and misses it submits *nothing*. Which button ends the form is therefore
  defined once, in :data:`_HELPERS`, class tag first and text only as a
  fallback, and clicked by finding it rather than by a second CSS list that can
  disagree with the probe.

Porsline shows one question per page, so a survey's ``form.max_pages`` must be
well above its *question* count (the 237-question form needed 241 pages); the
deployment default of 40 stops both phases a fifth of the way in.
"""

from __future__ import annotations

import asyncio
import re
import weakref
from typing import Any
from urllib.parse import urlsplit

from personabot.domain.models import Option, Question, QuestionType
from personabot.domain.scenarios import identify_scenario
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

# The attribute :js:func:`endButton` stamps on the button it found, so Playwright
# can retry the *same* element that the probe reported.
_END_MARK = "data-personabot-end"

# --- timings ---
_SETTLE_MS = 700          # after the question changes, before we touch anything
_CHANGE_TIMEOUT_MS = 8_000
_END_TIMEOUT_MS = 15_000  # submitting is a round trip, not a local transition
_POLL_MS = 300
_COMBOBOX_TIMEOUT_MS = 3_000
_NAV_ATTEMPTS = 3
# Statement pages a scenario may sit behind (welcome text, instructions).
_SCENARIO_SCAN_PAGES = 6
_FUZZY_MIN_SCORE = 85
# Dropdown options render with their position in front — «۸. تهران». Left on,
# a short name like «تهران» scores 77 against its own option and is rejected.
# The separator must be followed by a space, so «3.5 ساعت» is left alone.
_OPTION_NUMBERING = re.compile(r"^\s*[0-9۰-۹٠-٩]+\s*[.)\-]\s+")

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

# Same-origin API calls whose 2xx response means the answer set reached the
# server. `partial_submit_is_enabled` is False on this survey, so nothing is
# persisted until the one call that posts the whole response.
_SUBMIT_ENDPOINT = re.compile(r"/(responses?|submit|answers?)/?(\?|$)", re.IGNORECASE)

# Shared by every script below, so "which button ends the form" has exactly one
# definition. The predecessor kept the probe's answer and the click's CSS
# selector apart: the probe counted a «پایان» button as submit, the selector
# listed only `shared_submit__`/`shared_send_button__`/«ارسال», and this form
# ends on neither — its last page is a *statement* page whose button reads
# «پایان». Every submission therefore filled all 237 questions, declared the
# submit page reached, then blocked for 10s clicking a button that did not
# exist and failed with a bare Locator timeout.
_HELPERS = """
  const visible = (el) => {
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return rect.width > 0 && rect.height > 0
        && style.visibility !== 'hidden' && style.display !== 'none';
  };
  const clean = (text) => (text || '').trim().replace(/\\s+/g, ' ');
  const visibleButtons = () => [...document.querySelectorAll('button')].filter(visible);
  // Grounded in labels this form actually renders. Nothing speculative belongs
  // here: a token that matched mid-form would submit a partial response, which
  // is worse for the data than the crash this replaced.
  const isEndText = (text) => /^(ارسال|ثبت|پایان)$/.test(clean(text));
  // Two tiers, and the order between them is load-bearing. A survey that ends
  // on a *statement* page renders the statement's own «پایان» button before the
  // real send button, and «پایان» matches the text tier — so a single flat
  // `find` returns whichever comes first in the DOM, which is the statement
  // button. Clicking that submits nothing and never clears `hasSubmit`, because
  // the send button it skipped is still sitting there. The class tag is the
  // survey telling us which button posts the response; only when no button
  // carries it does the text tier get to guess.
  const isSendClass = (b) =>
      /shared_submit__|shared_send_button__/.test(String(b.className));
  // «شروع» opens a form; it never ends one. The welcome page is not the only
  // thing that renders it: a «single page display» group puts its own confirm
  // button at the foot of the section, reading «شروع» *and* carrying
  // `shared_submit__`, so the class tier reported it as the send button 30
  // pages before the end.
  const isStart = (b) => clean(b.innerText) === 'شروع'
                      || clean(b.getAttribute('aria-label')) === 'شروع';
  // A button nested inside a group's own wrapper belongs to that group, not to
  // the form — this keeps a survey that relabels its group confirm button from
  // resurrecting the same bug.
  const inGroup = (b) =>
      !!b.closest('div[class*="singlePageDisplay_question_wrapper__"]');
  const endButton = (buttons) => {
    const candidates = (buttons || visibleButtons())
        .filter((b) => !isStart(b) && !inGroup(b));
    return candidates.find(isSendClass)
        || candidates.find((b) => isEndText(b.innerText)
                               || isEndText(b.getAttribute('aria-label')))
        || null;
  };

  // Which element owns ONE question's controls. See the module docstring:
  // the classic wrapper when it owns exactly one label; else the narrowest
  // single-label ancestor if it holds any control (a «single page display»
  // item); else the wrapper again, keeping only the controls between this
  // label and the next (a group header sharing its first question's wrapper).
  const LABEL = 'label[for^="question-"]';
  const CONTROL = '[role="radio"], [role="combobox"], textarea, '
      + 'input:not([type=hidden]):not([type=radio]):not([type=checkbox])';
  const precedes = (a, b) =>
      !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
  const scopeFor = (label) => {
    const all = () => true;
    const outer = label.closest('div[class*="classic_question_wrapper__"]');
    if (!outer) return { box: label.parentElement || label, exclusive: true, owned: all };
    const owns = (el) => el.querySelectorAll(LABEL).length;
    if (owns(outer) === 1) return { box: outer, exclusive: true, owned: all };

    let narrow = label;
    for (let n = label.parentElement; n && n !== outer; n = n.parentElement) {
      if (owns(n) !== 1) break;
      narrow = n;
    }
    if (narrow !== label && narrow.querySelector(CONTROL)) {
      return { box: narrow, exclusive: true, owned: all };
    }

    const later = [...outer.querySelectorAll(LABEL)]
      .filter((other) => other !== label && precedes(label, other));
    return {
      box: outer,
      exclusive: false,
      owned: (el) => precedes(label, el) && !later.some((other) => precedes(other, el)),
    };
  };
  const controlsFor = (label, selector) => {
    const scope = scopeFor(label);
    return [...scope.box.querySelectorAll(selector)].filter(scope.owned);
  };
"""

# One round trip returns everything both phases need about the current page.
# Doing this in the page beats N Playwright `is_visible()` calls, and — unlike
# parsing `page.content()` with BeautifulSoup — it can tell a rendered question
# from one the SPA is merely keeping in the DOM.
_PROBE = "() => {" + _HELPERS + """
  const questions = [...document.querySelectorAll('label[for^="question-"]')]
    .filter(visible)
    .map((label) => {
      const id = label.getAttribute('for').slice('question-'.length);
      const scope = scopeFor(label);
      const title = label.querySelector('[class*="title_text"]');
      // A header that owns no control is skipped like a statement page,
      // instead of being reported as a copy of its first question.
      const choices = [...scope.box.querySelectorAll('[role="radio"]')].filter(scope.owned);
      const inputs = [...scope.box.querySelectorAll(
        'input:not([type=hidden]):not([type=radio]):not([type=checkbox]), textarea')]
        .filter(scope.owned);
      return {
        id,
        title: clean(title ? title.innerText : label.innerText),
        options: choices.map((choice) => {
          const label = choice.querySelector('[class*="choice_label"]');
          return clean(label ? label.innerText : choice.innerText);
        }),
        answered: choices.some((c) => c.getAttribute('aria-checked') === 'true'),
        inputs: inputs.map((i) => ({ id: i.id, tag: i.tagName.toLowerCase() })),
        // A dropdown is a search <input> inside a Downshift combobox. It looks
        // like a text question, but typed text is only a filter: nothing is
        // recorded until an option from its menu is clicked.
        combobox: [...scope.box.querySelectorAll('[role="combobox"]')].some(scope.owned),
        hasTable: scope.exclusive && !!scope.box.querySelector('table'),
      };
    });

  const buttons = visibleButtons();
  const submit = endButton(buttons);
  const statement = buttons.find((b) => /statement_button/.test(String(b.className)))
                 || buttons.find((b) => clean(b.innerText) === 'ادامه');
  const next = buttons.find((b) => b.getAttribute('aria-label') === 'بعدی');

  return {
    signature: questions.map((q) => q.id).join(','),
    questions,
    // Every visible radio on the page, for the extractor's bleed guard.
    pageRadios: [...document.querySelectorAll('[role="radio"]')].filter(visible).length,
    hasStart: buttons.some((b) => isStart(b) && !inGroup(b)),
    hasSubmit: !!submit && !submit.disabled,
    hasStatement: !!statement,
    nextEnabled: !!next && !next.disabled,
  };
}
"""

# Clicks the very button the probe reports as `hasSubmit`, and marks it so a
# Playwright retry can address the same element. A native `.click()` is what the
# SPA's own handlers listen for.
_CLICK_END_BUTTON = "(mark) => {" + _HELPERS + """
  const button = endButton();
  if (!button) return false;
  button.setAttribute(mark, '');
  button.click();
  return true;
}
"""


# The options a dropdown's menu is rendering right now. Downshift only mounts
# them while the menu is open, and the menu is found through the input's own
# `aria-controls` rather than a build-hashed class.
_COMBOBOX_OPTIONS = "(questionId) => {" + _HELPERS + """
  const input = document.getElementById('question-' + questionId);
  const menuId = input && input.getAttribute('aria-controls');
  const menu = menuId && document.getElementById(menuId);
  if (!menu) return { menu: null, options: [] };
  return {
    menu: menuId,
    options: [...menu.querySelectorAll('[role="option"]')].map((o) => clean(o.innerText)),
  };
}
"""


async def _read_state(page: Any) -> dict[str, Any]:
    return await page.evaluate(_PROBE)


async def _open_combobox(page: Any, question_id: str, text: str = "") -> dict[str, Any]:
    """Open a dropdown, optionally filtered by ``text``, and wait for its options.

    Returns the :data:`_COMBOBOX_OPTIONS` result; ``options`` is empty when the
    menu never rendered any.
    """
    field = page.locator(f"#question-{question_id}").first
    await field.click(timeout=10_000)
    await field.fill(text, timeout=10_000)

    menu: dict[str, Any] = {"menu": None, "options": []}
    for _ in range(_COMBOBOX_TIMEOUT_MS // _POLL_MS):
        menu = await page.evaluate(_COMBOBOX_OPTIONS, question_id)
        if menu["options"]:
            break
        await page.wait_for_timeout(_POLL_MS)
    # One-to-one, so an index into these is still an index into the menu.
    menu["options"] = [_OPTION_NUMBERING.sub("", option) for option in menu["options"]]
    return menu


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


async def _wait_for_end(page: Any, signature: str) -> bool:
    """Wait for the click on the end button to take.

    Submitting is a server round trip rather than a local transition, so this
    waits longer than :func:`_wait_for_change`. The form has moved on when the
    end button is gone or a different page is showing.
    """
    for _ in range(_END_TIMEOUT_MS // _POLL_MS):
        await page.wait_for_timeout(_POLL_MS)
        state = await _read_state(page)
        if not state["hasSubmit"] or state["signature"] != signature:
            return True
    return False


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


def reject_bled_options(questions: list[Question], *, page_radios: int | None) -> None:
    """Refuse a page where every question reports *all* of the page's radios.

    That is what resolving choices against a container holding several
    questions produces: each question comes back carrying the union of the
    page's options — free-text fields included, typed as RADIO. Left unchecked
    it is silent and destructive: extraction reports success and phase 1
    overwrites the schema before anything objects.

    An earlier version of this guard fired whenever two questions on a page had
    identical option lists. On a «single page display» Likert group — 30 items,
    same five options each — that is simply correct, and the guard blocked the
    extraction. Identical lists are not the tell; lists as long as the whole
    page's radio count are.
    """
    choices = [q for q in questions if q.options and q.type is QuestionType.RADIO]
    if len(choices) < 2 or not page_radios:
        return
    if any(len(q.options) != page_radios for q in choices):
        return
    raise ExtractionError(
        f"{len(choices)} questions on one page each report all {page_radios} of the "
        f"page's radios as their options. The options belong to the page, not to "
        f"any one question — this is Porsline's grouped renderer. Use the "
        f"`porsline_v2` provider for this form."
    )


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
            elif entry.get("combobox"):
                # Opening the menu only reads it; nothing is selected, so the
                # walk still writes nothing into the live form.
                menu = await _open_combobox(page, entry["id"])
                await page.keyboard.press("Escape")
                options = tuple(
                    Option(text=text, value=text) for text in menu["options"] if text
                )
                if options:
                    question_type = QuestionType.SELECT
                else:
                    question_type = QuestionType.TEXT
                    log.warning(
                        "Dropdown %s rendered no options; saving it as a text "
                        "question.", entry["id"],
                    )
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

        reject_bled_options(questions, page_radios=state.get("pageRadios"))
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
    def __init__(self, workspace: Any, browser: Any, schema: Any = None, scenarios: Any = ()) -> None:
        super().__init__(workspace, browser, schema, scenarios)
        # Answering auto-advances, so `advance` has to know which page
        # `fill_page` acted on — otherwise it clicks "next" over the question
        # that just appeared and skips it. Keyed by page because one page serves
        # one submission, which keeps this correct under CONCURRENT_SUBMISSIONS.
        self._last_fill: weakref.WeakKeyDictionary[Any, tuple[str, int]] = (
            weakref.WeakKeyDictionary()
        )
        # URLs of same-origin writes the server accepted, per page. The ending
        # screen is themeable free text, so the response POST is the one success
        # signal this form cannot render differently from survey to survey.
        self._accepted: weakref.WeakKeyDictionary[Any, list[str]] = (
            weakref.WeakKeyDictionary()
        )

    async def prepare(self, page: Any) -> None:
        self._watch_for_accepted_response(page)
        if not await _click_start(page):
            raise SubmissionError("Clicked «شروع» but no question ever rendered.")

    async def detect_scenario(self, page: Any) -> str | None:
        """Walk the statement pages up to the scenario the form drew.

        The scenario is a statement page like any divider, told apart by the
        number it opens with. Stops on the scenario itself, so the fill loop's
        first «ادامه» is the one that moves past it.
        """
        state = await _read_state(page)
        for _ in range(_SCENARIO_SCAN_PAGES):
            for entry in state["questions"]:
                if entry["options"] or entry["inputs"]:
                    return None  # reached a real question without seeing one
                key = identify_scenario(entry["title"], self.scenarios)
                if key is not None:
                    return key
            next_state = await _navigate(page, state)
            if next_state is None:
                return None
            state = next_state
        return None

    def _watch_for_accepted_response(self, page: Any) -> None:
        """Record the API writes the server accepts while this page is open."""
        accepted: list[str] = []
        self._accepted[page] = accepted
        host = urlsplit(self.workspace.require_form_url()).netloc

        def on_response(response: Any) -> None:
            request = response.request
            if request.method not in ("POST", "PUT", "PATCH"):
                return
            parts = urlsplit(response.url)
            if parts.netloc != host or not _SUBMIT_ENDPOINT.search(parts.path):
                return
            if 200 <= response.status < 300:
                accepted.append(response.url)

        page.on("response", on_response)

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
                elif entry.get("combobox"):
                    # Must come before `inputs`: filling the search box alone
                    # records nothing, and the form bounces back to this
                    # required question at submit time.
                    if await self._select_from_combobox(page, question_id, answer):
                        filled += 1
                elif entry["inputs"]:
                    field_id = entry["inputs"][0]["id"]
                    if field_id:
                        field = page.locator(f"#{field_id}")
                    elif len(state["questions"]) > 1:
                        # The page-wide fallback below picks the first field on
                        # the page, which is only this question's field when
                        # this question is the page. On a grouped page it would
                        # write one respondent's answer into someone else's row.
                        log.warning(
                            "Question %s has an id-less input on a page holding "
                            "%d questions; leaving it blank rather than guessing "
                            "which field is its own.",
                            question_id, len(state["questions"]),
                        )
                        continue
                    else:
                        field = page.locator(
                            f'{_QUESTION_BOX} input, {_QUESTION_BOX} textarea'
                        )
                    await field.first.fill(answer, timeout=10_000)
                    filled += 1
            except PlaywrightError as exc:
                log.warning("Could not fill %s: %s", question_id, exc)

        self._last_fill[page] = (state["signature"], filled)
        return filled

    @staticmethod
    async def _select_from_combobox(page: Any, question_id: str, answer: str) -> bool:
        """Pick ``answer`` from a dropdown's menu. False if no option matches.

        Typing narrows the menu, but a filter that matches nothing (a spelling
        variant, a search that is not substring-based) must not lose the
        answer, so the full list is read before giving up.
        """
        menu = await _open_combobox(page, question_id, answer)
        index = _match_option(answer, menu["options"])
        if index is None:
            menu = await _open_combobox(page, question_id)
            index = _match_option(answer, menu["options"])
        if index is None:
            await page.keyboard.press("Escape")
            log.warning(
                "Answer %r matches none of dropdown %s's options %s.",
                answer, question_id, menu["options"][:40],
            )
            return False

        await page.locator(f'[id="{menu["menu"]}"] [role="option"]').nth(index).click(
            timeout=10_000
        )
        return True

    @staticmethod
    async def _select_choice(page: Any, question_id: str, index: int) -> None:
        """Click the nth choice of a question.

        Clicking by index sidesteps text matching in the browser entirely, and a
        native ``element.click()`` is what the SPA's own auto-advance listens
        for.
        """
        await page.locator(f'label[for="question-{question_id}"]').first.evaluate(
            "(label, index) => {" + _HELPERS + """
                const choices = controlsFor(label, '[role="radio"]');
                if (!choices[index]) throw new Error('choice ' + index + ' is gone');
                choices[index].click();
            }""",
            index,
        )

    async def advance(self, page: Any) -> bool:
        from playwright.async_api import Error as PlaywrightError

        state = await _read_state(page)

        if state["hasSubmit"]:
            return await self._finish(page, state)

        filled_signature, filled = self._last_fill.get(page, (None, 0))

        if filled:
            if state["signature"] != filled_signature:
                # The SPA already advanced itself. Clicking anything now would
                # skip the question in front of us.
                return True
            # Only a one-question page auto-advances on being answered. A
            # grouped page never can — answering item 1 of 30 must not move the
            # form — so waiting out the full change timeout there just adds
            # eight idle seconds per group to every submission.
            if "," not in (filled_signature or ""):
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

    async def _finish(self, page: Any, state: dict[str, Any]) -> bool:
        """Click the button that ends the form and verify the click took.

        Always returns False: the page loop is over either way, and whether the
        response actually landed is :meth:`detect_success`'s question.
        """
        from playwright.async_api import Error as PlaywrightError

        signature = state["signature"]
        log.info("Reached the end of the form; submitting.")

        for attempt in range(1, _NAV_ATTEMPTS + 1):
            if not await page.evaluate(_CLICK_END_BUTTON, _END_MARK):
                # It vanished between the probe and the click, which is itself
                # the page having moved on.
                return False

            if await _wait_for_end(page, signature):
                return False

            # A native click can be dropped mid-transition, exactly as the
            # per-question clicks are. The probe tagged the element, so this
            # retries the same one instead of guessing at a selector.
            try:
                await page.locator(f"[{_END_MARK}]").first.click(force=True, timeout=5_000)
            except PlaywrightError as exc:
                log.debug("Playwright could not click the end button: %s", exc)

            if await _wait_for_end(page, signature):
                return False

            log.debug(
                "Porsline ignored the submit click (attempt %d/%d).",
                attempt, _NAV_ATTEMPTS,
            )
            state = await _read_state(page)
            signature = state["signature"]

        log.warning("Clicked submit %d times and the form never moved.", _NAV_ATTEMPTS)
        return False

    async def detect_success(self, page: Any) -> bool:
        from playwright.async_api import Error as PlaywrightError

        for _ in range(15):
            if self._accepted.get(page):
                log.debug("Server accepted %s.", self._accepted[page][-1])
                return True
            for selector in _SUCCESS_SELECTORS:
                try:
                    if await page.locator(selector).first.is_visible(timeout=500):
                        return True
                except PlaywrightError:
                    continue
            await asyncio.sleep(1)

        # Nothing confirmed it. Say what the page was showing, because "no
        # success confirmation" alone gave no way to tell a rejected submission
        # from a success screen this code does not recognise — and those two
        # need opposite responses from whoever reads the log.
        try:
            state = await _read_state(page)
            ended = not state["hasSubmit"] and not state["questions"]
            log.warning(
                "No confirmation: %d question(s) still visible, end button %s. %s",
                len(state["questions"]),
                "present" if state["hasSubmit"] else "gone",
                "The form did leave its last page, so the response may well have "
                "landed — check the survey's own response count against "
                "answers/done/ before re-running this file, or you will submit it "
                "twice." if ended else
                "The form never left its last page, so nothing was submitted.",
            )
        except PlaywrightError:
            pass
        return False
