"""Tests for the voice-answer transcript export.

The file name is the only thing tying a submitted response to its transcript,
so most of what matters here is what happens when that name is unusable.
"""

from __future__ import annotations

import json

import pytest

from personabot.application import export_transcripts
from personabot.domain.instrument import Instrument
from personabot.domain.models import FormSchema
from personabot.errors import SurveySpecError
from personabot.survey.loader import load_spec

from conftest import SURVEYS, make_workspace

REPO_SPEC = SURVEYS / "tehran_urban_network_fa" / "survey.yaml"

SCHEMA = FormSchema.from_list([
    {"question_id": "phone", "question_text": "شماره تماس", "type": "TEXT_INPUT", "options": []},
    {"question_id": "v1", "question_text": "پرسش صوتی یک", "type": "VOICE", "options": []},
    {"question_id": "v2", "question_text": "پرسش صوتی دو", "type": "VOICE", "options": []},
])


@pytest.fixture
def instrument():
    spec = load_spec(REPO_SPEC)
    spec.transcripts.filename_from = "phone"
    spec.transcripts.dir = "voice_texts"
    return Instrument(spec, SCHEMA)


@pytest.fixture
def settings(tmp_path):
    workspace = make_workspace(tmp_path, "tehran_urban_network_fa")
    workspace.ensure_directories()
    return workspace


def write_answers(settings, name, payload):
    (settings.answers_dir / name).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def test_one_file_per_respondent_named_by_the_bound_answer(settings, instrument):
    write_answers(settings, "a.json", {"phone": "09120000001", "v1": "الف", "v2": "ب"})
    write_answers(settings, "b.json", {"phone": "09120000002", "v1": "ج", "v2": "د"})

    result = export_transcripts.run(workspace=settings, instrument=instrument)

    assert result.succeeded == 2
    directory = settings.output_dir / "voice_texts"
    assert sorted(p.name for p in directory.glob("*.txt")) == [
        "09120000001.txt", "09120000002.txt",
    ]


def test_the_transcript_carries_every_voice_question_in_schema_order(settings, instrument):
    write_answers(settings, "a.json", {"phone": "09120000001", "v1": "پاسخ یک", "v2": "پاسخ دو"})
    export_transcripts.run(workspace=settings, instrument=instrument)

    text = (settings.output_dir / "voice_texts" / "09120000001.txt").read_text("utf-8")
    assert text.index("پرسش صوتی یک") < text.index("پرسش صوتی دو")
    assert "پاسخ یک" in text and "پاسخ دو" in text
    # The question that names the file is not itself a voice answer.
    assert "شماره تماس" not in text


def test_a_missing_voice_answer_is_marked_rather_than_dropped(settings, instrument):
    write_answers(settings, "a.json", {"phone": "09120000001", "v1": "پاسخ یک"})
    export_transcripts.run(workspace=settings, instrument=instrument)

    text = (settings.output_dir / "voice_texts" / "09120000001.txt").read_text("utf-8")
    assert "پرسش صوتی دو" in text
    assert "(بدون پاسخ)" in text


def test_two_respondents_sharing_an_identifier_is_a_failure_not_an_overwrite(
    settings, instrument
):
    # Silently overwriting would leave the researcher with one transcript for
    # two responses and no sign that anything was lost.
    write_answers(settings, "a.json", {"phone": "09120000001", "v1": "اول"})
    write_answers(settings, "b.json", {"phone": "09120000001", "v1": "دوم"})

    result = export_transcripts.run(workspace=settings, instrument=instrument)

    assert result.succeeded == 1
    assert result.failed == 1
    assert "duplicate" in result.failures[0].reason


def test_answer_sets_from_a_previous_survey_are_ignored_not_reported(settings, instrument):
    # An output directory is commonly reused across surveys. Those files share
    # no ids with this schema; reporting each as a failure would bury a real one
    # and make the command exit non-zero on a perfectly good run.
    write_answers(settings, "old.json", {"50270726": "هفتم", "50270688": "13سال"})
    write_answers(settings, "new.json", {"phone": "09120000001", "v1": "الف"})

    result = export_transcripts.run(workspace=settings, instrument=instrument)

    assert result.succeeded == 1
    assert result.skipped == 1
    assert result.failed == 0


def test_an_answer_set_with_no_identifier_is_reported(settings, instrument):
    write_answers(settings, "a.json", {"v1": "الف"})
    result = export_transcripts.run(workspace=settings, instrument=instrument)
    assert result.succeeded == 0
    assert result.failed == 1


def test_answers_already_submitted_are_still_exported(settings, instrument):
    (settings.answers_dir / "done" / "a.json").write_text(
        json.dumps({"phone": "09120000009", "v1": "الف"}, ensure_ascii=False), encoding="utf-8"
    )
    result = export_transcripts.run(workspace=settings, instrument=instrument)
    assert result.succeeded == 1
    assert (settings.output_dir / "voice_texts" / "09120000009.txt").is_file()


def test_a_filename_source_that_is_not_in_the_schema_fails_loudly(settings, instrument):
    # The usual cause is a republished form: ids change, and the binding silently
    # points at nothing.
    instrument.spec.transcripts.filename_from = "no_such_question"
    with pytest.raises(SurveySpecError, match="filename_from"):
        export_transcripts.run(workspace=settings, instrument=instrument)


def test_disabled_transcripts_write_nothing(settings, instrument):
    instrument.spec.transcripts.enabled = False
    write_answers(settings, "a.json", {"phone": "09120000001", "v1": "الف"})
    result = export_transcripts.run(workspace=settings, instrument=instrument)
    assert result.succeeded == 0
    assert not (settings.output_dir / "voice_texts").exists()


@pytest.mark.parametrize("raw,expected", [
    ("09120000001", "09120000001"),
    ("  0912 000 0001 ", "0912_000_0001"),
    ("../../etc/passwd", "etc_passwd"),
    ("", ""),
])
def test_filenames_cannot_escape_the_output_directory(raw, expected):
    # The value comes from model output, so path traversal is a real shape.
    assert export_transcripts.safe_filename(raw) == expected
