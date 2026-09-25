"""One survey, bound to one deployment.

Every 2.x fork of this project existed for a single reason: the output
directory, the form URL and the survey selection all lived in ``Settings`` —
one ``.env``, one ``output/`` per checkout — so a second questionnaire needed a
second copy of the repository. Fifteen copies later, each carried fixes the
others lacked, and one had even picked up another checkout's code through a
copied virtualenv.

A :class:`Workspace` is what those copies were standing in for. It pairs the
deployment :class:`~personabot.settings.Settings` with one
:class:`~personabot.survey.spec.SurveySpec` and derives everything
survey-specific from the pair:

* the survey folder ``surveys/<id>/`` — the YAML, the schema snapshot, and any
  prompt overrides;
* the output folder ``<OUTPUT_DIR>/<id>/`` — queues, receipts, transcripts,
  reports — so two surveys never share a queue;
* the form URL, provider and page budget, with the survey's own values winning
  over deployment-wide fallbacks.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from personabot.errors import ConfigError
from personabot.logging_setup import get_logger
from personabot.settings import Settings
from personabot.survey.spec import SurveySpec

log = get_logger(__name__)

SPEC_FILENAME = "survey.yaml"
SCHEMA_FILENAME = "form_schema.json"
PREVIOUS_SCHEMA_FILENAME = "form_schema.previous.json"
TEMPLATE_SURVEY = "_template"


@dataclass(frozen=True, slots=True)
class Workspace:
    """A survey spec bound to the deployment that runs it."""

    settings: Settings
    spec: SurveySpec
    spec_path: Path
    form_url_override: str | None = None
    provider_override: str | None = None

    # ----------------------------------------------------------- survey side

    @property
    def id(self) -> str:
        return self.spec.id

    @property
    def survey_dir(self) -> Path:
        return self.spec_path.parent

    @property
    def schema_file(self) -> Path:
        """The schema snapshot the spec's matchers are written against.

        Kept beside the YAML rather than in ``output/``: it is part of the
        survey's definition, and a republished form is something to notice at
        extraction (see :func:`personabot.survey.loader.save_schema`), not
        something to discover later as a coverage error.
        """
        return self.survey_dir / SCHEMA_FILENAME

    @property
    def previous_schema_file(self) -> Path:
        return self.survey_dir / PREVIOUS_SCHEMA_FILENAME

    @property
    def prompts_dir(self) -> Path:
        return self.survey_dir / "prompts"

    # ----------------------------------------------------------- output side

    @property
    def output_dir(self) -> Path:
        return self.settings.output_dir / self.spec.id

    @property
    def personas_dir(self) -> Path:
        return self.output_dir / "personas"

    @property
    def answers_dir(self) -> Path:
        return self.output_dir / "answers"

    @property
    def receipts_dir(self) -> Path:
        return self.output_dir / "receipts"

    @property
    def transcripts_dir(self) -> Path:
        return self.output_dir / self.spec.transcripts.dir

    @property
    def report_file(self) -> Path:
        return self.output_dir / "validation_report.json"

    def ensure_directories(self) -> None:
        """Create the output tree. Idempotent."""
        for path in (
            self.output_dir,
            self.personas_dir,
            self.personas_dir / "inprogress",
            self.personas_dir / "done",
            self.answers_dir,
            self.answers_dir / "inprogress",
            self.answers_dir / "done",
            self.receipts_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    # -------------------------------------------------------------- the form

    @property
    def form_url(self) -> str | None:
        """``--url``, else the survey's ``form.url``, else ``BASE_FORM_URL``.

        The survey wins over `.env` on purpose. `.env` is shared by every survey
        in the checkout, so letting its URL override a survey's own would send
        one questionnaire's answers into another questionnaire's form.
        """
        return (
            self.form_url_override
            or self.spec.form.url
            or self.settings.base_form_url
        )

    def require_form_url(self) -> str:
        url = self.form_url
        if not url:
            raise ConfigError(
                f"Survey {self.id!r} has no form URL. Set `form.url` in "
                f"{self.spec_path} (or pass --url). Phases 1 and 4 need it; "
                f"phases 2 and 3 do not."
            )
        return url

    @property
    def provider(self) -> str | None:
        return self.provider_override or self.spec.form.provider

    def require_provider(self) -> str:
        if not self.provider:
            raise ConfigError(
                f"Survey {self.id!r} names no provider. Set `form.provider` in "
                f"{self.spec_path}, or pass one: `personabot run <provider> …`."
            )
        return self.provider

    @property
    def max_pages(self) -> int:
        return self.spec.form.max_pages or self.settings.max_pages

    # ------------------------------------------------------------- prompts

    @property
    def base_prompt_pack(self) -> str:
        """``prompt.base``, else the locale's pack if one exists, else ``en``."""
        if self.spec.prompt.base:
            return self.spec.prompt.base
        locale = self.spec.locale.split("-")[0].lower()
        if (self.settings.prompts_dir / "base" / locale).is_dir():
            return locale
        return "en"

    def prompt_search_path(self) -> list[Path]:
        """Template directories, most specific first.

        The survey's own overrides, then a 2.x ``prompt_preset`` directory if the
        spec still names one, then the base pack.
        """
        paths = [self.prompts_dir]
        if self.spec.prompt_preset:
            paths.append(self.settings.prompts_dir / self.spec.prompt_preset)
        paths.append(self.base_prompt_dir)
        return paths

    @property
    def base_prompt_dir(self) -> Path:
        return self.settings.prompts_dir / "base" / self.base_prompt_pack


