# One command for judges and CI. BATCH is a JSONL/JSON/text list of organisation numbers.
BATCH ?= data-cache/dev-100.jsonl
COUNT ?= $(shell grep -c . $(BATCH))
BULK ?= data-cache/brreg-enheter.csv.gz
RUN_ID ?= local-$(shell date +%Y%m%d-%H%M%S)
NAV ?= data-cache/nav-feed
.PHONY: setup search-bin run test lint nav-index ted-snapshot
setup:
	uv sync --frozen
	mkdir -p data-cache
	test -f $(BULK) || curl -fL 'https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv' -o $(BULK)
	$(MAKE) search-bin
# The Rust search tool: built from source when cargo exists, else the release binary checked against SHA256SUMS.
# Without either, the run still completes and search-backed fields report "not checked".
search-bin:
	@mkdir -p bin
	@if command -v cargo >/dev/null 2>&1; then \
	  cargo build --release --locked --manifest-path search-rs/Cargo.toml && cp search-rs/target/release/signalpost-search bin/; \
	else \
	  scripts/fetch_search_bin.sh || echo "search binary unavailable: search fields will be reported as not checked"; \
	fi
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
ted-snapshot:
	uv run python scripts/ted_snapshot.py --universe data-cache/signalpost-universe.jsonl.gz --since 20240101 --output data/ted-no-notices.jsonl.gz
