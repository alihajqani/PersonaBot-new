# PersonaBot — CLAUDE.md

## Project Overview

PersonaBot is an AI-powered framework that automates form submission by generating realistic virtual personas and filling out online surveys. It uses Google Gemini to produce human-like answers, then drives a real browser via Playwright to submit them.

**4-Phase Pipeline:**
1. **Schema Extraction** — navigate the live form and save question structure to `output/form_schema.json`
2. **Persona Generation** — call Gemini to create N realistic user profiles → `output/personas/`
3. **Answer Generation** — for each persona, call Gemini to answer every question → `output/answers/`
4. **Form Submission** — open a real browser, fill and submit the form per answer file

## Project Structure

```
PersonaBot/
├── main.py                         # CLI entry point, ProviderManager, phase dispatcher
├── config.py                       # Env-var loading, logging setup, global singletons
├── utils.py                        # File I/O, directory setup, Tor IP renewal
├── cleaner.py                      # Delete incomplete JSON files (threshold-based)
├── requirements.txt
├── Makefile
├── .env                            # NOT in repo — see Configuration section
│
├── core/
│   ├── services.py                 # APIKeyManager: thread-safe round-robin key rotation
│   ├── persona_generator.py        # Phase 2 logic
│   └── answer_generator.py        # Phase 3 logic + fuzzy-match validation
│
├── providers/                      # One sub-package per form platform
│   ├── google_forms/
│   │   ├── __init__.py
│   │   ├── schema_extractor.py
│   │   └── form_submitter.py
│   ├── porsline/
│   │   ├── schema_extractor.py
│   │   └── form_submitter.py
│   └── avalform/
│       ├── schema_extractor.py
│       └── form_submitter.py
│
└── prompts/
    ├── persona_generation_prompt.json
    └── answer_generation_prompt.json
```

## How to Run

```bash
# Install dependencies + Playwright browsers
make install

# Run all 4 phases with the default provider (porsline)
make run-all

# Run a specific provider
make run-all PROVIDER=google_forms NUM_PERSONAS=10

# Run individual phases
make schema   PROVIDER=avalform
make persona  NUM_PERSONAS=20
make answer
make submit   PROVIDER=porsline

# Loop (e.g. run answer+submit 10 times with 2-min delay)
make loop PHASES=3,4 RUN_COUNT=10 DELAY_SECONDS=120

# Check output status
make status
```

Direct invocation:

```bash
python main.py <provider> --phases 1,2,3,4 --num-personas 5
```

## Configuration (.env)

```ini
# Required — at least one key; add _2, _3 … for round-robin rotation
GOOGLE_API_KEY_1="..."
GOOGLE_API_KEY_2="..."          # optional

GEMINI_MODEL_NAME="gemini-2.5-pro"

BASE_FORM_URL="https://..."     # URL of the live form

# Browser
HEADLESS_MODE="True"
SLOW_MO="50"

# Tor (optional)
USE_TOR="False"
TOR_SOCKS_HOST="127.0.0.1"
TOR_SOCKS_PORT="9050"
TOR_CONTROL_PORT="9051"
TOR_CONTROL_PASSWORD=""

OUTPUT_DIR="output"
```

## Adding a New Provider

Each provider is a Python package under `providers/<name>/` with exactly two files:

### `providers/<name>/schema_extractor.py`

Must expose:

```python
async def run() -> None: ...
```

Responsibilities:
- Launch a Playwright browser using `config.HEADLESS_MODE` and `config.SLOW_MO`
- Navigate to `config.BASE_FORM_URL`
- Walk through every page of the form (click Next/Submit as needed)
- Parse question structure into a list of dicts:

```python
{
    "question_id": str,   # unique field identifier
    "question_text": str,
    "type": str,          # "TEXT_INPUT" | "RADIO" | "SELECT" | "MATRIX_RADIO" | ...
    "options": [          # empty list for text inputs
        {"text": str, "value": str},
        ...
    ]
}
```

- Save the result with `utils.save_json_file(config.SCHEMA_FILE_PATH, data, "schema")`

### `providers/<name>/form_submitter.py`

Must expose:

```python
async def run() -> None: ...
```

