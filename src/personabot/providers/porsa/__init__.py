"""Porsa provider — «پُرسا», IRANDOC's own survey platform (porsa.irandoc.ac.ir).

Despite the near-identical share URL shape (``/s/<code>``), this is **not**
Porsline. It is a server-rendered Laravel/jQuery form, and it behaves in three
ways that no other provider here does. Those three facts are what this module
exists to encode.

How this form actually behaves
------------------------------

* **The whole questionnaire is already in the DOM on first load.** Every page
  lives in its own ``div#page_N.page-class``; all but the current one carry
  ``display: none``. Nothing is fetched as you paginate. So :meth:`parse_page`
  reads *all* pages in one round trip and :meth:`PorsaExtractor.advance` returns
  ``False`` immediately — extraction never clicks, never types, and therefore
  **never writes a placeholder answer into a live form**. That matters here: the
  platform is a real research instrument and partial rows would land in someone's
  dataset.

* **``body { visibility: hidden }`` until the page's own ``pageloaded`` event.**
  ``page-loaded.js`` reveals the body about a second after DOMContentLoaded.
  Until then every element reports ``visibility: hidden`` and Playwright refuses
  to click any of them — including after ``wait_until="networkidle"``. Waiting
  for that reveal is :meth:`prepare`'s whole job, and skipping it fails with a
  bare "element is not visible" that says nothing about the cause.

* **The radio inputs are ``opacity: 0`` and their labels have zero height.**
  ``.radioContainer input`` is positioned absolutely at ``opacity: 0`` and the
  visible control is a sibling ``span.radioCheckmark`` (25×25). Clicking the
  ``<label>`` fails — it collapses to zero height because both children are
  absolutely positioned. The span is the only real click target.

Two more details that shape the code
------------------------------------

* **Pagination is client-side and validating.** ``#nextButton`` runs
  ``checkPageQuestionsAnswerStatus`` and simply *returns* when the current page
  has a blank required question — no error is thrown and no navigation happens.
  A submitter that clicked and assumed would silently loop on one page, so
  :meth:`PorsaSubmitter.advance` verifies ``#pageNumber`` actually changed and,
  when it did not, names the questions the form flagged with ``.boder-danger``.

* **Nothing is POSTed until ``#submitButton``.** Paging back and forth is free;
  the single POST to the survey URL happens on final submit, after the page
  re-validates *every* question on *every* page. So an answer set with one gap
  fails at the very end rather than at the page that was short — which is why
  ``answering.completeness.min_ratio`` must be ``1.0`` for this form.

Selectors are plain Bootstrap/jQuery ids and classes rather than build hashes,
so they should prove considerably more durable than the Porsline ones.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from personabot.domain.models import Option, Question, QuestionType
from personabot.domain.text import normalize, normalize_for_match
from personabot.errors import (
    AlreadyRespondedError,
    CircuitError,
    ExtractionError,
    SubmissionError,
    SurveyClosedError,
)
from personabot.logging_setup import get_logger
from personabot.providers.base import FormSubmitter, SchemaExtractor
from personabot.providers.registry import register_extractor, register_submitter

log = get_logger(__name__)

# --- selectors ---
_FORM = "#pageForm"
_NEXT_BUTTON = "#nextButton"
_SUBMIT_BUTTON = "#submitButton"
_PAGE_NUMBER = "#pageNumber"

# --- timings ---
_READY_TIMEOUT_MS = 30_000
_CHANGE_TIMEOUT_MS = 10_000
_POLL_MS = 250
# Slower than _POLL_MS: each tick that finds no form reads the page's text, and
# the deadline is the readiness budget rather than a couple of seconds.
_FORM_POLL_SECONDS = 1.0
_NAV_ATTEMPTS = 3
_SUBMIT_SETTLE_MS = 3_000
_FUZZY_MIN_SCORE = 85

# `data-type` -> our normalized vocabulary. The platform ships more types than
# any one survey uses; mapping them all means a form that adds a text question
# later does not silently become UNKNOWN.
_TYPE_MAP = {
    "likert": QuestionType.RADIO,
    "singleChoice": QuestionType.RADIO,
    "multipleChoice": QuestionType.CHECKBOX,
    "textAnswer": QuestionType.TEXT,
    "textNumber": QuestionType.TEXT,
    "textEmail": QuestionType.TEXT,
    "textDate": QuestionType.TEXT,
    # Expanded to one entry per row by `readMatrixRows` in `_PROBE` (see below),
    # so by the time an entry reaches here it is a single choice question over
    # the matrix's shared column options — RADIO/CHECKBOX, never QuestionType.MATRIX.
    "matrixSingleChoice": QuestionType.RADIO,
    "matrixMultipleChoice": QuestionType.CHECKBOX,
}

_SUCCESS_TEXTS = (
    "با موفقیت",
    "سپاسگزاریم",
    "سپاس‌گزاریم",
    "با تشکر",
    "پاسخ شما ثبت شد",
    "تکمیل کردید",
    # Verbatim confirmation text on the live form's post-submit page.
    # Already covered by the bare "سپاسگزاریم" above, but named explicitly
    # since it is the exact string a submission actually lands on.
    "از شما برای پاسخ‌گویی به این پرسش‌نامه سپاسگزاریم",
)

# The platform tracks who has answered and serves a form-less error page to
# anyone who returns — «کاربر گرامی شما پیشتر به این پرسش‌نامه پاسخ داده‌اید».
# Behind Tor that is a property of the exit IP, so it is a routing problem, not
# a broken form: the same answer set succeeds on the next circuit. Detecting it
# matters because the page contains no `#pageForm` at all, so without this the
# only symptom is a missing selector — which reads as a load failure and sends
# you auditing markup that is perfectly fine.
#
def _squeeze(text: str) -> str:
    """Fold a Persian phrase down to something two spellings of it agree on.

    `normalize_for_match` is not enough here. It strips the ZWNJ, so
    «پرسش‌نامه» becomes «پرسشنامه» — but the same word is also written with a
    real space, «پرسش نامه», which strips to nothing of the sort. Whitespace and
    ZWNJ are interchangeable in this orthography, and this is prose on a
    server-rendered page rather than a schema option, so all three spellings
    have to converge. Dropping every space is safe *because* the target is a
    known marker phrase being substring-matched, not user content being
    compared for meaning.
    """
    return _WHITESPACE_ANYWHERE.sub("", normalize_for_match(text))


_WHITESPACE_ANYWHERE = re.compile(r"\s+")

_ALREADY_ANSWERED_TEXTS = tuple(
    _squeeze(text)
    for text in (
        "شما پیشتر به این پرسش‌نامه پاسخ داده‌اید",
        "پیشتر به این پرسش‌نامه پاسخ داده‌اید",
    )
)

# porsa.irandoc.ac.ir reuses one template — a `.left-part` sidebar offering
# «خانه»/«نام‌نویسی» links, no `#pageForm` anywhere on the page — for every
# survey link that does not currently serve the live questionnaire: closed,
# deactivated by its owner, not yet opened, or simply wrong. The desktop and
# mobile variants of that sidebar's blurb differ in their opening clause
# («... با پُرسا ...» vs «... با این سامانه ...») but both end in the same
# words, which is the substring matched here so the check survives either
# viewport.
_SURVEY_CLOSED_TEXT = _squeeze("از لینک‌های زیر استفاده کنید")

# One evaluate() returns everything both phases need, for every page at once.
# The alternative — N Playwright locator calls per question across 91 questions
# and 5 pages — is roughly 500 round trips to read a form that is already fully
# present in the DOM.
_PROBE = """
() => {
  const clean = (t) => (t || '').replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim();
  const text = (el) => clean(el ? el.textContent : '');
  const digits = (s) => {
    // The platform renders page numbers in Persian/Arabic-Indic digits.
    const map = {'\\u06f0':'0','\\u06f1':'1','\\u06f2':'2','\\u06f3':'3','\\u06f4':'4',
                 '\\u06f5':'5','\\u06f6':'6','\\u06f7':'7','\\u06f8':'8','\\u06f9':'9',
                 '\\u0660':'0','\\u0661':'1','\\u0662':'2','\\u0663':'3','\\u0664':'4',
                 '\\u0665':'5','\\u0666':'6','\\u0667':'7','\\u0668':'8','\\u0669':'9'};
    return (s || '').replace(/[\\u06f0-\\u06f9\\u0660-\\u0669]/g, (d) => map[d]);
  };
  const shown = (el) => {
    if (!el) return false;
    const style = getComputedStyle(el);
    return style.display !== 'none' && style.visibility !== 'hidden';
  };

  const form = document.querySelector('#pageForm');
  if (!form) {
    return {ready: false, form: false, pages: [], questions: []};
  }

  const readQuestion = (box, pageNumber) => {
    const type = box.dataset.type || '';
    const listMode = String(box.dataset.listmode) === 'true';

    let options = [];
    if (type === 'likert') {
      // Column headers carry the option labels; the radios sit in the row below,
      // one <td> per column, so the two lists align by index.
      options = [...box.querySelectorAll('tr.likertText td')].map(text);
    } else if (listMode) {
      options = [...box.querySelectorAll('select option')]
        .map(text).filter((t) => t !== '');
    } else {
      // Choice rows are `<tr><th>label</th><td><input></td></tr>`, so the <th>
      // order matches the input order.
      options = [...box.querySelectorAll('tbody th')].map(text);
    }

    const controls = [...box.querySelectorAll('input[type=radio], input[type=checkbox]')];
    const fields = [...box.querySelectorAll(
      'textarea, input[type=text], input[type=number], input[type=email]')];

    return {
      id: box.id,
      type,
      listMode,
      page: pageNumber,
      number: clean(digits(text(box.querySelector('.qNumberStyle span')))),
      text: text(box.querySelector('.textareaStyle')),
      help: text(box.querySelector('.textareaStyle2')),
      options: options.filter((o) => o !== ''),
      optionCount: options.length,
      controlCount: controls.length,
      fieldCount: fields.length,
      name: controls.length ? controls[0].name : (fields.length ? fields[0].name : ''),
      answered: listMode
        ? !!(box.querySelector('select') || {}).value
        : controls.some((c) => c.checked),
      flagged: box.classList.contains('boder-danger'),
      optional: box.classList.contains('valueCanBeEmpty')
             || box.classList.contains('conditionalValueCanBeEmpty'),
      hidden: !shown(box),
    };
  };

  // A matrix box is one `data-type` but N questions: each `<tr>` after the
  // header row is its own item, sharing the header row's columns as its
  // options and its own radio/checkbox `name` as the thing that makes it
  // answerable. Without this, the whole table collapses into a single
  // unanswerable stub carrying only the box's generic instruction text —
  // which is most of the instrument on a form built from matrix questions.
  const readMatrixRows = (box, pageNumber) => {
    const type = box.dataset.type || '';
    // The header `<tr>` is the only row using `<th>`; data rows use `<td>`
    // throughout, so this both lists the shared columns and (via the filter
    // below) tells the two kinds of row apart.
    const columns = [...box.querySelectorAll('tbody th')].map(text).filter((t) => t !== '');
    const dataRows = [...box.querySelectorAll('tbody tr')].filter((tr) => !tr.querySelector('th'));

    return dataRows.map((row, index) => {
      const controls = [...row.querySelectorAll('input[type=radio], input[type=checkbox]')];
      const fields = [...row.querySelectorAll(
        'textarea, input[type=text], input[type=number], input[type=email]')];
      const rowName = controls.length ? controls[0].name : (fields.length ? fields[0].name : '');

      return {
        id: rowName || `${box.id}_${index}`,
        type,
        listMode: false,
        page: pageNumber,
        number: clean(digits(text(box.querySelector('.qNumberStyle span')))),
        text: text(row.querySelector('td')),
        help: '',
        options: columns,
        optionCount: columns.length,
        controlCount: controls.length,
        fieldCount: fields.length,
        name: rowName,
        answered: controls.some((c) => c.checked),
        flagged: box.classList.contains('boder-danger'),
        optional: box.classList.contains('valueCanBeEmpty')
               || box.classList.contains('conditionalValueCanBeEmpty'),
        hidden: !shown(box),
      };
    });
  };

  const pages = [...form.querySelectorAll('.page-class')].map((page) => {
    const number = parseInt((page.id.match(/\\d+/) || ['0'])[0], 10);
    const boxes = [...page.querySelectorAll('.questionsBorder')];
    return {
      id: page.id,
      number,
      visible: shown(page),
      boxCount: boxes.length,
      questions: boxes.flatMap((box) => {
        const type = box.dataset.type || '';
        return type === 'matrixSingleChoice' || type === 'matrixMultipleChoice'
          ? readMatrixRows(box, number)
          : [readQuestion(box, number)];
      }),
    };
  });

  const next = document.querySelector('#nextButton');
  const submit = document.querySelector('#submitButton');
  const status = window.SurveyStatus || {};

  return {
    ready: getComputedStyle(document.body).visibility === 'visible',
    form: true,
    currentPage: parseInt(digits(text(document.querySelector('#pageNumber'))), 10) || 0,
    totalPages: status.surveyTotalPageNumber || pages.length,
    // The platform's own count is of BOXES, not of the questions we report —
    // a matrix box is one box and N of our questions. `totalBoxes` is the
    // like-for-like figure `parse_page` checks it against.
    totalQuestions: status.surveyTotalQuestionNumber || null,
    totalBoxes: pages.reduce((sum, p) => sum + p.boxCount, 0),
    hasNext: shown(next) && !(next || {}).disabled,
    hasSubmit: shown(submit) && !(submit || {}).disabled,
    pages,
    questions: pages.flatMap((p) => p.questions),
  };
}
"""


async def _read_state(page: Any) -> dict[str, Any]:
    return await page.evaluate(_PROBE)


async def _body_text(page: Any) -> str:
    """The page's visible text, tidied but still readable, or ``""``."""
    from playwright.async_api import Error as PlaywrightError

    try:
        raw = await page.locator("body").inner_text(timeout=5_000)
    except PlaywrightError:
        return ""
    return normalize(raw, fold_arabic=False, zwnj="keep", strip_trailing="")


