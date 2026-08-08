from __future__ import annotations

import json
from pathlib import Path

import pytest

from personabot.domain.instrument import Instrument
from personabot.domain.models import FormSchema
from personabot.survey.loader import load_spec

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
LIVE_SPEC = REPO_ROOT / "configs" / "surveys" / "sexual_intimacy_fa.yaml"


@pytest.fixture(scope="session")
def schema() -> FormSchema:
    """The live Porsline form as extracted by phase 1 (51 questions)."""
    return FormSchema.from_list(json.loads((FIXTURES / "form_schema.json").read_text("utf-8")))


@pytest.fixture(scope="session")
def spec():
    return load_spec(LIVE_SPEC)


@pytest.fixture(scope="session")
def instrument(spec, schema) -> Instrument:
    return Instrument(spec, schema)
