"""PersonaBot — configuration-driven synthetic survey responses.

One codebase, many questionnaires: each form lives in ``surveys/<id>/`` as data.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("personabot")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "3.0.0"
