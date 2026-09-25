"""The Porsline provider, bound to the real DOM contract.

These are the regression tests for the discovery that the refactored provider
could not read this form at all: it never clicked «شروع», it looked for
navigation by button *text* when the next button is an icon carrying only
``aria-label="بعدی"``, and it resolved options against the label's nearest
``div[class*="question"]`` — a wrapper that holds the title and none of the
choices. Extraction produced either nothing or 237 optionless TEXT questions.

The page states below are verbatim shapes returned by the provider's DOM probe
during a full walk of the live form.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from personabot.domain.models import Option, Question, QuestionType
from personabot.errors import ExtractionError
from personabot.providers.porsline import (
    _CLICK_END_BUTTON,
    _END_MARK,
    _PROBE,
    PorslineExtractor,
    PorslineSubmitter,
    _match_option,
    reject_bled_options,
)


class FakePage:
    """Stands in for a Playwright page by replaying one probe result."""

    def __init__(self, state: dict) -> None:
        self.state = state

    async def evaluate(self, _script, *args):
        return self.state


def _state(**questions) -> dict:
    return {"signature": "", "questions": [], "hasStart": False,
            "hasSubmit": False, "hasStatement": False, "nextEnabled": True, **questions}


RADIO_PAGE = _state(
    signature="51531905",
    questions=[{
        "id": "51531905",
        "title": "جنسیت",
        "options": ["آقا", "خانم"],
        "answered": False,
        "inputs": [],
        "hasTable": False,
    }],
)

# A section divider. It owns a question id and a label, but asks nothing and
# advances only via its own «ادامه» button.
STATEMENT_PAGE = _state(
    signature="51536339",
    hasStatement=True,
    questions=[{
        "id": "51536339",
        "title": "لطفا با دقت به سوالات زیر پاسخ دهید.",
        "options": [],
        "answered": False,
        "inputs": [],
        "hasTable": False,
    }],
)


class TestParsePage:
    @pytest.mark.asyncio
    async def test_choices_are_extracted(self, extractor):
        questions = await extractor.parse_page(FakePage(RADIO_PAGE))

        assert len(questions) == 1
        question = questions[0]
        assert question.question_id == "51531905"
        assert question.type is QuestionType.RADIO
        assert question.option_values == ("آقا", "خانم")

    @pytest.mark.asyncio
    async def test_title_and_options_drop_the_numbering(self, extractor):
        """The DOM renders «* ۱ جنسیت جنسیت» and «۱ آقا»; neither may survive.

        A numbered option never equals what the LLM answers, so every answer
        would fall to the fuzzy tier or be discarded outright.
        """
        question = (await extractor.parse_page(FakePage(RADIO_PAGE)))[0]

        assert question.question_text == "جنسیت"
        assert question.option_values == ("آقا", "خانم")

    @pytest.mark.asyncio
    async def test_statement_pages_are_not_questions(self, extractor):
        """Emitting these would send a divider to the LLM and count it against
        the completeness gate, which no persona could then pass."""
        assert await extractor.parse_page(FakePage(STATEMENT_PAGE)) == []


class TestMatchOption:
    AGREEMENT = ["کاملا مخالفم", "مخالفم", "نظری ندارم", "موافقم", "کاملا موافقم"]

    def test_exact_answer(self):
        assert _match_option("موافقم", self.AGREEMENT) == 3

    def test_exact_wins_over_a_longer_option_containing_it(self):
        """«موافقم» is a substring of «کاملا موافقم».

        The predecessor matched by substring and could select the wrong point on
        a Likert scale — a silent, systematic data error rather than a crash.
        """
        assert _match_option("موافقم", self.AGREEMENT) == 3
        assert _match_option("کاملا موافقم", self.AGREEMENT) == 4

    def test_arabic_and_persian_letter_variants_are_folded(self):
        # Arabic yeh/kaf for the Persian ones, as models routinely emit.
        assert _match_option("نظري ندارم", self.AGREEMENT) == 2

    def test_near_miss_falls_back_to_fuzzy(self):
        assert _match_option("موافق", self.AGREEMENT) == 3

    def test_unrelated_answer_matches_nothing(self):
        assert _match_option("سیب زمینی سرخ کرده", self.AGREEMENT) is None

    def test_empty_answer_matches_nothing(self):
        assert _match_option("", self.AGREEMENT) is None


@pytest.fixture
def extractor():
    return PorslineExtractor(workspace=SimpleNamespace(settings=None), browser=None)


# --- the DOM contract for ending the form ---------------------------------
#
# These run the provider's real scripts in a real browser, because the bug they
# guard was invisible to a replayed probe result: the probe *correctly* reported
# a submit page, and the separate CSS constant used to click it matched nothing.
# Every one of 227 submissions filled all 237 questions and then died on a 10s
# `Locator.click` timeout with the response never sent.

# The live form's last page, copied from a failure receipt. It is a *statement*
# page — no `shared_submit__` class anywhere, and the button reads «پایان».
ENDING_PAGE_HTML = """
<div class="classicQuestionsList_classic_question_wrapper___nG7V">
  <div class="questionIntro_question_intro_wrapper__NsVQv">
    <h1><label for="question-52601297">
      <span class="questionIntro_question_intro_title_text__fP8aX">
        <p>ممنون از وقتی که برای پاسخ‌دهی به پرسشنامه من گذاشتید</p>
      </span>
    </label></h1>
  </div>
  <button class="styles_statement_button__yYynP false" aria-label="پایان">پایان</button>
