# Signalpost agent

> Give it a Norwegian organisation number; it returns a sourced company profile: identity, filed accounts,
> leadership, workplaces, verified website and company-owned profiles, each fact tied to its source and date.

**Challenge:** [Builderr Signalpost](https://builderr.ai/challenges/signalpost) · **Builder:** Fury Oktria Putra (solo, with AI agents)

## Run it in one command

```bash
make run BATCH=path/to/companies.jsonl
```

`BATCH` is a JSONL/JSON/text list of organisation numbers. `make run` installs pinned dependencies
(`uv sync --frozen`, Python 3.12), downloads the Brønnøysund bulk snapshot if it is missing, and writes
one terminal envelope per input to `out/<run-id>-envelopes.jsonl`, plus profiles and a run report.

## What this agent adds to the Builderr reference agent

**Identity first.** Every fact is tied to the exact legal entity before it is published. Name similarity
alone never attaches anything; "not checked" is never reported as zero.

| Area | Addition | Identity rule |
|---|---|---|
| Website | Candidates from the registry website and e-mail domain, Wikidata (P2333) and NAV employer homepages, DNS-checked legal-name `.no`/`.com` domains (tried even when the registry offered a candidate that turned out dead or unproven), and (with a key) a search API | a guessed, shared or search-found domain publishes only with the organisation number, registry contact details, or full legal name plus registered place on the site |
| Group sites | A registry-linked site that does not present itself as the company's own (manager, parent, group) | published as `related_website`; `official_website` is `ambiguous`; its social links, jobs and posts are not attributed |
| Social profiles | Links on the verified own site, plus Wikidata profiles recorded against the organisation number | own site or organisation number |
| Jobs | Active NAV ads (arbeidsplassen.nav.no) and the company's own careers page (schema.org `JobPosting`, recruiting-system links) | NAV names the employer by organisation number (company or registered sub-unit); each ad is re-checked live before publishing; a partial index can never assert "no jobs" |
| Public activity | Dated posts from the own site's news feed (RSS/Atom), sitemap and homepage | own site only; attachment, archive and other-country pages are skipped |
| Public activity | EU public-procurement notices (TED): contracts won, tenders published | the notice names the organisation number as winner or buyer |
| Public activity | News articles found by a news search | the article names the organisation number, or the full legal name with its legal form plus the registered place |
| Summary | Evidence-only brief: what the company does, accounts and trend, leadership, workplaces, hiring, activity, web presence, signals, and what is unknown; every sentence links its sources | built only from published claims |
| Refresh | `changes` lists material changes since the previous run (`--previous`) | compared by stable identity, not by text |

Every attempt, accepted or rejected, is kept in the profile with its reason.

## Sources and rights

- **Official, open data:** Brønnøysundregistrene Enhetsregisteret (bulk and API) and Regnskapsregisteret
  ([NLOD 2.0](https://data.norge.no/nlod/en/2.0)); NAV job ads (arbeidsplassen.nav.no public ad pages,
  robots.txt allows all; NAV's public feed API); TED notices (free reuse, Commission Decision 2011/833/EU);
  Wikidata (CC0).
- **Company-owned:** the verified website, its sitemap, news feed and careers page, fetched with robots.txt respected.
- **Search (candidates only):** `search-rs/` (Rust): Brave Search API when `BRAVE_SEARCH_API_KEY` is set,
  GDELT news (keyless, one request per 5.5 s) and Wikipedia. A search result is never evidence on its own.
- **Not used:** LinkedIn, Facebook, Instagram, Google or other platforms whose terms forbid automated collection.
  Contact persons named in job ads are never copied.

### Declared caches (shipped or built before a run)

| Cache | Built by | Freshness rule |
|---|---|---|
| `data/wikidata-no-orgnr.jsonl.gz` | `scripts/wikidata_snapshot.py` | websites still pass the website identity gate at run time |
| `data/ted-no-notices.jsonl.gz` | `make ted-snapshot` | notices are dated; the snapshot date is recorded on each claim |
| `data-cache/nav-feed/active-ads.jsonl.gz` | `make nav-index` | a starting list only; every matched ad is re-checked with NAV during the run |
| `data-cache/search/` | the search tool | results are candidates; cache TTL 7 days |

### Secrets

`BRAVE_SEARCH_API_KEY` (optional) is read from the environment only, never from the command line, and never
logged. Without it the run still completes and search-backed fields report `failed` (not checked).

## Optional local model (off by default)

`--llm-model PATH` lets a small open-weight model (Qwen2.5-1.5B-Instruct, Apache-2.0, 4-bit GGUF, about 1.1 GB,
CPU only via llama.cpp; install with `uv sync --extra llm`) read careers pages that have no structured job data.
Every title it returns must occur on the page, appear as a heading or in a hiring sentence, and pass a second
yes/no check, or it is dropped.

Measured on 46 saved careers pages labelled by hand: 15 correct job titles, 22 wrong items (job categories,
service names, subsidiaries), 4 missed; median 6.5 s per page on an M1. That precision is too low for a
submission scored on precision, so the model stays off in our runs. The test is reproducible with
`scripts/collect_career_pages.py` and `scripts/eval_local_model.py`.

## Cost to run

No paid APIs and no model calls in this version: third-party cost per official run is US$0.
Measured on the full 1,000-company batch (run `v9b`, 11 Oct 2026, one MacBook Air M1, default settings):
36 minutes, 11,847 outbound requests, 448 MB downloaded, median request 1.1 s (p95 2.8 s).
The job-ad index (`make nav-index`, ~13,500 public ad pages at one page per 1.5 s) is built once beforehand
and reused; the TED snapshot ships in `data/`.

## Scorecard

Measure a run the way the challenge scores it, then re-check a sample of its claims against the live sources:

```bash
uv run python scripts/scorecard.py --envelopes out/<run-id>-envelopes.jsonl \
  --json out/<run-id>-scorecard.json --html out/<run-id>-scorecard.html --audit 60
```

It reports coverage per external field family (70% company recall, 30% claim recall), which discovery source
found each website and why the other candidates were rejected, how many claims are `failed` rather than
`not_available`, and the share of audited claims that still reproduce when the source is re-fetched. The
audit sample is seeded, so the same run gives the same sample.

Two companion scripts answer questions about the discovery ceiling rather than about one run:

```bash
uv run python scripts/dns_funnel.py          # what the registry offers, and how many name-derived domains exist
uv run python scripts/name_domain_probe.py   # do those domains pass the identity gate, and how often
```

## Tests

```bash
make test
```

## License

Files written for this entry are MIT: `search-rs/`; in `src/norway_company_agent/`: `site_candidates.py`,
`site_discovery.py`, `site_intel.py`, `nav_feed.py`, `nav_jobs.py`, `claims_view.py`, `brief.py`, `changes.py`,
`wikidata.py`, `procurement.py`, `search_client.py`, `search_enrich.py`, `llm_extract.py`; in `scripts/`:
`nav_active_index.py`, `wikidata_snapshot.py`, `ted_snapshot.py`, `build_report.py`, `scorecard.py`,
`dns_funnel.py`, `name_domain_probe.py`, `fetch_search_bin.sh`, `collect_career_pages.py`,
`eval_local_model.py`, and the `run_signalpost.py` changes; the tests for those files; `Makefile`, see
[LICENSE](LICENSE). The rest of the code and the documents under `docs/` come from
the Builderr Signalpost starter kit and are used under the challenge terms; they are not covered by the MIT license.
