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

| Addition | Why |
|---|---|
| Website candidates from the registry e-mail domain | ~89% of the universe has no registry website, but many list a company-domain e-mail |
| Legal-name `.no` domain candidates (DNS-checked) | finds sites the registry never recorded |
| Shared-domain detection | an e-mail domain used by several entities (accountant, ISP, group mailbox) is never trusted on its own |
| Contact and place proof | registry e-mail/phone on the site, or full legal name plus registered town, proves the site |
| Strict gate for guessed domains | a guessed or shared domain publishes only with the organisation number, registry contact details, or full name plus registered town on the site |
| Active job ads from NAV | every ad on arbeidsplassen.nav.no names its employer by organisation number; an ad is attached when that number is the company or one of its registered sub-units, and only after NAV confirms during the run that it is still active |
| Careers pages on verified sites | schema.org `JobPosting` entries, and links from the company's own careers page to ads on recruiting systems (Webcruiter, Jobbnorge, Recman, Teamtailor, FINN, NAV) |
| Dated news posts on verified sites | from the site's sitemap and homepage links, newest first; published only with a title and a date |
| Contract claims in every envelope | `claims`, `evidence` and `external_observations` (starter observation schema) next to the full profile |

Every attempt, accepted or rejected, is kept in `evidence.website.value.discovery.attempts` with its reason.

## Sources

Official: Brønnøysundregistrene Enhetsregisteret (bulk + API) and Regnskapsregisteret, open data under
[NLOD 2.0](https://data.norge.no/nlod/en/2.0). NAV's public job-ad feed API (`pam-stilling-feed.nav.no`,
public token from NAV's own endpoint) and the arbeidsplassen.nav.no sitemap. Company-owned: the verified
company website, its sitemap and careers page, fetched with robots.txt respected. No search engine, social
platform or third-party directory is scraped. Contact persons named in job ads are never copied.

### Declared cache

`data-cache/nav-feed/active-ads.jsonl.gz` lists active NAV ads by employer number (`make nav-index`
builds it, about an hour). During a run it is only a starting list: the run reads the feed pages newer
than the index, and re-checks every matched ad with NAV before publishing it. Without the cache the jobs
field reports `failed`, never zero.

## Cost to run

No paid APIs and no model calls in this version: third-party cost per official run is US$0.
Measured on 100 random companies: ~5 minutes, ~750 outbound requests.

## Tests

```bash
make test
```

## License

Files written for this entry (`src/norway_company_agent/site_candidates.py`, `site_discovery.py`,
`site_intel.py`, `nav_feed.py`, `nav_jobs.py`, `claims_view.py`, `scripts/nav_active_index.py`, the
`scripts/run_signalpost.py` changes, `tests/test_site_discovery.py`, `tests/test_site_intel.py`,
`tests/test_nav_jobs.py`, `Makefile`) are MIT, see [LICENSE](LICENSE). The rest of the code and the documents under `docs/` come from
the Builderr Signalpost starter kit and are used under the challenge terms; they are not covered by the MIT license.