</div>
"""

# A section divider mid-form. It must *not* read as the end of the form.
DIVIDER_PAGE_HTML = """
<div class="classicQuestionsList_classic_question_wrapper___nG7V">
  <div class="questionIntro_question_intro_wrapper__NsVQv">
    <h1><label for="question-51536339">
      <span class="questionIntro_question_intro_title_text__fP8aX">
        <p>لطفا با دقت به سوالات زیر پاسخ دهید.</p>
      </span>
    </label></h1>
  </div>
  <button class="styles_statement_button__yYynP false" aria-label="ادامه">ادامه</button>
</div>
"""

# The other layout the provider has to keep supporting.
SHARED_SUBMIT_HTML = """
<div class="classicQuestionsList_classic_question_wrapper___nG7V">
  <button class="shared_submit__aB3dE">ارسال</button>
</div>
"""

# Both endings on one page, in the order the live survey emits them. Copied from
# output/receipts — a closing *statement* card renders its «پایان» button first,
# and the button that actually posts the response comes after it.
CLOSING_STATEMENT_WITH_SUBMIT_HTML = """
<div class="classicQuestionsList_classic_question_wrapper___nG7V">
  <div class="questionIntro_question_intro_wrapper__NsVQv">
    <h1><label for="question-51536340">
      <span class="questionIntro_question_intro_title_text__fP8aX">
        <p>ممنون از وقتی که برای پاسخ‌دهی به پرسشنامه من گذاشتید</p>
      </span>
    </label></h1>
  </div>
  <button class="styles_statement_button__yYynP false" aria-label="پایان">پایان</button>
  <button class="shared_confirm_button__g_OvU shared_send_button__Q83So shared_submit__7OvzI false">ارسال</button>
