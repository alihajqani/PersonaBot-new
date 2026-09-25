"""Text normalization for matching Persian survey content.

Audit finding A2. The predecessor compared LLM output against schema options
with ``fuzz.ratio`` after only stripping whitespace and a couple of punctuation
marks. That is not enough for Persian, because several visually identical
letters have distinct code points:

===========================  ==========  ==========
Character                    Arabic      Persian
===========================  ==========  ==========
yeh                          ي U+064A    ی U+06CC
kaf                          ك U+0643    ک U+06A9
===========================  ==========  ==========

A model that emits Arabic yeh where the schema holds Persian yeh mismatches on
*every occurrence of that letter*. On a long option such as «همیشه توافق داریم»
that pushes the similarity ratio below the acceptance threshold, so a correct
answer is silently discarded, counted as missing, and can sink the whole persona
below the completeness gate. The failure is invisible in the logs — it looks
like the model simply did not answer.

Normalization here is used for **comparison only**. What gets stored is always
the canonical option string taken from the schema, never the normalized form.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final

# --- letters that differ only by code point ---
_LETTER_FOLDS: Final[dict[str, str]] = {
    "ي": "ی",  # ARABIC YEH        -> FARSI YEH
    "ى": "ی",  # ALEF MAKSURA      -> FARSI YEH
    "ك": "ک",  # ARABIC KAF        -> KEHEH
    "ة": "ه",  # TEH MARBUTA       -> HEH
    "أ": "ا",  # ALEF WITH HAMZA ABOVE -> ALEF
    "إ": "ا",  # ALEF WITH HAMZA BELOW -> ALEF
    "آ": "ا",  # ALEF WITH MADDA   -> ALEF
}

# --- digits: Arabic-Indic and Extended Arabic-Indic both fold to ASCII ---
_DIGIT_FOLDS: Final[dict[str, str]] = {
    **{chr(0x0660 + i): str(i) for i in range(10)},  # ٠١٢٣٤٥٦٧٨٩
    **{chr(0x06F0 + i): str(i) for i in range(10)},  # ۰۱۲۳۴۵۶۷۸۹
}

# Harakat (short-vowel diacritics), superscript alef, and tatweel carry no
# lexical weight here and appear inconsistently in both schema and model output.
_STRIP_MARKS: Final[re.Pattern[str]] = re.compile(
    "[ً-ْٰـ]"
)

_ZWNJ: Final[str] = "‌"
_BIDI_MARKS: Final[re.Pattern[str]] = re.compile("[‎‏‪-‮⁦-⁩]")
_WHITESPACE: Final[re.Pattern[str]] = re.compile(r"\s+")

_TRANSLATION = str.maketrans({**_LETTER_FOLDS, **_DIGIT_FOLDS})


def normalize(
    text: str,
    *,
    unicode_form: str = "NFKC",
    fold_arabic: bool = True,
    zwnj: str = "strip",
    strip_trailing: str = ".,؛",
    collapse_whitespace: bool = True,
) -> str:
    """Normalize ``text`` for comparison.

    Args:
        unicode_form: Unicode normalization form, or ``""`` to skip. NFKC folds
            presentation forms and compatibility characters.
        fold_arabic: Map Arabic letter and digit variants onto Persian/ASCII.
        zwnj: ``"strip"`` removes the zero-width non-joiner, ``"space"``
            replaces it with a space, ``"keep"`` leaves it. Stripping is the
            safe default — models place it inconsistently.
        strip_trailing: Characters trimmed from the end.
        collapse_whitespace: Collapse internal runs of whitespace to one space.
    """
    if not text:
        return ""

    result = str(text)

    if unicode_form:
        result = unicodedata.normalize(unicode_form, result)

    result = _BIDI_MARKS.sub("", result)

    if zwnj == "strip":
        result = result.replace(_ZWNJ, "")
    elif zwnj == "space":
        result = result.replace(_ZWNJ, " ")

    if fold_arabic:
        result = result.translate(_TRANSLATION)
        result = _STRIP_MARKS.sub("", result)

    # The Persian comma is written both with and without a following space.
    result = result.replace("، ", "،")

    if collapse_whitespace:
        result = _WHITESPACE.sub(" ", result)

    result = result.strip()
    if strip_trailing:
        result = result.rstrip(strip_trailing).strip()

    return result


def normalize_for_match(text: str) -> str:
    """Aggressive normalization used by the question matcher.

    Always applied to both sides, so a spec written with Persian yeh still
    matches a schema extracted with Arabic yeh.
    """
    return normalize(text, fold_arabic=True, zwnj="strip", strip_trailing="")
