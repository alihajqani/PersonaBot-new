"""The ``new-survey`` command — start a questionnaire as a folder, not a fork.

In 2.x a new form began with copying the whole repository. Here it begins with
copying ``surveys/_template/`` (or an existing survey whose population or
prompts are a good starting point) into ``surveys/<id>/``.

When the source is an existing survey, its schema snapshot is *not* copied —
it describes another form, and every matcher in the new YAML has to be written
against the new form's own extraction — and its form URL is blanked, so the new
survey cannot submit into the old survey's form by accident.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from personabot.errors import ConfigError
from personabot.settings import Settings
from personabot.workspace import (
    PREVIOUS_SCHEMA_FILENAME,
    SCHEMA_FILENAME,
    SPEC_FILENAME,
    TEMPLATE_SURVEY,
)

_VALID_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]*$")
_ID_LINE = re.compile(r"^id:[^\n]*$", re.MULTILINE)
_FORM_URL_LINE = re.compile(r"^(?P<indent>[ \t]+)url:[^\n]*$", re.MULTILINE)


def new_survey(settings: Settings, survey_id: str, *, source: str | None = None) -> Path:
    """Create ``surveys/<survey_id>/`` from the template or another survey."""
    if not _VALID_ID.match(survey_id):
        raise ConfigError(
            f"{survey_id!r} is not a usable survey id. Use letters, digits, `_` "
            f"and `-`, starting with a letter or digit (e.g. `job_stress_fa`)."
        )

    target = settings.surveys_dir / survey_id
    if target.exists():
        raise ConfigError(f"{target} already exists; pick another id or edit it in place.")

    origin = settings.surveys_dir / (source or TEMPLATE_SURVEY)
    if not (origin / SPEC_FILENAME).is_file():
        raise ConfigError(f"Cannot copy from {origin}: it has no {SPEC_FILENAME}.")

    shutil.copytree(
        origin,
        target,
        ignore=shutil.ignore_patterns(SCHEMA_FILENAME, PREVIOUS_SCHEMA_FILENAME, "__pycache__"),
    )

    spec_path = target / SPEC_FILENAME
    text = spec_path.read_text(encoding="utf-8")
    text, replaced = _ID_LINE.subn(f"id: {survey_id}", text, count=1)
    if not replaced:
        text = f"id: {survey_id}\n" + text
    if source:
        text = _FORM_URL_LINE.sub(
            lambda m: f"{m.group('indent')}url: null   # the NEW form's URL goes here",
            text,
            count=1,
        )
    spec_path.write_text(text, encoding="utf-8")
    return target
