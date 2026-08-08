"""The ``status`` command.

``make status`` counted files with shell ``ls``, so it could not tell a complete
answer set from a truncated one, nor a valid JSON file from a corrupt one. This
parses every file and validates it against the live schema.

Strictly read-only. It never deletes anything — which is the other half of
retiring ``cleaner.py``, whose fixed threshold of 90 exceeded this form's 51
questions and would have destroyed the entire corpus.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from personabot.domain.instrument import Instrument
from personabot.infrastructure.storage.json_store import read_json_or_none
from personabot.settings import Settings


@dataclass(slots=True)
class FileGroup:
    label: str
    total: int = 0
    valid: int = 0
    incomplete: list[tuple[str, float]] = field(default_factory=list)
    corrupt: list[str] = field(default_factory=list)


@dataclass(slots=True)
class StatusReport:
    schema_questions: int | None
    answerable: int | None
    groups: list[FileGroup] = field(default_factory=list)
    receipts: int = 0
    warnings: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = ["", "PersonaBot status", "=" * 62]

        if self.schema_questions is None:
            lines.append("  schema            : MISSING — run phase 1 first")
        else:
            lines.append(
                f"  schema            : {self.schema_questions} questions "
                f"({self.answerable} answerable)"
            )
        lines.append("")
        lines.append(f"  {'stage':<24}{'total':>7}{'valid':>8}{'partial':>9}{'corrupt':>9}")
        lines.append("  " + "-" * 57)

        for group in self.groups:
            lines.append(
                f"  {group.label:<24}{group.total:>7}{group.valid:>8}"
                f"{len(group.incomplete):>9}{len(group.corrupt):>9}"
            )

        lines.append("  " + "-" * 57)
        lines.append(f"  {'receipts (failures)':<24}{self.receipts:>7}")

        for group in self.groups:
            if group.incomplete:
                lines.append(f"\n  partial in {group.label}:")
                for name, ratio in group.incomplete[:8]:
                    lines.append(f"    {name}  {ratio:.0%}")
                if len(group.incomplete) > 8:
                    lines.append(f"    … and {len(group.incomplete) - 8} more")
            if group.corrupt:
                lines.append(f"\n  corrupt in {group.label}:")
                for name in group.corrupt[:8]:
                    lines.append(f"    {name}")

        for warning in self.warnings:
            lines.append(f"\n  ! {warning}")

        lines.append("")
        return "\n".join(lines)


def _scan_answers(directory: Path, label: str, instrument: Instrument | None) -> FileGroup:
    group = FileGroup(label=label)
    if not directory.is_dir():
        return group

    expected = set(instrument.answerable_ids) if instrument else None

    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix != ".json":
            continue
        group.total += 1

        payload = read_json_or_none(path)
        if not isinstance(payload, dict):
            group.corrupt.append(path.name)
            continue

        if expected is None:
            group.valid += 1
            continue

        answered = len(expected & set(payload))
        ratio = answered / len(expected) if expected else 0.0
        threshold = instrument.spec.answering.completeness.min_ratio
        if ratio >= threshold:
            group.valid += 1
        else:
            group.incomplete.append((path.name, ratio))

    return group


def _scan_personas(directory: Path, label: str) -> FileGroup:
    group = FileGroup(label=label)
    if not directory.is_dir():
        return group

    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix != ".json":
            continue
        group.total += 1
        payload = read_json_or_none(path)
        if not isinstance(payload, dict) or "demographics" not in payload:
            group.corrupt.append(path.name)
        elif not payload.get("details"):
            # Sampled but never given prose — phase 3 would skip it.
            group.incomplete.append((path.name, 0.0))
        else:
            group.valid += 1
    return group


def build_report(settings: Settings, instrument: Instrument | None) -> StatusReport:
    report = StatusReport(
        schema_questions=len(instrument.schema) if instrument else None,
        answerable=len(instrument.answerable_ids) if instrument else None,
    )

    report.groups = [
        _scan_personas(settings.personas_dir, "personas pending"),
        _scan_personas(settings.personas_dir / "done", "personas done"),
        _scan_answers(settings.answers_dir, "answers ready", instrument),
        _scan_answers(settings.answers_dir / "inprogress", "answers in progress", instrument),
        _scan_answers(settings.answers_dir / "done", "answers submitted", instrument),
    ]

    if settings.receipts_dir.is_dir():
        report.receipts = sum(1 for p in settings.receipts_dir.iterdir() if p.is_file())

    stale = next((g for g in report.groups if g.label == "answers in progress"), None)
    if stale and stale.total:
        report.warnings.append(
            f"{stale.total} answer file(s) are claimed. If no worker is running, "
            f"release them with `personabot recover`."
        )

    if instrument is None:
        report.warnings.append(
            "Without a schema, answer files cannot be checked for completeness."
        )

    return report
