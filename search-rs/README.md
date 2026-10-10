# signalpost-search

A small Rust command-line tool that finds **candidate** websites and news articles for companies.
The signalpost-agent Python code calls it, then checks each candidate itself. A search hit is never
taken as proof of identity.

It reads queries as JSON Lines on stdin, sends each query to the selected providers, and writes one
JSON line per result on stdout. It writes one JSON stats line to stderr when it ends.

## Build and test

```sh
cd search-rs
cargo build --release --locked        # binary: target/release/signalpost-search
cargo test --locked
cargo clippy --all-targets -- -D warnings
cargo fmt --check
```

TLS uses rustls (ring), not OpenSSL, so static `*-linux-musl` builds work. `ci/release.yml` is a
ready-to-move GitHub Actions workflow. It builds x86_64/aarch64 Linux musl and aarch64 macOS
binaries and attaches them, with `SHA256SUMS`, to the release for a `search-rs-v*` tag.

## Usage

```sh
signalpost-search [--cache-dir DIR] [--cache-ttl-hours 168] [--concurrency 4] \
                  [--providers brave,gdelt,wikipedia] [--timeout-secs 20] \
                  < queries.jsonl > results.jsonl
```

| Flag | Default | Meaning |
|---|---|---|
| `--cache-dir` | off | Directory for the result cache. No cache when you leave it out. |
| `--cache-ttl-hours` | 168 | Use cached results younger than this. 0 always refetches. |
| `--concurrency` | 4 | Maximum requests in flight **per provider** (1–64). |
| `--providers` | `brave,gdelt,wikipedia` | Providers to query, comma-separated. |
| `--timeout-secs` | 20 | Timeout for each HTTP request (1–300). |

Exit code 0 means every input line was processed, including lines that produced error lines.
Exit code 1 means a fatal I/O problem; stderr then has a `{"fatal": ...}` line. Exit code 2 means
bad flags.

### Input (one JSON object per line)

```json
{"id": "912345678:web:1", "query": "\"Vinsnesbakk Elektro\" Sandnes", "kind": "web", "count": 10, "lang": "no"}
```

`id` and `query` are required. `kind` is `web` (default) or `news`. `count` defaults to 10 and is
capped at 50, then at each provider's own limit. `lang` defaults to `no`.

### Output (stdout, one JSON object per line, in no fixed order)

A result:

```json
{"id":"912345678:web:1","provider":"brave","kind":"web","rank":1,"url":"https://…","title":"…","snippet":"…","published":"2025-11-03T09:12:00Z","retrieved_at":"2026-10-10T08:00:00Z","cached":false}
```

- `published` is an ISO date or date-time, or `null` when the provider gives none or gives only a
  relative age such as "2 days ago".
- `retrieved_at` is the UTC time of the original fetch. For a cached result it is the time the
  result was first fetched, not the time of this run.
- `cached` is `true` when the result came from the disk cache, or from an identical query earlier
  in the same run.
- GDELT and Brave results can also have `domain` and `language`.

A query that was searched and found nothing:

```json
{"id":"…","provider":"wikipedia","kind":"web","empty":true,"retrieved_at":"…","cached":false}
```

A query that failed or was skipped:

```json
{"id":"…","provider":"brave","error":"no_key"}
```

| `error` | Meaning |
|---|---|
| `no_key` | `BRAVE_SEARCH_API_KEY` is not set. No request is sent. |
| `unsupported_kind` | The provider does not serve this kind: GDELT is news only, Wikipedia is web only. |
| `rate_limited` | HTTP 429 or GDELT's "slow down" text, still failing after retries. |
| `http_<code>` | Any other non-2xx status, e.g. `http_401`, or `http_500` after retries. |
| `timeout`, `network` | Timeout or connection failure, still failing after retries. |
| `bad_query` | The provider rejected the query, e.g. GDELT's "phrase is too short". `detail` gives the reason. |
| `bad_response` | The provider sent a body that is not valid JSON. |

A malformed input line (processing continues):

```json
{"id": null, "error": "bad_input", "line": 3, "detail": "invalid JSON"}
```

`id` is filled in when it could be read. `line` is the 1-based line number. Blank lines are ignored.