async def _explain_missing_form(page: Any, timeout_ms: int) -> Exception:
    """Turn "there is no #pageForm" into an error that names the actual cause.

    Four very different situations produce the same missing selector, and the
    body text is what separates them:

    * the platform's "you have already answered" page — a routing problem,
      solved by a different exit IP;
    * the platform's generic error/info template — the survey is closed,
      deactivated, or the link is otherwise wrong. Not a routing problem, but
      still worth one retry on another circuit before believing it, in case a
      transient block is serving the same shell;
    * some other fully-rendered page — an upstream block, an unrecognised
      layout. Still reported as a circuit problem, because behind Tor what you
      are served depends on the exit IP and a second route is the cheap first
      hypothesis. The page's own words are quoted so that when it is *not* the
      route, the real reason is right there in the message rather than one
      debugging session away;
    * nothing rendered at all — the response never finished arriving.
    """
    text = await _body_text(page)
    squeezed = _squeeze(text)

    if any(marker in squeezed for marker in _ALREADY_ANSWERED_TEXTS):
        return AlreadyRespondedError(
            "Porsa says this visitor has already answered "
            "(«شما پیشتر به این پرسش‌نامه پاسخ داده‌اید»). The exit IP is spent — "
            "the same answer set will go through on another circuit. With "
            "USE_TOR off, every submission comes from one address, so the form "
            "accepts exactly one."
        )

    if not text:
        return CircuitError(
            f"Porsa returned no readable page within {timeout_ms // 1000}s. "
            f"The response never finished arriving over this circuit."
        )

    excerpt = text[:200] + ("…" if len(text) > 200 else "")

    if _SURVEY_CLOSED_TEXT in squeezed:
        return SurveyClosedError(
            f"Porsa served its generic error/info page (no #pageForm, a "
            f"«خانه»/«نام‌نویسی» sidebar) within {timeout_ms // 1000}s — the "
            f"template it uses for a survey that is closed, deactivated, or "
            f"otherwise not currently live. The page says: «{excerpt}»"
        )

    return CircuitError(
        f"Porsa served a page with no #pageForm on it within "
        f"{timeout_ms // 1000}s. The page says: «{excerpt}»"
    )


