"""Logging configuration.

Called explicitly from ``cli.py``. Importing this module configures nothing —
the predecessor cleared every handler on the root logger at import time, which
meant any program that imported *any* part of PersonaBot silently lost its own
logging setup.

Two other habits from the old code are dropped here:

* ``logging.warning`` was used to render informational messages yellow. Levels
  carry meaning for filtering and alerting; colour is the formatter's job.
  Notable-but-not-wrong events use :func:`log_highlight` instead.
* Handlers were attached to the root logger. Everything now hangs off the
  ``personabot`` logger, so third-party output is unaffected.
"""

from __future__ import annotations

import logging
import sys

_ROOT_NAME = "personabot"
_HIGHLIGHT = 25  # between INFO (20) and WARNING (30)

logging.addLevelName(_HIGHLIGHT, "NOTE")


def get_logger(name: str) -> logging.Logger:
    """Return a child of the ``personabot`` logger.

    Pass ``__name__``; the ``personabot.`` prefix is stripped to keep lines short.
    """
    suffix = name.removeprefix("personabot.")
    return logging.getLogger(f"{_ROOT_NAME}.{suffix}" if suffix else _ROOT_NAME)


def log_highlight(logger: logging.Logger, message: str, *args: object) -> None:
    """Log something noteworthy but not a problem — an exit IP, a phase banner.

    Renders in colour without lying about severity.
    """
    logger.log(_HIGHLIGHT, message, *args)


def configure_logging(level: str = "INFO", *, use_color: bool | None = None) -> None:
    """Configure the ``personabot`` logger. Idempotent.

    Args:
        level: Level name for PersonaBot's own output.
        use_color: Force colour on/off. Defaults to autodetecting a TTY, so
            redirecting to a file yields clean text.
    """
    logger = logging.getLogger(_ROOT_NAME)
    logger.setLevel(level.upper())
    logger.propagate = False  # do not leak into a host application's handlers

    for existing in list(logger.handlers):
        logger.removeHandler(existing)

    if use_color is None:
        use_color = sys.stderr.isatty()

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_build_formatter(use_color))
    logger.addHandler(handler)

    # These are chatty at INFO and drown out our own output.
    for noisy in ("stem", "httpx", "httpcore", "openai", "google_genai", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _build_formatter(use_color: bool) -> logging.Formatter:
    fmt = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
    datefmt = "%H:%M:%S"

    if not use_color:
        return logging.Formatter(fmt, datefmt=datefmt)

    try:
        import colorlog
    except ImportError:
        return logging.Formatter(fmt, datefmt=datefmt)

    return colorlog.ColoredFormatter(
        "%(log_color)s%(asctime)s %(levelname)-7s%(reset)s "
        "%(cyan)s[%(name)s]%(reset)s %(message_log_color)s%(message)s",
        datefmt=datefmt,
        log_colors={
            "DEBUG": "white",
            "INFO": "green",
            "NOTE": "yellow",
            "WARNING": "bold_yellow",
            "ERROR": "bold_red",
            "CRITICAL": "bold_white,bg_red",
        },
        secondary_log_colors={
            "message": {
                "NOTE": "yellow",
                "WARNING": "yellow",
                "ERROR": "red",
                "CRITICAL": "bold_red",
            }
        },
    )
