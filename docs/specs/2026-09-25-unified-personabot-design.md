# PersonaBot 3.0 — Unified Design Spec

- **Date:** 2026-09-25
- **Status:** Approved for implementation (written before any code, per the
  project owner's instruction)
- **Target version:** 3.0.0 (tag `v3.0.0`)

---

## 1. Why this project exists

PersonaBot fills online questionnaires with synthetic respondents
("silicon sampling"): it samples a population from a latent-trait model, has an
LLM write each respondent's life story, has the LLM answer the questionnaire in
character — one construct at a time — and submits the answers through a real
browser.

The idea was applied form by form. Every new questionnaire was handled by
**copying the whole repository** (`PersonaBot-new`, `-150`, `-Raoufnadi`,
`-Soheyli`, `-azaduni`, `-behnaz`, `-fatemehf`, `-haana`, `-hadi`, `-nyayesh`,
`-sazmani`, `-yasamin`, `-zahra`, plus copies). Each copy then solved the
challenges its own form raised, **in that copy only**. After fifteen copies:

* each fix lives in one fork and is missing from the others (the Porsline
  end-of-form fix exists in 9 forks; the single-page-group fix in 1; token-aware
  key pacing in 1; Tor circuit retry in 2; …);
* the forks disagree about how to solve the same problem (two different ways
  of scoping a Porsline question's radios; three versions of `check-keys`);
* copying `.venv` between forks made the editable install resolve to *another*
  checkout, so commands silently ran foreign code (guarded by a `check-venv`
  target that exists only because of the forking);
* one `output/` and one `.env` per checkout is the only reason forking was
  needed at all.

**Goal:** one repository that carries the union of what all forks learned, in
which a new questionnaire is a new *folder of data files*, never a new copy of
the code.

### Success criteria

1. Adding a new form means creating `surveys/<id>/` (a `survey.yaml`, an
   optional `prompts/` folder of overrides) — no Python edits, no repo copy.
2. Several surveys coexist: each gets its own output directory; switching is
   `SURVEY=<id>` / `--survey <id>`.
3. Every behaviour fixed in any fork is present, and where forks conflicted the
   conflict is resolved deliberately (section 5).
4. All 14 questionnaires found in the forks are migrated, each with its schema
   snapshot, and **every one validates with full coverage in the test suite**.
5. The test suite (ported from all forks + new tests) passes.
6. The project is versioned (`3.0.0`), has a changelog, is committed to git and
   tagged `v3.0.0`.

### Non-goals

* No new form platform, no new LLM backend, no UI.
* No change to the statistical model or the analysis maths — they were
  identical in every fork and are carried over verbatim.
* No migration of generated `output/` data from the forks.

---

## 2. What was learned — inventory by fork

The inventory below is read from the **code** of each fork (diffed against the
common ancestor `PersonaBot-new`), not from their documentation.

| Area | Fork(s) | Problem met on a live form | Fix carried into 3.0 |
|---|---|---|---|
| Porsline end of form | Soheyli, azaduni, zahra, nyayesh, behnaz, 150 | Forms end on a *statement* page whose «پایان» precedes the real send button; clicking the text match posted nothing | One JS definition of the end button (send class first, text second), click the same element the probe found, confirm by waiting for the page to move |
| Porsline confirmation | same | Ending screen is themeable free text | Watch same-origin 2xx POST/PUT/PATCH to `/responses|submit|answers` as the success signal, then text selectors; explain *why* when unconfirmed |
| Porsline dropdowns | behnaz | Downshift combobox looks like a text input; typing records nothing | Detect `[role=combobox]`, read the menu (`aria-controls`), strip «۸.» numbering, click the option by index; extractor records it as `SELECT` |
| Porsline group header | behnaz | Group header shares a wrapper with its first question | Controls belong to the nearest preceding question label (document order) |
| Porsline single-page group | 150 | One wrapper holds 30 items → each item got all 150 radios | Scope each label to the narrowest ancestor owning exactly one label; no auto-advance wait on multi-question pages; id-less input on a grouped page is left blank rather than guessed |
| Porsline group «شروع» | 150 | Group confirm button reads «شروع» and has `shared_submit__` → treated as end of form at page 39/70 | `endButton` excludes «شروع» and anything inside a group wrapper |
| Porsline bleed guard | sazmani | Grouped renderer: every question carried the page's option union; schema silently wrong | Refuse to write such a schema (guard made precise, see §5.2) |
| Porsline grouped renderer | sazmani | Matrices, groups, voice items, confirm button with `shared_submit__` | `porsline_v2` provider: read the definition API, flatten matrix rows to `<id>#<row>`, fill by `data-pb-*` tags, `dispatch_event` clicks, verify rows answered |
| Voice questions | sazmani | Audio-only items cannot be typed | `QuestionType.VOICE`; answered as prose; `export-transcripts` writes one file per respondent named by another answer |
| Respondent index | sazmani | Serial identifiers (phone numbers) must not repeat across batches | `index` available to expressions; `--start-index` |
| Between-subjects vignettes | behnaz | Form draws one of 12 scenarios at random per session | `scenarios:` in spec; balanced assignment in phase 2; scenario text in phase 3 prompt; phase 4 opens the form first, recognises the drawn scenario, then claims a matching answer set |
| Porsa platform | hadi, haana | Server-rendered form, `body{visibility:hidden}` until `pageloaded`, opacity-0 radios, client-side validating pagination, matrix boxes | `porsa` provider (haana's version, which expands matrices per row) |
| Porsa gatekeeping | haana | Returning visitor → form-less "already answered" page; closed survey → generic template | `AlreadyRespondedError`, `SurveyClosedError` diagnosed from page text within the first second |
| Tor isolation | haana, hadi | Chromium refuses SOCKS5 auth, so per-credential isolation (`IsolateSOCKSAuth`) made every context fail | Isolate per SocksPort (`TOR_SOCKS_EXTRA_PORTS`), rotate, burn failed ports for a cooldown |
| Route failures | haana, hadi | One bad circuit killed a unit of work | `CircuitError` (raised only before anything is POSTed) + `on_circuit` retry on a fresh circuit; preflight IP echo as a health check |
| Slow links | fatemehf | Images/fonts starved concurrent submissions | `BLOCK_HEAVY_ASSETS` aborts image/font/media requests; `goto` wait condition per provider |
| Google Forms | fatemehf | Controls `aria-disabled` until hydrated; ZWNJ broke `repr`-built selectors; rejected Next looked like success; stylesheet failures collapse layout | Hydration wait, CSS-escaped `data-value` selector, advance/reject detection, scripted-click fallback, answered-state verification |
| Receipts | fatemehf | Provider-raised failures skipped the screenshot | Every submission failure writes a receipt |
| Key pacing | sazmani | Free tier binds on input tokens/minute; lowest-index keys starved the pool's tail | Token ledger per key (sliding window, 85% safety), least-recently-released ordering, jittered wake-ups |
| Quota errors | sazmani | Per-day quotas retried every 60 s forever | Parse `RetryInfo.retryDelay` and `QuotaFailure` scope; day-scope cooldown 900 s |
| Transient retries | sazmani | Retry landed on the same key and turned a blip into a quota storm | Charge tokens on failure and park the key for its backoff |
| Gemini client | sazmani | New SDK client (TLS handshake) per request, never closed | One client per key, closed at the end |
| Geo-block proxy | 150, Raoufnadi | `GEMINI_PROXY` was read by nothing; aiohttp path ignored proxy | Proxy passed through `HttpOptions` (+ pinned httpx transport); SOCKS needs `socksio`, checked up front |
| Credential check | 150, Raoufnadi, … | Bad keys surfaced mid-run as vague failures | `check-keys` (free `count_tokens` or `--live`), same HTTP route as real calls, geo-block diagnosis |
| Status | 150 | Stranded persona claims vanished from the report; receipts double-counted | `personas/inprogress` row, receipts counted per failure stem |
| Inspect | haana | Long items truncated when authoring constructs | `inspect-schema --full` |
| Makefile | several | Copied venv resolved to another checkout | `check-venv` guard kept; `python -m pip` everywhere |

Knowledge that lives in the **prompts and configs** rather than code (collected
from the 14 surveys' prompt presets) is folded into the new Persian base prompt
pack (§4.4): pick options by meaning, never by list position; scales in one
form may run in opposite directions; mind whether an item is about the
respondent or about someone else (spouse, school, organisation); do not guess
the hypothesis; copy option strings verbatim even with typos; stay consistent
with sampled facts (children, relationship length, sleep times); traits are
independent — do not collapse a person into one "type"; no clinical labels or
test names in personas; concrete cultural detail; respectful boundaries on
sensitive content.

---

## 3. Architecture

### 3.1 Repository layout

```
PersonaBot/
├── pyproject.toml            version 3.0.0, entry point `personabot`
├── Makefile                  SURVEY=<id> aware
├── .env.example              deployment settings only (keys, browser, Tor)
├── CHANGELOG.md
├── README.md
├── docs/
│   ├── specs/                this document
│   ├── HOWTO_NEW_SURVEY.md   the author's workflow for a new form
│   └── KNOWLEDGE.md          the lessons table above, kept current
├── prompts/
│   └── base/
│       ├── fa/               Persian base pack (default for locale fa)
│       └── en/               English base pack
├── surveys/
│   ├── _template/            copied by `personabot new-survey`
│   │   └── survey.yaml
│   └── <survey_id>/
│       ├── survey.yaml       the instrument + form + prompt variables
│       ├── form_schema.json  schema snapshot written by phase 1
│       └── prompts/          OPTIONAL template overrides
├── src/personabot/
│   ├── cli.py  settings.py  workspace.py  errors.py  logging_setup.py
│   ├── survey/        spec.py (pydantic schema of survey.yaml), loader.py
│   ├── domain/        models, text, matching, expressions, sampling,
│   │                  instrument, scenarios, answer_validation
│   ├── application/   generate_personas, generate_answers, analyze, status,
│   │                  inspect, check_keys, export_transcripts, scaffold
│   ├── infrastructure/
│   │   ├── llm/       client, keypool, gemini, openai_compat, factory
│   │   ├── browser/   factory (circuits), tor
│   │   ├── prompts/   renderer (layered loader)
│   │   └── storage/   json_store, workqueue
│   └── providers/     base, registry, porsline, porsline_v2, porsa,
│                      google_forms, avalform, digisurvey
├── output/<survey_id>/       generated; git-ignored
└── tests/
```

### 3.2 The three layers of configuration

| Layer | Where | Holds | Changes per form? |
|---|---|---|---|
| Deployment | `.env` → `Settings` | API keys, model, concurrency, browser, Tor, output root, log level | No |
| Survey | `surveys/<id>/survey.yaml` → `SurveySpec` | form (provider, URL, page budget), population, scales, constructs, static answers, scenarios, transcripts, answering policy, analysis, prompt variables | **Yes** |
| Wording | `prompts/base/<lang>/` + optional `surveys/<id>/prompts/` | Jinja2 templates | Only when the base pack's blocks/variables are not enough |

`Settings` stays deployment-only: the output-layout properties and form URL
move out of it into `Workspace`.

### 3.3 `Workspace` — one survey, bound to one deployment

```python
@dataclass(frozen=True)
class Workspace:
    settings: Settings
    spec: SurveySpec
    survey_dir: Path          # surveys/<id>/ (or the dir of an ad-hoc --config)
    spec_path: Path
    output_dir: Path          # <OUTPUT_ROOT>/<spec.id>/
    form_url_override: str | None   # from --url
    provider_override: str | None   # from the CLI positional

    schema_file   -> survey_dir / "form_schema.json"
    prompts_dir   -> survey_dir / "prompts"
    personas_dir, answers_dir, receipts_dir, transcripts_dir -> under output_dir
    form_url      -> --url  >  spec.form.url  >  BASE_FORM_URL (fallback only)
    provider      -> CLI   >  spec.form.provider
    max_pages     -> spec.form.max_pages  >  MAX_PAGES
    require_form_url(), require_provider(), ensure_directories()
```

Precedence rule for the form URL is deliberate: a shared `BASE_FORM_URL` in
`.env` must never override the URL a survey declares, or survey A's answers get
submitted to survey B's form. When both are set and differ, a warning is
logged and the survey's URL is used.

**Survey resolution** (CLI): `--config <file>` > `--survey <id>` >
`SURVEY_CONFIG` > `SURVEY`. With none, commands that need a survey fail with the
list of available surveys.

**Schema location.** The schema is part of a survey's definition — the YAML's
matchers are written against it — so phase 1 writes it into the survey folder
(`surveys/<id>/form_schema.json`). If a schema already exists and the new one
differs, the old one is kept as `form_schema.previous.json` and the added /
removed question ids are logged: a republished form is detected at extraction
instead of as a coverage error later.

**Output isolation.** `output/<id>/{personas,answers,receipts,transcripts}` plus
`validation_report.json`. Two surveys never share a queue.

### 3.4 Pipeline (unchanged in shape)

1. **schema** — provider extractor walks the live form (or reads its API) →
   `form_schema.json`.
2. **personas** — pure-Python sampling (factor model, demographics, response
   style, scenario assignment); the LLM writes only `details` prose.
3. **answers** — static bindings from the persona record; careless responders in
   code; one LLM request per construct with shuffled items; answers snapped to
   canonical option strings; completeness gate with targeted retry.
4. **submit** — provider submitter fills the form through the file work queue,
   one isolated browser context (and Tor circuit) per submission.

Answer file format is unchanged (flat `question_id → answer`), plus
underscore-prefixed metadata keys that submitters strip: `_scenario` (existing)
and `_persona` (new, for tracing a submission back to its persona).

---

## 4. Components

### 4.1 `survey/spec.py`

Carries the base schema verbatim plus:

* `ScenarioSpec`, `SurveySpec.scenarios` (behnaz).
* `TranscriptsSpec`, `SurveySpec.transcripts` (sazmani).
* **new** `FormSpec` — `provider: str | None`, `url: str | None`,
  `max_pages: int | None`.
* **new** `PromptSpec` — `base: str | None` (defaults from `locale`),
  `role`, `study_context`, `persona_sections: dict[str, str]`,
  `persona_rules: list[str]`, `answer_rules: list[str]`,
  `scenario_preamble`, `vars: dict[str, Any]` (free-form, for custom
  templates).
* `prompt_preset` becomes optional and legacy: when set, `prompts/<preset>/`
  joins the template search path so a 2.x config still renders.
* `version` accepts 1 and 2.

### 4.2 `domain/`

`text`, `matching`, `expressions`, `answer_validation` — verbatim (identical in
every fork). `models` gains `QuestionType.VOICE` and `Persona.scenario`.
`scenarios` from behnaz. `instrument` from behnaz (`scenario_for`). `sampling`
from sazmani (`index`, `start_index`) with the seed fix of §5.4.

### 4.3 `application/`

* `generate_personas` — scenarios + start index; **auto-continues** the index
  series from personas already on disk when `--start-index` is not given.
* `generate_answers` — scenario-aware; writes `_persona`; careless RNG seeded
  from a stable CRC of the persona id (Python's `hash()` is salted per process,
  so the old seed was not reproducible).
* `check_keys` — 150's version (proxy-aware, geo-block diagnosis) with
  Raoufnadi's separation of *notes* from *warnings*.
* `export_transcripts` (sazmani), `inspect` (haana `--full`), `status` (150),
  `analyze` (verbatim) — all re-pointed at `Workspace`.
* **new** `scaffold` — `personabot new-survey <id>` copies
  `surveys/_template/` and fills the id.

### 4.4 Prompts — layered loader and base packs

Search path, first hit wins:

1. `surveys/<id>/prompts/`
2. `prompts/<prompt_preset>/` (legacy, only when the spec sets it)
3. `prompts/base/<prompt.base or locale>/`

and the base pack is also addressable as `base/<name>` so an override can
`{% extends "base/answers.system.j2" %}` and replace just one block.

The **Persian base pack** is synthesised from the 14 surveys' presets. Its
templates expose blocks (`role`, `who_you_are`, `life`, `levels`, `style`,
`scenario`, `rules`, `output`) and read `survey.prompt.*`:

* `answers.system.j2` — role from `prompt.role`; scenario section with
  `prompt.scenario_preamble`; the generic rules listed in §2 followed by
  `prompt.answer_rules`.
* `persona.system.j2` — `prompt.study_context`; generic fidelity/intensity/
  independence/realism rules followed by `prompt.persona_rules`; `details`
  keys from `prompt.persona_sections` (a generic set when empty).
* `answers.user.j2`, `persona.user.j2` — identical to the shared Persian
  versions used by 11 of the 14 surveys.

Template variables are a superset of 2.x (`survey` is now passed to both
phases), so every migrated preset renders unchanged.

### 4.5 `infrastructure/`

* `llm/` — sazmani's `client`, `keypool`, `gemini` (per-key clients, quota
  details, transient parking) with 150's `build_http_options` /
  `require_proxy_support`; factory validates the proxy and logs pacing.
* `browser/factory` — haana's `Circuit`/`circuit()` + incognito, fatemehf's
  heavy-asset blocking.
* `browser/tor` — haana's SocksPort rotation, `burn`, `verify_circuit`.
* `prompts/renderer` — layered loader (§4.4).
* `storage/` — verbatim.

### 4.6 `providers/`

* `base` — haana's `open_form`/`on_circuit`/`nav_wait_until` +
  behnaz's scenario-driven submission (`detect_scenario`, `_run_by_scenario`)
  + fatemehf's receipt-on-every-failure. Providers receive a `Workspace`.
* `porsline` — merged (§5.1).
* `porsline_v2` (sazmani), `porsa` (haana), `google_forms` (fatemehf),
  `avalform` / `digisurvey` (verbatim) — re-pointed at `Workspace`.

### 4.7 CLI and Makefile

```
personabot [--survey ID | --config FILE] [--log-level L] <command>
  run [PROVIDER] --phases 1,2,3,4 --num-personas N --seed S
                 --start-index I --limit N --url URL
  validate-config [--schema FILE]
  inspect-schema [--schema FILE] [--ids] [--full]
  status | recover | analyze | export-transcripts
  check-keys [--live]            (no survey needed)
  providers | surveys            (no survey needed)
  new-survey ID [--from ID]      (no survey needed)
```

Makefile targets keep their 2.x names and add `SURVEY=`: `make run-all
SURVEY=teacher_innovation_km_ai_fa`. `PROVIDER` defaults to the survey's
`form.provider`.

---

## 5. Decisions where forks conflicted

### 5.1 Porsline: which radios belong to a question

behnaz: take the `classic_question_wrapper__`, keep controls after the label and
before the next label (document order). 150: climb to the narrowest ancestor
owning exactly one label. Each handles a case the other breaks:

* behnaz's group header — the first question's label sits in its own intro
  wrapper whose radios are its *siblings*; 150's narrow box contains no radios,
  so the question would be dropped as a statement page.
* 150's single-page group — 30 items in one wrapper; behnaz's rule works only
  if items never interleave, and gives no per-item `hasTable`.

**Merged rule** (`scopeFor` in the shared JS helpers, used by the probe *and*
the click): if the classic wrapper owns exactly one label, use it. Otherwise
use the narrowest single-label ancestor **if it contains any controls**; if it
contains none, fall back to the wrapper with the document-order ownership
filter. Tables are only reported for exclusive scopes.

### 5.2 Porsline: the bleed guard

sazmani refuses a page where ≥2 choice questions share an identical option
list. On 150's single-page Likert group (30 items, same 5 options) that is a
false positive. **Precise rule:** bleed = ≥2 choice questions on the page and
*each* reports as many options as there are radios on the whole page. The probe
now returns the page's radio count.

### 5.3 Retry budget without Tor

haana: without Tor, one attempt (same route, same failure). fatemehf: on a slow
direct link, a hung navigation often succeeds on a second try. **Decision:**
`CircuitError` retries up to `BROWSER_MAX_ATTEMPTS` with Tor, and up to
`min(2, BROWSER_MAX_ATTEMPTS)` without. Both remain safe because `CircuitError`
is only raised before anything is POSTed.

### 5.4 Reproducible batches

sazmani's `--start-index` kept the RNG seeded by `seed` alone, so a second
seeded batch reproduced the first batch's draws under new numbers. **Decision:**
the batch RNG is seeded with `f"{seed}:{start_index}"` when a seed is set;
unseeded runs stay clock-seeded.

### 5.5 Scenario submission + circuits

behnaz's scenario loop opened contexts directly; haana's retry wraps a whole
submission. **Decision:** the scenario worker opens each session inside
`browser.circuit()`; a `CircuitError` while opening burns that circuit and
counts toward the "failed before reaching the scenario" stop condition, exactly
like other pre-scenario errors.

---

## 6. Migration of existing surveys

For each of the 14 surveys: copy `survey.yaml` unchanged except for (a) a new
`form:` block with the provider, URL and page budget read from the fork that ran
it, and (b) removal of `prompt_preset` (its templates move to
`surveys/<id>/prompts/`). Copy the fork's `output/form_schema.json` as the
snapshot (`sexual_intimacy_fa` uses the test fixture). Where a fork's Makefile
default did not match the form (fatemehf's Google Form), the provider is taken
from the URL.

---

## 7. Testing

* Port every fork's tests, choosing the most complete version of each file
  (e.g. porsline tests from 150 + behnaz + sazmani; llm tests from sazmani;
  porsa from haana), adapted to `Workspace`.
* **New:** `test_surveys.py` — every `surveys/*/survey.yaml` loads, its prompts
  render against a synthetic persona, and it has **full coverage** against its
  schema snapshot.
* **New:** workspace resolution and URL precedence; layered prompt loader
  (override, extends, legacy preset); schema snapshot diff on re-extraction;
  seeded batch independence; auto-continued start index; bleed guard precision.
* Browser code keeps its existing style of tests: pure functions and fakes, no
  live network.

---

## 8. Versioning and release

* Semantic versioning. 3.0.0 because the layout (surveys folder, per-survey
  output) and the provider constructor signature change.
* `CHANGELOG.md` in Keep-a-Changelog format; `personabot --version` reads the
  package version.
* Git: spec committed first; implementation in focused commits; release commit
  bumps nothing further and is tagged `v3.0.0` (annotated).

---

## 9. Implementation plan

1. Scaffold: pyproject (3.0.0), Makefile, `.env.example`, `.gitignore`.
2. Core: errors, settings, logging, workspace; survey spec/loader.
3. Domain: carry over + merges (§4.2, §5.4).
4. Infrastructure: llm, browser, tor, storage, layered renderer.
5. Providers: base (§5.3, §5.5), porsline merge (§5.1, §5.2), the rest.
6. Application + CLI + scaffold.
7. Prompt base packs (fa, en) and survey template.
8. Migrate the 14 surveys (§6).
9. Tests: port, adapt, add; run until green.
10. Docs: README, HOWTO_NEW_SURVEY, KNOWLEDGE; CHANGELOG; commit; tag.