### Stats (stderr, last line)

```json
{"stats":{"queries":2,"bad_input":0,"requests":2,"cache_hits":0,"errors":0,"estimated_cost_usd":0.0,"elapsed_secs":6.1,"by_provider":{"gdelt":{"queries":2,"requests":1,"retries":0,"cache_hits":0,"deduped":0,"results":5,"empty":0,"errors":0,"skipped":1,"cache_write_errors":0}}}}
```

`requests` counts HTTP attempts, retries included. `estimated_cost_usd` is the number of successful
(2xx) Brave responses × `BRAVE_SEARCH_USD_PER_1000` / 1000. Cache hits and failed requests count
as free.

## Providers and rate limits

| Provider | Kinds | Key | Limit used |
|---|---|---|---|
| `brave` — Brave Search API (`/web/search`, `/news/search`; `country=NO`, `search_lang=nb` for `lang=no`) | web, news | `BRAVE_SEARCH_API_KEY` | `BRAVE_SEARCH_RPS` (default 1 request/s) |
| `gdelt` — GDELT DOC 2.0 `artlist`, last 12 months, newest first | news | none | 1 request / 5.5 s, process-wide (GDELT asks for at most one per 5 s) |
| `wikipedia` — MediaWiki search on `no.wikipedia.org` (`en.` when `lang=en`) | web | none | 2 requests/s |

- One limiter per provider, shared by all tasks. A slot is reserved before each attempt, so
  concurrency never causes a burst.
- Retries: at most 3 attempts on 429, 5xx, timeouts and connection errors. The delay is
  exponential with jitter (base 1 s; base 5.5 s for GDELT). `Retry-After` is used when present.
  A 429 also delays the next requests for that provider.
- A `Retry-After` longer than 60 s stops calls to that provider for that long. Its remaining
  queries return `rate_limited` at once instead of stalling the run.
- Other 4xx responses are not retried.
- Every request sends
  `User-Agent: signalpost-agent/0.2 (+https://github.com/tbd-hackathons/signalpost-agent)`.
- Redirects are followed only within the same host, so the Brave key header is never sent to
  another host.

GDELT is the slow path: 1,000 news queries need at least 1,000 × 5.5 s ≈ 92 minutes. Run news
queries against GDELT only where you need them, or rely on Brave news plus the cache.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `BRAVE_SEARCH_API_KEY` | unset | Brave key. Read from the environment only; there is no flag for it. It is sent only as the `X-Subscription-Token` header and never printed. |
| `BRAVE_SEARCH_RPS` | `1` | Brave requests per second. Match your plan. |
| `BRAVE_SEARCH_USD_PER_1000` | `5.0` | Price used for `estimated_cost_usd`. |
| `SIGNALPOST_SEARCH_BRAVE_URL` | `https://api.search.brave.com/res/v1` | Brave base URL; `/web/search` or `/news/search` is appended. |
| `SIGNALPOST_SEARCH_GDELT_URL` | `https://api.gdeltproject.org/api/v2/doc/doc` | GDELT endpoint. |
| `SIGNALPOST_SEARCH_WIKIPEDIA_URL` | `https://{lang}.wikipedia.org/w/api.php` | MediaWiki endpoint. `{lang}` becomes `no` or `en`. Article URLs are built from it. |
| `SIGNALPOST_SEARCH_TEST_MIN_INTERVAL_MS`, `SIGNALPOST_SEARCH_TEST_BACKOFF_MS` | unset | Test hooks that override the rate-limit interval and the retry base. They apply only to providers whose URL points at `localhost`, `127.0.0.1` or `[::1]`, so they cannot speed up calls to a real service. |

## Cache

`<cache-dir>/<provider>/<sha256("provider|kind|lang|count|query")>.json` holds the results and
`fetched_at`. A file is used while it is younger than the TTL. Results with zero hits are cached.
Errors are never cached. Each file is written to a temp file and then renamed, so readers never see
a partial file. A corrupt file counts as a miss. An identical query (same provider, kind, lang,
count and text) is fetched once per run; every `id` that asked for it still gets its own lines.

## Licence

MIT, the same as the rest of the repository. See [`../LICENSE`](../LICENSE).