Responsibilities:
- Read all `.json` files from `config.ANSWERS_DIR_PATH`
- For each file, open a fresh browser, navigate to the form, fill and submit
- On success: move the file to `os.path.join(config.ANSWERS_DIR_PATH, "done", filename)`
- On failure: log the error (optionally save a screenshot to `config.RECEIPTS_DIR_PATH`)
- Respect `config.USE_TOR` and call `utils.renew_tor_ip()` between submissions if enabled

### Minimal template

```python
# providers/myplatform/schema_extractor.py
from playwright.async_api import async_playwright
import config, utils

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=config.HEADLESS_MODE, slow_mo=config.SLOW_MO)
        page = await browser.new_page()
        await page.goto(config.BASE_FORM_URL, wait_until="networkidle")
        # ... parse questions ...
        await browser.close()
    utils.save_json_file(config.SCHEMA_FILE_PATH, schema_data, "schema")
```

```python
# providers/myplatform/form_submitter.py
import os, shutil, asyncio
from playwright.async_api import async_playwright
import config, utils

async def run():
    files = [f for f in os.listdir(config.ANSWERS_DIR_PATH) if f.endswith('.json')]
    done_dir = os.path.join(config.ANSWERS_DIR_PATH, "done")
    os.makedirs(done_dir, exist_ok=True)
    async with async_playwright() as p:
        for fname in files:
            answers = utils.load_json_file(os.path.join(config.ANSWERS_DIR_PATH, fname), fname)
            success = await submit_single_form(p, answers)
            if success:
                shutil.move(os.path.join(config.ANSWERS_DIR_PATH, fname),
                            os.path.join(done_dir, fname))
```

After creating the two files, run:

```bash
python main.py myplatform --phases 1
```

`ProviderManager` in `main.py` dynamically imports `providers.myplatform.schema_extractor` and `providers.myplatform.form_submitter` — no registration step needed.

## Key Internals

### `config.py`
Imported at startup; sets up colored logging, loads `.env`, and initialises `google_api_key_manager` (an `APIKeyManager` instance). All provider files import `config` to read settings.

### `core/services.py` — `APIKeyManager`
Thread-safe round-robin over `GOOGLE_API_KEY_1`, `GOOGLE_API_KEY_2`, … Use `config.google_api_key_manager.get_next_key()` when making Gemini calls.

### `core/answer_generator.py`
After Gemini returns answers, every choice-type answer is fuzzy-matched against the schema options using `thefuzz` (≥ 85% confidence). A persona whose answer file covers < 80% of questions is discarded.

### Output layout

```
output/
├── form_schema.json
├── personas/
│   ├── <uuid>.json       # pending
│   └── done/             # processed by Phase 3
├── answers/
│   ├── <uuid>.json       # ready to submit
│   └── done/             # submitted by Phase 4
└── receipts/             # screenshots / HTML debug dumps
```

## Dependencies

| Package | Purpose |
|---|---|
| `playwright` | Browser automation |
| `google-generativeai` | Gemini API |
| `beautifulsoup4` | HTML parsing in schema extractors |
| `thefuzz` + `python-Levenshtein` | Fuzzy answer validation |
| `python-dotenv` | `.env` loading |
| `colorlog` | Coloured terminal logging |
| `stem` + `PySocks` | Tor IP rotation (optional) |

Install everything:

```bash
make install
# or
pip install -r requirements.txt && playwright install
```

## Conventions & Gotchas

- All provider functions are `async`. Playwright operations must stay inside the same `async_playwright()` context manager.
- Use `force=True` on `.click()` for elements that may be overlaid. For React submit buttons, prefer `element.evaluate("el => el.click()")`.
- `config.SLOW_MO` adds a fixed delay (ms) between every Playwright action — keep it low (50) in headless CI, raise it (200+) when debugging visually.
- Schema types must match what `answer_generator.py` expects: `"RADIO"` / `"SELECT"` questions need a non-empty `options` list; `"TEXT_INPUT"` should have `"options": []`.
- Gemini safety filters are set to `BLOCK_NONE` in the core generators — this is intentional to allow diverse persona simulation.
- `cleaner.py` can be run manually to prune incomplete JSON files from `output/` before a submission run.
