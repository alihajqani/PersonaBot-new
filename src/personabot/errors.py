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

    Carries what the server said about the violation, because "quota" alone is
    not actionable. A per-minute limit clears on its own in under a minute; a
    per-day one does not, and parking such a key for 60 seconds only produces a
    retry storm that never converges. ``retry_after`` is the server's own
    ``RetryInfo.retryDelay`` — always a better number than a local guess.
    """

    retryable = True

    def __init__(
        self,
        message: str,
        *,
        retry_after: float | None = None,
        scope: str = "unknown",
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.scope = scope


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


class CircuitError(BrowserError):
    """The route this attempt travelled was unusable — dead, too slow, or
    serving something other than the form.

    Separate from ``ExtractionError``/``SubmissionError`` because the remedy is
    different. Nothing is wrong with the work or the selectors, so the caller
    should retry the same item on a *different* Tor circuit rather than give up
    on it. That distinction is not theoretical: measured against the target
    form, three of six circuits were unusable at the same moment — one needed
    64s to deliver a 649 KB page, one returned zero bytes, and one landed on an
    exit IP the form had already seen. A single failed attempt therefore says
    almost nothing about whether the item can succeed.

    **Invariant:** only ever raised *before* anything is POSTed to the form.
    :meth:`FormSubmitter.submit_one` retries on this exception, so raising it
    after a submit would double-submit.
    """

    retryable = True


class AlreadyRespondedError(CircuitError):
    """The form recognised this visitor as having already answered.

    A :class:`CircuitError` because the fix is the same — come back on another
    exit IP — but given its own name because the cause is worth stating. The
    platform answers with an error page that contains no form at all, so
    without this the symptom is an unexplained missing selector, which reads as
    "the page failed to load" and sends you debugging markup that is fine.
    """


class SurveyClosedError(CircuitError):
    """Porsa served its generic error/info page instead of the questionnaire.

    porsa.irandoc.ac.ir reuses one template — a sidebar with «خانه»/
    «نام‌نویسی» links and no ``#pageForm`` — for every page that is not the
    live form: a closed or deactivated survey, an invalid share link, one
    that has not opened yet. A :class:`CircuitError` so the usual bounded
    retry on another circuit still runs first (cheap insurance against a
    transient block masquerading as this page), but named because, unlike
    :class:`AlreadyRespondedError`, a different exit IP will not fix a survey
    the researcher actually closed — the message quotes the page's own text
    so the real reason is visible without guessing.
    """


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