# ============================================================== resolution

def available_surveys(settings: Settings) -> list[str]:
    """Survey ids under ``SURVEYS_DIR``, excluding templates (``_``-prefixed)."""
    root = settings.surveys_dir
    if not root.is_dir():
        return []
    return sorted(
        entry.name
        for entry in root.iterdir()
        if entry.is_dir()
        and not entry.name.startswith(("_", "."))
        and (entry / SPEC_FILENAME).is_file()
    )


def resolve_spec_path(
    settings: Settings,
    *,
    survey: str | None = None,
    config: Path | None = None,
) -> Path:
    """Which survey YAML to use.

    ``--config`` > ``--survey`` > ``SURVEY_CONFIG`` > ``SURVEY``. The command
    line always beats the environment, and an explicit file beats an id.
    """
    if config is not None:
        return _existing(Path(config), "--config")
    if survey:
        return _survey_yaml(settings, survey, "--survey")
    if settings.survey_config is not None:
        return _existing(settings.survey_config, "SURVEY_CONFIG")
    if settings.survey:
        return _survey_yaml(settings, settings.survey, "SURVEY")

    choices = available_surveys(settings)
    raise ConfigError(
        "No survey selected. Pass --survey <id> (or set SURVEY in .env). "
        f"Available in {settings.surveys_dir}/: {', '.join(choices) or '(none)'}"
    )


def open_workspace(
    settings: Settings,
    *,
    survey: str | None = None,
    config: Path | None = None,
    form_url: str | None = None,
    provider: str | None = None,
) -> Workspace:
    """Resolve, load and bind a survey."""
    from personabot.survey.loader import load_spec

    spec_path = resolve_spec_path(settings, survey=survey, config=config)
    spec = load_spec(spec_path)
    workspace = Workspace(
        settings=settings,
        spec=spec,
        spec_path=spec_path,
        form_url_override=form_url or None,
        provider_override=provider or None,
    )
    _warn_on_inconsistencies(workspace)
    return workspace


def _warn_on_inconsistencies(workspace: Workspace) -> None:
    spec, settings = workspace.spec, workspace.settings
    folder = workspace.survey_dir.name
    if workspace.spec_path.name == SPEC_FILENAME and folder != spec.id:
        log.warning(
            "Survey folder %r declares id %r; output goes to %s.",
            folder, spec.id, workspace.output_dir,
        )
    if (
        spec.form.url
        and settings.base_form_url
        and settings.base_form_url != spec.form.url
        and not workspace.form_url_override
    ):
        log.warning(
            "BASE_FORM_URL in .env (%s) differs from %s's form.url (%s). Using the "
            "survey's own URL; BASE_FORM_URL is only a fallback for surveys that "
            "declare none.",
            settings.base_form_url, spec.id, spec.form.url,
        )


def _survey_yaml(settings: Settings, survey: str, source: str) -> Path:
    path = settings.surveys_dir / survey / SPEC_FILENAME
    if path.is_file():
        return path
    choices = available_surveys(settings)
    raise ConfigError(
        f"{source}={survey!r}: no {path}. Available surveys: "
        f"{', '.join(choices) or '(none)'}. Create one with "
        f"`personabot new-survey {survey}`."
    )


def _existing(path: Path, source: str) -> Path:
    if not path.is_file():
        raise ConfigError(f"{source}: survey config not found: {path}")
    return path
