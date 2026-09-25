"""Porsline provider for the *grouped* renderer — matrices, groups, voice.

Why this is a second provider rather than a patch to ``porsline``
-----------------------------------------------------------------

``porsline`` was written against the **classic** renderer: one question per
page, a flat list of ``[role="radio"]`` choices, no tables. It walks the DOM to
discover the instrument. That is the right tool for the form it was built for,
and the wrong one here — this renderer breaks every assumption it makes:

* **A single-page group.** Five questions share one page inside a ``type: 8``
  group. The classic extractor climbs from a label to the nearest question
  wrapper; here that climb lands on the container holding *all five*, so every
  question comes back carrying the union of the five option lists. Run against
  this form it reported 5 questions each with 11 options — the same 11.
* **Matrix questions.** Fifteen of the twenty-one items are ``<table>`` grids
  (35×4, 6×5, 7×5 — 210 cells in total). Every cell input carries the *matrix's*
  id, so the DOM cannot say which row a cell belongs to; rows are addressable
  only by position.
* **A confirm button that looks like a submit button.** The group's «ادامه»
  carries the class ``shared_submit__``, which the classic provider treats as
  the end of the form. It stopped on page 1 of 22 and called that success.
* **Voice questions.** Six ``type: 12`` items are audio recorders. Nothing can
  be typed into them, so they are answered out of band — see
  :mod:`personabot.application.export_transcripts`.

Extraction reads the API, not the DOM
-------------------------------------

The page fetches its own definition from
``/api/surveys/<slug>/responders/`` on load, and that payload carries every
question, row, column and required-flag exactly as the author configured them.
Reading it is both more faithful and less invasive than paginating a live form:
no placeholder values are typed anywhere.

The endpoint answers **POST only** — GET returns 405 — because the same call
opens a responder session and returns the questionnaire for it. So extraction
registers one responder record, exactly as opening the form in a browser does.
That is Porsline's design, not a choice made here; it is also why extraction is
a once-per-form operation rather than something to re-run casually.

Matrix rows are flattened into ordinary single-choice questions with the id
``"<matrix id>#<row index>"``. That keeps the rest of the pipeline — matcher,
validator, completeness gate, submitter — working on the one shape it knows,
and the row index in the id is exactly what the submitter needs to find the
right table row, since the DOM will not tell it.

Facts established by walking the live form end to end (22 pages)
----------------------------------------------------------------

* A welcome page gates everything; «شروع» is a plain ``<button>``.
* The group page needs «ادامه» (``aria-label="ادامه"``) *and then* «بعدی»; the
  first click validates and relabels the button to «بازبینی» without moving.
* Every other page advances on «بعدی» (``aria-label="بعدی"``, no text).
* «بعدی» paginates past unanswered required questions, so a walk needs no
  placeholder answers. The exception is the final page, where it is disabled.
* On the last question a second «بعدی» reveals the real send button, class
  ``shared_send_button__``. That class — not ``shared_submit__`` — is the one
  that ends the form.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from personabot.domain.models import Option, Question, QuestionType
from personabot.domain.text import normalize_for_match
from personabot.errors import ExtractionError, SubmissionError
from personabot.logging_setup import get_logger
from personabot.providers.base import FormSubmitter, SchemaExtractor
from personabot.providers.registry import register_extractor, register_submitter

log = get_logger(__name__)

# --- Porsline question type codes, from the definition payload ---
_T_STATEMENT = 1
_T_TEXT = 2
_T_CHOICE = 3
_T_GROUP = 8
_T_UPLOAD = 12
_T_MATRIX = 14

# --- selectors ---
_START_BUTTON = 'button:has-text("شروع")'
_NEXT_BUTTON = 'button[aria-label="بعدی"]'
_CONFIRM_BUTTON = 'button[aria-label="ادامه"]'
# The real end-of-form button. `shared_submit__` is deliberately NOT here: the
# group's confirm button carries it, and matching it ends the run on page 1.
_SEND_BUTTON = 'button[class*="shared_send_button__"]'

# --- timings ---
_SETTLE_MS = 600
_CHANGE_TIMEOUT_MS = 12_000
_POLL_MS = 300
_NAV_ATTEMPTS = 3
_FINISH_ATTEMPTS = 6

_SUCCESS_SELECTORS = (
    "text=سپاس‌گزاریم",
    "text=سپاسگزاریم",
    "text=با تشکر",
    "text=ثبت شد",
    "text=پاسخ شما ثبت",
    'a:has-text("ساخت پرسشنامه در پُرس‌لاین")',
    "[class*='thank']",
)

_TAG = "data-pb"

# The body the page itself POSTs to open a responder session. Everything here is
# either inert or a value the browser would report anyway; `is_preview: false`
# is what makes the endpoint return the live questionnaire rather than a draft.
_SESSION_REQUEST: dict[str, Any] = {
    "variables": {"continue_token": ""},
    "device": {"resolution": "1280*720"},
    "link_referrer": 8,
    "is_preview": False,
    "auth_code": None,
    "g-recaptcha-response": None,
    "refresh_responder": False,
    "answering_started_surveys": "",
    "responded_surveys": "",
}

# Tags the current page so Playwright can address controls positionally without
# depending on CSS-module class hashes, which change on every vendor deploy.
#
# Scoping matters more than it looks: on the group page five questions share one
# container, so "the radios on this page" is not "the radios of this question".
# The climb below grows a question's box upward only while its parent still
# contains exactly one question label, which lands on the real per-question
# wrapper in both the grouped and the one-per-page layouts.
_TAGGER = """
() => {
  const vis = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const clean = (t) => (t || '').trim().replace(/\\s+/g, ' ');

  document.querySelectorAll('[data-pb-q]').forEach((el) => {
    el.removeAttribute('data-pb-q');
    el.removeAttribute('data-pb-i');
    el.removeAttribute('data-pb-row');
    el.removeAttribute('data-pb-col');
    el.removeAttribute('data-pb-kind');
  });

  const labels = [...document.querySelectorAll('label[for^="question-"]')].filter(vis);
  const questions = labels.map((label) => {
    const id = label.getAttribute('for').slice('question-'.length);

    let box = label.parentElement;
    while (box && box.parentElement &&
           box.parentElement.querySelectorAll('label[for^="question-"]').length === 1) {
      box = box.parentElement;
    }
    if (!box) box = label.parentElement;

    const table = box.querySelector('table');
    if (table) {
      const cols = [...table.querySelectorAll('thead th')].map((th) => clean(th.innerText));
      const rows = [...table.querySelectorAll('tbody tr')].map((tr, r) => {
        const radios = [...tr.querySelectorAll('[role="radio"]')];
        radios.forEach((radio, c) => {
          radio.setAttribute('data-pb-q', id);
          radio.setAttribute('data-pb-row', String(r));
          radio.setAttribute('data-pb-col', String(c));
        });
        const titleCell = tr.querySelector('td');
        return {
          title: clean(titleCell ? titleCell.innerText : ''),
          cells: radios.length,
          answered: radios.some((x) => x.getAttribute('aria-checked') === 'true'),
        };
      });
      return { id, kind: 'matrix', cols, rows };
    }

    const radios = [...box.querySelectorAll('[role="radio"]')].filter(vis);
    if (radios.length) {
      const options = radios.map((radio, i) => {
        radio.setAttribute('data-pb-q', id);
        radio.setAttribute('data-pb-i', String(i));
        const own = radio.querySelector('[class*="choice_label"]');
        // The first choice's aria-label repeats the question stem; the visible
        // text does not, so prefer it and fall back only when it is empty.
        return clean(own ? own.innerText : radio.innerText).replace(/^[0-9\\u06F0-\\u06F9]+\\s*/, '');
      });
      return {
        id, kind: 'choice', options,
        answered: radios.some((x) => x.getAttribute('aria-checked') === 'true'),
      };
    }

    const fields = [...box.querySelectorAll(
      'textarea, input:not([type=hidden]):not([type=radio]):not([type=checkbox]):not([type=button])'
    )].filter(vis);
    if (fields.length) {
      fields.forEach((field, i) => {
        field.setAttribute('data-pb-q', id);
        field.setAttribute('data-pb-i', String(i));
      });
      return {
        id, kind: 'text',
        fields: fields.map((f) => ({ type: f.type, value: f.value })),
      };
    }

    if (box.querySelector('[class*="voiceRecord"]')) return { id, kind: 'voice' };
    return { id, kind: 'other' };
  });

  const buttons = [...document.querySelectorAll('button')].filter(vis);
  const byClass = (re) => buttons.find((b) => re.test(String(b.className)));
  const next = buttons.find((b) => b.getAttribute('aria-label') === 'بعدی');
  const confirm = buttons.find((b) => b.getAttribute('aria-label') === 'ادامه');

  return {
    signature: questions.map((q) => q.id).join(','),
    questions,
    hasStart: buttons.some((b) => clean(b.innerText) === 'شروع'),
    hasSend: !!byClass(/shared_send_button__/),
    hasConfirm: !!confirm && !confirm.disabled,
    nextEnabled: !!next && !next.disabled,
  };
}
"""


def api_url_for(form_url: str) -> str:
    """The definition endpoint behind a public Porsline form URL.

    ``https://survey.porsline.ir/s/Ub25MG8N``
    → ``https://survey.porsline.ir/api/surveys/Ub25MG8N/responders/``
    """
    parts = urlparse(form_url)
    if not parts.scheme or not parts.netloc:
        raise ExtractionError(
            f"BASE_FORM_URL must be an absolute URL, got {form_url!r}. "
            f"Add the https:// scheme."
        )
    segments = [segment for segment in parts.path.split("/") if segment]
    if len(segments) < 2 or segments[0] != "s":
        raise ExtractionError(
            f"Cannot read a survey slug from {form_url!r}; expected a path like /s/<slug>."
        )
    return f"{parts.scheme}://{parts.netloc}/api/surveys/{segments[1]}/responders/"


def strip_html(raw: str | None) -> str:
    """Question titles arrive as HTML fragments."""
    text = re.sub(r"<br\s*/?>", " ", raw or "", flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&zwnj;", "‌")
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return re.sub(r"\s+", " ", text).strip()


def matrix_row_id(matrix_id: str | int, row_index: int) -> str:
    """The flattened id for one matrix row.

    The row index is carried in the id because the rendered DOM does not carry
    it: every cell input in a matrix repeats the *matrix's* id, so position is
    the only handle the submitter has.
    """
    return f"{matrix_id}#{row_index:02d}"


def split_matrix_row_id(question_id: str) -> tuple[str, int] | None:
    """Inverse of :func:`matrix_row_id`, or ``None`` for a plain question."""
    matrix_id, separator, row = question_id.partition("#")
    if not separator or not row.isdigit():
        return None
    return matrix_id, int(row)


def questions_from_definition(payload: dict[str, Any]) -> list[Question]:
    """Flatten a Porsline definition payload into the pipeline's schema shape."""
    raw_questions = (payload.get("survey") or {}).get("questions")
    if not isinstance(raw_questions, list) or not raw_questions:
        raise ExtractionError(
            "The definition payload carried no questions. Either the survey is "
            "closed or the response shape has changed."
        )

    questions: list[Question] = []
    for entry in raw_questions:
        kind = entry.get("type")
        question_id = str(entry.get("id"))
        title = strip_html(entry.get("title"))

        if kind in (_T_STATEMENT, _T_GROUP):
            # A welcome screen or a group header. It owns an id but asks
            # nothing, so admitting it would send it to the LLM and count it
            # against the completeness gate.
            log.info("Skipping %s page %s — %r", "group" if kind == _T_GROUP else "statement",
                     question_id, title[:60])
            continue

        if kind == _T_TEXT:
            questions.append(Question(question_id, title, QuestionType.TEXT))

        elif kind == _T_CHOICE:
            options = tuple(
                Option(text=str(choice.get("name", "")), value=str(choice.get("name", "")))
                for choice in entry.get("choices") or []
                if str(choice.get("name", "")).strip()
            )
            if not options:
                raise ExtractionError(
                    f"Choice question {question_id} ({title[:40]!r}) has no options."
                )
            questions.append(Question(question_id, title, QuestionType.RADIO, options))

        elif kind == _T_UPLOAD:
            # An audio recorder. Answered as prose and exported to a file; the
            # submitter leaves the control alone.
            questions.append(Question(question_id, title, QuestionType.VOICE))

        elif kind == _T_MATRIX:
            rows = entry.get("rows") or []
            if not rows:
                raise ExtractionError(f"Matrix {question_id} ({title[:40]!r}) has no rows.")
            for index, row in enumerate(rows):
                options = tuple(
                    Option(text=str(choice.get("name", "")), value=str(choice.get("name", "")))
                    for choice in row.get("choices") or []
                )
                if not options:
                    raise ExtractionError(
                        f"Matrix {question_id} row {index} has no columns."
                    )
                questions.append(
                    Question(
                        question_id=matrix_row_id(question_id, index),
                        question_text=strip_html(row.get("title")),
                        type=QuestionType.RADIO,
                        options=options,
                    )
                )

        else:
            log.warning(
                "Question %s has unsupported Porsline type %r (%r); skipping it. "
                "It will show up as an unassigned id if the spec expects it.",
                question_id, kind, title[:50],
            )

    return questions


