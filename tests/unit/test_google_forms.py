"""The Google Forms provider, bound to the real DOM contract.

Regression tests for the two silent defects behind a run where every submission
either stalled or dropped answers:

* Option lookup built its selector with ``repr``, which escapes format
  characters. Any option containing a ZWNJ — «خانه‌دار», «شاغل تمام‌وقت», most
  compound Persian words — produced a selector holding the literal six
  characters ``\\u200c`` and matched nothing, so the question stayed empty and
  the form rejected the page.
* The answer-verification pass keys off entry ids parsed out of
  ``data-params``. The JS copy of that regex must agree with the Python one, or
  every question reads as unanswered.
"""

from __future__ import annotations

import re

from personabot.providers.google_forms import (
    _ANSWERED_JS,
    _ENTRY_ID,
    _entry_id,
    _radio_by_value,
)

ZWNJ = "‌"


def test_radio_selector_keeps_zwnj_literal() -> None:
    selector = _radio_by_value(f"خانه{ZWNJ}دار")

    assert ZWNJ in selector
    assert "u200c" not in selector
    assert selector == f'div[role=\'radio\'][data-value="خانه{ZWNJ}دار"]'


def test_radio_selector_escapes_quotes_and_backslashes() -> None:
    selector = _radio_by_value('a "b" \\c')

    assert selector == 'div[role=\'radio\'][data-value="a \\"b\\" \\\\c"]'


def test_entry_id_reads_the_first_id_in_data_params() -> None:
    assert _entry_id(',[[111690692,null,0]],"i6"') == "entry.111690692"
    assert _entry_id("nothing here") is None
    assert _entry_id(None) is None


def test_browser_entry_id_regex_matches_the_python_one() -> None:
    """The two copies drift apart silently; this catches it."""
    javascript = re.search(r"/(.+?)/\.exec", _ANSWERED_JS)
    assert javascript is not None, "entry-id regex not found in the injected script"

    assert javascript.group(1) == _ENTRY_ID.pattern
    match = re.compile(javascript.group(1)).search(',[[111690692,null,0]],"i6"')
    assert match is not None and match.group(1) == "111690692"
