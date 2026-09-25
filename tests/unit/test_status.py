"""Regression tests for the `status` report.

The report is the only view onto the corpus, so a stage it forgets to scan is
indistinguishable from work that never happened.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import make_workspace
from personabot.application.status import build_report
from personabot.workspace import Workspace


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def settings(tmp_path: Path) -> Workspace:
    # Named `settings` for history; the output layout now lives on a Workspace.
    workspace = make_workspace(tmp_path)
    workspace.ensure_directories()
    return workspace


def _group(report, label: str):
    return next(g for g in report.groups if g.label == label)


def test_ensure_directories_creates_the_persona_claim_directory(settings: Workspace) -> None:
    assert (settings.personas_dir / "inprogress").is_dir()


def test_claimed_personas_are_reported(settings: Workspace) -> None:
    """A persona stranded in `inprogress/` used to vanish from the report."""
    persona = {"demographics": {"age": 30}, "details": "prose"}
    _write(settings.personas_dir / "inprogress" / "p1.json", persona)
    _write(settings.personas_dir / "done" / "p2.json", persona)

    report = build_report(settings, instrument=None)

    assert _group(report, "personas in progress").total == 1
    assert _group(report, "personas in progress").valid == 1
    assert _group(report, "personas done").total == 1
    assert any("1 personas" in w for w in report.warnings)


def test_receipts_count_failures_not_files(settings: Workspace) -> None:
    """Each failure writes a .png and a .html; counting files doubled it."""
    for suffix in (".png", ".html"):
        (settings.receipts_dir / f"20260101T000000_persona-a{suffix}").write_text("x")
    (settings.receipts_dir / "20260101T000001_persona-b.png").write_text("x")

    assert build_report(settings, instrument=None).receipts == 2


def test_answer_stages_are_counted_separately(settings: Workspace, instrument) -> None:
    complete = {qid: "x" for qid in instrument.answerable_ids}
    partial = dict(list(complete.items())[: len(complete) // 4])

    _write(settings.answers_dir / "ready.json", complete)
    _write(settings.answers_dir / "short.json", partial)
    _write(settings.answers_dir / "inprogress" / "claimed.json", complete)
    _write(settings.answers_dir / "done" / "sent.json", complete)
    (settings.answers_dir / "broken.json").write_text("{not json", encoding="utf-8")

    report = build_report(settings, instrument)

    ready = _group(report, "answers ready")
    assert (ready.total, ready.valid) == (3, 1)
    assert [name for name, _ in ready.incomplete] == ["short.json"]
    assert ready.corrupt == ["broken.json"]
    assert _group(report, "answers in progress").valid == 1
    assert _group(report, "answers submitted").valid == 1
    assert any("1 answers" in w for w in report.warnings)


def test_report_renders_every_stage(settings: Workspace) -> None:
    rendered = build_report(settings, instrument=None).render()
    for label in (
        "personas pending",
        "personas in progress",
        "personas done",
        "answers ready",
        "answers in progress",
        "answers submitted",
    ):
        assert label in rendered
