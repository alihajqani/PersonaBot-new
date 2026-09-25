# PersonaBot

Synthetic survey respondents ("silicon sampling") for online questionnaires —
**one codebase, one folder per questionnaire.**

PersonaBot samples a population from a latent-trait model, has an LLM write
each respondent's life in prose, has the LLM answer the questionnaire in
character one construct at a time, and submits the answers through a real
browser. Everything specific to a questionnaire lives in `surveys/<id>/`; the
code never changes between forms.

> 3.0 merges fifteen per-form copies of the 2.x project into this one tree. What
> each copy learned on its live form is listed in
> [docs/KNOWLEDGE.md](docs/KNOWLEDGE.md); the design is in
> [docs/specs/](docs/specs/2026-09-25-unified-personabot-design.md).

## Pipeline

| Phase | Command | What happens | Output |
|---|---|---|---|
| 1 schema | `make schema` | the provider walks the live form (or reads its API) | `surveys/<id>/form_schema.json` |
| 2 personas | `make persona` | traits + demographics sampled in Python; the LLM writes only `details` prose | `output/<id>/personas/` |
| 3 answers | `make answer` | static answers from the persona; one LLM request per construct; answers snapped to the form's exact options; completeness gate with targeted retry | `output/<id>/answers/` |
| 4 submit | `make submit` | one isolated browser context (and Tor circuit) per submission, through an atomic file work queue | `answers/done/`, `receipts/` on failure |

## Install

```bash
make install                 # .venv, the package, Chromium
cp .env.example .env         # add GEMINI_API_KEY_1 (…_2, _3 for a key pool)
make check-keys              # proves every key works (LIVE=1: also quota)
```

## Run an existing survey

```bash
make surveys                                         # list them
make validate  SURVEY=teacher_innovation_km_ai_fa    # config + coverage + prompts
make run-all   SURVEY=teacher_innovation_km_ai_fa NUM_PERSONAS=20
make status    SURVEY=teacher_innovation_km_ai_fa
make analyze   SURVEY=teacher_innovation_km_ai_fa    # reliability & response styles
```

Set `SURVEY=<id>` in `.env` to make one survey the default.

## Start a new questionnaire

```bash
make new-survey NAME=job_stress_fa              # or FROM=<existing id> to clone one
# edit surveys/job_stress_fa/survey.yaml: form.provider, form.url
make schema   SURVEY=job_stress_fa
make inspect  SURVEY=job_stress_fa ARGS="--full --ids"
# write population / scales / constructs / static_answers / prompt
make validate SURVEY=job_stress_fa              # must report full coverage
```

The full walk-through, including how far prompt customisation needs to go, is
[docs/HOWTO_NEW_SURVEY.md](docs/HOWTO_NEW_SURVEY.md).

## Layout

```
.env                      deployment only: keys, model, concurrency, browser, Tor
surveys/<id>/
  survey.yaml             form (provider, url) · population · scales · constructs
                          · static answers · scenarios · transcripts · prompt vars
  form_schema.json        schema snapshot, written by phase 1
  prompts/*.j2            OPTIONAL overrides of the base prompt pack
prompts/base/{fa,en}/     shared prompt templates with overridable blocks
output/<id>/              personas/ answers/ receipts/ transcripts … (git-ignored)
src/personabot/           the code
```

Three layers, three owners: `.env` never mentions a survey; `survey.yaml`
never mentions a credential; templates hold wording only. A survey's own
`form.url` always wins over `BASE_FORM_URL`, so one survey's answers can never
be sent to another survey's form.

## Providers

| Provider | Platform | Notes |
|---|---|---|
| `porsline` | survey.porsline.ir, classic renderer | welcome page, one question per page, auto-advance, dropdowns, «single page display» groups, between-subjects scenarios; set `form.max_pages` above the question count |
| `porsline_v2` | survey.porsline.ir, grouped renderer | reads the definition API; matrices flattened to `<id>#<row>`; voice items |
| `porsa` | porsa.irandoc.ac.ir | whole form in the DOM; validating pagination — use `completeness.min_ratio: 1.0` |
| `google_forms` | docs.google.com/forms | hydration wait, rejected-page detection |
| `avalform`, `digisurvey` | — | carried over from 2.x unchanged |

## Commands

```
personabot [--survey ID | --config FILE] <command>
  run [PROVIDER] --phases 1,2,3,4 --num-personas N [--seed S] [--start-index I] [--limit N] [--url URL]
  validate-config | inspect-schema [--full --ids] | status | analyze | recover | export-transcripts
  check-keys [--live] | providers | surveys | new-survey ID [--from ID]
```

Exit codes: 0 ok · 1 a phase or check failed · 2 configuration error · 130 interrupted.

## Tests

```bash
make test
```

Every survey under `surveys/` is part of the suite: it must load, cover every
question of its schema snapshot, and render all of its prompts.

## Ethics

Personas are synthetic. The prompts state that the study is a disclosed
AI-persona ("silicon sampling") study, and the analysis command exists to show
where synthetic data looks implausibly clean. Submit to a live form only with
the survey owner's knowledge and for the purpose they agreed to.
