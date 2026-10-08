# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Unique exit IPs per survey** (`UNIQUE_EXIT_IPS`, on by default with Tor).
  Every successful submission records its exit IP in
  `output/<survey>/exit_ips.jsonl`; a circuit whose IP is already there — from
  this run, an earlier run, or a concurrent session — is burned before the form
  is loaded and the submission retried on another circuit. Port rotation alone
  restarted with every process, and two runs a minute apart left from the same
  address.
- Avalform logs the confirmation text after each submission.

### Fixed

- A key parked after a transient error (500/503, dropped connection) was
  logged as "parked … after a quota error", which sent you looking at quotas
  during a provider outage. The log now names the real reason.
- **Avalform extraction submitted the form.** The extractor clicked the last
  page's «ارسال» before checking whether it was the last page, posting a
  placeholder response into the survey owner's data. The page position is now
  read first (hidden `page_number`, «صفحه N از M» in the title) and the last
  page is never submitted during extraction.
- **Avalform options were numeric codes.** Selects and radios were recorded by
  their `value` ("1", "2", …), so the model saw codes instead of choices and the
  submitter selected by a label that did not exist. Options are now the visible
  labels, mapped back to their values when submitting.
- **Avalform matrices and checkbox groups** are now parsed: each matrix row is a
  question with the header's columns as options; a checkbox group is one
  `CHECKBOX` question answered as `"option A | option B"`. A page the server
  refuses reports MachForm's own error messages.

## [3.1.0] — 2026-10-08

Several clients at once, each isolated. Design:
`docs/specs/2026-10-08-client-worktrees-design.md`.

### Added

- **Client worktrees.** `personabot client new|list|sync|deliver` and
  `make client`, `make clients`, `make sync`, `make deliver`. Each client
  questionnaire gets a git worktree under `clients/<client>/<survey>/` on branch
  `survey/<survey>`, with its own `output/`, `.venv` and `.env` (survey preset,
  form URLs dropped, optional key subset via `KEYS=1-7`).
- `deliver` auto-commits survey files, refuses other uncommitted changes,
  rebases onto `main`, runs `validate-config` and the tests in the worktree's
  venv, archives `output/<survey>/` to `clients/_archive/`, tags
  `delivered/<client>/<survey>/<date>`, fast-forwards and pushes `main`, and
  removes the worktree and branch.
- `client list` reports drift from `main`, uncommitted files, keys, queue
  counts, a missing venv, and API keys shared between clients.
- `CLIENTS_DIR` setting.
- README section 8, "Working on several clients at once".

### Changed

- The migrated-surveys test checks that the 14 original surveys are present
  rather than an exact count, so delivered client surveys can join `main`.

- The README is now the complete usage guide: installation, every `.env`
  setting, survey selection, running the pipeline, the command and make-target
  reference, the new-questionnaire workflow, a full `survey.yaml` reference,
  prompt customisation, providers, key pools and quotas, Tor, output formats,
  quality checks, troubleshooting and development.

### Fixed

- A dropped connection (`httpx.ConnectError` and other transport errors) was
  classified as a permanent error: `check-keys` reported working keys as
  "invalid", and generation gave up instead of retrying. Transient errors are
  now recognised anywhere in the exception's class hierarchy.

### Removed

- `docs/HOWTO_NEW_SURVEY.md` — merged into the README (section 7). The survey
  template now points there.

## [3.0.0] — 2026-09-25

The fifteen per-questionnaire copies of PersonaBot 2.x (`PersonaBot-new`,
`-150`, `-Raoufnadi`, `-Soheyli`, `-azaduni`, `-behnaz`, `-fatemehf`, `-haana`,
`-hadi`, `-nyayesh`, `-sazmani`, `-yasamin`, `-zahra` and their copies) merged
into one codebase. Design: `docs/specs/2026-09-25-unified-personabot-design.md`.

### Added

- **Survey folders.** A questionnaire is `surveys/<id>/` — `survey.yaml`, a
  `form_schema.json` snapshot, optional `prompts/` overrides. Select it with
  `--survey <id>`, `SURVEY=<id>` (make / `.env`) or `--config <file>`.
- **`Workspace`** binds one survey to the deployment: output goes to
  `output/<id>/`, so surveys never share a queue.
- **`form:` block** in `survey.yaml` (`provider`, `url`, `max_pages`). The
  survey's URL wins over `BASE_FORM_URL`, which is now only a fallback; a
  disagreement is logged.
- **`prompt:` block** in `survey.yaml` (`role`, `study_context`,
  `persona_sections`, `persona_rules`, `answer_rules`, `scenario_preamble`,
  `vars`) consumed by the new base prompt packs.
- **Base prompt packs** `prompts/base/fa` and `prompts/base/en`, with
  overridable Jinja blocks. The Persian pack carries the answering and persona
  rules the 14 surveys converged on.
