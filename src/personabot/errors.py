"""Exception hierarchy for PersonaBot.

Every failure path in the system raises one of these. This exists so that
`except Exception: pass` is never the right thing to write again — callers can
catch the specific category they know how to handle and let the rest surface.

Two properties matter downstream:

* ``retryable`` — whether the orchestrator should try the same unit of work
  again. Set by the raiser, not guessed by the catcher.
* ``PhaseResult`` collects these per item, so one failed persona or submission
  does not abort a batch, but the process still exits non-zero.
"""

from __future__ import annotations


class PersonaBotError(Exception):
    """Base for every error this application raises deliberately."""

    retryable: bool = False


# ===== CONFIGURATION =====

class ConfigError(PersonaBotError):
    """Invalid or missing runtime configuration (.env / Settings)."""


class SurveySpecError(ConfigError):
    """The survey YAML is malformed, or violates a modelling constraint.

    Raised for things Pydantic cannot express on its own: a latent dimension
    whose squared loadings sum to >= 1, a construct naming a trait that does not
    exist, a reverse_when referencing a question outside its own construct.
    """


class SchemaCoverageError(ConfigError):
    """The survey spec does not account for every question in the form schema.

    This is the guard against the failure that silently ruined every run before
    the refactor: stale content signatures matched nothing, all 51 questions
    collapsed into one catch-all bucket, and the system reported success. A spec
    that leaves questions unassigned — or declares scales and constructs that
    match nothing — is a bug, not a warning.
    """


class ExpressionError(ConfigError):
    """A config expression failed to parse, used a disallowed construct, or
    referenced an unknown name."""


# ===== LLM =====

class LLMError(PersonaBotError):
    """Base for LLM backend failures."""


class LLMQuotaError(LLMError):
    """Rate limited or quota exhausted for one key. Warrants rotating keys.

    Classified from SDK exception types and HTTP status, never by sniffing
    substrings of the message — the previous implementation tested for "rate",
    which matches the "rate" inside "gene-rate", so any error mentioning
    `generate_content` was misread as a quota failure.
    """

    retryable = True


class LLMTransientError(LLMError):
    """Transport-level failure (disconnect, timeout, 5xx). Retry with backoff."""

    retryable = True


class LLMResponseError(LLMError):
    """The call succeeded but the payload is unusable — blocked by safety
    filters, empty candidates, or not parseable as JSON."""

    retryable = True


class LLMKeysExhausted(LLMError):
    """Every configured API key was quota-limited within a single call."""


# ===== BROWSER / PROVIDERS =====

class BrowserError(PersonaBotError):
    """Base for Playwright-driven failures."""


class ExtractionError(BrowserError):
    """Phase 1 could not derive a usable schema from the live form."""


class SubmissionError(BrowserError):
    """Phase 4 could not complete a submission. The answer file stays put."""

    retryable = True


class PageBudgetExceeded(BrowserError):
    """A form walk hit its ``max_pages`` ceiling.

    Replaces the unbounded ``while True`` loops that could spin forever against
    a live site when a navigation heuristic misfired.
    """


# ===== TOR =====

class TorError(PersonaBotError):
    """Tor control port unreachable, auth failed, or circuit renewal failed.

    Raised rather than swallowed: proceeding on the previous exit IP defeats the
    entire point of enabling Tor, so the task fails and is retried instead.
    """

    retryable = True


# ===== STORAGE =====

class StorageError(PersonaBotError):
    """Filesystem work-queue failure."""


class ItemAlreadyClaimed(StorageError):
    """Another worker claimed this file first. Expected under concurrency; the
    caller should skip to the next item rather than treat it as an error."""