</div>
"""


@pytest.fixture
async def dom():
    """Serve a snippet of Porsline markup from a real browser page."""
    playwright = pytest.importorskip("playwright.async_api")

    async with playwright.async_playwright() as p:
        try:
            browser = await p.chromium.launch()
        except playwright.Error as exc:  # browsers not installed
            pytest.skip(f"chromium unavailable: {exc}")
        page = await browser.new_page()

        async def load(html: str):
            await page.set_content(f"<body>{html}</body>")
            return page

        yield load
        await browser.close()


class TestEndOfForm:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("html", "expected"),
        [
            (ENDING_PAGE_HTML, "پایان"),
            (SHARED_SUBMIT_HTML, "ارسال"),
            (CLOSING_STATEMENT_WITH_SUBMIT_HTML, "ارسال"),
        ],
        ids=[
            "statement-ending-page",
            "shared_submit-button",
            "closing-statement-above-submit",
        ],
    )
    async def test_the_button_the_probe_reports_is_the_button_we_click(
        self, dom, html, expected
    ):
        """The regression: probe and click must never disagree.

        `hasSubmit` used to be answered by one rule and the click by another, so
        the live form's «پایان» ending page was reported as submittable and then
        clicked with a selector matching zero elements.
        """
        page = await dom(html)

        assert (await page.evaluate(_PROBE))["hasSubmit"] is True
        assert await page.evaluate(_CLICK_END_BUTTON, _END_MARK) is True

        clicked = page.locator(f"[{_END_MARK}]")
        assert await clicked.count() == 1
        assert (await clicked.first.inner_text()).strip() == expected

    @pytest.mark.asyncio
    async def test_a_closing_statement_never_wins_over_the_send_button(self, dom):
        """The regression that made every submission a silent no-op.

        On a survey ending with a thank-you *statement* card, «پایان» is emitted
        before the send button and matches the end-text rule, so a single flat
        `find` returned it. Clicking it posts nothing and cannot clear
        `hasSubmit` — the send button it skipped is still on the page — so the
        run filled all 62 answers, clicked «پایان» until it ran out of retries,
        and reported "no success confirmation".
        """
        page = await dom(CLOSING_STATEMENT_WITH_SUBMIT_HTML)

        assert await page.evaluate(_CLICK_END_BUTTON, _END_MARK) is True

        marked = page.locator(f"[{_END_MARK}]")
        assert await marked.count() == 1
        assert "shared_send_button__" in await marked.first.get_attribute("class")

    @pytest.mark.asyncio
    async def test_a_mid_form_divider_is_not_the_end(self, dom):
        """«ادامه» shares the ending page's button class; only the text differs.

        Reading it as the end would submit ~200 questions early.
        """
        state = await (await dom(DIVIDER_PAGE_HTML)).evaluate(_PROBE)

        assert state["hasSubmit"] is False
        assert state["hasStatement"] is True

    @pytest.mark.asyncio
    async def test_clicking_reports_failure_when_nothing_ends_the_form(self, dom):
        """A false return is what lets `_finish` tell "already gone" from "stuck"."""
        page = await dom('<button aria-label="بعدی"></button>')

        assert await page.evaluate(_CLICK_END_BUTTON, _END_MARK) is False


# --- the DOM contract for question scoping --------------------------------
#
# The bug these guard: `survey.porsline.ir/s/NbFZTHqO` renders its «تعارض
# والدین» section as a *single page display* group — one
# `classic_question_wrapper__` holding a section heading and 30 items. Resolving
# options against that wrapper gave every item all 150 of the group's radios, so
# `make schema` recorded thirty 150-option questions (`make inspect` reported a
# "K=150" signature), and the submitter would have clicked the index-th radio of
# the whole group rather than of the row it was answering.


def _choice(text: str) -> str:
    return (
        '<div role="radio" aria-checked="false">'
        f'<span class="choice_choice_label__Jd7Kq">{text}</span></div>'
    )


def _intro(question_id: str, prefix: str, text: str) -> str:
    """The heading block every question and group carries.

    The «* ۱» numbering sits *outside* `title_text`, which is why the probe
    reads the span rather than the label.
    """
    return (
        '<div class="questionIntro_question_intro_wrapper__NsVQv"><div>'
        f'<h1 class="questionIntro_question_intro_title__FkcLg">'
        f'<label for="question-{question_id}">{prefix}'
        f'<span class="questionIntro_question_intro_title_text__fP8aX"><p>{text}</p></span>'
        "</label></h1></div></div>"
    )


_AGREE5 = ["کاملاً مخالفم", "مخالفم", "نظری ندارم", "موافقم", "کاملاً موافقم"]


def _grouped_item(question_id: str, number: str, text: str) -> str:
    choices = "".join(_choice(option) for option in _AGREE5)
    return (
        "<div><div>"
        '<div class="style_multiple_choice_question_wrapper__hjMgw'
        ' style_single_page_view__rSntf">'
        f"{_intro(question_id, f'* {number} ', text)}{choices}"
        "</div></div></div>"
    )


# The confirm button a group renders at the foot of its own section. Copied
# from the live DOM: it reads «شروع», exactly like the welcome page's start
# button, and carries `shared_submit__` — which is what made the end-of-form
# rule fire 31 pages early.
_GROUP_CONFIRM_BUTTON = (
    "<div><button "
    'class="shared_confirm_button__g_OvU shared_submit__7OvzI false" '
    'aria-label="شروع">شروع</button></div>'
)

# Two items instead of thirty; the wrapper holding more than one question is the
# whole of what makes the layout different.
GROUPED_PAGE_HTML = (
    '<div class="classicQuestionsList_classic_question_wrapper___nG7V">'
    '<div class="singlePageDisplay_question_wrapper__BGxMe">'
    '<div><div class="singlePageDisplay_group_intro_wrapper__9dM04'
    ' singlePageDisplay_has_media__OgtaE">'
    + _intro("53774815", "", "تعارض والدین")
    + "</div></div>"
    + _grouped_item("53774823", "۱", "والدینم اغلب بر سر مسائل کوچک با هم مشاجره می‌کنند.")
    + _grouped_item("53774824", "۲", "در خانه، چند بار در هفته شاهد دعوای لفظی والدینم هستم.")
    + _GROUP_CONFIRM_BUTTON
    + '</div><button aria-label="بعدی"></button></div>'
)

# The same group with its confirm button relabelled by the survey author, so the
# «شروع» text rule cannot fire. Only "this button lives inside the group" is
# left to tell it from a send button.
GROUPED_PAGE_RELABELLED_HTML = GROUPED_PAGE_HTML.replace(
    'aria-label="شروع">شروع<', 'aria-label="ثبت این بخش">ثبت این بخش<'
)

# The classic layout, where the choices really are siblings of the intro wrapper
# and the wrapper really is the right box. Kept alongside so a fix for the group
# case cannot quietly regress it.
SINGLE_QUESTION_PAGE_HTML = (
    '<div class="classicQuestionsList_classic_question_wrapper___nG7V">'
    + _intro("53775068", "* ۱ ", "وضعیت مالی نامساعد می تواند بر گرایش نوجوانان تاثیر بگذارد")
    + "".join(_choice(option) for option in _AGREE5)
    + '<button aria-label="بعدی"></button></div>'
)


class TestQuestionScoping:
    @pytest.mark.asyncio
    async def test_a_grouped_page_gives_each_item_only_its_own_choices(
        self, dom, extractor
    ):
        questions = await extractor.parse_page(await dom(GROUPED_PAGE_HTML))

        assert [q.question_id for q in questions] == ["53774823", "53774824"]
        for question in questions:
            assert question.type is QuestionType.RADIO
            assert question.option_values == tuple(_AGREE5)

    @pytest.mark.asyncio
    async def test_a_group_heading_is_not_a_question(self, dom, extractor):
        """It owns a question id and a label but asks nothing.

        Letting it through cost a schema row that no persona could answer and
        that still counted against the completeness gate.
        """
        ids = [q.question_id for q in await extractor.parse_page(await dom(GROUPED_PAGE_HTML))]

        assert "53774815" not in ids

    @pytest.mark.asyncio
    async def test_the_classic_one_question_page_still_resolves_to_the_wrapper(
        self, dom, extractor
    ):
        """The original fact: the choices are the intro wrapper's *siblings*.

        Scoping to the label's nearest box would find zero options here.
        """
        questions = await extractor.parse_page(await dom(SINGLE_QUESTION_PAGE_HTML))

        assert len(questions) == 1
        assert questions[0].question_text.startswith("وضعیت مالی")
        assert questions[0].option_values == tuple(_AGREE5)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "html",
        [GROUPED_PAGE_HTML, GROUPED_PAGE_RELABELLED_HTML],
        ids=["confirm-reads-shoroo", "confirm-relabelled"],
    )
    async def test_a_group_confirm_button_is_not_the_end_of_the_form(self, dom, html):
        """The regression that hid a third of the instrument.

        A «single page display» group renders its own confirm button at the
        foot of the section, and that button carries `shared_submit__` — the
        class tier's marker for "this posts the response". On
        `survey.porsline.ir/s/NbFZTHqO` the group is page 39 of 70, so phase 1
        stopped there and wrote 65 of the survey's 94 questions, while phase 4
        filled those 65, clicked the confirm button, watched the form move one
        page, and reported a submission that was never sent.

        Both tiers are exercised: the live button reads «شروع», and the
        relabelled variant is caught only by its position inside the group.
        """
        page = await dom(html)

        state = await page.evaluate(_PROBE)

        assert state["hasSubmit"] is False
        assert state["nextEnabled"] is True
        assert await page.evaluate(_CLICK_END_BUTTON, _END_MARK) is False

    @pytest.mark.asyncio
    async def test_a_group_confirm_button_is_not_the_welcome_page(self, dom):
        """`hasStart` decides whether `prepare` waits for a question to render.

        The group's confirm button carries the welcome button's exact label, so
        a text-only test reports a start button on a page 39 questions in.
        """
        state = await (await dom(GROUPED_PAGE_HTML)).evaluate(_PROBE)

        assert state["hasStart"] is False

    @pytest.mark.asyncio
    async def test_selecting_a_choice_clicks_the_right_row(self, dom):
        """`_select_choice` clicks by index, so it must share the probe's box.

        With the wrapper as the box, index 3 of item ۲ was radio 3 of the
        group's ten — a choice belonging to item ۱.
        """
        page = await dom(GROUPED_PAGE_HTML)
        await page.evaluate(
            """() => {
                window.__clicked = null;
                document.addEventListener('click', (event) => {
                    window.__clicked = event.target.closest('[role="radio"]');
                }, true);
            }"""
        )

        await PorslineSubmitter._select_choice(page, "53774824", 3)

        where = await page.evaluate(
            """() => {
                const item = document.querySelector('label[for="question-53774824"]')
                    .closest('[class*="style_multiple_choice_question_wrapper__"]');
                const all = [...document.querySelectorAll('[role="radio"]')];
                const mine = [...item.querySelectorAll('[role="radio"]')];
                return {global: all.indexOf(window.__clicked),
                        local: mine.indexOf(window.__clicked)};
            }"""
        )

        assert where["local"] == 3          # the 4th choice of item ۲
        assert where["global"] == 8         # not the 4th radio on the page


# ------------------------------------------------ dropdowns (from the behnaz fork)
#
# «ساکن کدام استان هستید؟» is a Downshift combobox. Its search <input> made the
# probe report a text question, so the submitter `fill()`-ed the province name
# into the filter, selected nothing, and every submission bounced back to this
# required question at the end with «۹۸٪ پاسخ داده‌اید». The markup outside the
# script is copied from that failure receipt; the script stands in for
# Downshift, which only mounts options while the menu is open and records a
# value only when one is clicked.
COMBOBOX_HTML = """
<div class="classicQuestionsList_classic_question_wrapper___nG7V">
 <div class="styles_dropdown_wrapper__TD_oW" id="downshift-9-menu" role="listbox">
  <div class="questionIntro_question_intro_wrapper__NsVQv">
    <h1 id="question-title-45901809"><label for="question-45901809">
      <span class="questionIntro_question_intro_title_text__fP8aX"><p>ساکن کدام استان هستید؟</p></span>
    </label></h1>
  </div>
  <div class="styles_container__Ya2ZD">
    <div class="styles_selector__yk_u2" role="combobox" aria-haspopup="listbox"
         aria-owns="downshift-9-menu" aria-expanded="false">
      <span><input id="question-45901809" aria-controls="downshift-9-menu"
                   autocomplete="off" value=""></span>
    </div>
    <ul id="downshift-9-items"></ul>
  </div>
 </div>
