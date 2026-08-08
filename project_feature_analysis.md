# Project Reverse-Engineering & Feature Analysis

> **Scope of this document.** Every statement below is derived by reading the source tree at `HEAD` (commit `d7504aa`). Where the existing `README.md` or `CLAUDE.md` contradicts the code, the **code** is treated as authoritative and the discrepancy is recorded in §7.

---

## 1. Executive Summary

### 1.1 What the project does

**PersonaBot** is a command-line, script-oriented Python framework that manufactures **synthetic survey response data** and submits it to live online questionnaire platforms through a real, automated browser.

The system is not a generic "form filler". The code reveals a far more specific purpose: it is a **"silicon sampling" research instrument** built around one concrete study — a Persian-language questionnaire on **marital satisfaction, dyadic adjustment, sexual intimacy, and adult attachment**, administered to a target population of **married Iranian women**. The entire answer-generation stack (`core/trait_sampler.py`, `core/instrument.py`, `prompts/answer_generation_prompt.json`, `analysis/validate.py`) is purpose-built for that instrument, down to hard-coded Persian option strings and literal Porsline question IDs.

The pipeline runs in four decoupled phases, each of which persists its output to the filesystem so that phases can be run independently, resumed, or looped:

| Phase | Name | Produces |
|---|---|---|
| 1 | Schema Extraction | `output/form_schema.json` |
| 2 | Persona Generation | `output/personas/<uuid>.json` |
| 3 | Answer Generation | `output/answers/<uuid>.json` |
| 4 | Form Submission | HTTP submission + file moved to `answers/done/` |

A fifth, out-of-band capability exists: **`analysis/validate.py`**, a psychometric validation harness that computes Cronbach's α, inter-item correlations, and response-style indices over the generated data.

### 1.2 What makes this codebase unusual

The distinguishing engineering effort in this project is **not** the browser automation — it is the deliberate machinery for injecting *statistical realism* into LLM-generated survey data. The code explicitly fights the well-documented failure modes of naive LLM survey simulation:

- Latent traits are sampled from a **3-factor multivariate-normal model** in pure Python (`core/trait_sampler.py`) so that inter-construct correlations have a literature-plausible structure *before* the LLM is ever called.
- Answers are **chunked by psychometric construct** rather than requested in one shot, explicitly to break the self-consistency that inflates Cronbach's α (`core/instrument.py` docstring).
- **Method variance** (acquiescence bias, extreme-response style, midpoint-hugging) is sampled per respondent and injected as natural-language nudges.
- **Careless/inattentive responders** (~4% of the population) are simulated *in code* via a straightlining algorithm, on the explicit reasoning that "LLMs cannot convincingly fake inattention".
- Demographic identity questions are answered **deterministically from the persona record**, never by the LLM, to guarantee identity consistency across a long questionnaire.

### 1.3 Inferred target audience and use case

The primary user is a **single technical researcher or small research team** operating the tool from a terminal on their own machine. Evidence: `Makefile`-driven workflows, `.env`-based single-tenant configuration, no web service, no database, no test suite, no authentication, output written to a local `output/` directory, and Persian-language logging comments addressed to a specific individual ("بر اساس HTML ارسالی شما" — "based on the HTML you sent").

The intended output is a corpus of synthetic questionnaire responses. The persona prompt states the study is a **"disclosed AI-persona study"** whose accompanying paper declares respondents to be AI-simulated. The Phase 4 submission machinery — Tor IP rotation, randomized inter-action delays, atomic file claiming for parallel workers — nonetheless implements capabilities characteristic of undisclosed bulk submission against live third-party survey platforms. This tension is noted here as an objective observation about the code's capabilities and is relevant to any refactoring decision about which features to preserve.

---

## 2. Core Features (Functional Requirements)

### Feature 2.1 — Dynamic Provider Loading

- **Trigger/Input:** Mandatory positional CLI argument: `python main.py <provider>`
- **Business Logic** (`main.py:20-53`):
  1. `ProviderManager.__init__` stores the provider name and immediately calls `_load_modules()`.
  2. For each of the two required module names (`schema_extractor`, `form_submitter`), `importlib.util.find_spec(f"providers.{provider}.{module}")` probes for existence without importing.
  3. If the spec resolves, `importlib.import_module` performs the real import; otherwise `None` is stored and an error logged.
  4. `is_valid()` returns `True` only if **both** modules loaded.
  5. `main()` calls `sys.exit(1)` if the provider is invalid.
- **Output/Result:** Two live module objects held on the `ProviderManager`, or process exit code 1.
- **Note:** There is no interface/ABC enforcement. The contract ("`run()` must be an async coroutine") is convention-only and unverified; a provider missing `run()` fails at call time with `AttributeError`, not at load time.

---

### Feature 2.2 — Phase Orchestration

- **Trigger/Input:** `--phases` (default `"1,2,3,4"`), `--num-personas` (default `5`)
- **Business Logic** (`main.py:90-116`):
  1. `utils.setup_directories()` creates `output/`, `personas/`, `answers/`, `receipts/`, and `answers/done/`.
  2. Provider is loaded and validated.
  3. `args.phases` is split on `,`, each element `.strip()`ed and cast with `int()`.
  4. Four independent `if N in phases_to_run:` guards dispatch to, in fixed order: provider schema extractor → `persona_generator.run()` → `answer_generator.run()` → provider form submitter.
- **Output/Result:** Side effects on the filesystem per phase. Always logs `"All selected phases have been executed."` regardless of whether individual phases succeeded.
- **Hidden behavior:** Phase execution order is hard-coded by the sequence of `if` statements, **not** by the order given in `--phases`. `--phases 4,1` runs phase 1 first. Unknown phase numbers (e.g. `--phases 7`) are silently ignored; non-numeric values raise an uncaught `ValueError`.

---

### Feature 2.3 — Phase 1: Schema Extraction (4 provider implementations)

- **Trigger/Input:** `--phases 1`; reads `config.BASE_FORM_URL`
- **Common Business Logic:** Every extractor follows the same shape: launch Chromium → navigate → loop over form pages → parse questions from the DOM into a normalized dict → **fill dummy answers to satisfy client-side validation** → click Next → stop at the Submit button → save via `utils.save_json_file`.
- **Output/Result:** `output/form_schema.json` — a JSON **array** of objects:
  ```json
  {"question_id": "...", "question_text": "...", "type": "...", "options": [{"text": "...", "value": "..."}]}
  ```

Per-provider mechanics:

