"""Loading and validating survey specs.

The only place in the survey layer that touches the filesystem. Everything it
returns is pure data, so the domain interpreters stay testable without I/O.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from personabot.domain.instrument import Instrument
from personabot.domain.models import FormSchema
from personabot.errors import ConfigError, SurveySpecError
from personabot.logging_setup import get_logger
from personabot.survey.spec import CategoricalDist, SurveySpec

log = get_logger(__name__)


def load_spec(path: str | Path) -> SurveySpec:
    """Read and validate a survey YAML.

    Raises:
        ConfigError: the file is missing or is not valid YAML.
        SurveySpecError: the content violates the schema, with a field path.
    """
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"Survey config not found: {path}")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")

    try:
        spec = SurveySpec.model_validate(raw)
    except ValidationError as exc:
        raise SurveySpecError(f"{path} is invalid:\n{_format_errors(exc)}") from exc

    _warn_on_soft_problems(spec, path)
    return spec


def load_schema(path: str | Path) -> FormSchema:
    """Read a form schema produced by phase 1."""
    path = Path(path)
    if not path.is_file():
        raise ConfigError(
            f"Form schema not found: {path}. Run phase 1 (schema extraction) first."
        )

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from exc

    if not isinstance(raw, list):
        raise ConfigError(f"{path}: expected a JSON array of questions")
    if not raw:
        raise ConfigError(f"{path}: schema is empty")

    return FormSchema.from_list(raw)


def build_instrument(
    spec_path: str | Path,
    schema_path: str | Path,
    *,
    require_coverage: bool = True,
) -> Instrument:
    """Load a spec and schema and bind them.

    Args:
        require_coverage: When true (the default for every generation phase),
            a question that resolves to nothing — or a rule that matches
            nothing — is a hard error. This is the check that would have caught
            the stale-signature collapse the moment it appeared.
    """
    spec = load_spec(spec_path)
    schema = load_schema(schema_path)
    instrument = Instrument(spec, schema)

    if require_coverage:
        instrument.require_full_coverage()
    else:
        report = instrument.coverage()
        if not report.ok:
            log.warning("Schema coverage is incomplete:\n%s", report.render())

    return instrument


def _format_errors(exc: ValidationError) -> str:
    """Render pydantic errors as readable field paths."""
    lines = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "(root)"
        lines.append(f"  {location}: {error['msg']}")
    return "\n".join(lines)


def _warn_on_soft_problems(spec: SurveySpec, path: Path) -> None:
    """Report things that are legal but probably mistakes.

    Audit A7 lives here: weights that do not sum to 1 are normalized rather than
    rejected, but staying silent is how the legacy occupation frame drifted to
    1.03 and quietly under-sampled its last category fourfold.
    """
    for variable in [*spec.population.demographics, *spec.population.derived_fields]:
        _warn_weights(variable.name, variable.body, path)

    for construct in spec.constructs:
        if len(construct.traits) > 1:
            log.debug(
                "%s: construct '%s' draws on %d traits; its prompt will carry all "
                "of their bands.", path.name, construct.key, len(construct.traits),
            )

    if spec.answering.chunk_by == "none":
        log.warning(
            "%s: chunk_by=none sends every item in one request. Construct chunking "
            "exists to break the self-consistency that inflates Cronbach's alpha; "
            "disabling it will produce implausibly clean psychometrics.",
            path.name,
        )


def _warn_weights(name: str, body: Any, path: Path) -> None:
    if isinstance(body, CategoricalDist):
        total = body.weight_sum
        if abs(total - 1.0) > 1e-6:
            log.warning(
                "%s: categorical weights for '%s' sum to %.4f, not 1.0. They will be "
                "normalized, so the effective probabilities differ from the numbers "
                "written in the file.",
                path.name, name, total,
            )
    cases = getattr(body, "cases", None)
    if cases:
        for case in cases:
            _warn_weights(name, case.body, path)
