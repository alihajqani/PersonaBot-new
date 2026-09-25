"""Jinja2 prompt rendering over a layered template search path.

Prompts are Jinja2 templates, never Python strings. Three things this buys:

* **No survey content in Python.** Wording is the templates' business.
* **``StrictUndefined``.** A template referencing a variable the caller does not
  supply raises instead of rendering an empty string. The 1.x answer prompt
  hard-coded nine Porsline question IDs; when the form was republished those IDs
  silently stopped existing and the model received instructions about fields
  that were not there.
* **Loops and conditionals.** The item list, reverse-key tags and trait bands
  are expressed in the template rather than string-concatenated in Python.

What 3.0 adds is **layering**. In 2.x every survey carried a full copy of four
templates, and 11 of the 14 surveys' user templates were byte-identical. Now a
template name resolves against a search path, most specific first:

1. ``surveys/<id>/prompts/`` — the survey's own overrides;
2. ``prompts/<prompt_preset>/`` — a 2.x preset, when a legacy spec names one;
3. ``prompts/base/<lang>/`` — the shared base pack.

The base pack is also reachable under the ``base/`` prefix, so an override can
``{% extends "base/answers.system.j2" %}`` and replace a single block instead
of copying the whole file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from jinja2 import (
    ChoiceLoader,
    Environment,
    FileSystemLoader,
    PrefixLoader,
    StrictUndefined,
    TemplateError,
    TemplateNotFound,
)

from personabot.errors import ConfigError
from personabot.logging_setup import get_logger

log = get_logger(__name__)


class PromptRenderer:
    """Renders templates found along a search path."""

    def __init__(
        self,
        search_path: Sequence[str | Path],
        *,
        base_dir: str | Path | None = None,
        globals: dict[str, Any] | None = None,
    ) -> None:
        self.search_path = [Path(p) for p in search_path if Path(p).is_dir()]
        if not self.search_path:
            raise ConfigError(
                "No prompt template directory exists. Looked in: "
                + ", ".join(str(p) for p in search_path)
            )
        loaders: list[Any] = [FileSystemLoader(str(p)) for p in self.search_path]
        if base_dir is not None and Path(base_dir).is_dir():
            loaders.append(PrefixLoader({"base": FileSystemLoader(str(base_dir))}))

        self._env = Environment(
            loader=ChoiceLoader(loaders),
            undefined=StrictUndefined,
            autoescape=False,
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=False,
        )
        # Variables every template can see, e.g. the survey block.
        self._globals = dict(globals or {})

    def render(self, template_name: str, **variables: Any) -> str:
        context = {**self._globals, **variables}
        try:
            return self._env.get_template(template_name).render(**context).strip()
        except TemplateNotFound as exc:
            raise ConfigError(
                f"Template {template_name!r} not found. Searched: "
                f"{', '.join(str(p) for p in self.search_path)}. Available: "
                f"{self.available() or '(none)'}"
            ) from exc
        except TemplateError as exc:
            raise ConfigError(
                f"Rendering {template_name} failed: {exc}. Variables supplied: "
                f"{sorted(context)}. Resolved from: {self.origin(template_name)}"
            ) from exc

    def render_pair(self, stem: str, **variables: Any) -> tuple[str, str]:
        """Render ``<stem>.system.j2`` and ``<stem>.user.j2`` together."""
        return (
            self.render(f"{stem}.system.j2", **variables),
            self.render(f"{stem}.user.j2", **variables),
        )

    def has(self, template_name: str) -> bool:
        return self.origin(template_name) is not None

    def origin(self, template_name: str) -> Path | None:
        """Which directory a template name resolves to — for diagnostics."""
        for directory in self.search_path:
            if (directory / template_name).is_file():
                return directory
        return None

    def available(self) -> list[str]:
        names: set[str] = set()
        for directory in self.search_path:
            names.update(p.name for p in directory.glob("*.j2"))
        return sorted(names)


def build_renderer(workspace: Any) -> PromptRenderer:
    """The renderer for a :class:`~personabot.workspace.Workspace`.

    ``survey`` is published as a global so both phases' templates can read
    ``survey.prompt.*`` without every caller having to pass it.
    """
    spec = workspace.spec
    renderer = PromptRenderer(
        workspace.prompt_search_path(),
        base_dir=workspace.base_prompt_dir,
        globals={"survey": survey_context(spec)},
    )
    log.debug(
        "Prompts for %s: %s", spec.id, " > ".join(str(p) for p in renderer.search_path)
    )
    return renderer


def survey_context(spec: Any) -> dict[str, Any]:
    """What templates see as ``survey``."""
    return {
        "id": spec.id,
        "locale": spec.locale,
        "title": spec.title,
        "prompt": spec.prompt.model_dump(),
    }
