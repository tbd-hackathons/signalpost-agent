# 100-company smoke test

The local scale test the challenge asks for: 100 companies from the public universe
(`companies.jsonl`), run with the one evaluator command on 11 Oct 2026 (WIB):

```bash
make run BATCH=smoke-test/companies.jsonl
```

| | |
|---|---|
| Code | commit `323d4fb` (the only later change narrows the parking-page rule for related sites; it changes no claim in this batch) |
| Result | 100 of 100 terminal envelopes; all five validation checks pass (`run-report.json`) |
| Time and cost | 19 minutes, 1,396 outbound requests, US$0 third-party cost, one 16 GB M1 Mac |
| Live re-audit | 60 published claims re-fetched and re-checked against their sources: **60 of 60 reproduce** (`scorecard.json`) |
| Coverage | verified own website on 20 of 100 companies; 85 published external claims |

Files: `envelopes.jsonl` (the output, one envelope per company), `run-report.json` (counts, timings,
validation), `scorecard.json` / `scorecard.html` (coverage per field family and the live re-audit),
`report.html` (the browsable report page: open it locally).

Earlier in the day, the first smoke run's audit caught one related-site claim that pointed at a domain
registrar's parking page (ALCE SPRINKLER AS). That rule was fixed in `323d4fb` and this smoke test was
re-run on the fixed code.
