# PersonaBot

Generates synthetic survey responses from statistically-structured personas and
submits them to live questionnaire platforms through a real browser.

Running a different survey requires **no Python changes** — only `.env`, a
survey config file, and a prompt preset.

```
.env                      credentials, endpoints, concurrency, browser, Tor
configs/surveys/*.yaml    the instrument: population, scales, constructs, bindings
prompts/<preset>/*.j2     wording and tone
```

Keeping these three apart is the point of the design. Mixing them is what
produced the failure described in *Why the rewrite happened* below.

## Quick start

```bash
make install                  # .venv + dependencies + chromium
cp .env.example .env          # add GEMINI_API_KEY_1 and BASE_FORM_URL

make schema PROVIDER=porsline # phase 1 — extract the form's structure
make validate                 # phase 1.5 — CHECK YOUR CONFIG COVERS IT
make persona NUM_PERSONAS=20  # phase 2 — sample personas, write prose
make answer                   # phase 3 — answer, validate, gate on completeness
make submit PROVIDER=porsline # phase 4 — fill and submit

make status                   # what is on disk, validated
make analyze                  # Cronbach's alpha, inter-item r, response styles
```

`make validate` is not optional. See below.

## The pipeline

| Phase | Command | Produces |
|---|---|---|
| 1 | `run <provider> --phases 1` | `output/form_schema.json` |
| 2 | `run --phases 2` | `output/personas/<uuid>.json` |
| 3 | `run --phases 3` | `output/answers/<uuid>.json` |
| 4 | `run <provider> --phases 4` | live submission, file moved to `answers/done/` |

Each phase persists to disk, so phases are independently runnable, resumable and
parallelizable. A file's location *is* its state.

Phases run **in the order you list them**, and a phase that fails makes the
process exit non-zero — so `make loop` stops instead of repeating a broken run.

## Writing a survey config

Start from `configs/surveys/_template.yaml`, which is fully commented. For a
worked example against a real 51-question form, see `sexual_intimacy_fa.yaml`.

Four things it declares:

**A population.** Latent traits from a factor model (`theta = L·f + u·e`),
demographics from named distributions — `categorical`, `uniform_int`, `normal`
with truncation, `conditional`, `bernoulli`, `derived`. Later variables may
reference earlier ones and any trait z-score via `z.<dimension>`. Expressions are
sandboxed: arithmetic and a fixed function table, never `eval`.

**Scales.** A response format plus the direction its options run in. Two scales
in one instrument can share option strings with opposite meanings, so coding
direction is declared per scale rather than inferred.

**Constructs.** Which questions measure what, and which are reverse-keyed.
Matched by a small DSL — by id, question text, option signature, or scale —
evaluated in order, first match wins. That ordering is also how you split one
scale into two poles: list the narrower rule first.

**Static answers.** Questions answered straight from the persona rather than by
the model. This guarantees identity consistency and keeps arithmetic (a spouse's
age, a derived duration) deterministic.

### `validate-config` is the guard that matters

```
$ make validate
Schema coverage: 51 questions (12 static, 0 skipped, 39 psychometric)
  constructs:
    sexual_satisfaction           13
    ...
✓ Every question is accounted for.
```

A question that matches nothing, or a rule that matches nothing, is an **error**.
There is deliberately no catch-all construct.

## Why the rewrite happened

The predecessor's `core/instrument.py` matched hard-coded Persian strings from a
questionnaire that was **not the one being run**. Every signature missed. The
result, verified by running the old code against the live schema:

```
classify()           -> Counter({'other': 51})   # all 51 questions, one bucket
static_answers()     -> {}                        # nothing
is_reverse()         -> 0 of 51
```

So construct chunking — the deliberate guard against the self-consistency that
inflates Cronbach's alpha — never ran. Five of six latent dimensions never
reached a prompt. And the analysis harness auto-reversed any item whose
correlation came out negative, which manufactures reliability from noise:

| construct | keyed from config | legacy auto-reversal |
|---|---|---|
| sexual_satisfaction | −0.510 | **0.481** |
| partner_responsiveness | −0.069 | **0.429** |

Both columns describe the *same uniformly random answers*. The pipeline reported
success throughout. Nothing failed loudly, which is why the coverage check now
exists.

## Configuration (`.env`)

```ini
GEMINI_API_KEY_1="..."          # _2, _3 … are leased round-robin with cooldowns
GEMINI_MODEL_NAME="gemini-2.5-pro"
AI_PROVIDER="gemini"            # or "vllm" for any OpenAI-compatible endpoint

SURVEY_CONFIG="configs/surveys/_template.yaml"
BASE_FORM_URL="https://..."     # phases 1 and 4 only

CONCURRENT_LLM_TASKS=4          # in-flight generations
CONCURRENT_SUBMISSIONS=2        # in-flight browser submissions

HEADLESS_MODE="True"
SLOW_MO=50
MAX_PAGES=40                    # hard ceiling on form pagination

USE_TOR="False"
OUTPUT_DIR="output"
```

## Concurrency and Tor

API keys are **leased**, not handed out round-robin: one holder at a time, with a
cooldown park on rate-limit errors. Round-robin under concurrency gives the same
key to several in-flight requests, which trips its limit.

For submissions, each browser context gets its own SOCKS credentials. Tor's
`IsolateSOCKSAuth` maps distinct credentials to distinct circuits, so concurrent
submissions exit from **different IPs without `NEWNYM`** — which is process-global
and cannot give N concurrent contexts N addresses. Proxy settings go to Playwright
only; `os.environ` is never mutated.

## Adding a provider

Create `src/personabot/providers/<name>/__init__.py` and implement four hooks
each for extraction and submission:

```python
@register_extractor("myplatform")
class MyExtractor(SchemaExtractor):
    async def parse_page(self, page) -> list[Question]: ...
    async def advance(self, page) -> bool: ...

@register_submitter("myplatform")
class MySubmitter(FormSubmitter):
    async def fill_page(self, page, answers) -> int: ...
    async def advance(self, page) -> bool: ...
    async def detect_success(self, page) -> bool: ...
```

The base classes own the bounded pagination loop, browser and context lifecycle,
atomic claiming, Tor scoping and failure receipts. `make providers` lists what is
registered.

## Layout

```
src/personabot/
├── cli.py, settings.py, logging_setup.py, errors.py
├── domain/          pure: models, expressions, matching, sampling,
│                    instrument, answer_validation, text
├── survey/          spec (pydantic models) + loader
├── application/     generate_personas, generate_answers, status, analyze
├── infrastructure/  llm/, browser/, storage/, prompts/
└── providers/       base, registry, porsline, google_forms, avalform, digisurvey
```

`domain/` imports nothing from the rest of the project and touches no I/O, which
is why it is testable without a `.env`, an API key or a browser.

## Tests

```bash
make test        # 171 tests, no network, no browser, no API key
```

Phases 2 and 3 are covered end to end against a stub LLM.

## Notes

- Gemini safety filters are set to `BLOCK_NONE`. This is deliberate: personas
  cover sexual-health content the defaults refuse, and filtering would bias the
  sample toward respondents whose answers happen to be inoffensive.
- Phase 1 writes placeholder values into a live form in order to paginate. On a
  platform with server-side persistence, intermediate pages may be recorded.
- `make clean` removes caches only. It no longer deletes `output/`.
- There is no `cleaner.py`. Completeness is checked against the live schema at
  the end of phase 3; `status` reports on disk, read-only, and deletes nothing.