async def _read_state(page: Any) -> dict[str, Any]:
    return await page.evaluate(_TAGGER)


async def _wait_for_change(page: Any, signature: str) -> dict[str, Any] | None:
    """Wait until the visible question set changes. ``None`` if it never does."""
    for _ in range(_CHANGE_TIMEOUT_MS // _POLL_MS):
        await page.wait_for_timeout(_POLL_MS)
        state = await _read_state(page)
        if state["signature"] != signature or state["hasSend"]:
            await page.wait_for_timeout(_SETTLE_MS)
            return await _read_state(page)
    return None


async def _click_start(page: Any) -> bool:
    """Get past the welcome page. True once a question is showing."""
    state = await _read_state(page)
    if not state["hasStart"]:
        return bool(state["questions"])

    from playwright.async_api import Error as PlaywrightError

    log.info("Welcome page: clicking «شروع».")
    for attempt in range(1, _NAV_ATTEMPTS + 1):
        try:
            await page.locator(_START_BUTTON).first.click(force=True, timeout=15_000)
        except PlaywrightError as exc:
            log.debug("Could not click «شروع»: %s", exc)
        if await _wait_for_change(page, state["signature"]) is not None:
            return True
        log.debug("«شروع» did not take (attempt %d/%d).", attempt, _NAV_ATTEMPTS)
    return False


@register_extractor("porsline_v2")
class PorslineV2Extractor(SchemaExtractor):
    """Reads the whole instrument from the definition endpoint in one request.

    It still runs inside the page-walking template, but as a single "page": the
    definition is fetched in :meth:`prepare`, returned by :meth:`parse_page`,
    and :meth:`advance` immediately ends the walk. Nothing is typed into the
    live form.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._payload: dict[str, Any] | None = None

    async def prepare(self, page: Any) -> None:
        url = api_url_for(self.workspace.require_form_url())
        log.info("Fetching the survey definition from %s", url)

        # The page has already POSTed this endpoint to open a responder session,
        # so a GET on the same context returns the definition for that session.
        response = await page.request.post(url, data=_SESSION_REQUEST, timeout=60_000)
        if not response.ok:
            raise ExtractionError(
                f"{url} returned HTTP {response.status}. The survey may be closed, "
                f"the slug wrong, or the endpoint may have moved. (It answers POST "
                f"only; a 405 means the request method is no longer what it wants.)"
            )
        payload = await response.json()
        if not isinstance(payload, dict):
            raise ExtractionError(
                f"{url} returned {type(payload).__name__}, expected a JSON object."
            )
        self._payload = payload

        survey = payload.get("survey") or {}
        log.info(
            "Survey %r (id=%s): %d top-level item(s).",
            survey.get("name"), survey.get("id"), len(survey.get("questions") or []),
        )

    async def parse_page(self, page: Any) -> list[Question]:
        if self._payload is None:
            raise ExtractionError("prepare() did not run; no definition to parse.")
        questions = questions_from_definition(self._payload)

        kinds: dict[str, int] = {}
        for question in questions:
            kinds[str(question.type)] = kinds.get(str(question.type), 0) + 1
        log.info("Flattened to %d answerable question(s): %s", len(questions), kinds)
        return questions

    async def advance(self, page: Any) -> bool:
        return False


@register_submitter("porsline_v2")
class PorslineV2Submitter(FormSubmitter):
    """Fills the rendered form page by page.

    Matrix cells are addressed by ``(row, column)`` position — see
    :func:`matrix_row_id` for why there is no id to use instead — and every
    control is reached through the ``data-pb-*`` attributes the tagger writes,
    so a vendor redeploy that rehashes the CSS-module class names does not
    break the selectors.
    """

    async def prepare(self, page: Any) -> None:
        if not await _click_start(page):
            raise SubmissionError(
                "Clicked «شروع» but no question rendered. The welcome page markup "
                "has probably changed."
            )

    async def fill_page(self, page: Any, answers: dict[str, str]) -> int:
        state = await _read_state(page)
        filled = 0

        for entry in state["questions"]:
            match entry["kind"]:
                case "matrix":
                    filled += await self._fill_matrix(page, entry, answers)
                case "choice":
                    filled += await self._fill_choice(page, entry, answers)
                case "text":
                    filled += await self._fill_text(page, entry, answers)
                case "voice":
                    # Answered as text and exported to a transcript file; there
                    # is nothing here a browser can fill.
                    log.debug("Leaving voice question %s empty (optional).", entry["id"])
                case _:
                    log.debug("Nothing to fill for question %s.", entry["id"])

        return filled

    async def _fill_matrix(
        self, page: Any, entry: dict[str, Any], answers: dict[str, str]
    ) -> int:
        matrix_id = entry["id"]
        columns = [normalize_for_match(col) for col in entry["cols"]]
        filled = 0

        for row_index, row in enumerate(entry["rows"]):
            if row.get("answered"):
                continue
            question_id = matrix_row_id(matrix_id, row_index)
            answer = answers.get(question_id)
            if answer is None:
                log.warning(
                    "%s: no answer for matrix row %s (%r).",
                    self.name, question_id, row.get("title", "")[:40],
                )
                continue

            column_index = self._column_index(answer, columns)
            if column_index is None:
                log.warning(
                    "%s: answer %r for %s matches none of the columns %s.",
                    self.name, answer, question_id, entry["cols"],
                )
                continue
            if column_index >= row.get("cells", 0):
                log.warning(
                    "%s: column %d is out of range for row %s (%d cells).",
                    self.name, column_index, question_id, row.get("cells", 0),
                )
                continue

            cell = page.locator(
                f'[{_TAG}-q="{matrix_id}"][{_TAG}-row="{row_index}"]'
                f'[{_TAG}-col="{column_index}"]'
            ).first
            if await self._click(cell, question_id):
                filled += 1

        # Check the DOM instead of trusting the click count. A radio that failed
        # to register is the one failure mode that would otherwise reach the
        # platform as a required field left blank — and the form then refuses to
        # submit, several pages later, with nothing pointing back to the cause.
        after = await _read_state(page)
        current = next(
            (q for q in after["questions"] if q["id"] == matrix_id and q["kind"] == "matrix"),
            None,
        )
        if current is not None:
            blank = [
                index for index, row in enumerate(current["rows"]) if not row.get("answered")
            ]
            if blank:
                shown = ", ".join(str(i) for i in blank[:12])
                log.warning(
                    "%s: matrix %s still has %d unanswered row(s) after filling: %s%s",
                    self.name, matrix_id, len(blank), shown,
                    "…" if len(blank) > 12 else "",
                )

        return filled

    @staticmethod
    def _column_index(answer: str, columns: list[str]) -> int | None:
        """Which column a stored answer refers to.

        Phase 3 snaps answers onto the schema's own option strings, so exact
        equality after normalization is the expected path. Matrix column labels
        are short ("0", "3+/4+"), which makes fuzzy matching more dangerous than
        useful here — a near miss silently records the wrong score — so an
        unmatched answer is reported rather than guessed at.
        """
        target = normalize_for_match(answer)
        for index, column in enumerate(columns):
            if column and column == target:
                return index
        return None

    async def _fill_choice(
        self, page: Any, entry: dict[str, Any], answers: dict[str, str]
    ) -> int:
        question_id = entry["id"]
        if entry.get("answered"):
            return 0
        answer = answers.get(question_id)
        if answer is None:
            log.debug("%s: no answer for choice question %s.", self.name, question_id)
            return 0

        target = normalize_for_match(answer)
        options = [normalize_for_match(option) for option in entry["options"]]

        index = next((i for i, option in enumerate(options) if option == target), None)
        if index is None:
            index = self._fuzzy_index(target, options)
        if index is None:
            log.warning(
                "%s: answer %r for %s matches none of %s.",
                self.name, answer, question_id, entry["options"],
            )
            return 0

        radio = page.locator(f'[{_TAG}-q="{question_id}"][{_TAG}-i="{index}"]').first
        return 1 if await self._click(radio, question_id) else 0

    @staticmethod
    def _fuzzy_index(target: str, options: list[str]) -> int | None:
        """Last-resort match for a choice label that drifted since extraction.

        Only used for prose-length options, never for matrix columns.
        """
        from thefuzz import fuzz

        best_index, best_score = None, 85
        for index, option in enumerate(options):
            if not option:
                continue
            score = int(fuzz.ratio(target, option))
            if score > best_score:
                best_index, best_score = index, score
        return best_index

    async def _fill_text(
        self, page: Any, entry: dict[str, Any], answers: dict[str, str]
    ) -> int:
        question_id = entry["id"]
        answer = answers.get(question_id)
        if answer is None or not str(answer).strip():
            return 0
        fields = entry.get("fields") or []
        if not fields:
            return 0
        if fields[0].get("value"):
            return 0

        field = page.locator(f'[{_TAG}-q="{question_id}"][{_TAG}-i="0"]').first
        from playwright.async_api import Error as PlaywrightError

        try:
            await field.fill(str(answer), timeout=15_000)
            return 1
        except PlaywrightError as exc:
            log.warning("%s: could not type into %s: %s", self.name, question_id, exc)
            return 0

    async def _click(self, locator: Any, question_id: str) -> bool:
        """Select a choice by dispatching the click rather than aiming at it.

        The radio is an ``<input>`` hidden behind a styled ``<span>``: it has no
        usable hit box, and a matrix is both wider and taller than the viewport,
        so Playwright's real mouse click fails with "Element is outside of the
        viewport" — even with ``force=True``, which skips the actionability
        checks but still needs a point to aim at. Scrolling does not help,
        because the obstacle is the element's own geometry, not the page's.

        ``dispatch_event`` sends a bubbling DOM click straight to the element,
        which React's delegated listener handles exactly like a user's, and it
        costs no actionability wait — which matters when one page holds 35 of
        them. Whether the click actually *registered* is then checked against
        the DOM by the caller rather than assumed.
        """
        from playwright.async_api import Error as PlaywrightError

        try:
            await locator.dispatch_event("click", timeout=10_000)
            return True
        except PlaywrightError as exc:
            log.warning("%s: could not click %s: %s", self.name, question_id, exc)
            return False

    async def advance(self, page: Any) -> bool:
        state = await _read_state(page)

        if state["hasSend"]:
            await self._finish(page)
            return False

        if state["hasConfirm"]:
            # «ادامه» validates the group and stays on the same page, relabelling
            # its own button to «بازبینی»; «بعدی» is what actually turns it.
            #
            # So this click must NOT wait for the question set to change. An
            # earlier version reused the retrying navigation helper here, which
            # waited 12s for a change that never comes, gave up, and clicked
            # «ادامه» twice more — and those surplus clicks landed on the
            # relabelled button and jumped the form forward, silently skipping
            # the whole first matrix. 35 required cells went unanswered and
            # nothing in the log said so.
            await self._click_once(page, _CONFIRM_BUTTON)
            after = await _read_state(page)
            if after["hasSend"]:
                await self._finish(page)
                return False
            if after["signature"] != state["signature"]:
                return True
            state = after

        if not state["nextEnabled"]:
            log.info("«بعدی» is disabled and no send button is showing; stopping.")
            return False

        moved = await self._click_and_settle(page, _NEXT_BUTTON, state["signature"])
        if not moved:
            after = await _read_state(page)
            if after["hasSend"]:
                await self._finish(page)
            return False
        return True

    async def _click_once(self, page: Any, selector: str) -> None:
        """Click a button that is not expected to turn the page."""
        from playwright.async_api import Error as PlaywrightError

        try:
            await page.locator(selector).first.click(force=True, timeout=15_000)
        except PlaywrightError as exc:
            log.debug("Could not click %s: %s", selector, exc)
        await page.wait_for_timeout(_SETTLE_MS)

    async def _click_and_settle(self, page: Any, selector: str, signature: str) -> bool:
        """Click a navigation button, retrying the clicks Porsline drops.

        Clicks landing mid-transition are silently ignored by the SPA, so this
        waits for the visible question set to actually change rather than
        trusting a sleep — otherwise the walk clicks "next" over a page it never
        filled.
        """
        from playwright.async_api import Error as PlaywrightError

        for attempt in range(1, _NAV_ATTEMPTS + 1):
            target = page.locator(selector).first
            try:
                await target.click(force=True, timeout=15_000)
            except PlaywrightError as exc:
                log.debug("Could not click %s: %s", selector, exc)

            if await _wait_for_change(page, signature) is not None:
                return True
            log.debug(
                "Porsline ignored the %s click (attempt %d/%d).",
                selector, attempt, _NAV_ATTEMPTS,
            )
        return False

    async def _finish(self, page: Any) -> None:
        """Click through the end-of-form buttons until the platform confirms.

        The last page shows «بازبینی» (review) before the final send, and the
        vendor has changed how many steps sit between the two. Clicking whatever
        send button is present and re-checking is more durable than encoding a
        fixed number of screens.
        """
        from playwright.async_api import Error as PlaywrightError

        for attempt in range(1, _FINISH_ATTEMPTS + 1):
            if await self.detect_success(page):
                return
            button = page.locator(_SEND_BUTTON).first
            if not await button.count():
                return
            label = ""
            try:
                label = (await button.inner_text()).strip()
            except PlaywrightError:
                pass
            log.info("%s: clicking the send button (%r), step %d.", self.name, label, attempt)
            try:
                await button.click(force=True, timeout=20_000)
            except PlaywrightError as exc:
                log.warning("%s: send click failed: %s", self.name, exc)
                return
            await page.wait_for_timeout(2_500)

    async def detect_success(self, page: Any) -> bool:
        for selector in _SUCCESS_SELECTORS:
            try:
                if await page.locator(selector).first.is_visible(timeout=1_500):
                    return True
            except Exception:  # noqa: BLE001 — a missing selector is not an error
                continue
        return False
