"""Tests for the grouped-renderer Porsline provider.

The parts worth pinning are the ones that ran wrong against the live form and
would fail silently if they regressed: flattening a matrix into addressable
rows, and refusing to guess at a column label.
"""

from __future__ import annotations

import pytest

from personabot.domain.models import QuestionType
from personabot.errors import ExtractionError
from personabot.providers.porsline_v2 import (
    PorslineV2Submitter,
    api_url_for,
    matrix_row_id,
    questions_from_definition,
    split_matrix_row_id,
    strip_html,
)


def definition(*questions):
    return {"survey": {"id": 1, "name": "t", "questions": list(questions)}}


def matrix(qid, title, rows, columns):
    return {
        "id": qid,
        "type": 14,
        "title": title,
        "rows": [
            {
                "id": qid + 1 + index,
                "title": row,
                "choices": [{"name": name} for name in columns],
            }
            for index, row in enumerate(rows)
        ],
    }


# ===== url =====

def test_api_url_is_derived_from_the_public_form_url():
    assert (
        api_url_for("https://survey.porsline.ir/s/Ub25MG8N")
        == "https://survey.porsline.ir/api/surveys/Ub25MG8N/responders/"
    )


def test_a_url_without_a_scheme_is_rejected_with_the_fix_named():
    # BASE_FORM_URL without https:// reaches Playwright as an invalid URL and
    # fails deep inside the navigation stack; catching it here says why.
    with pytest.raises(ExtractionError, match="https://"):
        api_url_for("survey.porsline.ir/s/Ub25MG8N")


def test_a_url_that_is_not_a_form_link_is_rejected():
    with pytest.raises(ExtractionError, match="slug"):
        api_url_for("https://survey.porsline.ir/dashboard")


# ===== row ids =====

def test_row_ids_round_trip():
    assert matrix_row_id("53944059", 7) == "53944059#07"
    assert split_matrix_row_id("53944059#07") == ("53944059", 7)


def test_a_plain_question_id_is_not_mistaken_for_a_matrix_row():
    assert split_matrix_row_id("53941844") is None


# ===== flattening =====

def test_each_matrix_row_becomes_its_own_question():
    schema = questions_from_definition(
        definition(matrix(100, "شدت رابطه", ["الف", "ب", "ج"], ["0", "1", "2", "3"]))
    )
    assert [q.question_id for q in schema] == ["100#00", "100#01", "100#02"]
    assert [q.question_text for q in schema] == ["الف", "ب", "ج"]
    assert all(q.type is QuestionType.RADIO for q in schema)
    assert schema[0].option_values == ("0", "1", "2", "3")


def test_welcome_and_group_pages_are_left_out():
    # They own a question id but ask nothing. Admitting them would send them to
    # the LLM and count them against the completeness gate.
    schema = questions_from_definition(
        definition(
            {"id": 1, "type": 1, "title": "<p>با سلام</p>"},
            {"id": 2, "type": 8, "title": "اطلاعات اولیه:"},
            {"id": 3, "type": 2, "title": "نام سازمان", "answer_required": True},
        )
    )
    assert [q.question_id for q in schema] == ["3"]


def test_an_audio_question_is_typed_as_voice_not_text():
    # The distinction is what keeps the submitter from trying to type into a
    # recorder, and what the transcript export selects on.
    schema = questions_from_definition(
        definition({"id": 9, "type": 12, "title": "در یک پیام صوتی توضیح دهید"})
    )
    assert schema[0].type is QuestionType.VOICE


def test_a_choice_question_keeps_its_options():
    schema = questions_from_definition(
        definition({
            "id": 5, "type": 3, "title": "نوع سازمان",
            "choices": [{"name": "دولتی"}, {"name": "مردم نهاد"}],
        })
    )
    assert schema[0].option_values == ("دولتی", "مردم نهاد")


def test_a_choice_question_with_no_options_is_an_error_not_a_blank():
    with pytest.raises(ExtractionError, match="no options"):
        questions_from_definition(
            definition({"id": 5, "type": 3, "title": "نوع سازمان", "choices": []})
        )


def test_an_empty_payload_is_an_error():
    with pytest.raises(ExtractionError, match="no questions"):
        questions_from_definition({"survey": {"questions": []}})


def test_titles_arrive_as_html_and_are_flattened():
    assert strip_html("<p>سطر<br/>دوم</p>&nbsp;") == "سطر دوم"


# ===== column matching =====

@pytest.mark.parametrize("answer,expected", [
    ("0", 0),
    ("3", 3),
    ("۳", 3),          # Persian digits fold to ASCII before comparison
    ("1+/2+", None),   # not a column of this scale
    ("متوسط", None),   # a label the model invented
])
def test_a_matrix_answer_only_matches_an_exact_column(answer, expected):
    # Deliberately strict. Matrix columns are one or two characters, so fuzzy
    # matching would happily snap "1" onto "0" — silently recording the wrong
    # score in a cell nobody will re-read. An unmatched answer is reported.
    assert PorslineV2Submitter._column_index(answer, ["0", "1", "2", "3"]) is expected


def test_the_signed_stance_columns_match_exactly():
    columns = ["4-/3-", "2-/1-", "0", "1+/2+", "3+/4+"]
    assert PorslineV2Submitter._column_index("3+/4+", columns) == 4
    assert PorslineV2Submitter._column_index("0", columns) == 2
