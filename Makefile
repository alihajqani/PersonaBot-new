# PersonaBot — workflow automation
#
# Every survey target takes SURVEY=<id> (a folder under surveys/), or falls back
# to SURVEY in .env. PROVIDER defaults to the survey's own form.provider.
#
#   make surveys
#   make validate  SURVEY=teacher_innovation_km_ai_fa
#   make run-all   SURVEY=teacher_innovation_km_ai_fa NUM_PERSONAS=20

PYTHON        ?= .venv/bin/python
PERSONABOT    ?= $(PYTHON) -m personabot.cli
SURVEY        ?=
CONFIG        ?=
PROVIDER      ?=
PHASES        ?= 1,2,3,4
NUM_PERSONAS  ?= 5
RUN_COUNT     ?= 10
DELAY_SECONDS ?= 120
LIMIT         ?=
SEED          ?=
START_INDEX   ?=
LIVE          ?=
NAME          ?=
FROM          ?=
ARGS          ?=

SELECT_ARG := $(if $(CONFIG),--config $(CONFIG),$(if $(SURVEY),--survey $(SURVEY),))
LIMIT_ARG  := $(if $(LIMIT),--limit $(LIMIT),)
SEED_ARG   := $(if $(SEED),--seed $(SEED),)
START_ARG  := $(if $(START_INDEX),--start-index $(START_INDEX),)
LIVE_ARG   := $(if $(LIVE),--live,)
FROM_ARG   := $(if $(FROM),--from $(FROM),)

.PHONY: help venv install check-venv test clean \
        surveys new-survey providers check-keys \
        validate inspect status analyze recover transcripts \
        schema persona answer submit run-all loop

help:
	@echo "PersonaBot — one checkout, one folder per questionnaire"
	@echo ""
	@echo "  make install        Create .venv and install everything (incl. browsers)"
	@echo "  make test           Run the test suite"
	@echo "  make check-keys     Test every API key (LIVE=1 spends a few tokens)"
	@echo ""
	@echo "  make surveys        List surveys under surveys/"
	@echo "  make new-survey     NAME=<id> [FROM=<existing id>] — start a new form"
	@echo "  make providers      List registered form providers"
	@echo ""
	@echo "  make schema         Phase 1 — extract the form schema into surveys/<id>/"
	@echo "  make inspect        Summarize the schema, draft a scales: block"
	@echo "                      ARGS=\"--full --ids\" for an untruncated inventory"
	@echo "  make validate       Check the survey config and its schema coverage"
	@echo "  make persona        Phase 2 — generate personas"
	@echo "  make answer         Phase 3 — generate answers"
	@echo "  make submit         Phase 4 — submit forms"
	@echo "  make run-all        All four phases (PHASES=… to choose)"
	@echo "  make loop           Repeat a run RUN_COUNT times"
	@echo ""
	@echo "  make status         Report on output/<id>/ (read-only)"
	@echo "  make analyze        Psychometric report over the generated corpus"
	@echo "  make transcripts    Export voice-only answers to text files"
	@echo "  make recover        Return stale in-progress claims to the queue"
	@echo ""
	@echo "  Variables: SURVEY=$(SURVEY) PROVIDER=$(PROVIDER) PHASES=$(PHASES)"
	@echo "             NUM_PERSONAS=$(NUM_PERSONAS) LIMIT=<n> SEED=<n> START_INDEX=<n>"

venv:
	@test -d .venv || python3 -m venv .venv

# Always `$(PYTHON) -m pip`, never `.venv/bin/pip`: a venv copied from another
# checkout keeps that script's shebang, so `.venv/bin/pip` installs into the
# *other* project's environment while looking like it worked.
install: venv
	$(PYTHON) -m pip install --upgrade pip setuptools
	$(PYTHON) -m pip install -e ".[dev]"
	$(PYTHON) -m playwright install chromium

# Guards a failure the 2.x forks actually hit: a .venv copied from a sibling
# checkout still resolved `personabot` to that other tree, so every target ran
# foreign code against this directory's .env and output/ — silently.
check-venv:
	@$(PYTHON) -c 'import pathlib, sys; \
	import personabot; \
	got = pathlib.Path(personabot.__file__).resolve().parents[2]; \
	want = pathlib.Path("$(CURDIR)").resolve(); \
	sys.exit(0 if got == want else "error: personabot resolves to %s, not %s. This .venv points at another checkout - run: make install" % (got, want))'

test: check-venv
	$(PYTHON) -m pytest tests/ -q

# Removes caches only. Generated personas, answers and submission history are
# never touched.
clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache

# --- no survey needed ---

surveys: check-venv
	$(PERSONABOT) surveys

new-survey: check-venv
	@test -n "$(NAME)" || (echo "usage: make new-survey NAME=<id> [FROM=<existing id>]" && exit 2)
	$(PERSONABOT) new-survey $(NAME) $(FROM_ARG)

providers: check-venv
	$(PERSONABOT) providers

check-keys: check-venv
	$(PERSONABOT) check-keys $(LIVE_ARG)

# --- per survey ---

validate: check-venv
	$(PERSONABOT) $(SELECT_ARG) validate-config

inspect: check-venv
	$(PERSONABOT) $(SELECT_ARG) inspect-schema $(ARGS)

status: check-venv
	$(PERSONABOT) $(SELECT_ARG) status

analyze: check-venv
	$(PERSONABOT) $(SELECT_ARG) analyze

transcripts: check-venv
	$(PERSONABOT) $(SELECT_ARG) export-transcripts

recover: check-venv
	$(PERSONABOT) $(SELECT_ARG) recover

schema: check-venv
	$(PERSONABOT) $(SELECT_ARG) run $(PROVIDER) --phases 1

persona: check-venv
	$(PERSONABOT) $(SELECT_ARG) run --phases 2 --num-personas $(NUM_PERSONAS) $(SEED_ARG) $(START_ARG)

answer: check-venv
	$(PERSONABOT) $(SELECT_ARG) run --phases 3 $(LIMIT_ARG)

submit: check-venv
	$(PERSONABOT) $(SELECT_ARG) run $(PROVIDER) --phases 4 $(LIMIT_ARG)

run-all: check-venv
	$(PERSONABOT) $(SELECT_ARG) run $(PROVIDER) --phases $(PHASES) \
		--num-personas $(NUM_PERSONAS) $(LIMIT_ARG) $(SEED_ARG) $(START_ARG)

# Stops on the first failing run: phases return non-zero when they fail, so a
# broken run no longer hides behind ten retries. Respondent numbering continues
# on its own from one iteration to the next, and with SEED set each iteration is
# reproducible yet distinct (the seed is mixed with the start index).
loop: check-venv
	@for i in $$(seq 1 $(RUN_COUNT)); do \
		echo "=== run $$i/$(RUN_COUNT) ==="; \
		$(PERSONABOT) $(SELECT_ARG) run $(PROVIDER) --phases $(PHASES) \
			--num-personas $(NUM_PERSONAS) $(LIMIT_ARG) $(SEED_ARG) || exit $$?; \
		if [ $$i -lt $(RUN_COUNT) ]; then sleep $(DELAY_SECONDS); fi; \
	done