async def _await_form(page: Any, timeout_ms: int) -> None:
    """Wait for ``#pageForm``, but stop the moment the page rules it out.

    A plain ``wait_for_selector`` spends the entire budget on a page that was
    never going to have a form, and only then looks at what the page actually
    said. That is backwards for the most common failure here: the platform
    answers a returning visitor immediately, with a complete error page. Waiting
    45s to read a message that was there in the first second is 45s not spent
    trying the next circuit — and behind Tor, with more submissions than exit
    IPs, that message is routine rather than exceptional.

    The body is only read when the form is absent, so the healthy path — form
    present on the first look — costs one ``count()`` and nothing else.
    """
    deadline = time.monotonic() + timeout_ms / 1000

    while True:
        if await page.locator(_FORM).count():
            return

        diagnosis = await _explain_missing_form(page, timeout_ms)
        if isinstance(diagnosis, (AlreadyRespondedError, SurveyClosedError)):
            raise diagnosis
        if time.monotonic() >= deadline:
            raise diagnosis

        await asyncio.sleep(_FORM_POLL_SECONDS)


async def _wait_ready(page: Any, *, timeout_ms: int = _READY_TIMEOUT_MS) -> dict[str, Any]:
    """Wait out ``body { visibility: hidden }`` and return the settled state.

    The form is fully parsed and interactive well before this fires, but nothing
    is *clickable* until it does — so every provider entry point goes through
    here rather than trusting ``networkidle``.

    Both waits fail with a retryable error, because both are reached only when
    the page did not arrive intact, and neither has POSTed anything.
    """
    from playwright.async_api import Error as PlaywrightError

    await _await_form(page, timeout_ms)

    try:
        await page.wait_for_function(
            "() => getComputedStyle(document.body).visibility === 'visible'",
            timeout=timeout_ms,
        )
    except PlaywrightError as exc:
        raise CircuitError(
            f"Porsa never revealed the form body within {timeout_ms // 1000}s. "
            f"The markup is present but the page keeps `body "
            f"{{visibility: hidden}}` until its own `pageloaded` event, which "
            f"needs page-loaded.js — so its assets never finished arriving: "
            f"{(str(exc).strip().splitlines() or [''])[0]}"
        ) from exc

    state = await _read_state(page)
    if not state.get("form"):
        raise ExtractionError("No #pageForm on the page — this is not a Porsa survey.")
    return state


