"""Jinja2 prompt rendering.

Prompts move from JSON strings carrying ``str.format`` placeholders to Jinja2
templates in ``prompts/<preset>/``. Three things this buys:

* **No survey content in Python.** Wording is the preset's business.
* **``StrictUndefined``.** A template referencing a variable the caller does not
  supply raises instead of rendering an empty string. The predecessor's answer
  prompt hard-coded nine Porsline question IDs; when the form was republished
  those IDs silently stopped existing and the model simply received
  instructions about fields that were not there.
* **Loops and conditionals.** The item list, reverse-key tags and trait bands
  are expressed in the template rather than string-concatenated in Python.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateError, TemplateNotFound

from personabot.errors import ConfigError
from personabot.logging_setup import get_logger

log = get_logger(__name__)


class PromptRenderer:
    """Renders the templates of one prompt preset."""

    def __init__(self, preset_dir: str | Path) -> None:
        self.preset_dir = Path(preset_dir)
        if not self.preset_dir.is_dir():
            raise ConfigError(
                f"Prompt preset directory not found: {self.preset_dir}. "
                f"The survey spec's `prompt_preset` must name a directory under prompts/."
            )
        self._env = Environment(
            loader=FileSystemLoader(str(self.preset_dir)),
            undefined=StrictUndefined,
            autoescape=False,
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=False,
        )

    def render(self, template_name: str, **variables: Any) -> str:
        try:
            return self._env.get_template(template_name).render(**variables).strip()
        except TemplateNotFound as exc:
            available = sorted(p.name for p in self.preset_dir.glob("*.j2"))
            raise ConfigError(
                f"Template {template_name!r} not found in {self.preset_dir}. "
                f"Available: {available or '(none)'}"
            ) from exc
        except TemplateError as exc:
            raise ConfigError(
                f"Rendering {template_name} from {self.preset_dir} failed: {exc}. "
                f"Variables supplied: {sorted(variables)}"
            ) from exc

    def render_pair(self, stem: str, **variables: Any) -> tuple[str, str]:
        """Render ``<stem>.system.j2`` and ``<stem>.user.j2`` together."""
        return (
            self.render(f"{stem}.system.j2", **variables),
            self.render(f"{stem}.user.j2", **variables),
        )

    def has(self, template_name: str) -> bool:
        return (self.preset_dir / template_name).is_file()


def build_renderer(prompt_preset: str, *, prompts_root: str | Path = "prompts") -> PromptRenderer:
    return PromptRenderer(Path(prompts_root) / prompt_preset)
