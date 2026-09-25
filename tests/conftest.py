from __future__ import annotations

import json
from pathlib import Path

import pytest

from personabot.domain.instrument import Instrument
from personabot.domain.models import FormSchema
from personabot.settings import Settings
from personabot.survey.loader import load_spec
from personabot.workspace import Workspace

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
SURVEYS = REPO_ROOT / "surveys"
PROMPTS = REPO_ROOT / "prompts"

# The live 51-question Porsline form the 2.x suite was written against.
LIVE_SURVEY = SURVEYS / "sexual_intimacy_fa"
LIVE_SPEC = LIVE_SURVEY / "survey.yaml"
LIVE_SCHEMA = LIVE_SURVEY / "form_schema.json"


def make_settings(tmp_path: Path, **overrides) -> Settings:
    """Settings that never read the developer's .env and write under tmp_path."""
    values = {
        "_env_file": None,
        "output_dir": tmp_path / "output",
        "surveys_dir": SURVEYS,
        "prompts_dir": PROMPTS,
    }
    values.update(overrides)
    return Settings(**values)


def make_workspace(tmp_path: Path, survey: str = "sexual_intimacy_fa", **overrides) -> Workspace:
    settings = make_settings(tmp_path, **overrides)
    spec_path = settings.surveys_dir / survey / "survey.yaml"
    return Workspace(settings=settings, spec=load_spec(spec_path), spec_path=spec_path)


@pytest.fixture(scope="session")
def schema() -> FormSchema:
    """The live Porsline form as extracted by phase 1 (51 questions)."""
    return FormSchema.from_list(json.loads(LIVE_SCHEMA.read_text("utf-8")))


@pytest.fixture(scope="session")
def spec():
    return load_spec(LIVE_SPEC)


@pytest.fixture(scope="session")
def instrument(spec, schema) -> Instrument:
    return Instrument(spec, schema)


@pytest.fixture
def workspace(tmp_path) -> Workspace:
    return make_workspace(tmp_path)
