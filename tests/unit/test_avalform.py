"""Avalform (MachForm) provider against markup shaped like the live form.

The HTML below is synthetic but reproduces the live structure element for
element: a select with numeric option values, a free-text field, a checkbox
group spread over ``element_N_k`` inputs, and a matrix whose rows are separate
radio groups sharing the header's columns.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from personabot.domain.models import QuestionType
from personabot.errors import ExtractionError
from personabot.providers.avalform import (
    AvalformExtractor,
    AvalformSubmitter,
    page_info,
    parse_fields,
)


def _page(number: int, total: int, body: str, button: str = "ادامه") -> str:
    return f"""<html><head><title>پرسشنامه نمونه - صفحه {number} از {total}</title></head>
<body><form id="form_1" method="post" action="/view.php"><ul>
{body}
<li id="li_buttons" class="buttons">
  <input type="hidden" name="form_id" value="1" />
  <input type="hidden" name="page_number" value="{number}" />
  <input class="button_text btn_primary" type="submit" id="submit_primary" name="submit_primary" value="{button}" />
</li></ul></form></body></html>"""


SELECT = """<li id="li_4" class="dropdown">
  <label class="description" for="element_4">نسبت شما با کودک <span class="required">*</span></label>
  <div><select id="element_4" name="element_4">
    <option value="" selected="selected"></option>
    <option value="1">مادر</option><option value="2">پدر</option><option value="5">سایر</option>
  </select></div></li>"""

TEXT = """<li id="li_5"><label class="description" for="element_5">سن کودک <span class="required">*</span></label>
  <div><input id="element_5" name="element_5" type="text" value="" /></div></li>"""

CHECKBOXES = """<li id="li_16" class="checkboxes">
  <label class="description">منابع اطلاعات</label><div>
  <input id="element_16_1" name="element_16_1" type="checkbox" value="1" /><label class="choice" for="element_16_1">اینترنت</label>
  <input id="element_16_2" name="element_16_2" type="checkbox" value="1" /><label class="choice" for="element_16_2">پزشک</label>
  <input id="element_16_3" name="element_16_3" type="checkbox" value="1" /><label class="choice" for="element_16_3">خانواده و دوستان</label>
  </div></li>"""

MATRIX = """<li id="li_20" class="matrix"><table><thead><tr>
  <th scope="col"><span style="display:none">لطفا پاسخ دهید</span></th>
  <th id="mc_20_1">هرگز</th><th id="mc_20_2">گاهی</th><th id="mc_20_3">همیشه</th></tr></thead><tbody>
  <tr id="mr_20"><td class="first_col">در اینترنت جستجو می‌کنم.</td>
    <td><label for="element_20_1" style="display:none">هرگز</label><input id="element_20_1" name="element_20" type="radio" value="1" /></td>
    <td><label for="element_20_2" style="display:none">گاهی</label><input id="element_20_2" name="element_20" type="radio" value="2" /></td>
    <td><label for="element_20_3" style="display:none">همیشه</label><input id="element_20_3" name="element_20" type="radio" value="3" /></td></tr>
  <tr id="mr_21"><td class="first_col">محتوا تولید می‌کنم.</td>
    <td><label for="element_21_1" style="display:none">هرگز</label><input id="element_21_1" name="element_21" type="radio" value="1" /></td>
    <td><label for="element_21_2" style="display:none">گاهی</label><input id="element_21_2" name="element_21" type="radio" value="2" /></td>
    <td><label for="element_21_3" style="display:none">همیشه</label><input id="element_21_3" name="element_21" type="radio" value="3" /></td></tr>
  </tbody></table></li>"""

FIRST_PAGE = _page(1, 3, SELECT + TEXT + CHECKBOXES)
LAST_PAGE = _page(3, 3, MATRIX, button="ارسال")


class TestPageInfo:
    def test_position_comes_from_the_title_and_the_hidden_field(self):
        info = page_info(FIRST_PAGE)
        assert (info.number, info.total, info.is_last) == (1, 3, False)
        assert page_info(LAST_PAGE).is_last

    def test_a_single_page_form_is_last_by_its_button(self):
        html = FIRST_PAGE.replace("صفحه 1 از 3", "").replace('name="page_number" value="1"', "")
        assert page_info(html.replace('value="ادامه"', 'value="ارسال"')).is_last

    def test_server_side_errors_are_read(self):
        html = FIRST_PAGE.replace("<ul>", '<ul><li class="error"><p class="error">این فیلد الزامی است</p></li>')
        assert page_info(html).errors == ["این فیلد الزامی است"]


class TestParse:
    def test_options_are_labels_not_numeric_codes(self):
        """2.x recorded `<option value="1">مادر</option>` as "1"."""
        select = parse_fields(FIRST_PAGE)[0]
        assert select.question.type is QuestionType.SELECT
        assert select.question.option_values == ("مادر", "پدر", "سایر")
        assert select.values == ["1", "2", "5"]
        assert select.question.question_text == "نسبت شما با کودک"

    def test_free_text(self):
        text = parse_fields(FIRST_PAGE)[1]
        assert (text.kind, text.question.question_id) == ("text", "element_5")

    def test_a_checkbox_group_is_one_question(self):
        group = parse_fields(FIRST_PAGE)[2]
        assert group.question.question_id == "element_16"
        assert group.question.type is QuestionType.CHECKBOX
        assert group.question.option_values == ("اینترنت", "پزشک", "خانواده و دوستان")
        assert group.inputs == ["element_16_1", "element_16_2", "element_16_3"]

    def test_every_matrix_row_is_a_question_with_the_header_as_options(self):
        rows = parse_fields(LAST_PAGE)
        assert [r.question.question_id for r in rows] == ["element_20", "element_21"]
        assert rows[1].question.question_text == "محتوا تولید می‌کنم."
        assert rows[1].question.option_values == ("هرگز", "گاهی", "همیشه")
        assert rows[1].values == ["1", "2", "3"]


class _Page:
    """Serves fixed HTML; any click is a test failure."""

    def __init__(self, html: str) -> None:
        self.html = html

    async def content(self) -> str:
        return self.html

    def locator(self, selector):
        raise AssertionError(f"clicked {selector} on the last page")


class TestExtractionNeverSubmits:
    @pytest.mark.asyncio
    async def test_the_last_page_is_not_posted(self):
        """2.x clicked first and checked afterwards — posting a junk response."""
        extractor = AvalformExtractor(SimpleNamespace(settings=None), None)
        assert await extractor.advance(_Page(LAST_PAGE)) is False
        await extractor.fill_placeholders(_Page(LAST_PAGE), [])   # no placeholder typed either

    @pytest.mark.asyncio
    async def test_a_page_that_does_not_advance_reports_the_server_error(self):
        stuck = FIRST_PAGE.replace("<ul>", '<ul><li class="error"><p class="error">سن کودک الزامی است</p></li>')

        class Page(_Page):
            def locator(self, selector):
                async def click(**_):
                    self.html = stuck
                return SimpleNamespace(first=SimpleNamespace(click=click))

            async def wait_for_load_state(self, *_args, **_kwargs):
                pass

        extractor = AvalformExtractor(SimpleNamespace(settings=None), None)
        with pytest.raises(ExtractionError, match="سن کودک الزامی است"):
            await extractor.advance(Page(FIRST_PAGE))


@pytest.fixture
async def dom():
    playwright = pytest.importorskip("playwright.async_api")
    async with playwright.async_playwright() as p:
        try:
            browser = await p.chromium.launch()
        except playwright.Error as exc:
            pytest.skip(f"chromium unavailable: {exc}")
        page = await browser.new_page()

        async def load(html: str):
            await page.set_content(html)
            return page

        yield load
        await browser.close()


class TestFill:
    @pytest.mark.asyncio
    async def test_labels_are_mapped_back_to_the_forms_values(self, dom):
        page = await dom(FIRST_PAGE)
        submitter = AvalformSubmitter(SimpleNamespace(settings=None), None)

        filled = await submitter.fill_page(page, {
            "element_4": "سایر",
            "element_5": "4",
            "element_16": "اینترنت | خانواده و دوستان",
        })

        assert filled == 3
        assert await page.locator("#element_4").input_value() == "5"
        assert await page.locator("#element_5").input_value() == "4"
        checked = [await page.locator(f"#element_16_{i}").is_checked() for i in (1, 2, 3)]
        assert checked == [True, False, True]

    @pytest.mark.asyncio
    async def test_each_matrix_row_gets_its_own_column(self, dom):
        page = await dom(LAST_PAGE)
        submitter = AvalformSubmitter(SimpleNamespace(settings=None), None)

        assert await submitter.fill_page(page, {"element_20": "همیشه", "element_21": "هرگز"}) == 2
        assert await page.locator("#element_20_3").is_checked()
        assert await page.locator("#element_21_1").is_checked()

    @pytest.mark.asyncio
    async def test_an_unknown_option_fills_nothing(self, dom):
        page = await dom(FIRST_PAGE)
        submitter = AvalformSubmitter(SimpleNamespace(settings=None), None)
        assert await submitter.fill_page(page, {"element_4": "عمو", "element_16": "رادیو"}) == 0
