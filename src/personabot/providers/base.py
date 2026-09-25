"""Provider base classes — the template method that removes ~60% duplication.

The providers independently reimplemented the pagination loop, the ``done/``
move, browser launch options, the Tor pre-flight, success polling, page-loop
failsafes and failure-artifact writing. The base classes own all of that; a
provider supplies only what is genuinely platform-specific.

Bugs fixed here once, rather than once per provider:

* **Unbounded ``while True``** in three extractors and one submitter. A form
  whose navigation heuristic misfired looped forever against a live site.
  Pagination is now bounded by the survey's page budget.
* **Playwright ``has_text`` treated as a regex.** Porsline used
  ``has_text=f"^{value}$"``. Given a *string*, Playwright does substring
  matching and the anchors match literally, so the "exact" tier could never fire
  and every answer fell through to a substring match — where a short option that
  prefixes a longer one selects the wrong choice. :func:`exact_text` builds a
  real anchored regex.
* **Hard-coded receipt paths**, and an unguarded ``open()`` that raised *inside*
  the error handler. Receipts go to the survey's output folder and never raise.
* **Receipts only for some failures.** A pagination stall raised by a provider
  skipped the screenshot — exactly when it is worth the most. Every submission
  failure now writes one.
* **Bare ``except``.** ~50 of them swallowed ``KeyboardInterrupt`` included.
* **No retry when the route failed.** A single bad Tor circuit killed the whole
  unit of work, even though the next SocksPort in the rotation would have
  carried it. Both phases now retry on a fresh circuit; see :func:`on_circuit`.

Submission has two drivers. The ordinary one claims an answer file and submits
it. The **scenario** driver serves between-subjects designs, where the *form*
draws which vignette a session shows: it opens the form first, recognises the
scenario, and only then claims an answer set written for that scenario.
"""

from __future__ import annotations

import asyncio
import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Sequence, TypeVar

from personabot.domain.models import FormSchema, PhaseResult, Question
from personabot.domain.scenarios import split_answer_file
from personabot.errors import (
    CircuitError,
    ExtractionError,
    ItemAlreadyClaimed,
    PageBudgetExceeded,
    SubmissionError,
    TorError,
)
from personabot.infrastructure.browser.factory import BrowserFactory, Circuit
from personabot.infrastructure.browser.tor import TorManager, verify_circuit
from personabot.infrastructure.storage.json_store import read_json_or_none
from personabot.infrastructure.storage.workqueue import WorkQueue
from personabot.logging_setup import get_logger
from personabot.survey.spec import ScenarioSpec

if TYPE_CHECKING:
    from personabot.workspace import Workspace

log = get_logger(__name__)


def exact_text(value: str) -> re.Pattern[str]:
    """An anchored, escaped regex for Playwright's ``has_text``.

    Playwright only treats ``has_text`` as a pattern when given a compiled
    regex; a string is always a substring match. Passing ``"^x$"`` therefore
    looked like exact matching but searched for the literal characters ``^x$``.
    """
    return re.compile(f"^{re.escape(value.strip())}$")


T = TypeVar("T")


async def open_form(page: Any, url: str, *, wait_until: str, timeout_ms: int) -> None:
    """Navigate to the form, treating any failure as a failure of the route.

    Every way this can fail — connect, proxy, TLS, timeout — is a property of
    the route rather than of the form or the selectors, so the whole thing
    collapses to one :class:`CircuitError` and the caller retries elsewhere.
    Raising it *here*, before the provider touches the page, is also what makes
    retrying a submission safe: nothing has been POSTed yet.
    """
    from playwright.async_api import Error as PlaywrightError

    try:
        await page.goto(url, wait_until=wait_until, timeout=timeout_ms)
    except PlaywrightError as exc:
        detail = (str(exc).strip().splitlines() or [""])[0]
        raise CircuitError(
            f"Could not load {url} within {timeout_ms // 1000}s "
            f"(wait_until={wait_until!r}): {detail}"
        ) from exc


