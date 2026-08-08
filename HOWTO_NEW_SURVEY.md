# How to run PersonaBot on a new form

Ten steps. Only three of them involve real thinking; the rest are commands.

You will edit exactly three things: `.env`, one YAML file, and one prompt preset.
**No Python.**

---

## Step 0 — Install (once)

```bash
make install          # creates .venv, installs deps, downloads Chromium
make test             # 171 tests should pass
```

---

## Step 1 — Point `.env` at the new form

```bash
cp .env.example .env   # first time only
```

Edit `.env`:

```ini
GEMINI_API_KEY_1="AIza..."          # add _2, _3 … for more throughput
GEMINI_MODEL_NAME="gemini-2.5-pro"

BASE_FORM_URL="https://survey.porsline.ir/s/XXXXXXX"   # ← your new form

# Leave this pointing at the template for now. You will change it in step 4.
SURVEY_CONFIG="configs/surveys/_template.yaml"

HEADLESS_MODE="False"    # watch the browser the first time; set True later
CONCURRENT_LLM_TASKS=4
CONCURRENT_SUBMISSIONS=2
```

**Which provider?** Look at the URL: `porsline.ir` → `porsline`, `docs.google.com/forms`
→ `google_forms`, `avalform.com` → `avalform`, `digisurvey` → `digisurvey`.
Run `make providers` to list them.

---

## Step 2 — Extract the schema

```bash
make schema PROVIDER=porsline
```

Writes `output/form_schema.json`.

> **Heads up:** to page through the form, this types placeholder values into it.
> If the platform saves partial responses, junk rows may appear. Use a test copy
> of the form if you have one.

Sanity check — the number should match the real form:

```bash
python -c "import json;print(len(json.load(open('output/form_schema.json'))),'questions')"
```

If a question is missing or its options are empty, the extractor needs a
selector fix for that platform. That *is* a code change — the only kind this
workflow ever needs.

---

## Step 3 — Look at what you extracted

```bash
make inspect
```

This groups every question by its **option signature** and prints a
ready-to-paste `scales:` block. Example output:

```
SCHEMA INVENTORY — 51 questions
  types: RADIO=42, TEXT_INPUT=9
  distinct option signatures: 5

SIGNATURE 1 — K=5, 27 question(s)
  options (in order):
    [0] هرگز
    [1] به ندرت
    ...
  questions:
    39021295  رابطه من و شوهرم صمیمانه است.
    ...

DRAFT `scales:` BLOCK — paste into your survey YAML
scales:
  - name: scale_1_k5
    match:
      options_equal:
        - "هرگز"
        ...
    coding: low_first
```

Add `--ids` to also get per-signature id lists:

```bash
make inspect ARGS=--ids
```

**Read the big signatures.** Those are your psychometric scales. Single-question
signatures are almost always demographics.

---

## Step 4 — Create your config and prompt preset

```bash
cp configs/surveys/_template.yaml configs/surveys/my_survey.yaml
cp -r prompts/_template prompts/my_survey
```

Point `.env` at it:

```ini
SURVEY_CONFIG="configs/surveys/my_survey.yaml"
```

And set the preset name inside `configs/surveys/my_survey.yaml`:

```yaml
id: my_survey
locale: fa                 # or en, etc.
prompt_preset: my_survey   # must match the prompts/ directory name
```

---

## Step 5 — Write the config (the one step that needs judgement)

You are filling in five blocks in `configs/surveys/my_survey.yaml`:

| Block | What it says | Where it comes from |
|---|---|---|
| `population` | who your respondents are, and their latent traits | your study design |
| `scales` | response formats + coding direction | **paste from step 3** |
| `constructs` | which items measure what, and which are reversed | reading the items |
| `static_answers` | questions answered from the persona, not the model | the free-text list in step 3 |
| `answering` | thresholds and retry policy | usually leave as-is |

### Use an LLM for the first draft

