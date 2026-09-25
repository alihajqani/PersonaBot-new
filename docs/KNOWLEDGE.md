# What the forms taught us

Every entry here was learned on a live questionnaire by one of the fifteen 2.x
copies of this project, read back from their code when they were merged into
3.0. Each is now handled for every survey. Keep this file current: when a new
form teaches something, add it here with the symptom you saw first.

Format: **symptom** → cause → what the code does now (where).

## Porsline (`providers/porsline`)

- **Extraction finds nothing** → no question exists until the welcome page's
  «شروع» is clicked → `prepare` clicks it and waits for a question.
- **Every question twice / half the clicks ignored** → clicks during the SPA
  transition are dropped → navigation waits for the visible question id to
  change and retries (`_navigate`, `_wait_for_change`).
- **The next button is never found** → it is an icon with
  `aria-label="بعدی"` and no text → located by aria-label.
- **A question skipped after answering** → answering auto-advances; clicking
  «بعدی» then skips the next one → `advance` checks whether the page already
  moved.
- **All questions filled, nothing submitted** → the form ends on a statement
  page whose «پایان» precedes the real send button → one JS definition of the
  end button (send class first, text second), click the element the probe
  found, confirm the page moved (`_HELPERS.endButton`, `_finish`).
- **"No success confirmation" although it worked** → the ending screen is free
  text per survey → success = a same-origin 2xx POST/PUT/PATCH to
  `/responses|submit|answers`, then text selectors; when unconfirmed, the log
  says whether the form left its last page (possible double-submit warning).
- **A required dropdown bounces back at submit** → it is a Downshift combobox;
  typing only filters → open the menu, strip «۸.» numbering, click the option
  by index; extraction records `SELECT` options.
- **Group header copied as a question** → header and first question share a
  wrapper → controls belong to the label that precedes them.
- **30 items each with 150 options (K=150 in inspect)** → a «single page
  display» group puts every item in one wrapper → per-item scope = narrowest
  ancestor with exactly one label (`scopeFor`, shared by probe and click).
- **Extraction stopped at page 39 of 70; submissions "succeeded" unsent** → the
  group's confirm button reads «شروع» and carries `shared_submit__` → the end
  button excludes «شروع» and anything inside a group wrapper.
- **Five questions with the same 11 options** → Porsline's grouped renderer →
  extraction refuses when every question reports all of the page's radios; use
  `porsline_v2`.
- **Both phases stop a fifth of the way** → one question per page → set
  `form.max_pages` above the question count.

## Porsline grouped renderer (`providers/porsline_v2`)

- Matrices repeat the matrix id on every cell → rows flattened to
  `<matrix id>#<row>`; cells addressed by position via `data-pb-*` tags.
- The group's «ادامه» carries `shared_submit__` → only `shared_send_button__`
  ends the form; «ادامه» validates and relabels itself, «بعدی» turns the page.
- Matrix radios have no hit box → `dispatch_event("click")`, then verify the
  rows registered.
- Voice recorders cannot be typed into → `VOICE` type, answered as prose,
  `export-transcripts`, file named by another submitted answer (serial phone
  derived from `index`).
- The definition endpoint is POST-only and opens a responder session → extract
  once per form.

## Porsa (`providers/porsa`)

- Nothing clickable after load → `body{visibility:hidden}` until
  `pageloaded` → wait for the reveal.
- Label clicks fail → inputs are `opacity:0`, labels zero-height → click
  `span.radioCheckmark`.
- Stuck on a page, no error → client-side validation silently refuses →
  verify `#pageNumber` moved; name the flagged questions; use
  `min_ratio: 1.0`.
- "Missing #pageForm" → either "you already answered" (exit IP spent →
  `AlreadyRespondedError`, retry on another circuit) or the closed-survey
  template (`SurveyClosedError`) — diagnosed from the page text within a
  second.
- Matrix boxes → one question per row, addressed by the row's input `name`.

## Google Forms (`providers/google_forms`)

- Clicks time out → controls are `aria-disabled` until hydration → wait for it.
- Options with ZWNJ never match → `repr()`-built selectors escape it → CSS
  string escaping only.
- Page 1 refilled 121 times → a rejected Next looks like no navigation → poll
  for "advanced" vs "rejected".
- Controls invisible → a stylesheet failed on a slow link → scripted clicks,
  then verify `aria-checked`.

## Browser and Tor (`infrastructure/browser`, `providers/base`)

- Every context failed with Tor on → Chromium refuses SOCKS5 authentication →
  isolate circuits per SocksPort (`TOR_SOCKS_EXTRA_PORTS`), not per credential.
- One bad circuit lost the work item → `CircuitError` (only before anything is
  POSTed) + `on_circuit` retry on another route; failed ports cool down.
- Dead circuits discovered late → preflight IP echo (20 s) before the form.
- Slow direct link → block images/fonts/media; one retry of a hung navigation
  even without Tor.
- A failure left no evidence → every submission failure writes a receipt.

## LLM (`infrastructure/llm`)

- "All keys exhausted" for non-quota errors → substring "rate" in
  "generate" → classify by exception type and HTTP status first.
- Free tier quota storms → the binding limit is input tokens/min/key → token
  ledger per key (`GEMINI_INPUT_TOKENS_PER_MINUTE`), 85 % safety margin.
- Keys 11–17 never used → ties broken by key index → least recently released.
- Per-day quota retried every minute → read `QuotaFailure` scope and
  `RetryInfo.retryDelay`; day scope parks the key 15 min.
- A 500 retried onto the same key → charge its tokens and park it for its
  backoff.
- Geo-blocked ("User location is not supported") → `GEMINI_PROXY`, scoped to
  the Gemini client (httpx transport pinned; SOCKS needs `socksio`).
- Which key is bad? → `check-keys` (free `count_tokens`, or `--live`).

## Data and method (`domain`, `application`)

- Correct answers discarded → Arabic vs Persian yeh/kaf → normalise both sides
  before matching; store the schema's canonical string.
- Implausibly high alpha → one request for the whole questionnaire → one
  request per construct, items shuffled.
- Careless respondents failed completeness → static answers were skipped →
  careless straight-lining applies static answers too; its RNG is now stable
  across processes.
- Second batch repeated the first → same seed, new numbers → seed mixed with
  the start index; numbering continues automatically.
- Vignette answers sent under the wrong vignette → the form draws the
  scenario → phase 4 opens the form, recognises the scenario (number first,
  fuzzy text second), then claims a matching answer set.

## Operations

- Commands ran another checkout's code → a copied `.venv` → `make check-venv`
  (and, since 3.0, no reason to copy the repository at all).
- One survey's answers risked going to another's form → a shared
  `BASE_FORM_URL` → a survey's `form.url` always wins; mismatch is warned.