- **Layered prompt loader**: `surveys/<id>/prompts/` → legacy
  `prompts/<prompt_preset>/` → `prompts/base/<lang>/`, with `base/…` available
  to `{% extends %}`.
- **Schema snapshot diff**: phase 1 writes into the survey folder, keeps the
  replaced schema as `form_schema.previous.json` and logs added / removed /
  changed question ids.
- **Commands**: `surveys`, `new-survey <id> [--from <id>]`, `--version`,
  `run --url`; `make surveys`, `make new-survey NAME=… [FROM=…]`.
- **Respondent numbering continues automatically** across batches when
  `--start-index` is not given.
- **`_persona`** metadata key in answer files (stripped before submission).
- Settings `BROWSER_LOCALE`, `BROWSER_TIMEZONE`.
- Optional extra `socks` (`pip install -e ".[socks]"`) for a SOCKS `GEMINI_PROXY`.
- The 14 questionnaires from the forks, migrated with their schemas and
  prompts, each covered by the test suite (load, full coverage, prompts render).
- `docs/HOWTO_NEW_SURVEY.md`, `docs/KNOWLEDGE.md`.

### Merged from the 2.x forks

- Porsline end-of-form detection, network-confirmed success, and diagnostic
  "no confirmation" messages (Soheyli, azaduni, zahra, nyayesh, behnaz, 150).
- Porsline dropdown (combobox) support and scenario detection (behnaz).
- Porsline «single page display» groups, group «شروع» exclusion, id-less input
  guard, no auto-advance wait on grouped pages (150).
- Porsline grouped-renderer guard and the `porsline_v2` provider; `VOICE`
  questions; `transcripts` and `export-transcripts`; `index` in expressions and
  `--start-index` (sazmani).
- Between-subjects `scenarios`: balanced assignment, scenario text in the
  answer prompt, scenario-routed submission (behnaz).
- `porsa` provider with matrix rows, already-answered and closed-survey
  diagnosis (haana, hadi).
- Tor isolation per SocksPort, circuit burn/cooldown, preflight health check,
  `CircuitError` retry, `NAV_TIMEOUT_SECONDS` / `READY_TIMEOUT_SECONDS` /
  `BROWSER_MAX_ATTEMPTS`, incognito, `inspect-schema --full` (haana, hadi).
- Google Forms hydration, rejection detection and scripted-click fallback;
  heavy-asset blocking; a receipt for every failed submission (fatemehf).
- Token-budget key pacing (`GEMINI_INPUT_TOKENS_PER_MINUTE`), least-recently
  released key order, server `retryDelay` and quota scope, transient-retry key
  parking, one SDK client per key (sazmani).
- `GEMINI_PROXY` applied to the Gemini client with a pinned httpx transport
  (150, Raoufnadi).
- `check-keys` with proxy awareness, geo-block diagnosis and notes vs warnings
  (150, Raoufnadi, Soheyli, …).
- `status` counts claimed personas and receipts per failure (150).
- `make check-venv` and `python -m pip` in the Makefile (several forks).

### Changed

- **Breaking:** providers are constructed with a `Workspace` instead of
  `Settings`; application phases take `workspace=`.
- **Breaking:** `Settings` no longer carries output paths or
  `require_form_url()`; `OUTPUT_DIR` is the root, per-survey folders beneath it.
- **Breaking:** survey configs moved from `configs/surveys/<id>.yaml` to
  `surveys/<id>/survey.yaml`; prompt presets from `prompts/<id>/` to
  `surveys/<id>/prompts/`. `prompt_preset` is optional (legacy).
- Porsline question scoping merges the behnaz and 150 rules: exclusive wrapper
  → narrowest single-label box with controls → document-order ownership.
- The Porsline grouped-renderer guard fires only when every question reports
  *all* of the page's radios (2.x fired on any identical option lists,
  blocking every Likert group).
- Route retries: the full `BROWSER_MAX_ATTEMPTS` with Tor, at most 2 without.
- Circuit errors raised after filling starts become `SubmissionError`, so a
  possibly-posted form is never replayed.
- `survey.yaml` `version` is `2`; version `1` files still load.

### Fixed

- A second seeded batch reproduced the first batch's draws under new
  respondent numbers; the batch seed now mixes in the start index (the first
  batch still reproduces 2.x output).
- Careless-respondent straight lines differed between processes (`hash()` is
  salted); now seeded from a CRC of the persona id.
- The Gemini client from sazmani ignored `GEMINI_PROXY`.

## [2.0.0] — 2026-08-08

The configuration-driven refactor (`PersonaBot-new`) from which every fork
started: survey YAML, matcher DSL, sandboxed expressions, factor-model
sampler, construct-chunked answering, Persian normalisation, atomic work
queue, provider template methods, psychometric analysis.