| Provider | ID source | Type detection | Page-advance strategy | Loop bound |
|---|---|---|---|---|
| **porsline** (`schema_extractor.py:198`) | `label[for="question-N"]` → `N` | `<table>`→`MATRIX`; `div[role=radiogroup]`/`choice_wrapper`→`RADIO`; else `TEXT_INPUT` | **Auto-advance detection**: compares the set of visible question IDs before/after interaction; falls back to `بعدی`/`تایید` buttons | **Unbounded `while True`** |
| **avalform** (`schema_extractor.py:150`) | `input[name]` attribute | `<select>`→`SELECT`; `type=radio`→`RADIO`; `textarea`/`text`→`TEXT_INPUT`; `li.matrix`→`MATRIX_RADIO` (multi-row expansion) | Classic full page loads via `input[name=submit_primary]` | **Unbounded `while True`** |
| **google_forms** (`schema_extractor.py:96`) | Regex `,\[\[(\d+),` over `data-params` → `entry.<N>` | `div[role=radiogroup]`→`RADIO`; `textarea`→`TEXT_AREA`; `input[type=text]`→`TEXT_INPUT` | `div[jsname=OCpkoe]` (Next) / `div[jsname=M2UYVd]` (Submit) | **Unbounded `while True`** |
| **digisurvey** (`schema_extractor.py:154`) | `div.question[data-id]` | radio / checkbox → `RADIO`/`CHECKBOX`; `textarea`→`TEXT_INPUT`; unknown → **skipped** | `button.btn.next.green`; submit identified by the text `ارسال` in the button | **`max_pages = 50`** |

Notable per-provider behaviors:
- **Porsline** implements a three-tier auto-advance heuristic: immediate ID-set comparison after a 1.5 s sleep, explicit button search, and a "late auto-advance" recheck after a further 2 s if a radio was clicked but nothing appeared to change.
- **Porsline** detects `MATRIX` questions but **never populates their options** — the branch contains only the comment `# Matrix logic...`. Matrix questions therefore reach the schema as `type: "MATRIX"` with `options: []`.
- **Avalform** uses a documented "dual-strategy" for radios: attempt `label[for=...]` click first (simple radios), fall back to `input.check()` (matrix radios with hidden labels).
- **DigiSurvey** re-waits up to 8 s and re-parses when a page yields zero new questions, to accommodate late SPA rendering.
- Dummy-fill values are hard-coded per provider: `"test_data"`/`"25"`/`"test@example.com"` (porsline), `"dummy_text"`/`random.randint(20,50)` (avalform), `"dummy text"`/`"25"` (google_forms), `"25"` (digisurvey).

---

### Feature 2.4 — Phase 2: Persona Generation

- **Trigger/Input:** `--phases 2 --num-personas N`
- **Business Logic** (`core/persona_generator.py`):
  1. Load `output/form_schema.json`; **abort if absent**. (The loaded schema is passed into `generate_and_save_personas` but is **never read** — see §7.)
  2. `trait_sampler.sample_population(N)` draws N complete synthetic respondents (traits + response styles + demographics) using **pure standard-library statistics**, with **no seed** (non-reproducible by default).
  3. Personas are split into batches of `PERSONA_BATCH_SIZE` (default 10) — explicitly to keep each HTTP request short enough to avoid proxy/server response timeouts.
  4. Per batch: `build_persona_prompts` renders each profile through `trait_sampler.format_profile_fa()` into a Persian text block and substitutes it into the templates in `prompts/persona_generation_prompt.json`.
  5. Temperature is randomized per batch: `random.uniform(0.75, 0.95)`.
  6. The LLM returns a JSON **array**; the code accepts either a bare list or `{"personas": [...]}`.
  7. Each returned item is matched back to its sampled profile by `id`. Items with an unknown `id` or missing `details` are discarded with a warning.
  8. **Merge step:** the persisted persona is the *sampled ground truth* (demographics, z-scores, bands, response style) plus **only** the `details` prose from the LLM. The LLM cannot alter the numeric ground truth.
  9. Sleep `AI_CALL_DELAY_SECONDS` between batches (skipped after the final batch).
- **Output/Result:** One file per persona at `output/personas/<uuid4>.json`. Note the **filename UUID is unrelated to the internal `id` field** (`persona_1`, `persona_2`, …).
- **Resilience:** A batch that fails to parse or raises returns `[]`, so remaining batches still execute. The final log line reports `saved/requested`, which may be less than requested.

**Persisted persona record:**
```json
{
  "id": "persona_1",
  "demographics": { "gender", "age", "education_level", "occupation",
                    "children_count", "years_married", "psych_problem",
                    "region", "religiosity", "household", "economic_situation" },
  "latent_traits_z":    { "<6 dimensions>": float },
  "latent_traits_band": { "<6 dimensions>": "<Persian band label>" },
  "response_style":     { "acquiescence": float, "extreme_response": float, "careless": bool },
  "details":            { "<4 Persian prose sections>": "..." }
}
```

---

### Feature 2.5 — Phase 3: Answer Generation (the system's most complex feature)

- **Trigger/Input:** `--phases 3`
- **Preconditions:** `form_schema.json` must exist; `output/personas/` must contain at least one `.json`.
- **Business Logic** (`core/answer_generator.py`), per persona file:

  **Step 1 — Static (non-LLM) answers.** `instrument.static_answers(schema, persona)` classifies every schema question and answers demographic questions **directly from the persona record**, bypassing the LLM entirely: gender, psychological-problem disclosure, occupation, children count, years married. Questions classified as `instruction` receive the literal string `"0"`. Rationale in the source: guarantees identity consistency and that every respondent is female.

  **Step 2 — Careless short-circuit.** If `persona.response_style.careless` is `True`, `instrument.careless_answers()` generates a straightlining pattern **in code** and the function returns immediately without any LLM call. Algorithm: pick a random option index as an anchor per construct; for each item, use the anchor with **70%** probability, otherwise drift ±1 position (clamped to range). Seeded deterministically from `hash(persona_id) & 0xFFFFFFFF`.

  **Step 3 — Construct-chunked LLM generation.** `instrument.group_by_construct()` buckets the psychometric questions into up to 10 constructs in a stable order (demographics/instructions excluded). For each construct:
   - Items are **shuffled** so they are not presented in scale order.
   - `build_chunk_prompts` assembles a prompt containing: the persona's demographics + prose (**never the raw z-scores**), only the trait *bands as Persian words* relevant to that construct, a response-style instruction sentence, and the item list.
   - For three constructs (`satisfaction`, `attachment_avoidance`, `attachment_anxiety`) each item is tagged `[معکوس]` (reverse-keyed) or `[مستقیم]` (direct) via `instrument.is_reverse()`.
   - Temperature randomized per chunk: `random.uniform(0.55, 0.85)`.
   - The response is passed through `_extract_json()` (first `{` … last `}`) to strip markdown fences, parsed, and validated.
   - Chunk failures are logged and **swallowed** — remaining constructs still run.
   - Sleep `AI_CALL_DELAY_SECONDS` after each chunk.

  **Step 4 — Validation & self-correction** (`validate_and_clean_answers`): see Feature 2.6.

  **Step 5 — Completeness gate & file transition.** If `len(answers) >= len(schema) * 0.8`, write `output/answers/<same filename>.json` and `shutil.move` the persona into `output/personas/done/`. Otherwise log an error and **leave the persona in place for retry**.

