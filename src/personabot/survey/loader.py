"""Loading and validating survey specs.

The only place in the survey layer that touches the filesystem. Everything it
returns is pure data, so the domain interpreters stay testable without I/O.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
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
    """Load a spec and schema from disk and bind them."""
    return instrument_for(load_spec(spec_path), schema_path, require_coverage=require_coverage)


def instrument_for(
    spec: SurveySpec,
    schema_path: str | Path,
    *,
    require_coverage: bool = True,
) -> Instrument:
    """Bind an already-loaded spec to a schema file.

    Args:
        require_coverage: When true (the default for every generation phase),
            a question that resolves to nothing — or a rule that matches
            nothing — is a hard error. This is the check that would have caught
            the stale-signature collapse the moment it appeared.
    """
    schema = load_schema(schema_path)
    instrument = Instrument(spec, schema)

    if require_coverage:
        instrument.require_full_coverage()
    else:
        report = instrument.coverage()
        if not report.ok:
            log.warning("Schema coverage is incomplete:\n%s", report.render())

    return instrument


@dataclass(frozen=True, slots=True)
class SchemaChange:
    """How a freshly extracted schema differs from the snapshot it replaced."""

    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    replaced_existing: bool = False

    @property
    def is_new(self) -> bool:
        return not self.replaced_existing

    @property
    def identical(self) -> bool:
        return self.replaced_existing and not (self.added or self.removed or self.changed)

    def describe(self) -> str:
        if self.is_new:
            return "first extraction"
        if self.identical:
            return "unchanged since the last extraction"
        parts = []
        for label, ids in (("added", self.added), ("removed", self.removed),
                           ("changed", self.changed)):
            if ids:
                preview = ", ".join(ids[:8]) + (" …" if len(ids) > 8 else "")
                parts.append(f"{len(ids)} {label} ({preview})")
        return "; ".join(parts)


def save_schema(
    schema: FormSchema, path: str | Path, *, previous_path: str | Path
) -> SchemaChange:
    """Write a freshly extracted schema, keeping and diffing the one it replaces.

    The spec's matchers are written against the schema, so a republished form
    is the one event that silently invalidates a finished survey config. It
    used to surface much later, as unassigned questions or rules matching
    nothing. Here it is reported the moment it happens, and the old snapshot is
    kept as ``previous_path`` so the two can be compared by hand.
    """
    from personabot.infrastructure.storage.json_store import read_json_or_none, write_json

    path, previous_path = Path(path), Path(previous_path)
    old_raw = read_json_or_none(path) if path.is_file() else None
    new_raw = schema.to_list()

    if not isinstance(old_raw, list):
        write_json(path, new_raw)
        return SchemaChange()

    old = FormSchema.from_list(old_raw)
    old_by_id, new_by_id = old.by_id, schema.by_id
    change = SchemaChange(
        added=tuple(qid for qid in schema.ids if qid not in old_by_id),
        removed=tuple(qid for qid in old.ids if qid not in new_by_id),
        changed=tuple(
            qid for qid in schema.ids
            if qid in old_by_id and old_by_id[qid] != new_by_id[qid]
        ),
        replaced_existing=True,
    )
    if not change.identical:
        write_json(previous_path, old_raw)
        log.warning(
            "The form changed since the last extraction: %s. The old schema is "
            "kept at %s — review the survey's matchers, then run validate-config.",
            change.describe(), previous_path,
        )
    write_json(path, new_raw)
    return change


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
