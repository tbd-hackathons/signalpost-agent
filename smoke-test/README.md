# 100-company smoke test

The local scale test the challenge asks for: 100 companies from the public universe
(`companies.jsonl`), run with the one evaluator command on 13 Oct 2026 (WIB):

```bash
make run BATCH=smoke-test/companies.jsonl
```

| | |
|---|---|
| Code | commit `bf35dba` (this folder is the only change after it) |
| Result | 100 of 100 terminal envelopes; all five validation checks pass; no company errors (`run-report.json`) |
| Time and cost | 16 minutes, 1,355 outbound requests, US$0 third-party cost, one 16 GB M1 Mac |
| Live re-audit | 60 published claims re-fetched and re-checked against their sources: **60 of 60 reproduce** (`scorecard.json`) |
| Coverage | verified own website on 20 of 100 companies; 82 published external claims |

Files: `envelopes.jsonl` (the output, one envelope per company), `run-report.json` (counts, timings,
validation), `scorecard.json` / `scorecard.html` (coverage per field family and the live re-audit),
`report.html` (the browsable report page: open it locally).

Two precision bugs were caught by checking runs like this one before submission, and fixed:
a registry website that now redirects to a domain registrar's parking page was labelled a related company
site (`323d4fb`), and a one-word legal name matched another entity's domain of the same name
(HAUKSTAD AS in Skien vs Haukstad Gård in Løten, `5a1ff8c`).