Paste this prompt into Claude along with your `make inspect` output and
`configs/surveys/_template.yaml`:

> I'm configuring a survey simulation tool. It is driven entirely by one YAML
> file — I need you to write that file for a new questionnaire.
>
> **Attached:**
> 1. `_template.yaml` — the schema of the config format, fully commented. Follow
>    its structure and field names exactly.
> 2. A schema inventory listing every question, grouped by option signature.
>
> **The survey measures:** `<describe your instrument — e.g. "job burnout and
> perceived organizational support in Iranian nurses; the MBI and SPOS scales">`
>
> **My target population:** `<e.g. "hospital nurses in Iran, aged 24–55, at least
> one year of clinical experience, roughly 70% female">`
>
> **Please produce:**
>
> 1. **`population`**
>    - A factor model with one latent dimension per construct you identify.
>      Give each dimension loadings on 2–3 named common factors. **Squared
>      loadings must sum to < 1 per dimension.** State the implied correlation
>      between each pair and justify the sign and rough magnitude from the
>      literature.
>    - Demographic variables matching the frame above, using the distribution
>      kinds the template documents. Declare them in dependency order — a
>      variable may only reference ones declared before it.
>    - `derived_fields` for any factual value the form asks for that the model
>      must not invent (a spouse's age, a duration, a weight).
>
> 2. **`scales`** — start from the draft block in the inventory. Rename each to
>    something meaningful. **Check `coding` on every one:** `low_first` if
>    option [0] is the low end, `high_first` if option [0] is the high end. Say
>    which ones you changed and why.
>
> 3. **`constructs`** — assign every choice question to exactly one construct,
>    using `question_id_in` lists. For each construct give `key`, `label` (in the
>    survey's language), and `traits`. Add `reverse_when` with the ids of items
>    whose high end means a *lower* level of the trait — negatively worded items,
>    complaints, symptoms.
>
> 4. **`static_answers`** — bind every free-text question to a persona field with
>    a Jinja expression like `"{{ persona.demographics.age }}"`. For instruction
>    text mis-detected as an input, use a literal value and set
>    `exclude_from_psychometrics: true`.
>
> **Hard constraints:**
> - Every question id in the inventory must appear exactly once, in either
>   `constructs` or `static_answers`. There is no catch-all — an unmatched
>   question is a validation error.
> - Do not invent question ids. Use only ids from the inventory.
> - Flag any item you are unsure about with a `# REVIEW:` comment rather than
>   guessing silently.
> - Warn me about any construct with fewer than 3 items — alpha is unstable there.
>
> Output the complete YAML file, with comments explaining each judgement call.

Then **read what it produced**. The construct assignments are a claim about your
instrument; the tool cannot check whether they are right, only whether they are
complete.

---

## Step 6 — Validate until it passes

```bash
make validate
```

This is the most important command in the workflow. It checks the YAML *and*
whether it accounts for every question:

```
✓ configs/surveys/my_survey.yaml is valid
  latent model: 3 dimension(s) on 2 factor(s)
                        burnout   support  ...
    burnout                1.00     -0.48
    ...

Schema coverage: 44 questions (6 static, 0 skipped, 38 psychometric)
  constructs:
    emotional_exhaustion      9
    ...
✓ Every question is accounted for.
```

**Read the correlation matrix.** It is the cheapest sanity check on your
psychometrics — if two constructs you expect to oppose each other show `+0.6`,
your loadings are wrong.

Common failures and what they mean:

| Message | Fix |
|---|---|
| `UNASSIGNED (n)` | those ids match no construct — add them |
| `CONSTRUCTS MATCHING NOTHING` | a stale or mistyped rule — almost always a wrong id list |
| `SCALES MATCHING NOTHING` | option strings don't match; re-copy from `make inspect` |
| `squared loadings sum to 1.25` | scale the loadings down below 1 |
| `references unknown trait` | a construct names a dimension you didn't declare |
| `not defined before it` | move that variable later in `demographics` |

Do not continue until this prints `✓ Every question is accounted for.`

---

## Step 7 — Adapt the prompt preset

Edit the four files in `prompts/my_survey/`:

- `persona.system.j2` — the study framing and the tone of the prose you want
- `persona.user.j2` — usually fine as-is
- `answers.system.j2` — how the respondent should think about answering
- `answers.user.j2` — usually fine as-is

Write them in the **survey's language**. Question text, ids and options are
injected at runtime from the schema — never hard-code an id in a prompt.

---

## Step 8 — Small test run

```bash
make persona NUM_PERSONAS=2
make answer
```

Then look at an actual file:

```bash
cat output/answers/*.json | head -40
make status
```

Check three things:

1. `status` shows 2 valid, 0 partial, 0 corrupt.
2. Static answers match their persona — open a file in `output/personas/done/`
   and confirm the age in the answer set equals the age in the persona.
3. Answers are not all the same option.

If personas fail the completeness gate, the log names the missing ids. Usually
either the model paraphrased an option (lower `fuzzy.min_score` slightly) or a
construct is failing to parse (run with `--log-level DEBUG`).

---

## Step 9 — Check the psychometrics before scaling up

```bash
make persona NUM_PERSONAS=40
make answer
make analyze
```

```
■ Emotional exhaustion (emotional_exhaustion)
    items 9 | complete respondents 40
    Cronbach alpha        : 0.831
    mean inter-item r     : 0.362
```

What to look for:

- **alpha above 0.95** → warned automatically. The model is being too
  self-consistent. Confirm `chunk_by: construct` and `shuffle_within_chunk: true`.
- **negative alpha** → your `coding` direction or `reverse_when` list is wrong.
- **`item-total r sign disagrees`** → those items correlate opposite to how you
  keyed them. Re-read them; either your reverse list is wrong or the item is bad.
- **straightlining rate** should land near your configured `careless` probability.

This is the step that catches a mis-specified instrument. Do it before you
generate hundreds of responses.

---

## Step 10 — Submit

Start with exactly one, watching the browser (`HEADLESS_MODE="False"`):

```bash
make submit PROVIDER=porsline LIMIT=1
```

Verify it landed in the platform's own dashboard. On failure, a screenshot and
HTML dump appear in `output/receipts/`.

Then scale up:

```bash
make run-all PROVIDER=porsline NUM_PERSONAS=50
make loop RUN_COUNT=10 DELAY_SECONDS=120 NUM_PERSONAS=20
```

`make loop` stops on the first failing run rather than repeating a broken one.

---

## Quick reference

```bash
make providers                       # which platforms are supported
make schema PROVIDER=x               # phase 1
make inspect                         # summarize the schema, draft `scales:`
make inspect ARGS=--ids              # + id lists per signature
make validate                        # check config AND coverage
make persona NUM_PERSONAS=n          # phase 2
make answer                          # phase 3
make submit PROVIDER=x LIMIT=n       # phase 4
make status                          # what's on disk, validated
make analyze                         # psychometric report
make recover                         # release stale in-progress claims
```

Override the config per command without editing `.env`:

```bash
make validate CONFIG=configs/surveys/other.yaml
```

---

## Gotchas

**If every question on your form is required, set `min_ratio: 1.0`.** At the
default `0.95` an answer set with two blanks is accepted in phase 3 and then
stalls in phase 4 on an empty required field.

**Check `coding` on every scale.** Getting the direction wrong doesn't error —
it quietly deflates that construct's alpha. `make analyze` is where you'd notice.

**Constructs with fewer than 3 items** get a correlation, not a reliability
coefficient. `make analyze` will say so rather than print a misleading number.

**Re-extracting the schema can invalidate your config.** If the form is
republished, ids may change. Re-run `make validate` after every `make schema` —
it will tell you immediately, which is exactly what the old version could not do.

**`make clean` no longer deletes `output/`.** It only clears caches.
