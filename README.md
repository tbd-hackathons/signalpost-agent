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

Every attempt, accepted or rejected, is kept in `evidence.website.value.discovery.attempts` with its reason.

## Sources

Official: Brønnøysundregistrene Enhetsregisteret (bulk + API) and Regnskapsregisteret, open data under
[NLOD 2.0](https://data.norge.no/nlod/en/2.0). Company-owned: the verified company website, fetched with
robots.txt respected. No search engine, social platform or third-party directory is scraped.

## Cost to run

No paid APIs and no model calls in this version: third-party cost per official run is US$0.
Measured on 100 random companies: ~5 minutes, ~750 outbound requests.

## Tests

```bash
make test
```

## License

Files written for this entry (`src/norway_company_agent/site_candidates.py`,
`src/norway_company_agent/site_discovery.py`, `scripts/run_signalpost.py` changes, `tests/test_site_discovery.py`,
`Makefile`) are MIT, see [LICENSE](LICENSE). The rest of the code and the documents under `docs/` come from
the Builderr Signalpost starter kit and are used under the challenge terms; they are not covered by the MIT license.
