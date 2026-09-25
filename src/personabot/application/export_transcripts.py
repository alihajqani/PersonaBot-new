"""Export the answers the form itself cannot carry.

Some platforms ask for a spoken answer: the question renders as an audio
recorder and accepts nothing else. A simulated respondent has no voice, so those
items are answered in prose during phase 3 like any other free-text question and
written here to one text file per respondent — leaving the recorder empty in the
form, which is fine because such questions are almost always optional.

The file is named after another answer that *did* reach the platform (a serial
phone number, typically), so a row in the platform's export and a transcript on
disk can be matched up afterwards without a lookup table.

Reading from ``answers/`` as well as ``answers/done/`` is deliberate: the
transcripts are worth having before submission, so they can be reviewed, and
worth keeping after it. Re-running is safe — a file is rewritten, never
duplicated.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterator

from personabot.domain.instrument import Instrument
from personabot.domain.models import PhaseResult, QuestionType
from personabot.errors import SurveySpecError
from personabot.infrastructure.storage.json_store import read_json_or_none
from personabot.logging_setup import get_logger
from personabot.workspace import Workspace

log = get_logger(__name__)

# Anything that would make the value unusable as a file name, or let it escape
# the output directory. Answer text is model-produced, so this is not optional.
_UNSAFE = re.compile(r"[^\w؀-ۿ.\-]+", re.UNICODE)


def safe_filename(value: str) -> str:
    """A file-name stem from an answer value, or ``""`` if nothing usable remains."""
    stem = _UNSAFE.sub("_", str(value).strip()).strip("._-")
    return stem[:120]


def _answer_files(workspace: Workspace) -> Iterator[Path]:
    for directory in (workspace.answers_dir, workspace.answers_dir / "done"):
        if directory.is_dir():
            yield from sorted(directory.glob("*.json"))


def render(
    answers: dict[str, Any], instrument: Instrument, *, header: str = ""
) -> str:
    """The transcript body for one respondent, in schema order."""
    wanted = {QuestionType.from_raw(t) for t in instrument.spec.transcripts.types}

    lines: list[str] = []
    if header:
        lines += [header.strip(), ""]

    index = 0
    for question in instrument.schema:
        if question.type not in wanted:
            continue
        index += 1
        answer = str(answers.get(question.question_id, "")).strip()
        lines += [
            f"### {index}. {question.question_text.strip()}",
            "",
            answer or "(بدون پاسخ)",
            "",
        ]
    return "\n".join(lines).rstrip() + "\n"


def run(*, workspace: Workspace, instrument: Instrument) -> PhaseResult:
    result = PhaseResult(phase="transcript export")
    config = instrument.spec.transcripts

    if not config.enabled:
        log.info("transcripts.enabled is false in %s; nothing to export.", workspace.spec_path)
        return result

    if config.filename_from not in instrument.schema.by_id:
        raise SurveySpecError(
            f"transcripts.filename_from = {config.filename_from!r} is not a question "
            f"in {workspace.schema_file}. Re-run `make schema` if the form was "
            f"republished, or correct the id."
        )

    wanted = {QuestionType.from_raw(t) for t in config.types}
    matching = [q for q in instrument.schema if q.type in wanted]
    if not matching:
        raise SurveySpecError(
            f"No question in the schema has a type in {config.types}, so every "
            f"transcript would be empty."
        )

    # From the instrument's own spec, which is what the rest of this run reads.
    directory = workspace.output_dir / config.dir
    directory.mkdir(parents=True, exist_ok=True)

    known_ids = set(instrument.schema.by_id)
    seen: dict[str, Path] = {}
    for path in _answer_files(workspace):
        payload = read_json_or_none(path)
        if not isinstance(payload, dict) or not payload:
            log.warning("Skipping %s: empty or unreadable.", path.name)
            result.record_failure(path.name, "unreadable answer file")
            continue

        # An output directory often still holds answer sets from the survey it
        # was used for previously. Those share no question ids with the current
        # schema, and reporting each as a failure would bury a real one. A file
        # that overlaps the schema but lacks the identifier is a different thing
        # — that one is broken, and is reported.
        if not (payload.keys() & known_ids):
            log.debug("Skipping %s: no question in it belongs to this schema.", path.name)
            result.skipped += 1
            continue

        stem = safe_filename(payload.get(config.filename_from, ""))
        if not stem:
            log.error(
                "Skipping %s: question %s has no usable answer to name the file with.",
                path.name, config.filename_from,
            )
            result.record_failure(path.name, "no filename value")
            continue

        # Two answer sets sharing an identifier means one would overwrite the
        # other, and the mapping back to the platform's rows would be wrong for
        # both. Say so rather than silently losing one.
        if stem in seen:
            log.error(
                "Skipping %s: %r is already used by %s. Two respondents share the "
                "value of question %s.",
                path.name, stem, seen[stem].name, config.filename_from,
            )
            result.record_failure(path.name, f"duplicate identifier {stem!r}")
            continue
        seen[stem] = path

        target = directory / f"{stem}.txt"
        target.write_text(render(payload, instrument, header=config.header), encoding="utf-8")
        result.record_success()
        log.info("%s -> %s", path.name, target)

    log.info(
        "Wrote %d transcript(s) covering %d question(s) to %s",
        result.succeeded, len(matching), directory,
    )
    if result.skipped:
        log.info(
            "Ignored %d answer set(s) that belong to a different instrument.",
            result.skipped,
        )
    return result
