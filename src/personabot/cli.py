"""Command-line entry point.

The only place that reads the environment, configures logging, or constructs
dependencies. Everything below receives what it needs as arguments.

Two behaviours the predecessor lacked:

* **Phases run in the order given.** ``main.py`` dispatched through four
  independent ``if N in phases`` guards, so ``--phases 4,1`` silently ran
  extraction first. Unknown numbers were ignored; non-numeric input raised an
  uncaught ``ValueError``.
* **Exit codes mean something.** It always logged "All selected phases have been
  executed" and exited 0, even having produced nothing — so `make loop` could
  not tell a working run from a broken one.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from personabot.domain.models import PhaseResult
from personabot.errors import PersonaBotError
from personabot.logging_setup import configure_logging, get_logger
from personabot.settings import Settings, load_settings

log = get_logger(__name__)

PHASE_NAMES = {1: "schema", 2: "personas", 3: "answers", 4: "submit"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="personabot",
        description="Configuration-driven synthetic survey response generation.",
    )
    parser.add_argument("--config", type=Path, help="Path to the survey spec YAML.")
    parser.add_argument("--log-level", default=None, help="DEBUG, INFO, WARNING, ERROR.")

    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run one or more pipeline phases.")
    run.add_argument("provider", nargs="?", help="Form platform (needed by phases 1 and 4).")
    run.add_argument(
        "--phases", default="1,2,3,4",
        help="Comma-separated phase numbers, executed in the order given.",
    )
    run.add_argument("--num-personas", type=int, default=5)
    run.add_argument("--seed", type=int, default=None, help="Make sampling reproducible.")
    run.add_argument("--limit", type=int, default=None, help="Cap items processed per phase.")

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

    sub.add_parser("status", help="Report on the output directory.")
    sub.add_parser("recover", help="Return stale claims to the queue.")
    sub.add_parser("analyze", help="Psychometric report over the generated corpus.")

    providers = sub.add_parser("providers", help="List registered providers.")
    providers.set_defaults(quiet=True)

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
    if args.config:
        overrides["survey_config"] = args.config
    if args.log_level:
        overrides["log_level"] = args.log_level
    return load_settings(**overrides)


async def _run_phases(args: argparse.Namespace, settings: Settings) -> list[PhaseResult]:
    from personabot.application import generate_answers, generate_personas
    from personabot.infrastructure.browser.factory import BrowserFactory
    from personabot.infrastructure.llm.factory import build_llm_client
    from personabot.infrastructure.prompts.renderer import build_renderer
    from personabot.infrastructure.storage.json_store import write_json
    from personabot.providers import registry
    from personabot.survey.loader import build_instrument, load_spec

    phases = parse_phases(args.phases)
    settings.ensure_directories()

    spec = load_spec(settings.survey_config)
    renderer = build_renderer(spec.prompt_preset)
    results: list[PhaseResult] = []

    needs_browser = any(phase in (1, 4) for phase in phases)
    needs_llm = any(phase in (2, 3) for phase in phases)

    if needs_browser:
        if not args.provider:
            raise PersonaBotError(
                f"Phases {[p for p in phases if p in (1, 4)]} need a provider. "
                f"Available: {registry.available()}"
            )
        # Resolve up front so an unknown name fails before any phase runs,
        # rather than after phase 1 has already announced itself.
        if 1 in phases:
            registry.get_extractor(args.provider)
        if 4 in phases:
            registry.get_submitter(args.provider)

    client = build_llm_client(settings) if needs_llm else None
    browser = BrowserFactory(settings) if needs_browser else None

    try:
        if browser:
            await browser.start()

        for phase in phases:
            log.info("===== PHASE %d — %s =====", phase, PHASE_NAMES[phase])

            if phase == 1:
                extractor = registry.get_extractor(args.provider)(settings, browser)
                schema = await extractor.run()
                write_json(settings.schema_file, schema.to_list())
                log.info("Wrote %d questions to %s", len(schema), settings.schema_file)
                result = PhaseResult(phase="schema extraction", succeeded=len(schema))

            elif phase == 2:
                result = await generate_personas.run(
                    settings=settings, spec=spec, client=client,
                    renderer=renderer, count=args.num_personas, seed=args.seed,
                )

            elif phase == 3:
                instrument = build_instrument(settings.survey_config, settings.schema_file)
                result = await generate_answers.run(
                    settings=settings, instrument=instrument, client=client,
                    renderer=renderer, limit=args.limit,
                )

            else:
                instrument = build_instrument(settings.survey_config, settings.schema_file)
                submitter = registry.get_submitter(args.provider)(
                    settings, browser, instrument.schema
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


def _validate_config(args: argparse.Namespace, settings: Settings) -> int:
    from personabot.domain.instrument import Instrument
    from personabot.survey.loader import load_schema, load_spec

    spec = load_spec(settings.survey_config)
    print(f"✓ {settings.survey_config} is valid")
    print(f"  id={spec.id}  locale={spec.locale}  preset={spec.prompt_preset}")

    model = spec.population.latent_model
    print(f"\n  latent model: {len(model.dimensions)} dimension(s) "
          f"on {len(model.common_factors)} factor(s)")
    names = list(model.dimensions)
    print(f"    {'':26s}" + "".join(f"{n[:9]:>10s}" for n in names))
    for a in names:
        row = "".join(f"{model.implied_correlation(a, b):>10.2f}" for b in names)
        print(f"    {a:26s}{row}")

    schema_path = args.schema or settings.schema_file
    if not Path(schema_path).is_file():
        print(f"\n  ! No schema at {schema_path}; coverage not checked.")
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        settings = _settings_from(args)
    except PersonaBotError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    configure_logging(args.log_level or settings.log_level)

    try:
        if args.command == "providers":
            from personabot.providers import registry

            for name in registry.available():
                print(name)
            return 0

        if args.command == "validate-config":
            return _validate_config(args, settings)

        if args.command == "inspect-schema":
            from personabot.application.inspect import summarize
            from personabot.survey.loader import load_schema

            summary = summarize(load_schema(args.schema or settings.schema_file))
            print(summary.render())
            if args.ids:
                print(summary.render_id_checklist())
            return 0

        if args.command == "status":
            from personabot.application.status import build_report
            from personabot.domain.instrument import Instrument
            from personabot.survey.loader import load_schema, load_spec

            instrument = None
            if settings.schema_file.is_file():
                instrument = Instrument(load_spec(settings.survey_config),
                                        load_schema(settings.schema_file))
            print(build_report(settings, instrument).render())
            return 0

        if args.command == "analyze":
            from personabot.application import analyze
            from personabot.survey.loader import build_instrument

            instrument = build_instrument(settings.survey_config, settings.schema_file)
            print(analyze.run(settings, instrument).render())
            return 0

        if args.command == "recover":
            from personabot.infrastructure.storage.workqueue import WorkQueue

            total = 0
            for directory in (settings.answers_dir, settings.personas_dir):
                total += WorkQueue(directory).recover_stale_claims()
            print(f"Released {total} stale claim(s).")
            return 0

        results = asyncio.run(_run_phases(args, settings))

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