- **Output/Result:** A flat `{question_id: answer_string}` JSON object per persona.

---

### Feature 2.6 — Fuzzy Answer Validation & Self-Correction

- **Trigger/Input:** Called internally per construct chunk with the **chunk** (not the full schema) as the reference.
- **Business Logic** (`core/answer_generator.py:86-133`):
  1. Keys beginning with `_` are skipped — this discards the `_reasoning` field the prompt mandates.
  2. Keys not present in the reference set are discarded as "Rogue question_id".
  3. For choice questions: `utils.normalize_string()` is applied (strip whitespace, strip trailing `.,؛`, collapse `، ` → `،`). Exact match wins immediately.
  4. Otherwise `thefuzz.fuzz.ratio` is computed against every valid option; the best match is accepted only if its ratio is **strictly greater than 85**. Accepted corrections are logged as `SELF-CORRECTION`.
  5. Below threshold, the answer is **dropped** (logged as `DISCARDING`) rather than guessed.
  6. Text questions are passed through as `str(value).strip()` with no validation.
  7. Missing question IDs are counted and warned about.
- **Output/Result:** A cleaned dict merged into the persona's accumulating answer set.

---

### Feature 2.7 — Phase 4: Form Submission (4 provider implementations)

- **Trigger/Input:** `--phases 4`; reads every `.json` in `output/answers/`
- **Common Business Logic:** For each answer file → optionally renew the Tor IP → launch a **fresh browser instance** → navigate → fill visible questions → advance → click submit → verify a success signal → move the file to `answers/done/` on success only.
- **Output/Result:** A live HTTP submission; the answer file relocated to `done/`; screenshots or HTML dumps in `output/receipts/` on failure.

Per-provider divergence is substantial:

| Aspect | porsline | google_forms | avalform | digisurvey |
|---|---|---|---|---|
| Success detection | 5-selector list incl. `سپاس‌گزاریم`, polled 15× at 1 s | `div.vHW8K` wait, 60 s | `div.form_success > h2:has-text(...)`, 30 s | URL contains `finish`/`done`/`result`, **or** the answer form is gone, **or** thank-you text |
| Submit click | `element.click()` via JS `evaluate`, force-click fallback | Standard `.click()` | Standard `.click()` | JS `evaluate` for submit, standard for next |
| Page-loop bound | `len(answers) + 20` | `15` | `10` | `50` |
| Uses schema at runtime | No (DOM-driven) | No (DOM-driven) | **Yes** — builds `schema_map` for typed dispatch | No (DOM-driven) |
| Inter-submission delay | `5 s` | `15 s` | `SUBMISSION_DELAY_SECONDS` env, default `15` | `5 s` |
| `slow_mo` | **`random.randint(2,4)*1000`** (2–4 s, ignores config) | `config.SLOW_MO` | `config.SLOW_MO` | `config.SLOW_MO` |
| Concurrency safety | **Atomic `inprogress/` claim** | None | None | None |
| Failure artifacts | HTML → hard-coded `output/receipts/`; screenshot → hard-coded `output/crash_*.png` | None | Screenshot → `RECEIPTS_DIR_PATH` | HTML + screenshot → `RECEIPTS_DIR_PATH` |

---

### Feature 2.8 — Concurrent-Worker Safety via Atomic File Claiming (porsline only)

- **Trigger/Input:** Two or more shells running `make submit PROVIDER=porsline` simultaneously.
- **Business Logic** (`providers/porsline/form_submitter.py:305-346`):
  1. `os.rename(answers/f.json, answers/inprogress/f.json)` — atomic on a single filesystem.
  2. Exactly one worker succeeds; every other worker catches `FileNotFoundError`/`OSError` and skips that file, preventing duplicate submissions.
  3. On confirmed success, an inner `mark_done()` closure moves the file from `inprogress/` to `done/` **the instant** success is detected (guarded by a `moved` flag so it runs at most once, across three separate success-detection call sites).
  4. On failure, the claim is **released** back to `answers/` so a later run can retry.
  5. An empty/unparseable answer file also releases its claim.
- **Output/Result:** `answers/inprogress/` acts as a distributed lock directory.

---

### Feature 2.9 — Tor IP Rotation

- **Trigger/Input:** `USE_TOR="True"`
- **Business Logic:**
  1. `utils.renew_tor_ip()` opens a `stem` `Controller` on `TOR_CONTROL_PORT`, authenticates with `TOR_CONTROL_PASSWORD`, checks `is_newnym_available()` (waiting via `wait_for_newnym()` if throttled), then sends `Signal.NEWNYM`.
  2. Callers sleep afterwards to let the circuit establish: 5 s (porsline, avalform, digisurvey) or 10 s (google_forms).
  3. The browser is launched with `proxy={"server": "socks5://host:port"}`.
  4. Three of four submitters navigate to `https://checkip.amazonaws.com` first to log the exit IP.
- **Divergent failure handling:** **avalform aborts** the submission if renewal fails; **google_forms continues on the old IP**; **porsline and digisurvey swallow the exception entirely** with a bare `except`.

---

### Feature 2.10 — Psychometric Validation Harness

- **Trigger/Input:** `python -m analysis.validate [--reference norms.json]` (standalone; **not reachable from `main.py` or the `Makefile`**)
- **Business Logic** (`analysis/validate.py`):
  1. Loads responses from **both** `answers/` and `answers/done/`.
  2. For each construct, builds a numeric matrix via `instrument.numeric_code()`, retaining only **listwise-complete** respondents; aborts a construct with `n < 3`.
  3. **Automatic reverse-item detection:** computes the corrected item-total correlation per item; any item with a negative correlation is reflected as `(1 + K) - v` and the matrix/totals recomputed.
  4. Computes Cronbach's α, mean inter-item *r*, corrected item-total correlations, scale mean/SD.
  5. Computes response-style indices per respondent: extreme-response rate (`c == 1 or c == K`), acquiescence rate (`c >= K/2 + 0.5`), midpoint rate (odd-length scales only), and flags straightlining when the within-respondent SD `< 0.5`. Respondents with fewer than 5 codable answers are excluded.
  6. Optionally diffs against a published-norms JSON.
  7. Prints **sanity warnings**: α > 0.95 ("implausibly high") and mean inter-item *r* > 0.6 ("over-clean correlation structure").
- **Output/Result:** Console report + `output/validation_report.json`. All statistics are implemented in pure Python — **no numpy/scipy**.

---

### Feature 2.11 — Output Cleaner

