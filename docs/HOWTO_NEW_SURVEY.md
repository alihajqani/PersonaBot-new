# How to run PersonaBot on a new form

A new questionnaire is a new folder, `surveys/<id>/`. You never copy the
repository and never edit Python. The steps below take a form from its URL to
validated synthetic answers; every command takes `SURVEY=<id>` (or set
`SURVEY=<id>` in `.env`).

## 0. Once per machine

```bash
make install
cp .env.example .env        # GEMINI_API_KEY_1, …; nothing survey-specific
make check-keys LIVE=1
```

## 1. Create the survey folder

```bash
make new-survey NAME=job_stress_fa
# or start from a survey with a similar population or prompts:
make new-survey NAME=job_stress_fa FROM=teacher_innovation_km_ai_fa
```

Cloning copies the YAML and prompt overrides but **not** the schema, and blanks
`form.url` — the schema describes the other form, and its URL would submit into
it.

In `surveys/job_stress_fa/survey.yaml` set:

```yaml
form:
  provider: porsline          # see "Choosing a provider" below
  url: https://survey.porsline.ir/s/XXXXXXXX
  max_pages: 400              # Porsline: above the QUESTION count
```

## 2. Extract the schema

```bash
make schema SURVEY=job_stress_fa
```

This writes `surveys/job_stress_fa/form_schema.json`. Re-running it later is
safe: if the form has changed, the old schema is kept as
`form_schema.previous.json` and the added / removed / changed question ids are
logged. That is your cue to re-check the matchers.

## 3. Read the inventory

```bash
make inspect SURVEY=job_stress_fa ARGS="--full --ids"
```

It groups questions by option signature (largest first — those are your
scales), lists free-text items (usually demographics → `static_answers`), and
prints a ready-to-paste `scales:` block plus per-signature id lists for
`question_id_in`.

## 4. Write the instrument

Work top to bottom through `survey.yaml` — each section is commented in the
template.

* **population** — latent factor model (squared loadings < 1 per dimension),
  bands, response style, demographics, derived fields. Declaration order is
  evaluation order; `z.<dimension>` and `index` (respondent number) are
  available to expressions.
* **scales** — one per response format. Check `coding`: `low_first` when
  option [0] is the low end, `high_first` when it is the high end. Getting it
  wrong deflates that construct's alpha.
* **constructs** — ordered, first match wins; list narrow rules first.
  `reverse_when` marks reverse-keyed items. There is no catch-all: an
  unmatched question is an error, on purpose.
* **static_answers** — questions answered from the persona record (age,
  gender, city, serial phone numbers), never by the LLM.
* **skip** — questions to ignore entirely.
* **scenarios** — between-subjects vignettes the form draws at random
  (`porsline` only). Personas are spread evenly over them.
* **transcripts** — audio-only questions, answered in prose and exported per
  respondent (`make transcripts`).
* **answering.completeness.min_ratio** — `1.0` whenever the form marks every
  question required (always on `porsa`), otherwise a set that passes here
  stalls on a blank field in phase 4.

## 5. Prompts — three levels, stop at the first that is enough

1. **Variables only (most forms).** Fill the `prompt:` block:

   ```yaml
   prompt:
     role: "یک معلم شاغل در آموزش و پرورش ایران"
     study_context: >-
       پاسخ‌دهندگان معلمان شاغل‌اند؛ پرسش‌نامه سه بخش دارد …
     persona_sections:
       background_and_teaching: "زمینه‌ی شغلی، مقطع و درس …"
       school_and_colleagues: "فضای مدرسه از نگاه او …"
     persona_rules: ["برای کسی که فرزند ندارد از بچه‌ها ننویس."]
     answer_rules: ["گویه‌های مربوط به مدرسه را ارزیابی خودت از مدرسه بدان."]
     scenario_preamble: "فرض کنید در یک سازمان مشغول به کار هستید …"
   ```

   The Persian base pack (`prompts/base/fa/`) already carries the ten answering
   rules the 2.x surveys converged on (read options by meaning not position,
   mind reverse tags, mind who an item is about, stay consistent with your own
   facts, vary realistically, copy options verbatim, …). `answer_rules` are
   appended as rules 11+.

2. **Override one block.** Create `surveys/<id>/prompts/answers.system.j2`:

   ```jinja
   {% extends "base/answers.system.j2" %}
   {% block levels %}…your own wording…{% endblock %}
   ```

   Blocks: answers — `role`, `who_you_are`, `life`, `levels`, `style`,
   `scenario`, `rules`; persona — `intro`, `context`, `fidelity`, `language`,
   `output`.

3. **Replace a whole template.** Put a complete `persona.user.j2` (etc.) in
   `surveys/<id>/prompts/`. `sleep_fomo_digital_burnout_fa` does this to print
   sleep times in the persona prompt; `tehran_urban_network_fa` to word matrix
   rows as actors.

`make validate` prints which directory each template resolved from.

Variables available to templates: `survey` (id, locale, title, prompt) in both
phases; persona phase — `dimensions`, `bands`, `profiles`; answer phase —
`construct`, `persona`, `trait_bands`, `response_style_text`, `reverse_tags`,
`questions`, `scenario`.

## 6. Validate

```bash
make validate SURVEY=job_stress_fa
```

It must end with "Every question is accounted for." Read the implied
correlation matrix it prints — it is the cheapest sanity check on the
population model.

## 7. Smoke test, then analyse before submitting

```bash
make run-all SURVEY=job_stress_fa PHASES=2,3 NUM_PERSONAS=3
make status  SURVEY=job_stress_fa
make analyze SURVEY=job_stress_fa
```

Alpha above 0.95 or mean inter-item r above 0.6 is the signature of LLM
self-consistency — revisit the population loadings and response styles before
generating at scale.

## 8. Submit

```bash
make submit SURVEY=job_stress_fa LIMIT=1     # one first, watch it
make submit SURVEY=job_stress_fa
```

Failures leave a screenshot + HTML pair in `output/<id>/receipts/`, and the
answer file goes back to the queue. `make recover` releases claims left by a
killed process.

## Choosing a provider

| The form… | Provider |
|---|---|
| is on survey.porsline.ir and shows one question per page | `porsline` |
| is on Porsline with matrices, grouped pages or voice questions — or `porsline` refuses the schema ("each report all N of the page's radios") | `porsline_v2` |
| is on porsa.irandoc.ac.ir | `porsa` (and `min_ratio: 1.0`) |
| is a Google Form | `google_forms` |

## Running more batches later

Phase 2 continues respondent numbering from what is already in
`output/<id>/personas/`, so serial identifiers derived from `index` never
repeat. With `SEED` set, each batch is reproducible and distinct.