async def on_circuit(
    *,
    browser: BrowserFactory,
    label: str,
    attempts: int,
    body: Callable[[Circuit], Awaitable[T]],
) -> T:
    """Run ``body`` on a fresh circuit, retrying elsewhere if the route fails.

    Only :class:`CircuitError` is retried. Anything else — a selector that
    moved, an answer the form rejected, a submission that went through but was
    not confirmed — means the attempt got far enough that another route would
    not help, and for a submitter it may mean the form was already POSTed, so
    retrying would submit twice.

    ``attempts`` comes from ``Settings.effective_browser_attempts``: the full
    budget behind Tor, where each attempt takes a different circuit, and at most
    two without it, where only a hung connection is worth a second try.
    """
    tor = browser.tor

    for attempt in range(1, attempts + 1):
        async with browser.circuit(label=label) as circuit:
            try:
                return await body(circuit)
            except CircuitError as exc:
                tor.burn(circuit.proxy)
                if attempt == attempts:
                    where = (
                        f"all {attempts} attempts failed"
                        if attempts > 1
                        else "the only attempt failed"
                    )
                    raise CircuitError(f"{label}: {where}. Last: {exc}") from exc
                log.warning(
                    "%s: attempt %d/%d failed (%s). Retrying on %s.",
                    label, attempt, attempts, exc,
                    "another circuit" if tor.enabled else "a fresh connection",
                )

    raise AssertionError("unreachable: the loop either returns or raises")


# ============================================================== extraction

class SchemaExtractor(ABC):
    """Phase 1: walk a live form and record its question structure."""

    name: str = "base"

    #: When the initial navigation is considered done. ``networkidle`` waits for
    #: every subresource to settle, which is the safe default for a form that
    #: builds itself from XHR. A provider with a readiness signal of its own
    #: (hydration, a reveal event) should ask for less: over a slow route the
    #: difference is the whole navigation budget, spent waiting for fonts and
    #: images that the parse never reads.
    nav_wait_until: str = "networkidle"

    def __init__(self, workspace: "Workspace", browser: BrowserFactory) -> None:
        self.workspace = workspace
        self.settings = workspace.settings
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
        url = self.workspace.require_form_url()

        schema = await on_circuit(
            browser=self.browser,
            label=f"extract-{self.name}",
            attempts=self.settings.effective_browser_attempts,
            body=lambda circuit: self._extract_once(circuit, url),
        )
        if not schema.questions:
            raise ExtractionError(f"No questions found at {url}.")
        return schema

    async def _extract_once(self, circuit: Circuit, url: str) -> FormSchema:
        """One walk of the form, on the circuit already opened for it."""
        collected: list[Question] = []
        seen: set[str] = set()
        max_pages = self.workspace.max_pages

        page = await circuit.context.new_page()
        page.set_default_timeout(self.settings.ready_timeout_seconds * 1000)
        await open_form(
            page, url,
            wait_until=self.nav_wait_until,
            timeout_ms=self.settings.nav_timeout_seconds * 1000,
        )
        await self.prepare(page)

        for page_number in range(1, max_pages + 1):
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
                f"Still paginating after {max_pages} pages. Either the form is "
                f"longer than the survey's `form.max_pages` (or MAX_PAGES) or the "
                f"navigation heuristic is misfiring — the legacy code would have "
                f"looped here forever."
            )

        return FormSchema(questions=tuple(collected))


# ============================================================== submission

