"""Provider base classes — the template method that removes ~60% duplication.

The four providers independently reimplemented the pagination loop, the
``done/`` move, browser launch options, the Tor pre-flight, success polling,
page-loop failsafes and failure-artifact writing. The base classes own all of
that; a provider supplies only what is genuinely platform-specific.

Bugs fixed here once, rather than four times:

* **Unbounded ``while True``** in three extractors and one submitter. A form
  whose navigation heuristic misfired looped forever against a live site.
  Pagination is now bounded by ``settings.max_pages``.
* **Playwright ``has_text`` treated as a regex.** Porsline used
  ``has_text=f"^{value}$"``. Given a *string*, Playwright does substring
  matching and the anchors match literally, so the "exact" tier could never fire
  and every answer fell through to a substring match — where a short option that
  prefixes a longer one selects the wrong choice. :func:`exact_text` builds a
  real anchored regex.
* **Hard-coded receipt paths.** Porsline wrote to ``output/receipts/...`` and
  ``output/crash_*.png`` regardless of ``OUTPUT_DIR``, and the ``open()`` was
  not guarded, so a missing directory raised *inside* the error handler.
* **Bare ``except``.** ~50 of them swallowed ``KeyboardInterrupt`` included.
"""

from __future__ import annotations

import asyncio
import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

from personabot.domain.models import FormSchema, PhaseResult, Question
from personabot.errors import (
    ExtractionError,
    ItemAlreadyClaimed,
    PageBudgetExceeded,
    SubmissionError,
    TorError,
)
from personabot.infrastructure.browser.factory import BrowserFactory
from personabot.infrastructure.browser.tor import TorManager, report_exit_ip
from personabot.infrastructure.storage.json_store import read_json_or_none, write_json
from personabot.infrastructure.storage.workqueue import WorkQueue
from personabot.logging_setup import get_logger
from personabot.settings import Settings

log = get_logger(__name__)


def exact_text(value: str) -> re.Pattern[str]:
    """An anchored, escaped regex for Playwright's ``has_text``.

    Playwright only treats ``has_text`` as a pattern when given a compiled
    regex; a string is always a substring match. Passing ``"^x$"`` therefore
    looked like exact matching but searched for the literal characters ``^x$``.
    """
    return re.compile(f"^{re.escape(value.strip())}$")


class SchemaExtractor(ABC):
    """Phase 1: walk a live form and record its question structure."""

    name: str = "base"

    def __init__(self, settings: Settings, browser: BrowserFactory) -> None:
        self.settings = settings
        self.browser = browser

    # --- hooks a provider implements ---

    @abstractmethod
    async def parse_page(self, page: Any) -> list[Question]:
        """Return the questions visible on the current page."""

    @abstractmethod
    async def advance(self, page: Any) -> bool:
        """Move to the next page. Return False at the last page."""

    async def fill_placeholders(self, page: Any, questions: list[Question]) -> None:
        """Satisfy client-side validation so the form will paginate.

        Note this writes placeholder values into a live third-party form. On a
        platform with server-side persistence, intermediate pages may be
        recorded. Override to no-op where the form permits it.
        """

    async def prepare(self, page: Any) -> None:
        """Dismiss a cookie banner, click a start button, and so on."""

    # --- the loop, owned here ---

    async def run(self) -> FormSchema:
        url = self.settings.require_form_url()
        collected: list[Question] = []
        seen: set[str] = set()

        async with self.browser.context(label=f"extract-{self.name}") as context:
            page = await context.new_page()
            await page.goto(url, wait_until="networkidle", timeout=60_000)
            await self.prepare(page)

            for page_number in range(1, self.settings.max_pages + 1):
                questions = await self.parse_page(page)
                fresh = [q for q in questions if q.question_id not in seen]
                for question in fresh:
                    seen.add(question.question_id)
                    collected.append(question)

                log.info(
                    "page %d: %d question(s), %d new (%d total)",
                    page_number, len(questions), len(fresh), len(collected),
                )

                await self.fill_placeholders(page, questions)

                if not await self.advance(page):
                    log.info("Reached the final page after %d page(s).", page_number)
                    break
            else:
                raise PageBudgetExceeded(
                    f"Still paginating after {self.settings.max_pages} pages. Either the "
                    f"form is longer than MAX_PAGES or the navigation heuristic is "
                    f"misfiring — the legacy code would have looped here forever."
                )

        if not collected:
            raise ExtractionError(f"No questions found at {url}.")

        return FormSchema(questions=tuple(collected))


