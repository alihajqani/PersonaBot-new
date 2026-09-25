"""The Porsa provider, bound to the real DOM contract of porsa.irandoc.ac.ir.

The page states below are verbatim shapes returned by the provider's own DOM
probe against the live 91-question form, trimmed to the questions each test
needs. They pin down the three facts that make this platform different from
every other provider here:

* the whole questionnaire is in the DOM at first load, one hidden
  ``div#page_N`` per page, so extraction is a single read and never clicks;
* two option lists are misspelled on the live form and must survive extraction
  and matching **uncorrected**, because an option string the browser cannot
  find cannot be clicked;
* ``#nextButton`` fails silently on an incomplete page, so a stuck page has to
  be reported as the data problem it is rather than retried forever.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from personabot.domain.models import QuestionType
from personabot.errors import (
    AlreadyRespondedError,
    CircuitError,
    SubmissionError,
    SurveyClosedError,
)
from personabot.providers.porsa import (
    PorsaExtractor,
    PorsaSubmitter,
    _await_form,
    _explain_missing_form,
    _match_option,
    _to_question,
)


def _question(**overrides) -> dict:
    base = {
        "id": "questionId7", "type": "likert", "listMode": False, "page": 1,
        "number": "7", "text": "تا چه اندازه به دنبال فرصت‌هایی برای بهبود کار خود هستید؟",
        "help": "", "options": ["خیلی کم", "کم", "متوسط", "زیاد", "خیلی زیاد"],
        "optionCount": 5, "controlCount": 5, "fieldCount": 0, "name": "lik_1_7",
        "answered": False, "flagged": False, "optional": False, "hidden": False,
    }
    return base | overrides


def _state(pages: list[list[dict]], **overrides) -> dict:
    built = [
        {"id": f"page_{n}", "number": n, "visible": n == 1, "questions": qs}
        for n, qs in enumerate(pages, start=1)
    ]
    return {
        "ready": True, "form": True, "currentPage": 1, "totalPages": len(built),
        "totalQuestions": sum(len(p["questions"]) for p in built),
        "hasNext": True, "hasSubmit": False,
        "pages": built,
        "questions": [q for page in built for q in page["questions"]],
    } | overrides


# Verbatim from the live form: «سن» is a 5-point likert, «جنسیت» a 2-option
# singleChoice, and both are recorded as RADIO.
DEMOGRAPHICS = [
    _question(id="questionId1", type="likert", number="1", text="سن",
              options=["20-30", "31-40", "41-50", "51-60", "60-70"], name="lik_1_1"),
    _question(id="questionId2", type="singleChoice", number="2", text="جنسیت",
              options=["زن", "مرد"], optionCount=2, controlCount=2, name="sch_1_2"),
]

# Q75 on page 4: «کاملاموافقم», missing the space. Q54 on page 3: «ب ندرت»,
# missing the ه. Neither may be "corrected" anywhere in the pipeline.
MISSPELLED = [
    _question(id="questionId54", number="54", page=3,
              options=["هرگز", "ب ندرت", "بعضی اوقات", "اغلب", "همواره"]),
    _question(id="questionId75", number="75", page=4,
              options=["کاملا مخالفم", "مخالفم", "متوسط", "موافقم", "کاملاموافقم"]),
]


class FakePage:
    """Replays one probe result and records what would have been clicked."""

    def __init__(self, state: dict) -> None:
        self.state = state
        self.clicked: list[tuple[str, int]] = []
        self.filled: list[tuple[str, str]] = []

    async def evaluate(self, _script, *args):
        return self.state

    async def wait_for_timeout(self, _ms):
        return None

    def locator(self, selector: str):
        return FakeLocator(self, selector)


class FakeLocator:
    def __init__(self, page: FakePage, selector: str, index: int = 0) -> None:
        self.page, self.selector, self.index = page, selector, index

    def nth(self, index: int) -> "FakeLocator":
        return FakeLocator(self.page, self.selector, index)

    async def click(self, **_kwargs):
        self.page.clicked.append((self.selector, self.index))

    async def fill(self, value: str, **_kwargs):
        self.page.filled.append((self.selector, value))

    async def evaluate(self, _script, *args):
        self.page.clicked.append((self.selector, self.index))

    async def select_option(self, **kwargs):
        self.page.filled.append((self.selector, kwargs.get("label", "")))

    async def count(self):
        return 1


class TestParsePage:
    @pytest.mark.asyncio
    async def test_reads_every_page_in_one_pass(self, extractor):
        """Hidden pages are still in the DOM, so all of them are extracted."""
        state = _state([DEMOGRAPHICS, [_question(id="questionId30", page=2)]])

        questions = await extractor.parse_page(FakePage(state))

        assert [q.question_id for q in questions] == [
            "questionId1", "questionId2", "questionId30",
        ]

    @pytest.mark.asyncio
    async def test_likert_and_single_choice_are_both_radio(self, extractor):
        questions = await extractor.parse_page(FakePage(_state([DEMOGRAPHICS])))

        assert [q.type for q in questions] == [QuestionType.RADIO, QuestionType.RADIO]
        assert questions[0].option_values == ("20-30", "31-40", "41-50", "51-60", "60-70")
        assert questions[1].option_values == ("زن", "مرد")

    @pytest.mark.asyncio
    async def test_misspelled_options_survive_verbatim(self, extractor):
        """The schema records what is on the page, not what it should say.

        «ب ندرت» and «کاملاموافقم» are typos on the live form. Correcting them
        here would produce an option string the browser can never find, so the
        answer would silently go unclicked and the form would refuse to submit.
        """
        questions = await extractor.parse_page(FakePage(_state([MISSPELLED])))

        assert questions[0].option_values[1] == "ب ندرت"
        assert questions[1].option_values[4] == "کاملاموافقم"

    @pytest.mark.asyncio
    async def test_help_text_is_appended_to_the_question(self, extractor):
        state = _state([[_question(text="متن سوال", help="توضیح تکمیلی")]])

        assert (await extractor.parse_page(FakePage(state)))[0].question_text == (
            "متن سوال توضیح تکمیلی"
        )

    @pytest.mark.asyncio
    async def test_extraction_is_a_single_pass(self, extractor):
        """`advance` is False immediately: every page was already read."""
        assert await extractor.advance(FakePage(_state([DEMOGRAPHICS]))) is False

    @pytest.mark.asyncio
    async def test_extraction_never_writes_into_the_live_form(self, extractor):
        page = FakePage(_state([DEMOGRAPHICS]))

        await extractor.fill_placeholders(page, [])

        assert page.clicked == [] and page.filled == []


class TestToQuestion:
    def test_text_question_has_no_options(self):
        question = _to_question(_question(
            type="textAnswer", options=[], optionCount=0,
            controlCount=0, fieldCount=1,
        ))

        assert question.type is QuestionType.TEXT
        assert question.options == ()

    def test_unknown_platform_type_is_recorded_not_dropped(self):
        """Dropping it would mean no answer is generated and nothing warns."""
        question = _to_question(_question(type="signature", options=[], optionCount=0,
                                          controlCount=0, fieldCount=0, text="امضا"))

        assert question.type is QuestionType.UNKNOWN

    def test_matrix_single_choice_row_is_radio(self):
        """A matrix row reaches `_to_question` already expanded by the probe's
        `readMatrixRows`, so it looks exactly like an ordinary single-choice
        question — one `name`, one set of shared column options — and must be
        answerable rather than flagged as an unanswerable whole-table stub."""
        question = _to_question(_question(
            id="msc_1_7_1", type="matrixSingleChoice", number="7",
            text="۱- هنگام مرافعه با همسرم، رابطه من با خانواده پدر و مادری او "
                 "قطع می‌شود.",
            options=["همیشه", "اکثراً", "گاهی", "بندرت", "هرگز"],
            optionCount=5, controlCount=5, name="msc_1_7_1",
        ))

        assert question.type is QuestionType.RADIO
        assert question.option_values == ("همیشه", "اکثراً", "گاهی", "بندرت", "هرگز")

    def test_matrix_multiple_choice_row_is_checkbox(self):
        question = _to_question(_question(
            id="mmc_1_9_1", type="matrixMultipleChoice", number="9",
            text="۱- گزینه(های) مرتبط را انتخاب کنید.",
            options=["الف", "ب", "ج"], optionCount=3, controlCount=3,
            name="mmc_1_9_1",
        ))

        assert question.type is QuestionType.CHECKBOX

    def test_empty_question_is_skipped(self):
        assert _to_question(_question(text="", help="", options=[], optionCount=0,
                                      controlCount=0, fieldCount=0)) is None


class TestMatchOption:
    EXTENT = ["خیلی کم", "کم", "متوسط", "زیاد", "خیلی زیاد"]
    AGREEMENT_TYPO = ["کاملا مخالفم", "مخالفم", "متوسط", "موافقم", "کاملاموافقم"]
    FREQUENCY_TYPO = ["هرگز", "ب ندرت", "بعضی اوقات", "اغلب", "همواره"]

    def test_exact_answer(self):
        assert _match_option("زیاد", self.EXTENT) == 3

    def test_exact_wins_over_a_longer_option_containing_it(self):
        """«کم» must not be captured by «خیلی کم» — that is a one-point error."""
        assert _match_option("کم", self.EXTENT) == 1

    def test_correctly_spelled_answer_finds_the_misspelled_option(self):
        """The model writes «کاملا موافقم»; only «کاملاموافقم» is clickable.

        The fuzzy tier has to land on index 4 and not on «موافقم» at index 3,
        which is the difference between the top of the scale and one below it.
        """
        assert _match_option("کاملا موافقم", self.AGREEMENT_TYPO) == 4
        assert _match_option("به ندرت", self.FREQUENCY_TYPO) == 1

    def test_answer_from_another_scale_matches_nothing(self):
        """Refusing beats guessing: a wrong click is unrecoverable data."""
        assert _match_option("خیلی زیاد", self.FREQUENCY_TYPO) is None

    def test_empty_answer_matches_nothing(self):
        assert _match_option("", self.EXTENT) is None


class TestSubmitterFillPage:
    @pytest.mark.asyncio
    async def test_only_the_visible_page_is_filled(self, submitter):
        """Every page is in the DOM; clicking a hidden one would throw."""
        state = _state([DEMOGRAPHICS, [_question(id="questionId30", page=2)]])
        page = FakePage(state)

        filled = await submitter.fill_page(page, {
            "questionId1": "31-40", "questionId2": "زن", "questionId30": "زیاد",
        })

        assert filled == 2
        assert all("questionId30" not in selector for selector, _ in page.clicked)

    @pytest.mark.asyncio
    async def test_the_clicked_index_is_the_matched_index(self, submitter):
        page = FakePage(_state([DEMOGRAPHICS]))

        await submitter.fill_page(page, {"questionId1": "41-50", "questionId2": "مرد"})

        assert [index for _, index in page.clicked] == [2, 1]

    @pytest.mark.asyncio
    async def test_an_unmatchable_answer_is_left_blank_not_guessed(self, submitter):
        page = FakePage(_state([DEMOGRAPHICS]))

        filled = await submitter.fill_page(page, {"questionId1": "چهل و پنج سال"})

        assert filled == 0 and page.clicked == []

    @pytest.mark.asyncio
    async def test_a_matrix_row_is_filled_by_its_shared_name(self, submitter):
        """A matrix row has no `#id` element of its own — only the `name`
        shared by that row's inputs — so it must be addressed differently from
        a box-level question and still land on the right control."""
        row = _question(
            id="msc_1_7_1", type="matrixSingleChoice", number="7",
            text="۱- هنگام مرافعه با همسرم...",
            options=["همیشه", "اکثراً", "گاهی", "بندرت", "هرگز"],
            optionCount=5, controlCount=5, name="msc_1_7_1",
        )
        page = FakePage(_state([[row]]))

        filled = await submitter.fill_page(page, {"msc_1_7_1": "گاهی"})

        assert filled == 1
        [(selector, index)] = page.clicked
        assert index == 2
        assert 'name="msc_1_7_1"' in selector


class TestSubmitterAdvance:
    @pytest.mark.asyncio
    async def test_a_stuck_page_names_the_questions_that_blocked_it(self, submitter):
        """`navigateToPage` returns silently on an incomplete page.

        Without this, the submitter clicks «بعدی» into the void until the page
        budget runs out and reports a timeout rather than the real cause.
        """
        state = _state([[
            _question(id="questionId1", answered=True),
            _question(id="questionId2", flagged=True),
        ]])
        page = FakePage(state)

        with pytest.raises(SubmissionError, match="questionId2"):
            await submitter.advance(page)

    @pytest.mark.asyncio
    async def test_missing_navigation_is_reported_as_markup_drift(self, submitter):
        state = _state([DEMOGRAPHICS], hasNext=False, hasSubmit=False)

        with pytest.raises(SubmissionError, match="navigation markup"):
            await submitter.advance(FakePage(state))

    @pytest.mark.asyncio
    async def test_the_last_page_submits_and_stops(self, submitter):
        state = _state([DEMOGRAPHICS], currentPage=5, hasNext=False, hasSubmit=True)
        page = FakePage(state)

        assert await submitter.advance(page) is False
        assert page.clicked == [("#submitButton", 0)]


@pytest.fixture
def extractor():
    return PorsaExtractor(workspace=SimpleNamespace(settings=None), browser=None)


@pytest.fixture
def submitter():
    return PorsaSubmitter(workspace=SimpleNamespace(settings=_FakeSettings()), browser=None)


class _FakeSettings:
    """Only the fields the submitter reads on the paths under test."""

    use_tor = False
    tor_socks_host = "127.0.0.1"
    tor_socks_port = 9050
    tor_control_port = 9051
    tor_control_password = ""
    ready_timeout_seconds = 45
    nav_timeout_seconds = 60
    browser_max_attempts = 3


class _TextOnlyPage:
    """A page whose body text is all that `_explain_missing_form` reads."""

    def __init__(self, text: str | None) -> None:
        self._text = text

    def locator(self, selector: str) -> "_TextOnlyPage":
        assert selector == "body"
        return self

    async def inner_text(self, timeout: int = 0) -> str:
        from playwright.async_api import Error as PlaywrightError

        if self._text is None:
            raise PlaywrightError("Timeout exceeded")
        return self._text


class TestMissingFormIsDiagnosed:
    """One missing selector, three unrelated causes.

    Porsa answers a returning visitor with a form-less error page, so «no
    `#pageForm`» is exactly what a spent exit IP looks like — identical to what
    a half-loaded page looks like. Reporting both as "the form never appeared"
    is what sent a real debugging session after selectors that were fine.
    """

    @pytest.mark.asyncio
    async def test_the_already_answered_page_is_named(self):
        # Verbatim from the live form, fetched without Tor.
        page = _TextOnlyPage(
            "کاربر گرامی شما پیشتر به این پرسش‌نامه پاسخ داده‌اید. "
            "برای آشنایی بیشتر با این سامانه از لینک‌های زیر استفاده کنید."
        )
        error = await _explain_missing_form(page, 45_000)

        assert isinstance(error, AlreadyRespondedError)
        assert "already answered" in str(error)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "spelling",
        [
            "شما پیشتر به این پرسش‌نامه پاسخ داده‌اید",   # ZWNJ, as the live page ships it
            "شما پیشتر به این پرسشنامه پاسخ داده اید",   # spaces where the ZWNJ was
            "شما پيشتر به اين پرسشنامه پاسخ داده ايد",   # plus Arabic yeh for Persian yeh
        ],
    )
    async def test_the_marker_survives_every_spelling_of_it(self, spelling):
        """ZWNJ, a plain space and no separator at all are all the same word.

        The matcher's own normalization is not sufficient: it strips the ZWNJ,
        which folds «پرسش‌نامه» and «پرسشنامه» together but leaves «پرسش نامه»
        outside. All three have to converge or the diagnosis silently reverts to
        "the form never appeared".
        """
        error = await _explain_missing_form(_TextOnlyPage(spelling), 45_000)
        assert isinstance(error, AlreadyRespondedError)

    @pytest.mark.asyncio
    async def test_an_unreadable_page_is_a_dead_circuit(self):
        error = await _explain_missing_form(_TextOnlyPage(None), 45_000)

        assert isinstance(error, CircuitError)
        assert not isinstance(error, AlreadyRespondedError)
        assert "never finished arriving" in str(error)

    @pytest.mark.asyncio
    async def test_some_other_page_is_quoted_verbatim(self):
        """When the route is not the problem, the page says what is."""
        error = await _explain_missing_form(
            _TextOnlyPage("این پرسش‌نامه بسته شده است."), 45_000
        )

        assert isinstance(error, CircuitError)
        assert not isinstance(error, SurveyClosedError)
        assert "بسته شده است" in str(error)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "text",
        [
            # Desktop sidebar (`.left-part.hidden-xs`), verbatim from the
            # platform's generic error/info template (e.g. its 404 page).
            "برای آشنایی بیشتر با پُرسا و استفاده از امکانات آن از "
            "لینک‌های زیر استفاده کنید. خانه نام‌نویسی",
            # Mobile variant (`.shown-xs`) — different opening clause, same
            # tail, which is the substring the matcher relies on.
            "برای آشنایی بیشتر با این سامانه و استفاده از امکانات آن "
            "می‌توانید از لینک‌های زیر استفاده کنید. خانه نام‌نویسی",
        ],
    )
    async def test_the_survey_closed_page_is_named(self, text):
        """Porsa's generic error/info template — no #pageForm anywhere on it —
        is what a closed or deactivated survey looks like, in both the
        desktop and mobile renderings of its sidebar."""
        error = await _explain_missing_form(_TextOnlyPage(text), 45_000)

        assert isinstance(error, SurveyClosedError)
        assert isinstance(error, CircuitError)
        assert not isinstance(error, AlreadyRespondedError)


class _FormPage(_TextOnlyPage):
    """A page whose form appears after `appears_after` polls, or never."""

    def __init__(self, text: str, *, appears_after: int | None) -> None:
        super().__init__(text)
        self.appears_after = appears_after
        self.looks = 0

    def locator(self, selector: str):
        if selector == "#pageForm":
            self.looks += 1
            found = (
                self.appears_after is not None and self.looks > self.appears_after
            )
            return _Counter(1 if found else 0)
        return super().locator(selector)


class _Counter:
    def __init__(self, value: int) -> None:
        self._value = value

    async def count(self) -> int:
        return self._value


class TestAwaitForm:
    """Waiting for the form, without waiting out a page that has none.

    The platform answers a returning visitor immediately and completely, so the
    verdict is in the first kilobyte. Spending the full readiness budget before
    reading it is time not spent on the next circuit — and behind Tor, with more
    submissions than exit IPs, that verdict is routine.
    """

    @pytest.mark.asyncio
    async def test_a_form_that_arrives_late_is_still_waited_for(self, monkeypatch):
        monkeypatch.setattr("personabot.providers.porsa._FORM_POLL_SECONDS", 0)
        page = _FormPage("در حال بارگذاری", appears_after=3)

        await _await_form(page, timeout_ms=45_000)
        assert page.looks == 4

    @pytest.mark.asyncio
    async def test_already_answered_bails_on_the_first_look(self, monkeypatch):
        monkeypatch.setattr("personabot.providers.porsa._FORM_POLL_SECONDS", 0)
        page = _FormPage(
            "شما پیشتر به این پرسش‌نامه پاسخ داده‌اید", appears_after=None
        )

        with pytest.raises(AlreadyRespondedError):
            await _await_form(page, timeout_ms=45_000)
        assert page.looks == 1, "no reason to keep waiting for a form that is ruled out"

    @pytest.mark.asyncio
    async def test_survey_closed_bails_on_the_first_look(self, monkeypatch):
        monkeypatch.setattr("personabot.providers.porsa._FORM_POLL_SECONDS", 0)
        page = _FormPage(
            "برای آشنایی بیشتر با پُرسا و استفاده از امکانات آن از "
            "لینک‌های زیر استفاده کنید.",
            appears_after=None,
        )

        with pytest.raises(SurveyClosedError):
            await _await_form(page, timeout_ms=45_000)
        assert page.looks == 1, "no reason to keep waiting for a form that is ruled out"

    @pytest.mark.asyncio
    async def test_an_undiagnosed_page_still_gets_the_full_budget(self, monkeypatch):
        """A slow page must not be mistaken for a broken one."""
        monkeypatch.setattr("personabot.providers.porsa._FORM_POLL_SECONDS", 0)
        page = _FormPage("در حال بارگذاری", appears_after=None)

        with pytest.raises(CircuitError) as caught:
            await _await_form(page, timeout_ms=0)
        assert "در حال بارگذاری" in str(caught.value)
        assert page.looks >= 1


class TestDetectSuccess:
    """The post-submit confirmation, recognised two independent ways.

    `#pageForm` gone is the structural signal a successful POST navigates
    away; the text markers are a second tier for a confirmation rendered onto
    the same shell (`#pageForm` still present or still momentarily counted).
    """

    @pytest.mark.asyncio
    async def test_true_once_the_form_is_gone(self, submitter):
        page = _FormPage("هر متنی", appears_after=None)

        assert await submitter.detect_success(page) is True

    @pytest.mark.asyncio
    async def test_true_on_the_live_confirmation_text(self, submitter):
        """Verbatim from the live form's post-submit page."""
        page = _FormPage(
            "از شما برای پاسخ‌گویی به این پرسش‌نامه سپاسگزاریم", appears_after=0
        )

        assert await submitter.detect_success(page) is True