class FormSubmitter(ABC):
    """Phase 4: fill and submit one answer set per browser context."""

    name: str = "base"

    #: See :attr:`SchemaExtractor.nav_wait_until`.
    nav_wait_until: str = "networkidle"

    # A session that drew a scenario with no answer set left is closed unsent.
    # The draw is uniform, so while any cell still has work each session misses
    # with probability at most (k-1)/k; this many misses in a row means the pool
    # and the form disagree, not bad luck ((11/12)^120 ≈ 3e-5 for k=12).
    SCENARIO_MISSES_PER_SCENARIO = 10
    # Sessions in a row whose scenario could not be recognised at all, or that
    # failed before reaching it (form down, Tor broken).
    SCENARIO_UNRECOGNISED_LIMIT = 3

    def __init__(
        self,
        workspace: "Workspace",
        browser: BrowserFactory,
        schema: FormSchema | None = None,
        scenarios: Sequence[ScenarioSpec] = (),
    ) -> None:
        self.workspace = workspace
        self.settings = workspace.settings
        self.browser = browser
        self.schema = schema
        self.scenarios = tuple(scenarios)

    @property
    def tor(self) -> TorManager:
        """The factory's manager — deliberately not one of our own.

        It carries mutable rotation state: which SocksPorts are in cooldown. A
        second instance would record burns that the code choosing the next
        circuit never reads, so every retry would land back on the port that
        just failed.
        """
        return self.browser.tor

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

    async def detect_scenario(self, page: Any) -> str | None:
        """Page forward to the scenario the form drew and return its key.

        Leave the page *on* the scenario, so the fill loop continues from there.
        Return None if no configured scenario appears before the first question.
        Only needed by providers that serve surveys declaring ``scenarios``.
        """
        raise SubmissionError(
            f"The {self.name} provider cannot detect scenarios, but the survey "
            f"declares {len(self.scenarios)}."
        )

    # --- one submission ---

    async def submit_one(self, answers: dict[str, str], label: str) -> None:
        """Submit one answer set. Raises SubmissionError on any failure.

        Retries on a fresh route when the *route* failed, which is the common
        case behind Tor. It is safe to retry only because every
        :class:`CircuitError` here is raised before the form is POSTed —
        preflight, navigation and the provider's readiness gate. Once
        :meth:`fill_page` starts, failures are :class:`SubmissionError` and the
        answer set goes back to the queue rather than being replayed against a
        form that may already hold it.
        """
        await on_circuit(
            browser=self.browser,
            label=label,
            attempts=self.settings.effective_browser_attempts,
            body=lambda circuit: self._submit_once(circuit, answers, label),
        )

    async def _submit_once(self, circuit: Circuit, answers: dict[str, str], label: str) -> None:
        page = await self._open(circuit, label)
        await self._fill_and_submit(page, answers, label)

    async def _open(self, circuit: Circuit, label: str) -> Any:
        """Open the form on ``circuit`` and run the provider's ``prepare``.

        :class:`CircuitError` passes through untouched — the route, not the
        work, and no receipt is worth writing for a page that never arrived.
        Anything else is saved as a receipt and raised as a SubmissionError.
        """
        url = self.workspace.require_form_url()
        if self.tor.enabled:
            await verify_circuit(circuit.context, label=label)

        page = await circuit.context.new_page()
        page.set_default_timeout(self.settings.ready_timeout_seconds * 1000)
        try:
            await open_form(
                page, url,
                wait_until=self.nav_wait_until,
                timeout_ms=self.settings.nav_timeout_seconds * 1000,
            )
            await self.prepare(page)
        except CircuitError:
            raise
        except SubmissionError as exc:
            await self._write_receipt(page, label)
            raise SubmissionError(f"{label}: {exc}") from exc
        except Exception as exc:
            await self._write_receipt(page, label)
            raise SubmissionError(f"{label}: {type(exc).__name__}: {exc}") from exc
        return page

    async def _fill_and_submit(self, page: Any, answers: dict[str, str], label: str) -> None:
        """Fill every page and submit. Never raises CircuitError.

        By the time this runs the form may already hold part of the response —
        or all of it — so a route failure here must not trigger a replay. Any
        CircuitError is therefore converted to a SubmissionError, which returns
        the answer file to the queue instead of retrying it on the spot.
        """
        try:
            filled_total = 0
            budget = min(self.workspace.max_pages, len(answers) + 20)
            for _ in range(budget):
                filled_total += await self.fill_page(page, answers)
                if not await self.advance(page):
                    break
            else:
                raise SubmissionError(f"still paginating after {budget} pages.")

            if not await self.detect_success(page):
                raise SubmissionError(
                    f"no success confirmation after submitting "
                    f"({filled_total} field(s) filled)."
                )

            log.info("%s: submitted (%d field(s) filled).", label, filled_total)

        except (SubmissionError, CircuitError) as exc:
            await self._write_receipt(page, label)
            raise SubmissionError(f"{label}: {exc}") from exc
        except Exception as exc:
            await self._write_receipt(page, label)
            raise SubmissionError(f"{label}: {type(exc).__name__}: {exc}") from exc

    async def _write_receipt(self, page: Any, label: str) -> None:
        """Save a screenshot and HTML dump for a failed submission.

        Honours the survey's output folder and never raises — a failure inside
        the failure handler used to mask the original error.
        """
        from playwright.async_api import Error as PlaywrightError

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        directory = self.workspace.receipts_dir
        try:
            directory.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(directory / f"{stamp}_{label}.png"), full_page=True)
            (directory / f"{stamp}_{label}.html").write_text(
                await page.content(), encoding="utf-8"
            )
            log.info("Receipt written to %s/%s_%s.*", directory, stamp, label)
        except (PlaywrightError, OSError) as exc:
            log.warning("Could not write a receipt for %s: %s", label, exc)

    # --- the phase ---

    async def run(self, *, limit: int | None = None) -> PhaseResult:
        result = PhaseResult(phase="form submission")
        queue = WorkQueue(self.workspace.answers_dir)
        queue.ensure()

        names = queue.pending_names()[: limit or None]
        if not names:
            log.warning("No answer files awaiting submission in %s.", self.workspace.answers_dir)
            return result

        if self.scenarios:
            await self._run_by_scenario(queue, names, result)
        else:
            await self._run_by_file(queue, names, result)

        log.info("Submission finished: %s", result.summary())
        return result

    async def _run_by_file(self, queue: WorkQueue, names: list[str], result: PhaseResult) -> None:
        concurrency = self.settings.concurrent_submissions
        log.info("Submitting %d answer set(s) with concurrency %d.", len(names), concurrency)
        self._log_tor_isolation(concurrency)

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
                        await self.submit_one(split_answer_file(payload)[1], label)
                    except (SubmissionError, CircuitError, TorError) as exc:
                        # CircuitError reaches here only once every route in
                        # the budget has failed. The answer file is released
                        # untouched, so a later run retries it.
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

    def _log_tor_isolation(self, concurrency: int) -> None:
        if not self.tor.enabled or concurrency <= 1:
            return
        ports = len(self.settings.tor_proxy_servers)
        log.info(
            "Tor is on: each context takes the next of %d SocksPort(s), and Tor "
            "never shares a circuit between two of them, so the %d concurrent "
            "submissions travel separate circuits and separate exit IPs.",
            ports, concurrency,
        )
        if concurrency > ports:
            log.warning(
                "CONCURRENT_SUBMISSIONS (%d) exceeds the %d SocksPort(s) in the "
                "rotation, so some in-flight submissions will share a circuit and "
                "an exit IP. Add ports to TOR_SOCKS_EXTRA_PORTS (and to torrc) or "
                "lower the concurrency.",
                concurrency, ports,
            )

    async def _run_by_scenario(
        self, queue: WorkQueue, names: list[str], result: PhaseResult
    ) -> None:
        """Submit when the form, not the file, picks the scenario.

        Each worker opens the form first, sees which scenario it drew, and only
        then claims an answer set written for that scenario. A session whose
        scenario has no work left is closed before anything is answered — with
        partial submission off, that records nothing on the platform.
        """
        known = {scenario.key for scenario in self.scenarios}
        pools: dict[str, list[str]] = {key: [] for key in known}
        for name in names:
            scenario, _ = split_answer_file(read_json_or_none(queue.root / name) or {})
            if scenario in pools:
                pools[scenario].append(name)
            else:
                log.error("%s has no known scenario (%r); regenerate it.", name, scenario)
                result.record_failure(name, f"unknown scenario {scenario!r}")

        log.info(
            "Answer sets per scenario: %s",
            ", ".join(f"{key}={len(pool)}" for key, pool in sorted(pools.items())),
        )

        max_misses = self.SCENARIO_MISSES_PER_SCENARIO * len(known)
        state: dict[str, Any] = {
            "session": 0, "misses": 0, "unrecognised": 0, "errors": 0, "stop": None,
        }

        def should_stop() -> bool:
            if state["stop"] is None:
                if not any(pools.values()):
                    state["stop"] = "done"
                elif state["misses"] >= max_misses:
                    state["stop"] = (
                        f"{state['misses']} sessions in a row drew a scenario with no "
                        f"answer set left"
                    )
                elif state["unrecognised"] >= self.SCENARIO_UNRECOGNISED_LIMIT:
                    state["stop"] = (
                        f"{state['unrecognised']} sessions in a row showed no "
                        f"recognisable scenario"
                    )
                elif state["errors"] >= self.SCENARIO_UNRECOGNISED_LIMIT:
                    state["stop"] = (
                        f"{state['errors']} sessions in a row failed before "
                        f"reaching the scenario"
                    )
            return state["stop"] is not None

        async def worker() -> None:
            while not should_stop():
                state["session"] += 1
                label = f"{self.name}-{state['session']:03d}"

                async with self.browser.circuit(label=label) as circuit:
                    # --- reach the scenario; nothing has been answered yet ---
                    try:
                        page = await self._open(circuit, label)
                    except CircuitError as exc:
                        self.tor.burn(circuit.proxy)
                        state["errors"] += 1
                        log.warning("%s: the route failed before the form loaded: %s", label, exc)
                        continue
                    except (SubmissionError, TorError) as exc:
                        state["errors"] += 1
                        log.error("%s failed before reaching the scenario: %s", label, exc)
                        continue

                    try:
                        drawn = await self.detect_scenario(page)
                    except Exception as exc:  # noqa: BLE001 — any failure here is pre-answer
                        state["errors"] += 1
                        log.error("%s: reading the scenario failed: %s", label, exc)
                        await self._write_receipt(page, label)
                        continue

                    state["errors"] = 0
                    if drawn is None:
                        state["unrecognised"] += 1
                        log.warning("%s: no recognisable scenario on the form.", label)
                        await self._write_receipt(page, label)
                        continue
                    state["unrecognised"] = 0

                    if not pools.get(drawn):
                        state["misses"] += 1
                        log.info("%s: drew scenario %s, none left; closing unsent.",
                                 label, drawn)
                        continue
                    state["misses"] = 0
                    name = pools[drawn].pop()

                    # --- answer it ---
                    try:
                        claimed = queue.claim(name)
                    except ItemAlreadyClaimed:
                        result.skipped += 1
                        continue

                    with claimed:
                        _, answers = split_answer_file(read_json_or_none(claimed.path) or {})
                        log.info("%s: scenario %s -> %s", label, drawn, name)
                        try:
                            await self._fill_and_submit(page, answers, label)
                        except SubmissionError as exc:
                            log.error("%s failed: %s", name, exc)
                            result.record_failure(name, str(exc), retryable=True)
                            continue
                        claimed.complete()
                        result.record_success()

                if self.settings.submission_delay_seconds:
                    await asyncio.sleep(self.settings.submission_delay_seconds)

        async with asyncio.TaskGroup() as group:
            for _ in range(max(1, min(self.settings.concurrent_submissions, len(names)))):
                group.create_task(worker())

        if state["stop"] != "done":
            leftover = sum(len(pool) for pool in pools.values())
            log.error("Stopped with %d answer set(s) unsent: %s.", leftover, state["stop"])
            for pool in pools.values():
                for name in pool:
                    result.record_failure(name, str(state["stop"]), retryable=True)
