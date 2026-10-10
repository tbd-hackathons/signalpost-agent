# One command for judges and CI. BATCH is a JSONL/JSON/text list of organisation numbers.
BATCH ?= data-cache/dev-100.jsonl
COUNT ?= $(shell grep -c . $(BATCH))
BULK ?= data-cache/brreg-enheter.csv.gz
RUN_ID ?= local-$(shell date +%Y%m%d-%H%M%S)
NAV ?= data-cache/nav-feed
.PHONY: setup run test lint nav-index
setup:
	uv sync --frozen
	mkdir -p data-cache
	test -f $(BULK) || curl -fL 'https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv' -o $(BULK)
run: setup
	uv run python scripts/run_signalpost.py --organisations $(BATCH) --bulk $(BULK) \
	  --profiles-output out/$(RUN_ID)-profiles.jsonl --output out/$(RUN_ID)-envelopes.jsonl \
	  --report out/$(RUN_ID)-report.json --run-id $(RUN_ID) --expected-count $(COUNT)
nav-index:
	uv run python scripts/nav_active_index.py --cache $(NAV) --workers 2
test:
	uv run --with pytest pytest -q
lint:
	uv run python -m compileall -q src scripts
