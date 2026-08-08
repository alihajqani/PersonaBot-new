# PersonaBot — workflow automation
# Targets keep the names they had before the refactor so existing habits work.

PYTHON        ?= .venv/bin/python
PERSONABOT    ?= $(PYTHON) -m personabot.cli
PROVIDER      ?= porsline
PHASES        ?= 1,2,3,4
NUM_PERSONAS  ?= 5
RUN_COUNT     ?= 10
DELAY_SECONDS ?= 120
CONFIG        ?=
LIMIT         ?=

CONFIG_ARG := $(if $(CONFIG),--config $(CONFIG),)
LIMIT_ARG  := $(if $(LIMIT),--limit $(LIMIT),)

ARGS ?=

.PHONY: help venv install test lint clean status validate analyze recover \
        run-all schema inspect persona answer submit providers loop

help:
	@echo "PersonaBot"
	@echo "  make install        Create .venv and install everything (incl. browsers)"
	@echo "  make test           Run the test suite"
	@echo ""
	@echo "  make validate       Check the survey config and its schema coverage"
	@echo "  make status         Report on output/ (validates JSON, read-only)"
	@echo "  make analyze        Psychometric report over the generated corpus"
	@echo "  make recover        Return stale in-progress claims to the queue"
	@echo "  make providers      List registered form providers"
	@echo ""
	@echo "  make schema         Phase 1 — extract the form schema"
	@echo "  make inspect        Summarize the schema, draft a scales: block"
	@echo "  make persona        Phase 2 — generate personas"
	@echo "  make answer         Phase 3 — generate answers"
	@echo "  make submit         Phase 4 — submit forms"
	@echo "  make run-all        All four phases"
	@echo "  make loop           Repeat a run RUN_COUNT times"
	@echo ""
	@echo "  Variables: PROVIDER=$(PROVIDER) PHASES=$(PHASES) NUM_PERSONAS=$(NUM_PERSONAS)"
	@echo "             CONFIG=<path/to/survey.yaml> LIMIT=<n>"
	@echo "  Example:   make run-all PROVIDER=google_forms NUM_PERSONAS=20"

venv:
	@test -d .venv || python3 -m venv .venv

install: venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -e ".[dev]"
	.venv/bin/playwright install chromium

test:
	.venv/bin/python -m pytest tests/ -q

inspect:
	$(PERSONABOT) $(CONFIG_ARG) inspect-schema $(ARGS)

validate:
	$(PERSONABOT) $(CONFIG_ARG) validate-config

status:
	$(PERSONABOT) $(CONFIG_ARG) status

analyze:
	$(PERSONABOT) $(CONFIG_ARG) analyze

recover:
	$(PERSONABOT) $(CONFIG_ARG) recover

providers:
	$(PERSONABOT) providers

schema:
	$(PERSONABOT) $(CONFIG_ARG) run $(PROVIDER) --phases 1

persona:
	$(PERSONABOT) $(CONFIG_ARG) run --phases 2 --num-personas $(NUM_PERSONAS)

answer:
	$(PERSONABOT) $(CONFIG_ARG) run --phases 3 $(LIMIT_ARG)

submit:
	$(PERSONABOT) $(CONFIG_ARG) run $(PROVIDER) --phases 4 $(LIMIT_ARG)

run-all:
	$(PERSONABOT) $(CONFIG_ARG) run $(PROVIDER) --phases $(PHASES) \
		--num-personas $(NUM_PERSONAS) $(LIMIT_ARG)

# Unlike the old loop, this stops on the first failing run: phases now return
# non-zero when they fail, so a broken run no longer hides behind ten retries.
loop:
	@for i in $$(seq 1 $(RUN_COUNT)); do \
		echo "=== run $$i/$(RUN_COUNT) ==="; \
		$(PERSONABOT) $(CONFIG_ARG) run $(PROVIDER) --phases $(PHASES) \
			--num-personas $(NUM_PERSONAS) $(LIMIT_ARG) || exit $$?; \
		if [ $$i -lt $(RUN_COUNT) ]; then sleep $(DELAY_SECONDS); fi; \
	done

# Removes caches only. Generated personas, answers and submission history are
# NOT touched — `make clean` used to be `rm -rf output/` with no confirmation.
clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache build dist *.egg-info
	@echo "Caches removed. output/ left intact — delete it by hand if you mean to."