</div>
<script>
  const PROVINCES = ['تهران', 'خراسان رضوی', 'خوزستان', 'فارس'];
  const input = document.getElementById('question-45901809');
  const list = document.getElementById('downshift-9-items');
  window.selected = null;
  const render = () => {
    list.innerHTML = '';
    PROVINCES.filter((p) => p.includes(input.value)).forEach((p, i) => {
      const li = document.createElement('li');
      li.id = 'downshift-9-item-' + i;
      li.setAttribute('role', 'option');
      // Numbered by position in the full list, as the live form renders «۸. تهران».
      const n = String(PROVINCES.indexOf(p) + 1).replace(/[0-9]/g, (d) => '۰۱۲۳۴۵۶۷۸۹'[d]);
      li.textContent = n + '. ' + p;
      li.addEventListener('click', () => { window.selected = p; input.value = p; list.innerHTML = ''; });
      list.appendChild(li);
    });
  };
  input.addEventListener('click', render);
  input.addEventListener('input', render);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') list.innerHTML = ''; });
</script>
"""


class TestCombobox:
    @pytest.mark.asyncio
    async def test_probe_reports_a_dropdown_not_a_text_question(self, dom):
        page = await dom(COMBOBOX_HTML)

        (question,) = (await page.evaluate(_PROBE))["questions"]
        assert question["combobox"] is True

    @pytest.mark.asyncio
    async def test_extraction_reads_the_menu_as_select_options(self, dom, extractor):
        page = await dom(COMBOBOX_HTML)

        (question,) = await extractor.parse_page(page)
        assert question.type is QuestionType.SELECT
        assert question.option_values == ("تهران", "خراسان رضوی", "خوزستان", "فارس")
        assert await page.evaluate("window.selected") is None

    @pytest.mark.asyncio
    async def test_submitter_clicks_the_option_instead_of_typing(self, dom):
        page = await dom(COMBOBOX_HTML)

        filled = await PorslineSubmitter(SimpleNamespace(settings=None), None).fill_page(page, {"45901809": "خوزستان"})

        assert filled == 1
        assert await page.evaluate("window.selected") == "خوزستان"

    @pytest.mark.asyncio
    async def test_a_short_answer_matches_its_numbered_option(self, dom):
        """«تهران» scored 77 against «۸. تهران» and was rejected; every
        submission with a short province name then died on this question."""
        page = await dom(COMBOBOX_HTML)

        assert await PorslineSubmitter._select_from_combobox(page, "45901809", "تهران")
        assert await page.evaluate("window.selected") == "تهران"

    @pytest.mark.asyncio
    async def test_a_filter_that_hides_the_answer_falls_back_to_the_full_list(self, dom):
        page = await dom(COMBOBOX_HTML)

        # Arabic yeh: the page's substring filter finds nothing, the matcher folds it.
        assert await PorslineSubmitter._select_from_combobox(page, "45901809", "خراسان رضوي")
        assert await page.evaluate("window.selected") == "خراسان رضوی"

    @pytest.mark.asyncio
    async def test_an_unknown_answer_selects_nothing(self, dom):
        page = await dom(COMBOBOX_HTML)

        assert not await PorslineSubmitter._select_from_combobox(page, "45901809", "آلاسکا")
        assert await page.evaluate("window.selected") is None


# ------------------------------------------- a group header sharing a wrapper
#
# The other multi-label layout, met in the behnaz fork: a group's header label
# and its first question's label share one classic wrapper, and the question's
# radios are siblings of its intro box — so the narrowest single-label box
# holds no radios at all. The merged rule must fall back to "controls between
# this label and the next" here, while still using per-item boxes for a
# «single page display» group.

HEADER_SHARING_WRAPPER_HTML = (
    '<div class="classicQuestionsList_classic_question_wrapper___nG7V">'
    + _intro("60000001", "", "بخش دوم: رضایت شغلی")
    + _intro("60000002", "* ۱ ", "از کارم راضی هستم.")
    + "".join(_choice(option) for option in _AGREE5)
    + '<button aria-label="بعدی"></button></div>'
)


class TestSharedWrapperHeader:
    @pytest.mark.asyncio
    async def test_the_first_question_keeps_its_sibling_choices(self, dom, extractor):
        questions = await extractor.parse_page(await dom(HEADER_SHARING_WRAPPER_HTML))

        assert [q.question_id for q in questions] == ["60000002"]
        assert questions[0].option_values == tuple(_AGREE5)

    @pytest.mark.asyncio
    async def test_selecting_a_choice_uses_the_same_ownership(self, dom):
        page = await dom(HEADER_SHARING_WRAPPER_HTML)
        await page.evaluate(
            """() => {
                window.__clicked = null;
                document.addEventListener('click', (event) => {
                    window.__clicked = event.target.closest('[role="radio"]');
                }, true);
            }"""
        )

        await PorslineSubmitter._select_choice(page, "60000002", 4)

        index = await page.evaluate(
            "() => [...document.querySelectorAll('[role=\"radio\"]')].indexOf(window.__clicked)"
        )
        assert index == 4


# --------------------------------------------------------------- bleed guard

def _radio(question_id: str, options: list[str]) -> Question:
    return Question(
        question_id, question_id, QuestionType.RADIO,
        tuple(Option(text=o, value=o) for o in options),
    )


class TestBleedGuard:
    def test_every_question_carrying_the_whole_page_is_refused(self):
        union = [f"o{i}" for i in range(11)]
        with pytest.raises(ExtractionError, match="porsline_v2"):
            reject_bled_options([_radio("a", union), _radio("b", union)], page_radios=11)

    def test_a_likert_group_with_identical_scales_is_fine(self):
        """The 2.x guard fired on identical lists — i.e. on every Likert group."""
        reject_bled_options(
            [_radio(str(i), _AGREE5) for i in range(30)], page_radios=150,
        )

    def test_a_single_question_page_is_never_bleed(self):
        reject_bled_options([_radio("a", _AGREE5)], page_radios=5)

    @pytest.mark.asyncio
    async def test_the_probe_counts_the_pages_radios(self, dom):
        state = await (await dom(GROUPED_PAGE_HTML)).evaluate(_PROBE)
        assert state["pageRadios"] == 2 * len(_AGREE5)