def _match_option(answer: str, options: list[str]) -> int | None:
    """Index of the choice this answer refers to, or ``None``.

    Phase 3 already snaps answers onto canonical schema strings, so normalized
    equality is the expected path and the fuzzy tier only covers schema drift.
    Exact is tried across *every* option before any looser tier — otherwise
    «موافقم» would be captured by «کاملا موافقم», which on a 5-point agreement
    scale is a two-point error in the direction that matters most.
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
        log.debug("Fuzzy-matched %r to %r (%d%%).", answer, options[best_index], best_score)
    return best_index


def _to_question(entry: dict[str, Any]) -> Question | None:
    """One probe entry as a schema :class:`Question`, or ``None`` to skip it."""
    question_type = _TYPE_MAP.get(entry["type"], QuestionType.UNKNOWN)
    options = tuple(Option(text=text, value=text) for text in entry["options"])

    if question_type is QuestionType.UNKNOWN:
        log.warning(
            "Question %s has data-type %r, which this provider does not know. "
            "Recording it as UNKNOWN.", entry["id"], entry["type"],
        )
    elif options and question_type is QuestionType.RADIO:
        # Every choice question must expose as many controls as labels, or the
        # index we click by is not the index we matched on.
        if not entry["listMode"] and entry["controlCount"] != len(options):
            log.warning(
                "Question %s lists %d option(s) but %d input(s); the option "
                "order may not align with the controls.",
                entry["id"], len(options), entry["controlCount"],
            )

    text = entry["text"]
    if entry["help"]:
        text = f"{text} {entry['help']}".strip()

    if not text and not options:
        log.info("Skipping %s — no text and no options.", entry["id"])
        return None

    return Question(
        question_id=entry["id"],
        question_text=text,
        type=question_type,
        options=options,
    )


@register_extractor("porsa")
class PorsaExtractor(SchemaExtractor):
    """Reads the whole instrument from the first page load, without touching it."""

    #: `_wait_ready` polls for the form itself and gates interactivity on the
    #: page's own `pageloaded` event, so navigation has nothing left to wait for
    #: — every stricter condition just moves the same wait earlier, under a
    #: budget that cannot tell "still arriving" from "never going to arrive".
    #:
    #: `networkidle` would block on fonts and images no parser reads.
    #: `domcontentloaded` looks right and is not: a `<script>` after a
    #: stylesheet blocks DOMContentLoaded until that stylesheet lands, so on the
    #: platform's own error page — the one served to a visitor who has already
    #: answered — it took ~50s to report a verdict that was in the first
    #: kilobyte of HTML. Committing hands control to the poll immediately, which
    #: is what makes an early diagnosis reachable at all.
    nav_wait_until = "commit"

    async def prepare(self, page: Any) -> None:
        state = await _wait_ready(
            page, timeout_ms=self.settings.ready_timeout_seconds * 1000
        )
        log.info(
            "Porsa form ready: %s question(s) across %s page(s), all present in "
            "the DOM.", state.get("totalQuestions"), state.get("totalPages"),
        )

    async def parse_page(self, page: Any) -> list[Question]:
        state = await _read_state(page)

        questions: list[Question] = []
        for entry in state["questions"]:
            question = _to_question(entry)
            if question is not None:
                questions.append(question)

        # Compared box-for-box against SurveyStatus, not question-for-question:
        # a matrix box is one box and N questions once expanded, so checking
        # `len(questions)` against the platform's own question count would flag
        # every matrix-bearing form as a parser gap. `totalBoxes` is absent on
        # states built before this field existed (e.g. in tests), in which case
        # the check is silently skipped rather than compared against zero.
        expected = state.get("totalQuestions")
        found = state.get("totalBoxes")
        if expected and found is not None and found != expected:
            log.warning(
                "Found %d question box(es) but SurveyStatus reports %d. Some "
                "questions were skipped or the markup has changed.", found, expected,
            )
        return questions

    async def fill_placeholders(self, page: Any, questions: list[Question]) -> None:
        """Deliberately does nothing.

        Every page is already in the DOM, so the schema is readable without
        answering anything. No placeholder response is ever written into the
        live form and no partial row can reach the researcher's dataset.
        """

    async def advance(self, page: Any) -> bool:
        """Always ``False`` — :meth:`parse_page` already read every page."""
        return False


@register_submitter("porsa")
class PorsaSubmitter(FormSubmitter):
    """Fills one page at a time, letting the form's own validation gate each step."""

    #: See :attr:`PorsaExtractor.nav_wait_until`.
    nav_wait_until = "commit"

    async def prepare(self, page: Any) -> None:
        try:
            await _wait_ready(
                page, timeout_ms=self.settings.ready_timeout_seconds * 1000
            )
        except CircuitError:
            # Route problem, and nothing has been POSTed yet — the base class
            # retries this answer set on a fresh circuit. Converting it to a
            # SubmissionError here would spend the persona on a bad exit IP.
            raise
        except ExtractionError as exc:
            raise SubmissionError(str(exc)) from exc

    async def fill_page(self, page: Any, answers: dict[str, str]) -> int:
        from playwright.async_api import Error as PlaywrightError

        state = await _read_state(page)
        visible = [p for p in state["pages"] if p["visible"]]
        if not visible:
            raise SubmissionError("No page is displayed; the form did not initialise.")

        filled = 0
        for entry in visible[0]["questions"]:
            if entry["hidden"]:
                continue

            question_id = entry["id"]
            answer = answers.get(question_id)

            if answer is None:
                if not entry["optional"] and (entry["optionCount"] or entry["fieldCount"]):
                    log.warning("No answer for question %s; leaving it blank.", question_id)
                continue

            try:
                if entry["listMode"]:
                    await page.locator(f"#{question_id} select").first.select_option(
                        label=answer, timeout=10_000
                    )
                    filled += 1
                elif entry["optionCount"]:
                    index = _match_option(answer, entry["options"])
                    if index is None:
                        log.warning(
                            "Answer %r matches none of %s's options %s.",
                            answer, question_id, entry["options"],
                        )
                        continue
                    await self._select_choice(page, entry["name"], index)
                    filled += 1
                elif entry["fieldCount"]:
                    await page.locator(f'[name="{entry["name"]}"]').first.fill(
                        answer, timeout=10_000
                    )
                    filled += 1
            except PlaywrightError as exc:
                log.warning("Could not fill %s: %s", question_id, exc)

        return filled

    @staticmethod
    async def _select_choice(page: Any, name: str, index: int) -> None:
        """Click the nth choice of a question, addressed by its control name.

        Addressing by ``name`` rather than the box's own ``#id`` is what lets
        this same method fill an ordinary question and one row of a matrix
        table: a matrix row has no element of its own to anchor a ``#id``
        selector on, only the ``name`` shared by that row's radio/checkbox
        inputs — and every other question already carries a ``name`` too, so
        one addressing scheme covers both shapes.

        The real ``<input>`` is ``opacity: 0`` and its ``<label>`` collapses to
        zero height, so ``span.radioCheckmark`` — the styled 25×25 control the
        user actually sees — is the only clickable target. Clicking by index
        keeps text matching out of the browser entirely.
        """
        from playwright.async_api import Error as PlaywrightError

        control = page.locator(
            f'label.radioContainer:has(input[name="{name}"]) span.radioCheckmark, '
            f'label.checkboxContainer:has(input[name="{name}"]) span.checkboxCheckmark'
        ).nth(index)

        try:
            await control.click(timeout=10_000)
        except PlaywrightError:
            # Last resort: check the input itself. The form validates on
            # `input:checked`, so this satisfies it even if the styled control
            # is obscured.
            await page.locator(f'input[name="{name}"]').nth(index).evaluate(
                "element => element.click()"
            )

    async def advance(self, page: Any) -> bool:
        from playwright.async_api import Error as PlaywrightError

        state = await _read_state(page)
        current = state["currentPage"]

        if state["hasSubmit"]:
            log.info("Page %s is the last; submitting.", current)
            button = page.locator(_SUBMIT_BUTTON)
            try:
                await button.click(timeout=15_000)
            except PlaywrightError:
                await button.evaluate("element => element.click()")
            # The submit handler fetches a fresh CSRF token, then POSTs the form
            # as a real navigation. Give both a chance to land.
            await page.wait_for_timeout(_SUBMIT_SETTLE_MS)
            return False

        if not state["hasNext"]:
            raise SubmissionError(
                f"Page {current} offers neither «بعدی» nor «ثبت نهایی». The "
                f"navigation markup has changed."
            )

        for attempt in range(1, _NAV_ATTEMPTS + 1):
            try:
                await page.locator(_NEXT_BUTTON).click(timeout=10_000)
            except PlaywrightError as exc:
                log.debug("Could not click «بعدی»: %s", exc)

            moved = await self._wait_for_page_change(page, current)
            if moved is not None:
                return True

            log.debug("Porsa stayed on page %s (attempt %d/%d).",
                      current, attempt, _NAV_ATTEMPTS)

        # `navigateToPage` returns silently when the page has a blank required
        # question, so a stuck page is a data problem, not a timing one. The
        # form marks the offenders for us.
        blocked = [
            q["id"] for p in (await _read_state(page))["pages"]
            for q in p["questions"] if q["flagged"]
        ]
        raise SubmissionError(
            f"Porsa refused to leave page {current}: "
            f"{len(blocked)} question(s) still unanswered "
            f"({', '.join(blocked[:10]) or 'none flagged'}"
            f"{'…' if len(blocked) > 10 else ''}). "
            f"Every question on this form is required — set "
            f"answering.completeness.min_ratio to 1.0 in the survey config."
        )

    @staticmethod
    async def _wait_for_page_change(page: Any, previous: int) -> int | None:
        """Poll ``#pageNumber`` until it moves. ``None`` if it never does."""
        for _ in range(_CHANGE_TIMEOUT_MS // _POLL_MS):
            await page.wait_for_timeout(_POLL_MS)
            state = await _read_state(page)
            if state["currentPage"] != previous:
                return state["currentPage"]
        return None

    async def detect_success(self, page: Any) -> bool:
        """Confirm the POST landed.

        The platform renders its confirmation server-side and ships no
        client-side marker for it, so the structural signal is the reliable one:
        a successful submit navigates away from the questionnaire, and
        ``#pageForm`` is gone. The text markers are a secondary tier for a
        confirmation rendered onto the same shell.
        """
        from playwright.async_api import Error as PlaywrightError

        for _ in range(15):
            try:
                if not await page.locator(_FORM).count():
                    return True
                body = (await page.locator("body").inner_text(timeout=2_000)) or ""
                if any(marker in body for marker in _SUCCESS_TEXTS):
                    return True
            except PlaywrightError:
                pass
            await asyncio.sleep(1)
        return False