- **Trigger/Input:** `python cleaner.py <directory>` — standalone, requires typing `yes` at an interactive prompt.
- **Business Logic:** Scans `*.json` in the directory, loads each, skips non-dict payloads, and **permanently deletes** any file with fewer than `MIN_ENTRIES_THRESHOLD = 90` top-level keys.
- **Output/Result:** Files deleted from disk; a scanned/kept/deleted summary printed.
- **Note:** The threshold is a module-level constant with **no CLI override and no relationship to the live schema length**. `cleaner.py` does not import `config`, so it is the only entry point that runs without a valid `.env`.

---

### Feature 2.12 — Makefile Workflow Automation

- **Trigger/Input:** `make <target>` with optional variable overrides.
- **Targets:** `install` (pip + `playwright install`), `clean` (**`rm -rf output/`** plus `__pycache__` pruning), `status`, `run-all`, `schema`, `persona`, `answer`, `submit`, `loop`, `help`.
- **`loop`:** a shell `for` loop running `main.py` `RUN_COUNT` times (default 10) with `DELAY_SECONDS` (default 120) between runs, skipping the delay after the final run. This is the mechanism for bulk generation/submission.
- **`status`:** counts pending vs. `done/` files in `personas/` and `answers/`, checks for the schema file, and counts receipts.
- **Defaults:** `PROVIDER=porsline`, `PHASES=1,2,3,4`, `NUM_PERSONAS=5`.

---

## 3. Data Models & State Management

### 3.1 Formal models

**There are none.** The project contains **zero** ORM models, zero Pydantic schemas, and zero `@dataclass` declarations. Every structure is an untyped `Dict[str, Any]` passed between functions, with shape enforced only by convention and by `KeyError` at runtime. Type hints are present but are generic container types (`List[Dict[str, Any]]`), which document nothing about the payload.

### 3.2 Implicit data structures

**(a) Question / Schema Entry** — produced by all 4 extractors, consumed by phases 2–4 and `analysis/`:
```python
{"question_id": str, "question_text": str, "type": str, "options": [{"text": str, "value": str}]}
```
`type` is a free-form string. Values actually emitted across providers: `TEXT_INPUT`, `TEXT_AREA`, `RADIO`, `SELECT`, `CHECKBOX`, `MATRIX`, `MATRIX_RADIO`, `UNKNOWN`. **No provider emits the same complete set**, and no consumer handles all of them.

**(b) Persona Record** — see §2.4. Six latent dimensions: `marital_satisfaction`, `dyadic_cohesion`, `partner_responsiveness`, `relationship_commitment`, `attachment_avoidance`, `attachment_anxiety`.

**(c) Answer Record** — a flat `Dict[str, str]`. All values are strings, including numeric answers. The `_reasoning` key mandated by the prompt is generated by the LLM and then **stripped** during validation, so the model's stated rationale is never persisted.

**(d) Validation Report** — nested dict of per-construct statistics plus a `response_styles` block.

### 3.3 Persistence

**The filesystem is the database.** There is no SQLite, no Redis, no cache layer, no message queue.

State is encoded in **file location**, giving a directory-as-state-machine:

```
output/
├── form_schema.json            # phase-1 artifact; single global schema
├── validation_report.json      # analysis artifact
├── personas/
│   ├── <uuid>.json             # STATE: awaiting answer generation
│   └── done/<uuid>.json        # STATE: answers generated
├── answers/
│   ├── <uuid>.json             # STATE: ready to submit
│   ├── inprogress/<uuid>.json  # STATE: claimed by a worker (porsline only)
│   └── done/<uuid>.json        # STATE: successfully submitted
└── receipts/                   # screenshots + failed-submission HTML dumps
```