class FormSubmitter(ABC):
    """Phase 4: fill and submit one answer set per browser context."""

    name: str = "base"

    def __init__(self, settings: Settings, browser: BrowserFactory, schema: FormSchema | None = None) -> None:
        self.settings = settings
        self.browser = browser
        self.schema = schema
        self.tor = TorManager(settings)

    # --- hooks a provider implements ---

    @abstractmethod
    async def fill_page(self, page: Any, answers: dict[str, str]) -> int:
        """Fill every answerable field visible now. Return how many were filled."""

    @abstractmethod
    async def advance(self, page: Any) -> bool:
        """Click next/submit. Return False once the form has been submitted."""

    @abstractmethod
    async def detect_success(self, page: Any) -> bool:
        """Return True when the platform confirms the submission landed."""

    async def prepare(self, page: Any) -> None:
        """Per-submission setup before the first page is filled."""

    # --- the loop, owned here ---

    async def submit_one(self, answers: dict[str, str], label: str) -> None:
        """Submit one answer set. Raises SubmissionError on any failure."""
        url = self.settings.require_form_url()

        async with self.browser.context(label=label) as context:
            if self.tor.enabled:
                await report_exit_ip(context, label=label)

            page = await context.new_page()
            page.set_default_timeout(45_000)

            try:
                await page.goto(url, wait_until="networkidle", timeout=60_000)
                await self.prepare(page)

                filled_total = 0
                budget = min(self.settings.max_pages, len(answers) + 20)
                for page_number in range(1, budget + 1):
                    filled_total += await self.fill_page(page, answers)
                    if not await self.advance(page):
                        break
                else:
                    raise SubmissionError(
                        f"{label}: still paginating after {budget} pages."
                    )

                if not await self.detect_success(page):
                    await self._write_receipt(page, label)
                    raise SubmissionError(
                        f"{label}: no success confirmation after submitting "
                        f"({filled_total} field(s) filled). Receipt saved."
                    )

                log.info("%s: submitted (%d field(s) filled).", label, filled_total)

            except SubmissionError:
                raise
            except Exception as exc:
                await self._write_receipt(page, label)
                raise SubmissionError(f"{label}: {type(exc).__name__}: {exc}") from exc

    async def _write_receipt(self, page: Any, label: str) -> None:
        """Save a screenshot and HTML dump for a failed submission.

        Honours ``OUTPUT_DIR`` and never raises — a failure inside the failure
        handler used to mask the original error.
        """
        from playwright.async_api import Error as PlaywrightError

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        directory = self.settings.receipts_dir
        try:
            directory.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(directory / f"{stamp}_{label}.png"), full_page=True)
            (directory / f"{stamp}_{label}.html").write_text(
                await page.content(), encoding="utf-8"
            )
            log.info("Receipt written to %s/%s_%s.*", directory, stamp, label)
        except (PlaywrightError, OSError) as exc:
            log.warning("Could not write a receipt for %s: %s", label, exc)

    async def run(self, *, limit: int | None = None) -> PhaseResult:
        result = PhaseResult(phase="form submission")
        queue = WorkQueue(self.settings.answers_dir)
        queue.ensure()

        names = queue.pending_names()[: limit or None]
        if not names:
            log.warning("No answer files awaiting submission in %s.", self.settings.answers_dir)
            return result

        concurrency = self.settings.concurrent_submissions
        log.info("Submitting %d answer set(s) with concurrency %d.", len(names), concurrency)
        if self.tor.enabled and concurrency > 1:
            log.info(
                "Tor is on: each context gets its own SOCKS credentials, so the %d "
                "concurrent submissions travel separate circuits and separate exit IPs.",
                concurrency,
            )

        semaphore = asyncio.Semaphore(concurrency)

        async def process(index: int, name: str) -> None:
            async with semaphore:
                try:
                    claimed = queue.claim(name)
                except ItemAlreadyClaimed:
                    result.skipped += 1
                    return

                with claimed:
                    payload = read_json_or_none(claimed.path)
                    if not isinstance(payload, dict) or not payload:
                        log.error("%s is empty or unreadable; releasing.", name)
                        result.record_failure(name, "unreadable answer file")
                        return

                    label = f"{self.name}-{index:03d}"
                    try:
                        await self.submit_one(
                            {str(k): str(v) for k, v in payload.items()}, label
                        )
                    except (SubmissionError, TorError) as exc:
                        log.error("%s failed: %s", name, exc)
                        result.record_failure(name, str(exc), retryable=True)
                        return

                    claimed.complete()
                    result.record_success()

                if self.settings.submission_delay_seconds:
                    await asyncio.sleep(self.settings.submission_delay_seconds)

        async with asyncio.TaskGroup() as group:
            for index, name in enumerate(names):
                group.create_task(process(index, name))

        log.info("Submission finished: %s", result.summary())
        return result
