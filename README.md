# PersonaBot

Synthetic survey respondents ("silicon sampling") for online questionnaires —
**one codebase, one folder per questionnaire.**

PersonaBot samples a population from a latent-trait model, has an LLM write
each respondent's life in prose, has the LLM answer the questionnaire in
character one construct at a time, and submits the answers through a real
browser. Everything specific to a questionnaire lives in `surveys/<id>/`; the
code never changes between forms.

> 3.0 merges fifteen per-form copies of the 2.x project into this one tree.
> What each copy learned on its live form is in [docs/KNOWLEDGE.md](docs/KNOWLEDGE.md);
> the design is in [docs/specs/](docs/specs/2026-09-25-unified-personabot-design.md);
> changes are in [CHANGELOG.md](CHANGELOG.md).

## Contents

1. [How it works](#1-how-it-works)
2. [Installation](#2-installation)
3. [Configuration (`.env`)](#3-configuration-env)
4. [Selecting a survey](#4-selecting-a-survey)
5. [Running the pipeline](#5-running-the-pipeline)
6. [Command reference](#6-command-reference)
7. [Adding a new questionnaire](#7-adding-a-new-questionnaire)
8. [Working on several clients at once](#8-working-on-several-clients-at-once)
9. [`survey.yaml` reference](#9-surveyyaml-reference)
10. [Prompts](#10-prompts)
11. [Providers](#11-providers)
12. [LLM keys, quotas and proxies](#12-llm-keys-quotas-and-proxies)
13. [Tor](#13-tor)
14. [Outputs and file formats](#14-outputs-and-file-formats)
15. [Checking quality before submitting](#15-checking-quality-before-submitting)
16. [Troubleshooting](#16-troubleshooting)
17. [Development](#17-development)
18. [Responsible use](#18-responsible-use)

---

## 1. How it works

| Phase | What happens | Reads | Writes |
|---|---|---|---|
| **1 · schema** | The provider walks the live form (or reads its API) and records every question, its type and its options. Nothing is typed into the form. | the form URL | `surveys/<id>/form_schema.json` |
| **2 · personas** | Traits (a factor model), response style and demographics are sampled **in Python**. The LLM only writes each respondent's `details` prose; it can never alter the sampled numbers. | `survey.yaml` | `output/<id>/personas/*.json` |
| **3 · answers** | Static answers (age, gender, …) are filled from the persona record. Careless respondents are simulated in code. Every other question is asked **one construct per request**, items shuffled. Answers are snapped to the form's exact option strings and checked for completeness. | personas, schema | `output/<id>/answers/*.json` |
| **4 · submit** | One isolated browser context (and, optionally, Tor circuit) per answer set, claimed from a file queue so parallel workers never submit the same set twice. | answers, form URL | `answers/done/`, `receipts/` on failure |

Three layers of configuration, each with one owner:

| Layer | File | Holds | Changes per form? |
|---|---|---|---|
| Deployment | `.env` | API keys, model, concurrency, browser, Tor, output root | no |
| Survey | `surveys/<id>/survey.yaml` | form URL and provider, population, scales, constructs, static answers, scenarios, answering policy, prompt variables | **yes** |
| Wording | `prompts/base/<lang>/*.j2` + optional `surveys/<id>/prompts/*.j2` | Jinja2 prompt templates | only when the base pack is not enough |

## 2. Installation

Requirements: Linux or macOS, Python ≥ 3.11, `make`, internet access for the
LLM and the form.

```bash
cd PersonaBot
make install          # creates .venv, installs the package + dev tools + Chromium
cp .env.example .env  # then add at least GEMINI_API_KEY_1
make check-keys       # verifies every key (LIVE=1 also proves quota is left)
make test             # optional: the full test suite
```

`make install` runs `pip install -e ".[dev]"` and `playwright install chromium`.
For a SOCKS `GEMINI_PROXY`, install the extra: `.venv/bin/python -m pip install -e ".[socks]"`.

Every survey and pipeline `make` target first runs `check-venv`, which refuses to run if `.venv`
resolves `personabot` to a different checkout (a copied virtualenv does that
silently). If it fails, run `make install`.

Without `make`, use the CLI directly: `.venv/bin/python -m personabot.cli …`
(or `personabot …` with the venv activated).

## 3. Configuration (`.env`)

`.env` holds **deployment** settings only and is shared by every survey.
Nothing about a particular questionnaire belongs here. All keys are optional
except an API key for the chosen backend. `.env.example` lists them with
comments.

**Survey selection**

| Variable | Default | Meaning |
|---|---|---|
| `SURVEY` | — | Default survey id (a folder under `surveys/`) |
| `SURVEY_CONFIG` | — | Explicit path to a survey YAML; wins over `SURVEY` |
| `SURVEYS_DIR` | `surveys` | Where survey folders live |
| `PROMPTS_DIR` | `prompts` | Where the base prompt packs live |
| `CLIENTS_DIR` | `../clients` | Where client worktrees live ([§8](#8-working-on-several-clients-at-once)) |

**LLM**

| Variable | Default | Meaning |
|---|---|---|
| `AI_PROVIDER` | `gemini` | `gemini` or `vllm` (any OpenAI-compatible endpoint) |
| `GEMINI_API_KEY_1`, `_2`, … | — | Any number of keys; gaps in the numbering are fine |
| `GEMINI_MODEL_NAME` | `gemini-2.5-pro` | Model for both phases |
| `GEMINI_PROXY` | — | `http://…`, `https://…` or `socks5://…`; applies to Gemini calls only |
| `GEMINI_INPUT_TOKENS_PER_MINUTE` | `0` | Per-key input-token budget for pacing; `0` = no pacing ([§12](#12-llm-keys-quotas-and-proxies)) |
| `LLM_TIMEOUT_SECONDS` | `180` | Per request |
| `LLM_MAX_RETRIES` | `3` | Retries for transient errors (quota errors rotate keys instead) |
| `AI_CALL_DELAY_SECONDS` | `0` | Extra pause after each request |
| `PERSONA_BATCH_SIZE` | `10` | Personas written per LLM request in phase 2 |
| `VLLM_BASE_URL`, `VLLM_MODEL_NAME`, `VLLM_API_KEY` | — | For `AI_PROVIDER=vllm` |

**Concurrency**

| Variable | Default | Meaning |
|---|---|---|
| `CONCURRENT_LLM_TASKS` | `4` | Personas/batches generated in parallel. With Gemini, effective concurrency is capped by the number of keys |
| `CONCURRENT_SUBMISSIONS` | `2` | Browser submissions in parallel |

**Browser**

| Variable | Default | Meaning |
|---|---|---|
| `BASE_FORM_URL` | — | **Fallback only**, for a survey whose `form.url` is empty |
| `HEADLESS_MODE` | `True` | `False` to watch the browser |
| `SLOW_MO` | `50` | Milliseconds between browser actions (raise to 200+ when debugging) |
| `BROWSER_CHANNEL` | `chrome` | Empty = Playwright's bundled Chromium (falls back to it automatically) |
| `BROWSER_INCOGNITO` | `True` | Visible incognito window when headful |
| `BROWSER_LOCALE`, `BROWSER_TIMEZONE` | `fa-IR`, `Asia/Tehran` | What each context reports |
| `BLOCK_HEAVY_ASSETS` | `True` | Abort image/font/media requests (faster on slow links; receipts lose decoration) |
| `MAX_PAGES` | `40` | Default page budget when a survey sets no `form.max_pages` |
| `SUBMISSION_DELAY_SECONDS` | `15` | Pause after each submission, per worker |
| `NAV_TIMEOUT_SECONDS` | `60` | Budget to load the form, per attempt |
| `READY_TIMEOUT_SECONDS` | `45` | Budget for the provider's readiness check, per attempt |
| `BROWSER_MAX_ATTEMPTS` | `3` | Route retries per unit of work (capped at 2 without Tor) |

**Tor** — see [§13](#13-tor): `USE_TOR`, `TOR_SOCKS_HOST`, `TOR_SOCKS_PORT`,
`TOR_SOCKS_EXTRA_PORTS`, `TOR_CONTROL_PORT`, `TOR_CONTROL_PASSWORD`,
`TOR_CIRCUIT_COOLDOWN_SECONDS`.

**Output**: `OUTPUT_DIR` (default `output`, the root — each survey writes
`output/<id>/`) and `LOG_LEVEL` (`DEBUG`, `INFO`, `WARNING`, `ERROR`).

Real environment variables override `.env`, and command-line flags override
both.

## 4. Selecting a survey

Every command that works on a survey resolves exactly one, in this order:

1. `--config path/to/survey.yaml` (make: `CONFIG=…`)
2. `--survey <id>` (make: `SURVEY=<id>`)
3. `SURVEY_CONFIG` in the environment / `.env`
4. `SURVEY` in the environment / `.env`

`--survey` and `--config` are **global** options, so they go before the
command: `personabot --survey teacher_innovation_km_ai_fa status`.

```bash
make surveys                    # every survey, its provider and URL
```

The form URL is resolved separately: `run --url` > the survey's `form.url` >
`BASE_FORM_URL`. A survey's own URL always beats `.env`, so one questionnaire's
answers cannot be submitted into another's form. A warning is logged when
`BASE_FORM_URL` disagrees with the survey.

The provider comes from the command line (`run <provider>` / `PROVIDER=…`),
otherwise from `form.provider`.

## 5. Running the pipeline

Everything below takes `SURVEY=<id>`; it is omitted for brevity.

```bash
make schema                     # phase 1
make persona NUM_PERSONAS=20    # phase 2
make answer                     # phase 3
make submit                     # phase 4
make run-all NUM_PERSONAS=20    # phases 1-4 in one process
make run-all PHASES=2,3,4       # skip extraction; phases run in the order given
```

Useful variations:

```bash
make answer LIMIT=5                     # at most 5 personas this run
make submit LIMIT=1                     # one submission; watch it first
make persona NUM_PERSONAS=50 SEED=42    # reproducible sampling
make persona START_INDEX=101            # force the respondent numbering
make loop PHASES=3,4 RUN_COUNT=10 DELAY_SECONDS=120
```

* **Phase order** is what you write: `--phases 3,4` never runs extraction.
* **Respondent numbering** (`persona_<n>`, and `index` in expressions) continues
  from what is already in `output/<id>/personas/` unless `START_INDEX` is given,
  so serial identifiers never repeat across batches.
* **Seeds**: with `SEED`, each batch is reproducible *and* distinct from the
  other batches (the seed is mixed with the start index). Without it, sampling
  is clock-seeded. `population.seed` in the YAML is the default seed.
* **Incremental work**: each phase only takes what is pending. Personas that
  failed phase 3 stay in `personas/` and are retried next run; answer sets that
  failed phase 4 stay in `answers/`.
* **`make loop`** stops at the first failing run.

After a run, `make status` shows where everything is ([§14](#14-outputs-and-file-formats)).

## 6. Command reference

```
personabot [--survey ID | --config FILE] [--log-level LEVEL] [--version] <command>
```

| Command | Needs a survey | What it does |
|---|---|---|
| `run [PROVIDER] --phases 1,2,3,4` | yes | Run phases. Options: `--num-personas N` (default 5), `--seed S`, `--start-index I`, `--limit N`, `--url URL` |
| `validate-config [--schema FILE]` | yes | Validates the YAML, prints the form, output folder, where each prompt template resolves from, the implied trait-correlation matrix and the schema coverage report. Exit 1 if coverage is incomplete |
| `inspect-schema [--schema FILE] [--ids] [--full]` | yes | Inventory of the schema grouped by option signature, a draft `scales:` block, and (`--ids`) id lists per signature; `--full` stops truncating long items |
| `status` | yes | Read-only report on `output/<id>/`: every queue stage, complete / partial / corrupt files, claimed items, receipts |
| `analyze` | yes | Psychometric report over the answers; also writes `validation_report.json` |
| `export-transcripts` | yes | Writes answers to voice-only questions to one text file per respondent |
| `recover` | yes | Moves items stranded in `inprogress/` (by a killed process) back to the queue. Only run it when no worker is running |
| `check-keys [--live]` | no | Probes every API key (free `count_tokens`, or a tiny real generation) |
| `providers` | no | Lists form providers |
| `surveys` | no | Lists surveys with provider and URL |
| `new-survey ID [--from ID]` | no | Creates `surveys/ID/` from the template or from another survey |
| `client new CLIENT SURVEY [--from ID] [--keys SPEC] [--no-install]` | no | Creates an isolated client worktree ([§8](#8-working-on-several-clients-at-once)) |
| `client list` | no | Lists client worktrees |
| `client sync [CLIENT SURVEY]` | no | Rebases a client branch onto `main` |
| `client deliver [CLIENT SURVEY] [--no-push] [--keep] [--skip-checks] [--no-archive]` | no | Tags, merges into `main`, pushes, archives output, removes the worktree |

Exit codes: `0` success · `1` a phase or check failed · `2` configuration error
· `130` interrupted.

**Make targets** (all accept `SURVEY=`, `CONFIG=`):

| Target | Equivalent |
|---|---|
| `make install`, `make test`, `make clean` | setup, test suite, remove caches (never touches `output/`) |
| `make surveys`, `make providers` | `surveys`, `providers` |
| `make new-survey NAME=x [FROM=y]` | `new-survey x [--from y]` |
| `make client CLIENT=c SURVEY=s [FROM=…] [KEYS=1-7]` | `client new c s …` |
| `make clients`, `make sync`, `make deliver [NO_PUSH=1 KEEP=1 SKIP_CHECKS=1 NO_ARCHIVE=1]` | `client list`, `client sync`, `client deliver` (CLIENT/SURVEY optional inside a worktree) |
| `make check-keys [LIVE=1]` | `check-keys [--live]` |
| `make validate`, `make inspect [ARGS="--full --ids"]` | `validate-config`, `inspect-schema` |
| `make status`, `make analyze`, `make transcripts`, `make recover` | same-named commands |
| `make schema [PROVIDER=…]` | `run --phases 1` |
| `make persona [NUM_PERSONAS=… SEED=… START_INDEX=…]` | `run --phases 2` |
| `make answer [LIMIT=…]` | `run --phases 3` |
| `make submit [PROVIDER=… LIMIT=…]` | `run --phases 4` |
| `make run-all [PHASES=… NUM_PERSONAS=… LIMIT=… SEED=…]` | `run --phases …` |
| `make loop [RUN_COUNT=10 DELAY_SECONDS=120 …]` | `run-all` repeated, stopping on failure |

## 7. Adding a new questionnaire

A new form is a new folder. You never copy the repository and never edit
Python.

### Step 1 — create the folder

```bash
make new-survey NAME=job_stress_fa
# or start from a survey with a similar population or prompts:
make new-survey NAME=job_stress_fa FROM=teacher_innovation_km_ai_fa
```

The id may contain letters, digits, `_` and `-`. Cloning copies the YAML and
prompt overrides but **not** the schema (it describes another form) and blanks
`form.url` (it would submit into the other form).

### Step 2 — point it at the form

In `surveys/job_stress_fa/survey.yaml`:

```yaml
form:
  provider: porsline            # see §11 for which provider to use
  url: https://survey.porsline.ir/s/XXXXXXXX
  max_pages: 400                # Porsline: above the QUESTION count
```

### Step 3 — extract the schema

```bash
make schema SURVEY=job_stress_fa
```

This writes `surveys/job_stress_fa/form_schema.json`. Extraction never types
into Porsline, Porsa or the Porsline API form; the Google Forms, Avalform and
DigiSurvey extractors fill placeholder answers to page forward. Re-running it
later is safe: if the form changed, the old schema is kept as
`form_schema.previous.json` and the added / removed / changed question ids are
logged — your cue to re-check the matchers.

### Step 4 — read the inventory

```bash
make inspect SURVEY=job_stress_fa ARGS="--full --ids"
```

Questions are grouped by option signature, largest group first — those are
your scales. Free-text questions are listed separately (usually demographics →
`static_answers`). The output ends with a ready-to-paste `scales:` block and,
with `--ids`, the question ids of each signature for `question_id_in`.

### Step 5 — write the instrument

Fill `survey.yaml` top to bottom ([§9](#9-surveyyaml-reference) documents every
key; the template is commented, and the 14 folders under `surveys/` are worked
examples):

1. `population` — who the respondents are and what latent traits drive them.
2. `scales` — paste the draft from step 4, rename, and **check `coding`**.
3. `constructs` — which questions measure which traits; `reverse_when` for
   reverse-keyed items.
4. `static_answers` — questions answered from the persona record.
5. `skip`, `scenarios`, `transcripts` if the form needs them.
6. `answering.completeness.min_ratio` — `1.0` if every question is required.
7. `prompt` — the survey-specific wording ([§10](#10-prompts)).

### Step 6 — validate

```bash
make validate SURVEY=job_stress_fa
```

It must end with **"Every question is accounted for."** A question that matches
nothing, or a rule that matches nothing, is an error on purpose: a stale rule
used to collapse whole instruments into one bucket while the run reported
success. Read the implied correlation matrix it prints; it is the cheapest
sanity check on the population model. The `prompts:` lines show where each
template comes from.

### Step 7 — smoke test and analyse

```bash
make run-all SURVEY=job_stress_fa PHASES=2,3 NUM_PERSONAS=3
make status  SURVEY=job_stress_fa
make analyze SURVEY=job_stress_fa
```

Read a few files in `output/job_stress_fa/personas/` and `answers/`. See
[§15](#15-checking-quality-before-submitting) for what the analysis should look
like.

### Step 8 — generate and submit

```bash
make run-all SURVEY=job_stress_fa PHASES=2,3 NUM_PERSONAS=100
make submit  SURVEY=job_stress_fa LIMIT=1      # one first; watch it with HEADLESS_MODE=False
make submit  SURVEY=job_stress_fa
```

## 8. Working on several clients at once

Each client questionnaire gets its own **git worktree**: a full working copy
on its own branch, sharing this one repository. Clients never see each other's
survey, output, `.env` or code changes until delivery, and fixes made on `main`
reach every client with one command.

```
PBRfactor/
  PersonaBot/                    main — general code changes only
  clients/acme/job_stress_fa/    worktree on branch survey/job_stress_fa
  clients/beta/burnout_fa/       worktree on branch survey/burnout_fa
  clients/_archive/<client>/     output archives written at delivery
```

### The whole lifecycle

```bash
# in PersonaBot/ (main)
make client CLIENT=acme SURVEY=job_stress_fa              # FROM=<survey> to clone one, KEYS=1-7
cd ../clients/acme/job_stress_fa

# inside the worktree: SURVEY is preset, so no SURVEY= needed
nano surveys/job_stress_fa/survey.yaml                    # form.provider, form.url
make schema && make inspect ARGS="--full --ids"
make validate
make run-all NUM_PERSONAS=100
make sync                                                 # pick up fixes from main, any time
make deliver                                              # when the client is done
```

`make client` creates the worktree and branch from `main` (fast-forwarding
`main` from GitHub first when it can), writes the worktree's `.env` from the
base `.env` with `SURVEY` preset and form URLs removed, scaffolds and commits
`surveys/<id>/` (or continues it if that survey is already on `main` — a
returning client), and installs the worktree's own `.venv`. The venv must be
per worktree: an editable install runs exactly one checkout's code.

`make deliver` runs, stopping at the first problem with nothing half-done:

1. commits leftover changes under `surveys/<id>/` for you — but refuses if
   anything **outside** the survey folder is uncommitted (general code needs its
   own commit);
2. rebases the branch onto the current `main`;
3. runs `validate-config` and the full test suite in the worktree's venv;
4. archives `output/<id>/` to `clients/_archive/<client>/<id>-<time>.tar.gz`;
5. tags the delivered commit `delivered/<client>/<id>/<date>` (`-2`, … for a
   second delivery that day);
6. fast-forwards `main` in `PersonaBot/` and pushes `main` and the tag;
7. removes the worktree and its branch.

Options: `NO_PUSH=1`, `KEEP=1` (keep the worktree), `SKIP_CHECKS=1`,
`NO_ARCHIVE=1`. From `PersonaBot/` use `make deliver CLIENT=acme SURVEY=job_stress_fa`.
The tag means you can always rebuild exactly what a client got:
`git switch --detach delivered/acme/job_stress_fa/2026-10-20`.

### General fixes while clients are open

Make them in `PersonaBot/` on `main` (or a short `fix/…` branch merged into
`main`), commit them as `fix(…)`/`feat(…)`, then run `make sync` in each client
worktree. Avoid fixing shared code only inside a client worktree — that is how
the fifteen 2.x copies drifted apart. (A general fix committed there still
reaches `main`, but only at that client's delivery.)

### Shared resources

* **API keys.** Each running process paces only its own key pool, so two
  clients generating at the same time on the same keys hit quota sooner. Give
  them disjoint subsets: `make client … KEYS=1-7` and `KEYS=8-15` (numbers refer
  to the base `.env`; they are renumbered in the client's `.env`).
  `make clients` warns about overlaps.
* **Tor.** Shared as is. Two clients may use the same exit IPs; within one
  client, concurrent submissions already get distinct SocksPorts.

### Seeing everything

```bash
make clients        # from PersonaBot/ or any client worktree
```

shows each client worktree with commits ahead of / behind `main`, uncommitted
files, its number of keys, pending and submitted answer sets, and whether its
venv exists. `CLIENTS_DIR` in `.env` moves the `clients/` folder elsewhere.

## 9. `survey.yaml` reference

A complete, commented example is `surveys/_template/survey.yaml`.

### Top level

```yaml
version: 2              # 1 (a 2.x file) is still accepted
id: job_stress_fa       # letters, digits, _ and -; names the output folder
locale: fa              # picks prompts/base/fa unless prompt.base says otherwise
title: "…"              # optional, available to templates as survey.title
```

### `form`

```yaml
form:
  provider: porsline    # porsline | porsline_v2 | porsa | google_forms | avalform | digisurvey
  url: https://…        # absolute http(s) URL; wins over BASE_FORM_URL
  max_pages: 400        # page budget for phases 1 and 4; default MAX_PAGES
```

### `population`

```yaml
population:
  seed: null                        # default seed for phase 2

  latent_model:                     # theta = L·f + u·e, f ~ N(0, I), u = sqrt(1 - ΣL²)
    common_factors: [G, S]
    dimensions:
      job_stress:
        loadings: {G: 0.70, S: -0.20}   # squared loadings must sum to < 1
        label: "فشار کاری"              # the only thing the LLM sees, with a band word

  bands:                            # z-score cut points -> words (len(labels) = len(cuts)+1)
    cuts:   [-1.5, -0.9, -0.35, 0.35, 0.9, 1.5]
    labels: ["خیلی پایین", "پایین", "کمی پایین", "متوسط", "کمی بالا", "بالا", "خیلی بالا"]

  response_style:                   # drawn per respondent; drives nudges and careless mode
    acquiescence:     {dist: normal, mu: 0.0, sigma: 0.40}
    extreme_response: {dist: normal, mu: 0.0, sigma: 0.40}
    careless:         {dist: bernoulli, p: 0.04}   # true -> straight-lined in code

  demographics:                     # drawn in order; kept in the persona record
    - {name: age, dist: uniform_int, low: 22, high: 60}
    - {name: gender, dist: categorical, choices: [["زن", 0.55], ["مرد", 0.45]]}
    - {name: tenure_raw, dist: normal, mu: 10, sigma: 6, round: int,
       clip: [0, "age - 22"], persist: false}     # intermediate, not saved

  derived_fields:                   # deterministic facts, e.g. for static answers
    - {name: tenure, dist: derived, expr: "max(0, tenure_raw)"}
    - {name: phone, dist: derived, expr: "index"}
```

Distributions (`dist:`):

| `dist` | Keys |
|---|---|
| `constant` | `value` |
| `uniform_int` | `low`, `high` |
| `categorical` | `choices: [[label, weight], …]` — weights are normalised (a warning is logged if they do not sum to 1) |
| `normal` | `mu`, `sigma`, optional `round: int`, `clip: [lo, hi]`, `on_out_of_range: resample` (default when clipped) or `clip` |
| `derived` | `expr` |
| `bernoulli` | `p`, optional `labels: {"true": …, "false": …}` |
| `conditional` | `cases: [{when: expr, <dist>}, …, {default: true, <dist>}]` — first match wins; `default` last |

Any number may be an **expression** string. Expressions are sandboxed (no
imports, no attribute access on objects) and may use earlier variable names,
`z.<dimension>` (the respondent's trait z-score), `index` (respondent number),
response-style names, arithmetic, comparisons, `a if cond else b`, and
`min max abs round int float clamp sigmoid exp log sqrt floor ceil randint uniform gauss`.
A variable that references one declared after it is rejected at load.

### `scales`

A scale is a response format plus the direction its options run in.

```yaml
scales:
  - name: agree5
    match: {options_equal: ["کاملاً مخالفم", "مخالفم", "نظری ندارم", "موافقم", "کاملاً موافقم"]}
    coding: low_first      # option[0] is the LOW end (score = index + 1)
  - name: freq5_desc
    match: {options_equal: ["همیشه", "اغلب", "گاهی", "به ندرت", "هرگز"]}
    coding: high_first     # option[0] is the HIGH end (score = K - index)
```

`coding` only affects scoring in `analyze`, but getting it wrong deflates that
construct's alpha. Scales are matched first; first match wins.

### Matchers

Used by `scales`, `constructs` (`match`, `reverse_when`), `static_answers` and
`skip`. Text is normalised on both sides (Arabic/Persian letters and digits,
ZWNJ, diacritics), so a rule written with Persian yeh matches a schema with
Arabic yeh.

| Key | Matches when |
|---|---|
| `question_id: "123"` / `question_id_in: ["1", "2"]` | the id is listed |
| `type: RADIO` (or a list) | `TEXT_INPUT`, `RADIO`, `SELECT`, `CHECKBOX`, `MATRIX`, `VOICE`, `UNKNOWN` |
| `text_matches: "regex"` | the regex is found in the question text |
| `text_contains_any: [...]` / `text_contains_all: [...]` | substrings in the text |
| `options_equal: [...]` | the option **set** equals this (order-insensitive) |
| `options_include_any: [...]` / `options_include_all: [...]` | some / all of these options exist |
| `options_regex: "regex"` | some option matches |
| `option_count: 5` or `{min: 2, max: 3}` | number of options |
| `scale: agree5` (or a list) | the scale already assigned to the question |
| `always: true` / `never: true` | constant |
| `all: [...]`, `any: [...]`, `not: {...}` | combinators |

Several keys in one mapping are AND-ed. Every regex and list is checked at load.

### `constructs`

```yaml
constructs:
  - key: job_stress
    label: "فشار کاری"               # shown to the LLM as the section name
    traits: [job_stress]             # latent dimensions whose bands the prompt carries
    match: {question_id_in: ["101", "102", "103", "104"]}
    reverse_when: {question_id_in: ["103"]}   # reverse-keyed items, tagged for the LLM
```

Evaluated in order, first match wins — list narrower rules first. There is no
catch-all: every question must end up in a construct, a static answer or
`skip`, and every rule must match something. One construct = one LLM request
per persona.

### `static_answers`

Questions answered from the persona record, never by the LLM (identity facts,
arithmetic, serial numbers). `value` is a Jinja2 expression over `persona`
(`persona.demographics.*`, `persona.derived.*`, `persona.id`, …); an unknown
field is an error.

```yaml
static_answers:
  - name: age
    match: {question_id: "100"}
    value: "{{ persona.demographics.age }}"
    exclude_from_psychometrics: true
  - name: phone
    match: {question_id: "199"}
    value: "{{ '0912%07d' | format(persona.derived.phone) }}"
```

For a choice question the rendered value must be one of its options.

### `skip`

```yaml
skip:
  - {question_id: "150"}             # ignored everywhere, not counted for completeness
```

### `scenarios` (between-subjects vignettes, `porsline`)

For forms that show each respondent one randomly drawn vignette before the
items. Phase 2 spreads personas evenly over the scenarios, phase 3 gives each
persona its scenario text, and phase 4 opens the form, recognises which
scenario it drew (by the number it starts with, else by close text match), and
only then claims an answer set written for that scenario.

```yaml
scenarios:
  - key: "1"                              # the number the form prints first: «1. …»
    text: "در سازمان شما …"              # verbatim, shown to the LLM
    factors: {pay: emphasized, autonomy: high}   # for your analysis only; never in a prompt
```

### `transcripts` (voice-only questions, `porsline_v2`)

```yaml
transcripts:
  enabled: true
  dir: voice_texts                 # under output/<id>/
  filename_from: "53942610"        # a submitted answer that names each file (e.g. a serial phone)
  types: [VOICE]
  header: "…"                      # optional first line of every file
```

Voice questions are answered as prose in phase 3 and left empty in the form.
Run `make transcripts` after phase 3.

### `answering`

```yaml
answering:
  chunk_by: construct              # "none" = one request per persona (inflates alpha; warned)
  shuffle_within_chunk: true
  temperature: {min: 0.55, max: 0.85}
  normalization: {unicode: NFKC, fold_arabic_to_persian: true, zwnj: strip, strip_trailing: ".,؛،۔"}
  fuzzy: {algorithm: ratio, min_score: 86}        # below this an answer is discarded, not guessed
  completeness:
    min_ratio: 0.95                # 1.0 when every question is required
    denominator: answerable
    max_retries: 2
    retry_scope: failed_constructs_only           # or whole_persona
  careless: {enabled: true, anchor_stickiness: 0.70, drift: [-1, 1], apply_static_answers: true}
  reverse_tags: {direct: "[مستقیم]", reverse: "[معکوس]"}
```

### `response_style_nudges`

Turns the sampled response style into one sentence of the answer prompt.
`when` is an expression over the `response_style` values.

```yaml
response_style_nudges:
  rules:
    - {when: "acquiescence > 0.30", text: "در گویه‌های مرزی کمی به سمت موافقت متمایل می‌شوی."}
    - {when: "extreme_response < -0.30", text: "معمولاً گزینه‌های میانی را ترجیح می‌دهی."}
  default: "سبک پاسخ‌دهی تو عادی و متعادل است."
```

### `analysis`

```yaml
analysis:
  reverse_keys_from: config        # keep "config": deriving keys from data inflates alpha
  warn_on_sign_disagreement: true
  response_style_min_scale_points: 3
  straightlining: {scope: within_construct, sd_threshold: 0.5}
  sanity: {alpha_max: 0.95, mean_inter_item_r_max: 0.6}
  min_items_for_alpha: 3
```

### `prompt`

See [§10](#10-prompts).

## 10. Prompts

Templates are resolved along a search path, most specific first:

1. `surveys/<id>/prompts/` — the survey's own overrides
2. `prompts/<prompt_preset>/` — only if a legacy 2.x spec still names one
3. `prompts/base/<prompt.base or locale>/` — the base pack (`fa`, `en`)

Four templates are used: `persona.system.j2`, `persona.user.j2`,
`answers.system.j2`, `answers.user.j2`. Stop at the first level that is enough:

**Level 1 — variables only (most forms).** Fill the `prompt:` block:

```yaml
prompt:
  base: fa                        # optional; defaults from locale
  role: "یک معلم شاغل در آموزش و پرورش ایران"
  study_context: >-
    پاسخ‌دهندگان معلمان شاغل‌اند؛ پرسش‌نامه سه بخش دارد …
  persona_sections:               # the `details` keys the persona writer must produce
    background_and_teaching: "زمینه‌ی شغلی، مقطع و درس …"
    school_and_colleagues: "فضای مدرسه از نگاه او …"
  persona_rules: ["برای کسی که فرزند ندارد از بچه‌ها ننویس."]
  answer_rules: ["گویه‌های مربوط به مدرسه را ارزیابی خودت از مدرسه بدان."]
  scenario_preamble: "فرض کنید در یک سازمان مشغول به کار هستید …"
  vars: {}                        # free-form values for your own templates
```

The Persian base pack already contains the ten answering rules the 2.x surveys
converged on (choose options by meaning, not list position; scales may run in
opposite directions; mind reverse tags; mind whether an item is about the
respondent or about someone else; stay consistent with your own facts; vary
realistically; answer as an ordinary person and do not guess the hypothesis;
copy options verbatim even with typos; …). `answer_rules` continue as rule 11
onwards. The persona pack enforces fidelity to the sampled data, band
intensity, independence of traits, everyday (non-clinical) language, cultural
detail and restrained handling of sensitive topics. Without
`persona_sections`, four generic sections are used.

**Level 2 — replace one block.** Create `surveys/<id>/prompts/answers.system.j2`:

```jinja
{% extends "base/answers.system.j2" %}
{% block levels %}…your own wording…{% endblock %}
```

Blocks in `answers.system.j2`: `role`, `who_you_are`, `life`, `levels`,
`style`, `scenario`, `rules`. In `persona.system.j2`: `intro`, `context`,
`fidelity`, `language`, `output`.

**Level 3 — replace a whole template.** Put a complete file in
`surveys/<id>/prompts/`. Examples: `sleep_fomo_digital_burnout_fa` prints exact
sleep times in `persona.user.j2`; `tehran_urban_network_fa` words matrix rows
as actors in `answers.user.j2`.

**Variables** available to templates:

| Where | Variables |
|---|---|
| both phases | `survey` (`id`, `locale`, `title`, `prompt.*`) |
| persona phase | `dimensions` (`name`, `label`), `bands.labels`, `profiles` (`id`, `demographics`, `derived`, `bands`) |
| answer phase | `construct` (`key`, `label`), `persona` (`demographics`, `derived`, `details`), `trait_bands`, `response_style_text`, `reverse_tags`, `questions` (`id`, `text`, `options`, `direction`), `scenario` (`key`, `text`) or none |

Templates use Jinja2 with `StrictUndefined`: a misspelt variable is an error,
not an empty string. Raw z-scores never reach a prompt, only band words.
`make validate` prints where each template resolved from, and the test suite
renders every survey's prompts.

The persona response must be a JSON array of `{"id", "details": {…}}`; the
answer response a JSON object `{"_reasoning": "…", "<question id>": "<answer>", …}`.
Keep those output instructions if you replace the user templates.

## 11. Providers

| The form… | `form.provider` | Notes |
|---|---|---|
| is on survey.porsline.ir with one question per page | `porsline` | Clicks «شروع»; handles auto-advance, statement pages, dropdowns, «single page display» groups, forms that end on «پایان»; confirms success from the server response. Supports `scenarios`. Set `form.max_pages` above the question count |
| is on Porsline with matrix tables, grouped pages or voice questions | `porsline_v2` | Reads the survey definition from Porsline's API (this registers one responder session — extract once). Matrix rows become questions `<matrix id>#<row>`. Supports `transcripts` |
| is on porsa.irandoc.ac.ir | `porsa` | Reads every page at once; matrix rows become questions; every question is required, so use `completeness.min_ratio: 1.0`. Detects "already answered" and closed-survey pages |
| is a Google Form | `google_forms` | Waits for hydration; detects a rejected page and names the empty fields |
| is on Avalform / DigiSurvey | `avalform` / `digisurvey` | Carried over from 2.x unchanged |

If `porsline` refuses a schema with "each report all N of the page's radios",
the form uses Porsline's grouped renderer: switch to `porsline_v2`.

Adding a provider for a new platform is the one change that needs Python:
subclass `SchemaExtractor` and `FormSubmitter` in
`src/personabot/providers/<name>/__init__.py` and register them with
`@register_extractor("<name>")` / `@register_submitter("<name>")`. The base
classes own pagination budgets, retries, receipts, queues and Tor; a provider
implements `parse_page`/`advance` (extraction) and `fill_page`/`advance`/`detect_success`
(submission), optionally `prepare`, `fill_placeholders` and `detect_scenario`.

## 12. LLM keys, quotas and proxies

* **Key pool.** Add `GEMINI_API_KEY_1 … _N`. Each key serves one request at a
  time; keys are handed out least-recently-used, so the whole pool is used.
* **Free tier pacing.** Gemini's free tier limits *input tokens per minute per
  key*. Set `GEMINI_INPUT_TOKENS_PER_MINUTE` to the TPM value shown for your
  model at https://ai.dev/rate-limit and the pool paces each key to 85 % of it.
  Leave it at `0` on a paid tier (a warning reminds you it is off).
* **Quota errors** rotate to another key; the key is parked for the server's
  `retryDelay` (or 60 s, or 15 min for a daily quota). If every key is limited
  the request fails with "All N Gemini key(s) are rate-limited".
* **Transient errors** (timeouts, 5xx, unparsable JSON) are retried
  `LLM_MAX_RETRIES` times with jittered backoff on a different key.
* **`make check-keys`** probes each key on its own and reports ✓ ok,
  ~ quota, ✗ invalid or ? unreachable. `LIVE=1` sends a tiny real request, which
  also proves quota is left.
* **Geo-blocking** ("User location is not supported") is a network problem, not
  a key problem: set `GEMINI_PROXY`. It applies only to Gemini calls; the
  browser keeps its own route.
* **Other backends.** `AI_PROVIDER=vllm` with `VLLM_BASE_URL` and
  `VLLM_MODEL_NAME` works with any OpenAI-compatible server (vLLM, Ollama, LM
  Studio). The endpoint must support JSON responses.

## 13. Tor

Optional; routes each browser context through Tor so submissions do not all
come from one address.

Chromium does not support SOCKS5 authentication, so circuits are isolated **per
SocksPort**: each context takes the next port in a rotation, and N ports give N
concurrent submissions N exit IPs. Example `torrc` addition:

```
SocksPort 9050
SocksPort 9052
SocksPort 9053
SocksPort 9054
ControlPort 9051
HashedControlPassword <output of: tor --hash-password yourpassword>
MaxCircuitDirtiness 60
```

`.env`:

```ini
USE_TOR="True"
TOR_SOCKS_PORT=9050
TOR_SOCKS_EXTRA_PORTS="9052,9053,9054"
TOR_CONTROL_PASSWORD="yourpassword"
TOR_CIRCUIT_COOLDOWN_SECONDS=60        # match MaxCircuitDirtiness
CONCURRENT_SUBMISSIONS=4               # at most the number of ports
```

Before each submission the circuit is checked (an IP echo within 20 s) and the
exit IP logged. A circuit that fails before anything is submitted is taken out
of rotation for the cooldown and the item is retried on another one (up to
`BROWSER_MAX_ATTEMPTS`). Failures after the form has started are never
retried in place, so nothing is submitted twice. Where plain Tor is blocked,
configure a pluggable transport (e.g. snowflake) in `torrc`.

## 14. Outputs and file formats

```
surveys/<id>/form_schema.json        schema snapshot (phase 1)
surveys/<id>/form_schema.previous.json   the snapshot it replaced, if the form changed
output/<id>/
  personas/<uuid>.json               waiting for phase 3
  personas/inprogress/               claimed by a running phase 3
  personas/done/                     answered
  answers/<uuid>.json                ready to submit (same file name as its persona)
  answers/inprogress/                claimed by a running phase 4
  answers/done/                      submitted
  receipts/<time>_<label>.png|.html  screenshot + page of every failed submission
  voice_texts/<id>.txt               transcripts (name from transcripts.dir)
  validation_report.json             from `analyze`
```

A file's **location is its state**; moves are atomic, so several processes can
work the same queue. `make recover` returns `inprogress/` items to the queue
after a crash.

**Schema** — a JSON list of
`{"question_id", "question_text", "type", "options": [{"text", "value"}]}`.

**Persona** — `{"id": "persona_7", "demographics": {…}, "derived": {…},
"latent_traits_z": {…}, "latent_traits_band": {…}, "response_style": {…},
"details": {…}, "scenario": "4"}`. `latent_traits_z` is the ground truth and
is never shown to the LLM.

**Answer set** — a flat `{"<question id>": "<exact option or text>"}` plus
metadata keys starting with `_` (`_persona`, `_scenario`) that are never
submitted.

`make status` reads all of this without changing anything: per stage the total,
valid, partial (below the completeness ratio) and corrupt files, claimed items,
and the number of failed submissions.

## 15. Checking quality before submitting

`make analyze` reports, per construct: Cronbach's alpha, mean inter-item
correlation, scale mean and SD, and items whose item-total correlation
disagrees with the configured reverse keying; and overall: extreme-response,
acquiescence, midpoint and straight-lining rates.

Warning signs of LLM self-consistency (flagged automatically): alpha above
`sanity.alpha_max` (0.95) or mean inter-item r above
`sanity.mean_inter_item_r_max` (0.6). Remedies: lower the loadings in
`latent_model`, widen the response-style distributions, keep
`chunk_by: construct` and `shuffle_within_chunk: true`. A sign disagreement
usually means a missing `reverse_when` or a wrong scale `coding`.

## 16. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `No survey selected` | Pass `SURVEY=<id>`, or set `SURVEY` in `.env`. `make surveys` lists ids |
| `validate` reports UNASSIGNED questions or rules matching nothing | The form changed or a matcher is wrong. Re-run `make inspect` and fix `survey.yaml` |
| `form_schema.previous.json` appeared | The form was republished; compare the two and update the matchers |
| Phase 3: "discarded … matched no option" | The model paraphrased an option. Usually rare; persistent cases need a clearer prompt or a lower `fuzzy.min_score` |
| Phase 3: persona "incomplete" | Some constructs failed after retries; it stays queued for the next run. Check the LLM errors above it |
| `All N Gemini key(s) are rate-limited` | Add keys, set `GEMINI_INPUT_TOKENS_PER_MINUTE`, or lower `CONCURRENT_LLM_TASKS` |
| Every key "Rejected … location is not supported" | Set `GEMINI_PROXY` |
| Phase 4 stops early / "still paginating" | Raise `form.max_pages` (Porsline: above the question count) |
| Phase 4: "no success confirmation" | Open the receipt in `output/<id>/receipts/`. The log says whether the form left its last page; if it did, check the survey's response count before retrying, or it may be submitted twice |
| Porsa: "already answered" | That exit IP was used; with Tor another circuit is tried automatically. Without Tor the form accepts one response per address |
| Porsa refuses to leave a page | A required answer is missing; set `completeness.min_ratio: 1.0` and regenerate |
| Google Forms: "controls were still disabled" | Lower `CONCURRENT_SUBMISSIONS` |
| Items stuck in `inprogress/` | A process was killed; `make recover` (only with no worker running) |
| `check-venv` fails | The venv belongs to another checkout: `make install` |

Set `LOG_LEVEL=DEBUG` or `HEADLESS_MODE=False` to see more.

## 17. Development

```bash
make test                                # the whole suite
.venv/bin/python -m pytest tests/unit/test_porsline.py -q
```

* Tests use no network and no API key. Browser-level tests run the providers'
  real JavaScript against HTML snippets in Chromium and skip if it is missing.
* Every survey under `surveys/` is part of the suite: it must load, cover every
  question of its schema snapshot, and render all of its prompts.
* Code layout: `settings.py` (deployment), `workspace.py` (survey binding and
  paths), `survey/` (YAML schema and loading), `domain/` (sampling, matching,
  instrument, validation), `application/` (phases and reports),
  `infrastructure/` (LLM, browser, Tor, prompts, storage), `providers/`.
* Record every user-visible change under `[Unreleased]` in `CHANGELOG.md`; the
  project follows semantic versioning with annotated `vX.Y.Z` tags.
* When a live form teaches something new, add it to `docs/KNOWLEDGE.md`.

## 18. Responsible use

Personas are synthetic. The prompts describe the work as a disclosed
AI-persona ("silicon sampling") study, and `analyze` exists to show where
synthetic data looks implausibly clean. Submit to a live form only with the
survey owner's knowledge and for the purpose they agreed to.