Transitions are performed with `shutil.move` (phases 3, 4) and `os.rename` (porsline's atomic claim). The `<uuid>.json` filename is the correlation key that links a persona to its answers to its submission — but it is **not** stored inside the answer file, so an answer record carries no reference back to the persona that produced it.

### 3.4 In-memory state

- `config.py` module-level globals — the de-facto service locator; every module imports `config` directly.
- `config.gemini_api_key_manager` — a process-wide singleton holding a mutable `current_index` guarded by a `threading.Lock`.
- Per-run sets (`processed_ids`, `seen_ids`) preventing duplicate schema entries across paginated forms.

---

## 4. Architecture & Module Breakdown

### 4.1 Directory map and responsibilities

```
PersonaBot/
├── main.py                 # CLI parsing, ProviderManager, phase dispatch
├── config.py               # .env loading, logging setup, module-level global settings
├── utils.py                # File I/O, directory setup, Tor control, string/regex helpers
├── cleaner.py              # Standalone destructive pruning utility
├── Makefile                # Workflow automation and looping
├── requirements.txt        # 10 unpinned dependencies
├── .env.example            # Documented configuration template
│
├── core/                   # Platform-agnostic generation logic
│   ├── services.py         # APIKeyManager (thread-safe round-robin)
│   ├── llm_client.py       # LLMClient ABC + GeminiClient + VLLMClient + factory
│   ├── trait_sampler.py    # Latent-trait/demographic sampler (pure stdlib statistics)
│   ├── instrument.py       # Questionnaire domain knowledge: classification, coding, reverse-keying
│   ├── persona_generator.py# Phase 2
│   └── answer_generator.py # Phase 3 + fuzzy validation
│
├── providers/              # One package per survey platform (Strategy pattern)
│   ├── google_forms/       # has __init__.py
│   ├── porsline/           # has __init__.py
│   ├── avalform/           # NO __init__.py  (implicit namespace package)
│   └── digisurvey/         # NO __init__.py  (implicit namespace package)
│
├── prompts/                # Externalized prompt templates as JSON
│   ├── persona_generation_prompt.json
│   └── answer_generation_prompt.json
│
└── analysis/               # NO __init__.py
    └── validate.py         # Standalone psychometric harness
```

### 4.2 Architectural pattern

The system is a **procedural, script-based monolith** organized into a **staged batch pipeline**, with a **partial Strategy pattern** applied at the provider boundary and an **Abstract Factory** applied at the LLM boundary.

**Concretely:**
- **Not** MVC, layered, hexagonal, or Clean Architecture. There is no domain layer, no repository abstraction, no dependency injection, and no inversion of control.
- **Coupling direction is inverted from Clean Architecture:** `core/` (business logic) imports `config` (infrastructure) directly, and `core/instrument.py` embeds domain knowledge about one specific questionnaire as hard-coded Persian string literals. Infrastructure concerns and domain concerns are interleaved.
- **The only genuine abstraction is `core/llm_client.LLMClient`** — a proper base class with two interchangeable implementations behind a `get_llm_client()` factory. This is the one seam in the codebase that would survive a Clean Architecture refactor intact.
- **The provider "Strategy" is duck-typed and unenforced**: the contract is a module exposing `async def run()`, resolved by string-based dynamic import. No ABC, no `Protocol`, no registry.
- **Phases communicate only through the filesystem**, which is architecturally a strength — it makes each phase independently runnable, resumable, and parallelizable, and is the property the `Makefile loop` target and the porsline `inprogress/` claim mechanism both exploit.

### 4.3 Execution flow

```
$ python main.py porsline --phases 1,2,3,4 --num-personas 5
  │
  ├─ import config ──────► load .env, configure colorlog, build APIKeyManager,
  │                        validate BASE_FORM_URL (raises at import if missing)
  ├─ utils.setup_directories()
  ├─ ProviderManager("porsline") ──► importlib → 2 modules
  │
  ├─ Phase 1 ─► providers.porsline.schema_extractor.run()
  │             └─► Playwright → paginate → BeautifulSoup parse → form_schema.json
  ├─ Phase 2 ─► core.persona_generator.run(5)
  │             ├─► trait_sampler.sample_population(5)     [no LLM]
  │             └─► llm_client.generate() × ceil(5/10)     → personas/*.json
  ├─ Phase 3 ─► core.answer_generator.run()
  │             └─ per persona:
  │                ├─► instrument.static_answers()          [no LLM]
  │                ├─► if careless: instrument.careless_answers() → return  [no LLM]
  │                ├─► per construct: llm_client.generate() → validate → merge
  │                └─► 80% gate → answers/*.json + persona → personas/done/
  └─ Phase 4 ─► providers.porsline.form_submitter.run()
                └─ per answer file: atomic claim → Tor renew → browser →
                   fill → submit → verify → answers/done/
```

---

## 5. External Integrations & Dependencies

### 5.1 Libraries (`requirements.txt` — **all unpinned**)

| Package | Purpose in this codebase |
|---|---|
| `playwright` | Async browser automation; the sole mechanism for both extraction and submission |
| `beautifulsoup4` | HTML parsing in the porsline, avalform, and digisurvey extractors (google_forms parses via Playwright locators instead) |
| `google-genai` | Gemini SDK (**new** `google.genai` v1+ client, imported lazily inside `GeminiClient.generate`) |
| `openai` | `AsyncOpenAI` client used to reach any OpenAI-compatible endpoint (vLLM/Ollama/LM Studio) |
| `thefuzz` + `python-Levenshtein` | Fuzzy matching of LLM answers against valid options (85% threshold) |
| `python-dotenv` | `.env` loading at `config` import time |
| `colorlog` | Coloured console logging; `WARNING` is deliberately used to render some informational messages yellow |
| `stem` | Tor control-port protocol (`Signal.NEWNYM`) |
| `PySocks` | SOCKS5 support for the Tor proxy |

**Notably absent:** any testing framework, any linter/formatter config, `numpy`/`scipy` (all statistics are hand-rolled), and any packaging metadata (`pyproject.toml`, `setup.py`).

### 5.2 External services

| Service | Purpose | Failure mode |
|---|---|---|
| **Google Gemini API** | Persona prose + answer generation | Rotate keys on quota, exponential backoff on transient, `RuntimeError` when all keys exhausted |
| **vLLM / OpenAI-compatible endpoint** | Alternative LLM backend | **No retry, no timeout, no key rotation** |
| **Target survey platform** | The form at `BASE_FORM_URL` (Porsline, Google Forms, Avalform, DigiSurvey) | Per-provider heuristics; failure leaves the answer file unmoved |
| **Tor SOCKS + Control port** | Per-submission IP rotation | Inconsistent per provider (§2.9) |
| **`checkip.amazonaws.com`** | External IP verification before submission | Wrapped in try/except (porsline, digisurvey); 3-attempt retry (google_forms) |
| **HTTP proxy** | Optional egress proxy for Gemini | Set globally via `os.environ`, affecting the whole process |

### 5.3 Environment variables

Read via `os.getenv` in `config.py` unless noted.

| Variable | Default | Required | Effect |
|---|---|---|---|
| `BASE_FORM_URL` | — | **YES** | **`raise ValueError` at import time if unset.** Blocks every entry point except `cleaner.py`. |
| `AI_PROVIDER` | `"gemini"` | No | `"gemini"` \| `"vllm"`; anything else logs an error at import and raises in the factory |
| `GEMINI_API_KEY_1..N` | — | If gemini | Enumerated from `_1` upward; scanning **stops at the first gap** |
| `GEMINI_MODEL_NAME` | `"gemini-2.5-pro"` | No | Model ID |
| `GEMINI_PROXY` | `None` | No | If set, `setdefault`s `HTTPS_PROXY`/`HTTP_PROXY` **process-wide** |
| `VLLM_BASE_URL` | `http://localhost:8000/v1` | No | OpenAI-compatible endpoint |
| `VLLM_MODEL_NAME` | `""` | If vllm | Empty value → warning at import, `RuntimeError` in the factory |
| `VLLM_API_KEY` | `"EMPTY"` | No | Placeholder token |
| `AI_CALL_DELAY_SECONDS` | `0` | No | Inter-call sleep; free-tier Gemini guidance is 20 |
| `PERSONA_BATCH_SIZE` | `10` | No | Personas per LLM call |
| `LLM_TIMEOUT_SECONDS` | `180` | No | **Gemini only** (converted to ms) |
| `LLM_MAX_RETRIES` | `3` | No | **Gemini only**, transient errors |
| `HEADLESS_MODE` | `"True"` | No | Compared as `.lower() == "true"` |
| `SLOW_MO` | `50` | No | ms between Playwright actions; **ignored by the porsline submitter** |
| `BROWSER_CHANNEL` | `"chrome"` | No | `""` falls back to bundled Chromium |
| `USE_TOR` | `"False"` | No | Enables proxying + IP renewal |
| `TOR_SOCKS_HOST` / `TOR_SOCKS_PORT` | `127.0.0.1` / `9050` | No | SOCKS endpoint |
| `TOR_CONTROL_PORT` / `TOR_CONTROL_PASSWORD` | `9051` / `""` | No | Control-port authentication |
| `OUTPUT_DIR` | `"output"` | No | Root of all artifacts |
| `SUBMISSION_DELAY_SECONDS` | `15` | No | **Undocumented in `.env.example`**; read directly inside `providers/avalform/form_submitter.py:177`, not via `config` |

---

## 6. Hidden Behaviors & Edge Cases Handled

### 6.1 Import-time side effects

Importing `config` — which every module except `cleaner.py` does — performs substantial work before any application code runs: it loads `.env`, **clears all existing root logger handlers** (`root_logger.handlers.clear()`), installs a colorlog handler, downgrades `stem` and `httpx` to `WARNING`, constructs the `APIKeyManager`, mutates `os.environ` if `GEMINI_PROXY` is set, and **raises `ValueError` if `BASE_FORM_URL` is unset**. Any library importing this project inherits a hijacked root logger.

### 6.2 Magic numbers and hard-coded constants

| Value | Location | Meaning |
|---|---|---|
| `85` | `answer_generator.py:110` | Fuzzy-match threshold; acceptance requires **strictly greater** |
| `0.8` | `answer_generator.py:228` | Persona discard gate: answers must cover ≥80% of the schema |
| `90` | `cleaner.py:11` | Minimum key count before a file is **deleted** |
| `0.04` | `trait_sampler.py:215` | Probability a respondent is a careless responder |
| `0.70` | `instrument.py:288` | Straightlining stickiness for careless responders |
| `(0.75, 0.95)` / `(0.55, 0.85)` | persona / answer generators | Randomized temperature ranges |
| `25–45` | `trait_sampler.py:219` | Sampled age range (**contradicted by the prompt — see §7.4**) |
| `22, 2.8` | `trait_sampler.py:230` | Marriage-age distribution `N(22, 2.8)`, clipped to `[16, min(age-1, 29)]` |
| `0.10 / 0.20 / 0.10`, clipped `[0.03, 0.50]` | `trait_sampler.py:244` | Trait-informed psychological-problem disclosure probability |
| `[-1.5,-0.9,-0.35,0.35,0.9,1.5]` | `trait_sampler.py:63` | z-score cut points for the 7 Persian bands |
| `min(2**n, 30)` | `llm_client.py:102` | Capped exponential backoff |
| `0.5` | `validate.py:204` | Within-respondent SD below which a row is flagged as straightlining |
| `0.95` / `0.6` | `validate.py:255-257` | Sanity-warning thresholds for α and mean inter-item *r* |
| `len(answers)+20`, `15`, `10`, `50` | submitters | Divergent page-loop failsafes |
| `random.randint(2,4)*1000` | `porsline/form_submitter.py:172` | Randomized 2–4 s per-action delay |

### 6.3 Retry, rotation, and fallback logic

- **Gemini key rotation:** `_is_quota_error()` matches `429`, `quota`, `rate`, `resource_exhausted`, `too many requests` (lowercased substring match on the exception text). On a quota error the key rotates and the call is retried; after `num_keys` rotations a `RuntimeError` is raised naming the last error. **Note:** the rotation counter is not reset by a successful transient retry, and `get_next_key()` is called at the top of every loop iteration, so a transient retry also advances the key index.
- **Gemini transient retry:** `_is_transient_error()` matches on both the message and the exception **class name** (`remoteprotocolerror`, `server disconnected`, `timeout`, `connection`, `503`, `502`, `504`, `500`, `unavailable`, `internal`, …), with capped exponential backoff.
- **vLLM has neither.** A single failure propagates.
- **Blocked-response detection:** an empty `response.candidates` raises with `prompt_feedback` attached — which `_is_transient_error` then matches on the substring `"internal"` only incidentally.
- **JSON extraction fallback:** `_extract_json()` slices between the first `{` and last `}` to survive markdown fences. **This is only applied in the answer generator; the persona generator calls `json.loads` on the raw text.**
- **Persona batch isolation:** a failed batch returns `[]`; other batches proceed.
- **Construct chunk isolation:** a failed construct is logged; other constructs proceed and the 80% gate decides the outcome.
- **Playwright per-field isolation:** google_forms and avalform catch per-element errors and continue filling the rest of the page.
- **Tor IP-check retry:** google_forms retries the `checkip` navigation 3× with 5 s gaps to survive `net::ERR_NETWORK_CHANGED`.

### 6.4 Silent operations

- **`_reasoning` is discarded.** The answer prompt requires a first-person Persian rationale as the first key; `validate_and_clean_answers` skips all keys starting with `_`. The model's reasoning is never persisted or logged.
- **Schema extractors submit dummy data to live forms.** To paginate a form, every extractor fills real inputs with placeholder values and clicks Next. On a multi-page form with server-side persistence, this writes partial junk responses to the target platform. Extractors stop *at* the submit button, but intermediate pages may already have been posted.
- **`utils.log_current_ip_with_tor()` is dead code** — defined, never called.
- **Porsline `MATRIX` questions are silently incomplete** — detected, typed, but options never extracted.
- **DigiSurvey skips unknown question types entirely** — they never enter the schema, so no answer is ever generated for them and no warning is emitted.
- **`instrument.classify()` assigns `"0"` to instruction blocks** via `static_answers`, which the answer prompt then explicitly overrides with instructions to output `"خواندم"` and *never* `"0"` — two mechanisms competing over the same fields.
- **Logging misuse as a colour channel:** `logging.warning` is used for purely informational IP messages specifically to render them yellow (`utils.py:77`, comment: "Log as a warning to get the yellow color").

### 6.5 Domain-specific hidden logic

- **Signature-based scale detection** (`instrument._scale_of`): the rating scale of a question is inferred from the presence of specific Persian option strings (`"همیشه توافق داریم"`, `"کمی بعضی اوقات"`, `"هر روز"`, `"بسیار ناخشنود"`, …). This is deliberately ID-agnostic so it survives question-ID changes — but it is exquisitely sensitive to the exact wording.
- **Direction-aware ordinal coding** (`instrument.numeric_code`): four scales (`consensus`, `satisfaction`, `cohesion_freq_7`, `commitment`) list their high end at index 0 and are coded `K - i`; everything else is coded `i + 1`. The docstring explains this exists because different scales share identical option strings with opposite meanings.
- **Reverse-key tagging** applies to only three constructs and is keyword-driven (`طلاق`, `جدایی`, `راحت است`, `نگران نیستم`, …).
- **Trait-band words, never numbers, reach the LLM.** `format_profile_fa` renders only the 7-band Persian labels; raw z-scores are persisted but deliberately withheld from the prompt.
- **Attachment pole splitting:** a single `attachment` scale is bisected into anxiety vs. avoidance by scanning the question text for four keywords.
- **Careless respondents skip TEXT_INPUT items.** `careless_answers()` iterates constructs and `continue`s on any question with no options. Since `classify()` routes numeric TEXT_INPUTs (age, weight, height, menopause duration) into the `other` construct, a careless persona receives **no answer at all** for them — which can push it below the 80% gate and cause silent discard-and-retry.

### 6.6 Concurrency

The only concurrency primitives in the codebase are `threading.Lock` in `APIKeyManager` and the `os.rename` claim in the porsline submitter. Everything else is strictly sequential `await` in a single event loop — the code is `async` throughout but **never runs anything in parallel** (no `asyncio.gather`, no `TaskGroup`). The async style buys nothing but Playwright API compatibility.

---

## 7. Technical Debt & Pre-Refactoring Warnings

### 7.1 Architectural anti-patterns

**(a) `config.py` as a global service locator with import-time side effects.**
Every module does `import config` and reaches into module-level globals. This is the single largest obstacle to testability: there is no way to instantiate the system with alternate settings, no way to import any module without a valid `.env`, and no way to run a unit test without a live `BASE_FORM_URL`. **Refactoring target:** an injected, immutable settings object (`Settings` dataclass or Pydantic `BaseSettings`) constructed once in `main()` and passed down.

**(b) Duck-typed provider contract resolved by string interpolation.**
`importlib.import_module(f"providers.{name}.{module}")` with no interface. Typos produce runtime failures; a provider missing `run()` fails only when called. **Refactoring target:** an explicit `Protocol`/ABC (`SchemaExtractor`, `FormSubmitter`) with an explicit registry.

**(c) Massive duplication across the four providers.**
The four submitters independently reimplement: the answer-file iteration loop, the `done/` move, browser launch options, the Tor pre-flight, the success-polling loop, page-loop failsafes, and failure-artifact writing. The four extractors independently reimplement: browser launch, the paginate loop, the dedup set, and dummy filling. **Estimated ~60% of `providers/` is structurally identical logic with divergent details.** **Refactoring target:** a template-method base class or an orchestrator that owns the loop and delegates only `parse_page()`, `fill_question()`, `navigate()`, and `detect_success()`.

**(d) Domain knowledge hard-coded as Persian string literals in `core/`.**
`core/instrument.py` is not platform-agnostic core logic — it is a configuration file for one specific questionnaire, expressed as Python. Scale detection, construct classification, reverse-keying, and demographic mapping are all keyword matches against exact Persian strings. Adding a second questionnaire currently requires editing `core/`. **Refactoring target:** externalize the instrument definition into a declarative data file (YAML/JSON) with `instrument.py` reduced to a generic interpreter.

**(e) The answer prompt hard-codes literal Porsline question IDs.**
`prompts/answer_generation_prompt.json` contains an explicit mapping table for IDs `39020952`, `39020953`, `39021262`, `39021251`, `39021257`, `39021264`, `39021267`, `39020853`, `44413635`. **This silently couples the prompt layer to one specific deployed form.** Re-extracting the schema from a re-published form invalidates the prompt with no error — the LLM simply receives instructions about IDs that no longer exist, and the demographic TEXT_INPUTs get whatever the model improvises. This is the single most fragile dependency in the system.

**(f) Inconsistent package structure.** `providers/avalform/`, `providers/digisurvey/`, and `analysis/` have **no `__init__.py`** while `providers/google_forms/` and `providers/porsline/` do. They currently work as PEP 420 implicit namespace packages, but this will break under packaging, some import tooling, and `mypy` default settings.

### 7.2 Correctness defects and latent bugs

| # | Location | Issue |
|---|---|---|
| 1 | `porsline/form_submitter.py:73` | `choices.filter(has_text=f"^{answer_value}$")` — Playwright's `has_text` performs **substring** matching when given a string; a regex requires `re.compile`. The `^…$` anchors are matched **literally** and can never match, so the "exact match" tier is dead and every answer falls through to the substring tier. A short option that is a prefix of a longer one will select the wrong choice. |
| 2 | `persona_generator.py:63,111` | `generate_and_save_personas(schema, num_personas)` accepts `schema` and never uses it. `run()` loads and validates `form_schema.json` purely to gate on its existence. Phase 2 has **no real dependency on the schema** — the documented "personas are generated based on the form schema" is not what the code does. |
| 3 | `answer_generator.py:185` | `validate_and_clean_answers(raw, chunk)` is passed the **chunk**, not the full schema. Correct for filtering, but the "Missing answers for N questions" warning is then computed per-chunk, making the log misleading about overall completeness. |
| 4 | `instrument.py:284` + `answer_generator.py:228` | Careless personas never receive TEXT_INPUT answers (§6.5), which can drop them below the 0.8 gate → discarded and retried indefinitely. Careless respondents are thus systematically under-represented relative to the intended 4%. |
| 5 | `llm_client.py:59-98` | The key-rotation loop calls `get_next_key()` on **every** iteration including transient retries, so transient errors consume key rotations. `keys_tried` is never reset after a success within the same call. |
| 6 | `porsline/form_submitter.py:261,283` | Failure artifacts are written to hard-coded `output/receipts/…` and `output/crash_….png`, ignoring `config.RECEIPTS_DIR_PATH`. Setting `OUTPUT_DIR` to anything else silently misplaces them — and `output/crash_*.png` lands outside `receipts/` entirely. The `open()` is not wrapped, so a missing directory raises inside the error handler. |
| 7 | `porsline/schema_extractor.py:160` | `MATRIX` questions get `options: []`. Downstream, `answer_generator` treats an empty `options` list as TEXT_INPUT and instructs the LLM to "write a number string only". |
| 8 | `porsline/form_submitter.py:172` | `slow_mo=random.randint(2,4)*1000` hard-overrides `config.SLOW_MO`, making this provider 40–80× slower than configured with no way to opt out. |
| 9 | `main.py:102` | `[int(p.strip()) for p in args.phases.split(',')]` raises an unhandled `ValueError` on non-numeric input; unknown numbers are silently ignored. |
| 10 | `services.py:31-43` | Key enumeration stops at the first gap. Defining `GEMINI_API_KEY_1` and `GEMINI_API_KEY_3` silently loads only one key. |
| 11 | `config.py:76-79` | `GEMINI_PROXY` mutates process-global `HTTPS_PROXY`/`HTTP_PROXY`, which **also affects Playwright and the `checkip` calls**, not just Gemini as documented. |
| 12 | Extractors (all) | `while True` with no iteration cap in porsline, avalform, and google_forms. A form whose navigation heuristic misfires loops indefinitely against a live site. Only digisurvey bounds itself (`max_pages = 50`). |

### 7.3 Error-handling smells

- **Bare `except:` clauses that swallow everything**, including `KeyboardInterrupt` and `SystemExit`: `utils.py:34` (implicit), `porsline/schema_extractor.py` (lines 33, 57, 66, 102, 116, 196), `porsline/form_submitter.py` (lines 39, 55, 95, 117, 182, 193, 321, 330). Debugging a stuck run is materially harder because of these.
- **`except Exception: return False`** in `fill_question_on_page` (`porsline/form_submitter.py:94`) discards the exception entirely — the caller cannot distinguish "no such element" from "browser crashed".
- **Failure paths that log and continue** mean a run can complete with `"All selected phases have been executed."` while having produced nothing. There is no non-zero exit code on partial or total failure, which makes the `Makefile loop` target unable to detect a broken run.

### 7.4 Data-integrity contradiction requiring a decision before refactoring

`core/trait_sampler.py:219` samples `age = rng.randint(25, 45)` and computes `years_married`, `children_count`, and the psych-problem probability from that age. The persona record persists these values.

`prompts/persona_generation_prompt.json` instructs the LLM, in bold and repeatedly, that respondents must be **40–65 years old, post-menopausal for ≥12 months**, and explicitly: *"If the provided demographic input says she is 25, 30, or anything under 40, **IGNORE IT AND OVERRIDE IT**."*

The merge in `persona_generator.py:85-92` keeps the **sampled** demographics and only takes the LLM's `details` prose. The result is a persona file whose structured `demographics.age` may read `27` while its `details` prose describes a 52-year-old post-menopausal woman. Phase 3 then sends **both** to the LLM (`persona_ctx` includes `demographics` *and* `details`) alongside a prompt telling it to output `age` "EXACTLY from your `age` key" — while `static_answers()` independently writes `years_married` from the same under-40 sampled value.

The sampling frame, the prompt, and the static-answer mapping encode **three mutually inconsistent definitions of the target population.** This is not a style issue; it corrupts the demographic validity of the generated dataset. It must be reconciled — almost certainly by moving the 40–65 post-menopausal frame into `trait_sampler.py` and removing the override language from the prompt — **before** any structural refactoring, because both the sampler and the prompt would otherwise be carried forward with the contradiction intact.

### 7.5 Complex logic requiring careful preservation

These are the parts where a naive rewrite will silently degrade output quality. Each encodes non-obvious, deliberate reasoning:

1. **`trait_sampler.sample_respondent`** — the 3-factor loading model, chosen so `Σ = LL' + diag(u²)` is positive-semidefinite by construction without numpy. The loading signs and magnitudes encode intended inter-construct correlations. Changing them changes the psychometric properties of the whole dataset. The `__main__` self-test (`python -m core.trait_sampler 2000`) is the only verification mechanism in the repository — **preserve it and run it before and after any change.**
2. **Construct chunking + within-chunk shuffling** (`answer_generator.py:169-172`) — the deliberate mechanism against α inflation. Consolidating chunks "for efficiency" would defeat the project's core methodological purpose.
3. **`instrument.numeric_code` direction handling** — resolves scales that share option strings with opposite meanings. Any simplification to a flat `option → score` dict reintroduces the exact bug the docstring documents.
4. **`careless_answers` straightlining** — the 70% anchor stickiness with ±1 drift.
5. **The static-answer/LLM boundary** — which questions bypass the LLM is a correctness guarantee about identity consistency, not an optimization.
6. **`validate.py` auto-reverse detection** — reflecting items with negative item-total correlations before computing α.
7. **Porsline's three-tier auto-advance detection** — empirically tuned against a specific SPA's behavior; the sleep durations (1.5 s / 2 s / 2 s) are load-bearing.
8. **The atomic `inprogress/` claim** — the only correct concurrency mechanism in the codebase and the only thing preventing duplicate submissions under `make loop` in parallel shells.

### 7.6 Risk register for the refactoring phase

| Risk | Severity | Notes |
|---|---|---|
| **Zero automated tests** | **Critical** | No test directory, no fixtures, no CI. Every behavior above was verified by reading, not by executing. There is no safety net for any refactor. **Mitigation: write characterization tests against the pure functions first** — `trait_sampler`, `instrument.classify/numeric_code/careless_answers`, `answer_generator.validate_and_clean_answers`, and `analysis.validate`'s statistics are all deterministic (given a seed) and testable without a browser or an API key. |
| **All dependencies unpinned** | High | `requirements.txt` has no version constraints. The `google-genai` v1 API differs substantially from the legacy `google-generativeai` package; a Playwright major bump changes locator semantics. Pin before touching anything. |
| **Provider selectors are unversioned coupling to third-party DOMs** | High | Class names like `button.shared_submit__7OvzI` are CSS-module build hashes that change on any redeploy by the vendor. These will rot independently of this codebase. |
| **Prompt/schema ID coupling** | High | See §7.1(e). Re-extracting the schema invalidates the prompt silently. |
| **No `output/` in the working tree** | Medium | `git status` shows `D output/form_schema.json`; the directory does not currently exist. **There is no sample schema to develop against**, so any refactor of `instrument.py`'s classification logic cannot be verified without re-running Phase 1 against the live form. Capturing a schema fixture should be the first action taken. |
| **Documentation drift** | Medium | `README.md` documents `GOOGLE_API_KEY_*` and `gemini-1.5-flash`; the code reads `GEMINI_API_KEY_*` and defaults to `gemini-2.5-pro`. `README.md` and `CLAUDE.md` both omit `digisurvey`, `llm_client.py`, `trait_sampler.py`, `instrument.py`, and the entire `analysis/` package. `CLAUDE.md` still describes `google-generativeai` and `BLOCK_NONE` "in the core generators" (it is in `llm_client.py`). **Do not use either document as a specification.** |
| **`make clean` is `rm -rf output/`** | Medium | Destroys all generated personas, answers, and submission history with no confirmation. |
| **`cleaner.py` deletes files by a hard-coded threshold** | Medium | `MIN_ENTRIES_THRESHOLD = 90` bears no relation to the current schema length. If the form shrinks below 90 questions, running the cleaner deletes **every** valid answer file. |
| **Safety filters disabled** | Low (technical) / Notable (governance) | All four Gemini harm categories are set to `BLOCK_NONE` (`llm_client.py:74-79`). Deliberate, per the code comments, to permit diverse persona simulation over sexual-health content. Preserve the decision consciously rather than by inheritance. |
| **Live-site side effects during development** | Medium | Both Phase 1 and Phase 4 act against a real third-party service, and Phase 1 posts dummy data through intermediate pages. Refactoring iterations will generate real traffic and possibly real junk submissions unless a recorded/offline test harness is built first. |

---

## 8. Summary of Recommended Pre-Refactoring Actions

Ordered by dependency, derived strictly from the findings above:

1. **Capture a schema fixture.** Restore or re-extract `output/form_schema.json` and commit it as a test fixture. Nothing in `core/instrument.py` can be verified without it.
2. **Pin all dependencies** in `requirements.txt`.
3. **Write characterization tests** for the pure, browser-free logic: `trait_sampler` (seeded), `instrument`, `validate_and_clean_answers`, `analysis.validate`. This is the safety net; it is achievable because these functions are already free of I/O.
4. **Resolve the age/population contradiction (§7.4).** It is a data-correctness decision, not a code decision, and it must be made before either the sampler or the prompt is restructured.
5. **Decouple `config`** into an injected settings object — this unblocks everything else, including testing.
6. **Externalize the instrument definition** out of `core/instrument.py` and the hard-coded question-ID table out of `prompts/answer_generation_prompt.json`.
7. **Introduce explicit provider interfaces** and lift the duplicated orchestration loop out of the four submitters and four extractors.
8. **Replace bare `except:` clauses** and introduce non-zero exit codes on phase failure so `make loop` can detect broken runs.
