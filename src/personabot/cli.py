"""Command-line entry point.

The only place that reads the environment, configures logging, or constructs
dependencies. Everything below receives what it needs as arguments.

Behaviours worth knowing:

* **One checkout, many surveys.** Commands that act on a survey resolve one —
  ``--config`` > ``--survey`` > ``SURVEY_CONFIG`` > ``SURVEY`` — and bind it to
  the deployment as a :class:`~personabot.workspace.Workspace`. Its output goes
  to ``output/<survey id>/``.
* **Phases run in the order given.** 1.x dispatched through four independent
  ``if N in phases`` guards, so ``--phases 4,1`` silently ran extraction first.
* **Exit codes mean something.** 0 success, 1 a phase or check failed, 2 a
  configuration error, 130 interrupted — so `make loop` can tell a working run
  from a broken one.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from personabot import __version__
from personabot.domain.models import PhaseResult
from personabot.errors import PersonaBotError
from personabot.logging_setup import configure_logging, get_logger
from personabot.settings import Settings, load_settings
from personabot.workspace import Workspace, open_workspace

log = get_logger("personabot.cli")  # not __name__: `python -m` makes it __main__

PHASE_NAMES = {1: "schema", 2: "personas", 3: "answers", 4: "submit"}

# Commands that never touch a survey.
_SURVEYLESS = frozenset({"providers", "surveys", "check-keys", "new-survey"})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="personabot",
        description="Configuration-driven synthetic survey response generation.",
    )
    parser.add_argument("--version", action="version", version=f"personabot {__version__}")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--survey", "-s", help="Survey id: a folder under surveys/.")
    selection.add_argument("--config", type=Path, help="Path to a survey YAML (overrides --survey).")
    parser.add_argument("--log-level", default=None, help="DEBUG, INFO, WARNING, ERROR.")

    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run one or more pipeline phases.")
    run.add_argument(
        "provider", nargs="?",
        help="Form platform for phases 1 and 4. Defaults to the survey's form.provider.",
    )
    run.add_argument(
        "--phases", default="1,2,3,4",
        help="Comma-separated phase numbers, executed in the order given.",
    )
    run.add_argument("--num-personas", type=int, default=5)
    run.add_argument("--seed", type=int, default=None, help="Make sampling reproducible.")
    run.add_argument(
        "--start-index", type=int, default=None,
        help="First respondent number for phase 2. Default: continue after the "
             "highest number already in this survey's output.",
    )
    run.add_argument("--limit", type=int, default=None, help="Cap items processed per phase.")
    run.add_argument("--url", default=None, help="Override the survey's form URL for this run.")

    validate = sub.add_parser("validate-config", help="Check a survey spec, and its schema coverage.")
    validate.add_argument("--schema", type=Path, default=None)

    inspect = sub.add_parser(
        "inspect-schema",
        help="Summarize an extracted schema and emit a draft `scales:` block.",
    )
    inspect.add_argument("--schema", type=Path, default=None)
    inspect.add_argument(
        "--ids", action="store_true",
        help="Also print per-signature id lists for building question_id_in.",
    )
    inspect.add_argument(
        "--full", action="store_true",
        help="Print item text in full instead of truncating it at 72 columns. "
             "Use this when saving an inventory to read while writing a config.",
    )

    check_keys = sub.add_parser(
        "check-keys", help="Verify every configured API key/endpoint works."
    )
    check_keys.add_argument(
        "--live", action="store_true",
        help="Send a real (tiny) generation instead of the free count_tokens probe. "
             "Slower, costs a few tokens, but also proves quota is left.",
    )

    sub.add_parser(
        "export-transcripts",
        help="Write the voice-only answers to one text file per respondent.",
    )
    sub.add_parser("status", help="Report on the survey's output directory.")
    sub.add_parser("recover", help="Return stale claims to the queue.")
    sub.add_parser("analyze", help="Psychometric report over the generated corpus.")
    sub.add_parser("providers", help="List registered form providers.")
    sub.add_parser("surveys", help="List the surveys under surveys/.")

    new = sub.add_parser("new-survey", help="Create surveys/<id>/ from the template.")
    new.add_argument("id", help="The new survey's id (its folder name).")
    new.add_argument(
        "--from", dest="source", default=None,
        help="Copy an existing survey instead of the template (its schema is not copied).",
    )

    return parser


def parse_phases(raw: str) -> list[int]:
    phases: list[int] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        if not token.isdigit() or int(token) not in PHASE_NAMES:
            raise PersonaBotError(
                f"Invalid phase {token!r}. Valid phases: "
                + ", ".join(f"{n} ({name})" for n, name in PHASE_NAMES.items())
            )
        phases.append(int(token))
    if not phases:
        raise PersonaBotError("No phases selected.")
    return phases


def _settings_from(args: argparse.Namespace) -> Settings:
    overrides = {}
    if args.log_level:
        overrides["log_level"] = args.log_level
    return load_settings(**overrides)


def _workspace_from(args: argparse.Namespace, settings: Settings) -> Workspace:
    return open_workspace(
        settings,
        survey=args.survey,
        config=args.config,
        form_url=getattr(args, "url", None),
        provider=getattr(args, "provider", None),
    )


async def _run_phases(args: argparse.Namespace, workspace: Workspace) -> list[PhaseResult]:
    from personabot.application import generate_answers, generate_personas
    from personabot.infrastructure.browser.factory import BrowserFactory
    from personabot.infrastructure.llm.factory import build_llm_client
    from personabot.infrastructure.prompts.renderer import build_renderer
    from personabot.providers import registry
    from personabot.survey.loader import instrument_for, save_schema

    phases = parse_phases(args.phases)
    settings = workspace.settings
    workspace.ensure_directories()
    log.info("Survey %s — output in %s", workspace.id, workspace.output_dir)

    results: list[PhaseResult] = []
    needs_browser = any(phase in (1, 4) for phase in phases)
    needs_llm = any(phase in (2, 3) for phase in phases)

    provider = None
    if needs_browser:
        provider = workspace.require_provider()
        workspace.require_form_url()
        # Resolve up front so an unknown name fails before any phase runs,
        # rather than after phase 1 has already announced itself.
        if 1 in phases:
            registry.get_extractor(provider)
        if 4 in phases:
            registry.get_submitter(provider)

    renderer = build_renderer(workspace) if needs_llm else None
    client = build_llm_client(settings) if needs_llm else None
    browser = BrowserFactory(settings) if needs_browser else None

    try:
        if browser:
            await browser.start()

        for phase in phases:
            log.info("===== PHASE %d — %s =====", phase, PHASE_NAMES[phase])

            if phase == 1:
                extractor = registry.get_extractor(provider)(workspace, browser)
                schema = await extractor.run()
                change = save_schema(
                    schema, workspace.schema_file,
                    previous_path=workspace.previous_schema_file,
                )
                log.info(
                    "Wrote %d questions to %s (%s).",
                    len(schema), workspace.schema_file, change.describe(),
                )
                result = PhaseResult(phase="schema extraction", succeeded=len(schema))

            elif phase == 2:
                result = await generate_personas.run(
                    workspace=workspace, client=client, renderer=renderer,
                    count=args.num_personas, seed=args.seed,
                    start_index=args.start_index,
                )

            elif phase == 3:
                instrument = instrument_for(workspace.spec, workspace.schema_file)
                result = await generate_answers.run(
                    workspace=workspace, instrument=instrument, client=client,
                    renderer=renderer, limit=args.limit,
                )

            else:
                instrument = instrument_for(workspace.spec, workspace.schema_file)
                submitter = registry.get_submitter(provider)(
                    workspace, browser, instrument.schema, workspace.spec.scenarios
                )
                result = await submitter.run(limit=args.limit)

            results.append(result)
            log.info("%s", result.summary())

    finally:
        if browser:
            await browser.stop()
        if client:
            await client.aclose()

    return results


def _validate_config(args: argparse.Namespace, workspace: Workspace) -> int:
    from personabot.domain.instrument import Instrument
    from personabot.infrastructure.prompts.renderer import build_renderer
    from personabot.survey.loader import load_schema

    spec = workspace.spec
    print(f"✓ {workspace.spec_path} is valid")
    print(f"  id={spec.id}  locale={spec.locale}  provider={workspace.provider or '-'}")
    print(f"  form={workspace.form_url or '(no URL)'}  max_pages={workspace.max_pages}")
    print(f"  output={workspace.output_dir}")

    renderer = build_renderer(workspace)
    print("  prompts:")
    for name in ("persona.system.j2", "persona.user.j2", "answers.system.j2", "answers.user.j2"):
        origin = renderer.origin(name)
        print(f"    {name:20s} <- {origin or 'MISSING'}")

    model = spec.population.latent_model
    print(f"\n  latent model: {len(model.dimensions)} dimension(s) "
          f"on {len(model.common_factors)} factor(s)")
    names = list(model.dimensions)
    print(f"    {'':26s}" + "".join(f"{n[:9]:>10s}" for n in names))
    for a in names:
        row = "".join(f"{model.implied_correlation(a, b):>10.2f}" for b in names)
        print(f"    {a:26s}{row}")

    schema_path = args.schema or workspace.schema_file
    if not Path(schema_path).is_file():
        print(f"\n  ! No schema at {schema_path}; coverage not checked. Run phase 1 first.")
        return 0

    instrument = Instrument(spec, load_schema(schema_path))
    report = instrument.coverage()
    print()
    print(report.render())

    if not report.ok:
        print("\n✗ Coverage is incomplete — see above.")
        return 1
    print("\n✓ Every question is accounted for.")
    return 0


def _surveyless(args: argparse.Namespace, settings: Settings) -> int:
    if args.command == "providers":
        from personabot.providers import registry

        for name in registry.available():
            print(name)
        return 0

    if args.command == "surveys":
        from personabot.survey.loader import load_spec
        from personabot.workspace import SPEC_FILENAME, available_surveys

        for survey_id in available_surveys(settings):
            path = settings.surveys_dir / survey_id / SPEC_FILENAME
            try:
                spec = load_spec(path)
                detail = f"{spec.form.provider or '-':12s} {spec.form.url or '(no URL)'}"
            except PersonaBotError as exc:
                detail = f"INVALID: {str(exc).splitlines()[0]}"
            print(f"{survey_id:45s} {detail}")
        return 0

    if args.command == "check-keys":
        from personabot.application import check_keys

        report = asyncio.run(check_keys.run(settings, live=args.live))
        print(report.render())
        return 0 if report.ok else 1

    if args.command == "new-survey":
        from personabot.application.scaffold import new_survey

        target = new_survey(settings, args.id, source=args.source)
        print(f"Created {target}. Next:")
        print(f"  1. set form.provider and form.url in {target / 'survey.yaml'}")
        print(f"  2. personabot --survey {args.id} run --phases 1")
        print(f"  3. personabot --survey {args.id} inspect-schema --full --ids")
        print(f"  4. write the population/scales/constructs, then validate-config")
        return 0

    raise AssertionError(f"unhandled command {args.command}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        settings = _settings_from(args)
    except PersonaBotError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    configure_logging(args.log_level or settings.log_level)

    try:
        if args.command in _SURVEYLESS:
            return _surveyless(args, settings)

        workspace = _workspace_from(args, settings)

        if args.command == "validate-config":
            return _validate_config(args, workspace)

        if args.command == "inspect-schema":
            from personabot.application.inspect import summarize
            from personabot.survey.loader import load_schema

            summary = summarize(load_schema(args.schema or workspace.schema_file))
            print(summary.render(text_width=None if args.full else 72))
            if args.ids:
                print(summary.render_id_checklist())
            return 0

        if args.command == "status":
            from personabot.application.status import build_report
            from personabot.domain.instrument import Instrument
            from personabot.survey.loader import load_schema

            instrument = None
            if workspace.schema_file.is_file():
                instrument = Instrument(workspace.spec, load_schema(workspace.schema_file))
            print(f"\nSurvey {workspace.id} — {workspace.output_dir}")
            print(build_report(workspace, instrument).render())
            return 0

        if args.command == "export-transcripts":
            from personabot.application import export_transcripts
            from personabot.survey.loader import instrument_for

            instrument = instrument_for(workspace.spec, workspace.schema_file)
            result = export_transcripts.run(workspace=workspace, instrument=instrument)
            log.info("%s", result.summary())
            return 0 if result.ok else 1

        if args.command == "analyze":
            from personabot.application import analyze
            from personabot.survey.loader import instrument_for

            instrument = instrument_for(workspace.spec, workspace.schema_file)
            print(analyze.run(workspace, instrument).render())
            return 0

        if args.command == "recover":
            from personabot.infrastructure.storage.workqueue import WorkQueue

            total = 0
            for directory in (workspace.answers_dir, workspace.personas_dir):
                total += WorkQueue(directory).recover_stale_claims()
            print(f"Released {total} stale claim(s).")
            return 0

        results = asyncio.run(_run_phases(args, workspace))

    except PersonaBotError as exc:
        log.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        log.warning("Interrupted.")
        return 130

    failed = [r for r in results if not r.ok]
    if failed:
        log.error(
            "Completed with failures: %s",
            "; ".join(f"{r.phase} ({r.failed})" for r in failed),
        )
        return 1

    log.info("All phases completed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
