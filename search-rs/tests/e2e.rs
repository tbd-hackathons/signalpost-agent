//! End-to-end tests: run the real binary against a local mock HTTP server.

use serde_json::Value;
use std::collections::HashMap;
use std::net::SocketAddr;
use std::process::Stdio;
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;

const KEY: &str = "test-key-7f3a9c";
const UA: &str = "signalpost-agent/0.2 (+https://github.com/tbd-hackathons/signalpost-agent)";

fn fixture(name: &str) -> String {
    std::fs::read_to_string(format!(
        "{}/tests/fixtures/{name}",
        env!("CARGO_MANIFEST_DIR")
    ))
    .unwrap()
}

// ---------- tiny mock HTTP server ----------

#[derive(Debug, Clone)]
struct Req {
    path: String,
    query: String,
    headers: HashMap<String, String>,
}

impl Req {
    fn param(&self, name: &str) -> Option<String> {
        self.query.split('&').find_map(|kv| {
            let (k, v) = kv.split_once('=')?;
            (k == name).then(|| percent_decode(v))
        })
    }
}

struct Resp {
    status: u16,
    body: String,
    headers: Vec<(&'static str, String)>,
    delay: Duration,
}

fn ok(body: String) -> Resp {
    Resp {
        status: 200,
        body,
        headers: vec![],
        delay: Duration::ZERO,
    }
}

fn status(code: u16, body: &str) -> Resp {
    Resp {
        status: code,
        body: body.into(),
        headers: vec![],
        delay: Duration::ZERO,
    }
}

type Handler = Arc<dyn Fn(&Req, usize) -> Resp + Send + Sync>;

struct Mock {
    addr: SocketAddr,
    log: Arc<Mutex<Vec<Req>>>,
}

impl Mock {
    fn url(&self, path: &str) -> String {
        format!("http://{}{}", self.addr, path)
    }
    fn requests(&self) -> Vec<Req> {
        self.log.lock().unwrap().clone()
    }
    fn count(&self, path_prefix: &str) -> usize {
        self.requests()
            .iter()
            .filter(|r| r.path.starts_with(path_prefix))
            .count()
    }
}

fn percent_decode(s: &str) -> String {
    let b = s.as_bytes();
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'%' && i + 2 < b.len() {
            if let Ok(v) = u8::from_str_radix(&s[i + 1..i + 3], 16) {
                out.push(v);
                i += 3;
                continue;
            }
        }
        out.push(if b[i] == b'+' { b' ' } else { b[i] });
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

async fn start_mock(handler: Handler) -> Mock {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let log = Arc::new(Mutex::new(Vec::new()));
    let log2 = log.clone();
    tokio::spawn(async move {
        loop {
            let Ok((mut sock, _)) = listener.accept().await else {
                return;
            };
            let handler = handler.clone();
            let log = log2.clone();
            tokio::spawn(async move {
                let mut buf = Vec::new();
                let mut chunk = [0u8; 4096];
                while !buf.windows(4).any(|w| w == b"\r\n\r\n") {
                    match sock.read(&mut chunk).await {
                        Ok(0) | Err(_) => return,
                        Ok(n) => buf.extend_from_slice(&chunk[..n]),
                    }
                }
                let text = String::from_utf8_lossy(&buf).into_owned();
                let mut lines = text.split("\r\n");
                let target = lines
                    .next()
                    .and_then(|l| l.split(' ').nth(1))
                    .unwrap_or("/")
                    .to_string();
                let (path, query) = match target.split_once('?') {
                    Some((p, q)) => (p.to_string(), q.to_string()),
                    None => (target.clone(), String::new()),
                };
                let headers = lines
                    .take_while(|l| !l.is_empty())
                    .filter_map(|l| l.split_once(':'))
                    .map(|(k, v)| (k.trim().to_ascii_lowercase(), v.trim().to_string()))
                    .collect();
                let req = Req {
                    path,
                    query,
                    headers,
                };
                let nth = {
                    let mut g = log.lock().unwrap();
                    g.push(req.clone());
                    g.iter().filter(|r| r.path == req.path).count()
                };
                let resp = handler(&req, nth);
                if !resp.delay.is_zero() {
                    tokio::time::sleep(resp.delay).await;
                }
                let mut head = format!(
                    "HTTP/1.1 {} X\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n",
                    resp.status,
                    resp.body.len()
                );
                for (k, v) in &resp.headers {
                    head.push_str(&format!("{k}: {v}\r\n"));
                }
                head.push_str("\r\n");
                let _ = sock.write_all(head.as_bytes()).await;
                let _ = sock.write_all(resp.body.as_bytes()).await;
                let _ = sock.shutdown().await;
            });
        }
    });
    Mock { addr, log }
}

// ---------- running the binary ----------

struct Output {
    lines: Vec<Value>,
    stdout: String,
    stderr: String,
    stats: Value,
    success: bool,
}

async fn run_bin(args: &[&str], envs: &[(&str, String)], input: &str) -> Output {
    let mut cmd = tokio::process::Command::new(env!("CARGO_BIN_EXE_signalpost-search"));
    cmd.args(args)
        .env_clear()
        .env("SIGNALPOST_SEARCH_TEST_MIN_INTERVAL_MS", "0")
        .env("SIGNALPOST_SEARCH_TEST_BACKOFF_MS", "1")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    for (k, v) in envs {
        cmd.env(k, v);
    }
    let mut child = cmd.spawn().unwrap();
    let mut stdin = child.stdin.take().unwrap();
    stdin.write_all(input.as_bytes()).await.unwrap();
    drop(stdin);
    let out = tokio::time::timeout(Duration::from_secs(60), child.wait_with_output())
        .await
        .expect("binary timed out")
        .unwrap();
    let stdout = String::from_utf8(out.stdout).unwrap();
    let stderr = String::from_utf8(out.stderr).unwrap();
    let lines = stdout
        .lines()
        .map(|l| serde_json::from_str(l).unwrap_or_else(|e| panic!("bad line {l}: {e}")))
        .collect();
    let stats = stderr
        .lines()
        .rev()
        .find_map(|l| serde_json::from_str::<Value>(l).ok())
        .map(|v| v["stats"].clone())
        .unwrap_or(Value::Null);
    Output {
        lines,
        stdout,
        stderr,
        stats,
        success: out.status.success(),
    }
}

fn select<'a>(lines: &'a [Value], id: &str, provider: &str) -> Vec<&'a Value> {
    let mut v: Vec<&Value> = lines
        .iter()
        .filter(|l| l["id"] == id && l["provider"] == provider)
        .collect();
    v.sort_by_key(|l| l["rank"].as_u64().unwrap_or(0));
    v
}

fn standard_handler() -> Handler {
    Arc::new(|req: &Req, nth: usize| match req.path.as_str() {
        p if p.starts_with("/brave/") => {
            if req.headers.get("x-subscription-token").map(String::as_str) != Some(KEY) {
                return status(401, r#"{"error":"unauthorized"}"#);
            }
            if req.param("q").unwrap_or_default().contains("nothing") {
                return ok(r#"{"type":"search","web":{"results":[]}}"#.into());
            }
            if p.ends_with("/web/search") {
                ok(fixture("brave_web.json"))
            } else {
                ok(fixture("brave_news.json"))
            }
        }
        "/gdelt" => {
            if nth == 1 {
                // GDELT's "slow down" answer is HTTP 200 with plain text.
                ok(fixture("gdelt_rate_limited.txt"))
            } else {
                ok(fixture("gdelt_artlist.json"))
            }
        }
        "/w/api.php" => {
            if req
                .param("srsearch")
                .unwrap_or_default()
                .contains("nothing")
            {
                ok(fixture("wikipedia_empty.json"))
            } else {
                ok(fixture("wikipedia_search.json"))
            }
        }
        _ => status(404, "{}"),
    })
}

fn provider_env(mock: &Mock) -> Vec<(&'static str, String)> {
    vec![
        ("SIGNALPOST_SEARCH_BRAVE_URL", mock.url("/brave")),
        ("SIGNALPOST_SEARCH_GDELT_URL", mock.url("/gdelt")),
        ("SIGNALPOST_SEARCH_WIKIPEDIA_URL", mock.url("/w/api.php")),
    ]
}

const INPUT: &str = concat!(
    r#"{"id": "912345678:web:1", "query": "\"Vinsnesbakk Elektro\" Sandnes", "kind": "web", "count": 10, "lang": "no"}"#,
    "\n",
    r#"{"id": "912345678:news:1", "query": "Equinor", "kind": "news", "count": 5}"#,
    "\n",
    "this is not json\n",
    r#"{"query": "missing id"}"#,
    "\n",
    "\n",
    r#"{"id": "999:web:1", "query": "nothing here", "kind": "web", "count": 3}"#,
    "\n",
);

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn end_to_end_all_providers_then_cache() {
    let mock = start_mock(standard_handler()).await;
    let cache = tempfile::tempdir().unwrap();
    let cache_dir = cache.path().to_str().unwrap().to_string();
    let mut env = provider_env(&mock);
    env.push(("BRAVE_SEARCH_API_KEY", KEY.to_string()));

    let out = run_bin(
        &[
            "--cache-dir",
            &cache_dir,
            "--providers",
            "brave,gdelt,wikipedia",
        ],
        &env,
        INPUT,
    )
    .await;
    assert!(out.success, "stderr: {}", out.stderr);

    // brave web
    let bw = select(&out.lines, "912345678:web:1", "brave");
    assert_eq!(bw.len(), 3);
    assert_eq!(bw[0]["rank"], 1);
    assert_eq!(bw[0]["kind"], "web");
    assert_eq!(bw[0]["url"], "https://vinsnesbakk-elektro.example.no/");
    assert_eq!(bw[0]["published"], "2025-11-03T09:12:00Z");
    assert_eq!(bw[0]["cached"], false);
    assert!(bw[0]["retrieved_at"].as_str().unwrap().ends_with('Z'));
    assert!(bw[2]["published"].is_null());

    // wikipedia web: URLs derive from the (mock) endpoint
    let ww = select(&out.lines, "912345678:web:1", "wikipedia");
    assert_eq!(ww.len(), 2);
    assert_eq!(ww[0]["url"], mock.url("/wiki/Hannestad_barnehage"));

    // gdelt: news only; it recovered from one plain-text rate-limit answer
    let gw = select(&out.lines, "912345678:web:1", "gdelt");
    assert_eq!(gw.len(), 1);
    assert_eq!(gw[0]["error"], "unsupported_kind");
    let gn = select(&out.lines, "912345678:news:1", "gdelt");
    assert_eq!(gn.len(), 2);
    assert_eq!(gn[0]["published"], "2026-10-01T12:00:00Z");
    assert_eq!(gn[0]["domain"], "example.no");
    assert_eq!(mock.count("/gdelt"), 2);

    // brave news, wikipedia news skipped
    assert_eq!(select(&out.lines, "912345678:news:1", "brave").len(), 2);
    assert_eq!(
        select(&out.lines, "912345678:news:1", "wikipedia")[0]["error"],
        "unsupported_kind"
    );

    // searched-but-empty is distinguishable from not-searched
    for p in ["brave", "wikipedia"] {
        let e = select(&out.lines, "999:web:1", p);
        assert_eq!(e.len(), 1, "{p}");
        assert_eq!(e[0]["empty"], true);
    }

    // malformed input lines are reported with their 1-based line number
    let bad: Vec<&Value> = out
        .lines
        .iter()
        .filter(|l| l["error"] == "bad_input")
        .collect();
    assert_eq!(bad.len(), 2);
    let mut bad_lines: Vec<u64> = bad.iter().map(|l| l["line"].as_u64().unwrap()).collect();
    bad_lines.sort();
    assert_eq!(bad_lines, vec![3, 4]);
    assert!(bad.iter().all(|l| l["id"].is_null()));

    // outbound requests: UA everywhere, key only to Brave, correct params
    for r in mock.requests() {
        assert_eq!(r.headers.get("user-agent").map(String::as_str), Some(UA));
        assert_eq!(
            r.headers.get("accept").map(String::as_str),
            Some("application/json")
        );
        assert_eq!(
            r.headers.contains_key("x-subscription-token"),
            r.path.starts_with("/brave/"),
            "{}",
            r.path
        );
    }
    let web = mock
        .requests()
        .into_iter()
        .find(|r| r.path == "/brave/web/search" && !r.query.contains("nothing"))
        .unwrap();
    assert_eq!(web.param("q").unwrap(), "\"Vinsnesbakk Elektro\" Sandnes");
    assert_eq!(web.param("country").unwrap(), "NO");
    assert_eq!(web.param("search_lang").unwrap(), "nb");
    assert_eq!(web.param("count").unwrap(), "10");

    // the key never appears in output
    assert!(!out.stdout.contains(KEY));
    assert!(!out.stderr.contains(KEY));

    // stats
    let s = &out.stats;
    assert_eq!(s["queries"], 3);
    assert_eq!(s["bad_input"], 2);
    assert_eq!(s["requests"], 3 + 2 + 2);
    assert_eq!(s["cache_hits"], 0);
    assert_eq!(s["errors"], 0);
    assert_eq!(s["by_provider"]["gdelt"]["retries"], 1);
    assert_eq!(s["by_provider"]["gdelt"]["skipped"], 2);
    assert_eq!(s["estimated_cost_usd"], 0.015);

    // cache files: <dir>/<provider>/<sha256>.json
    let brave_files = std::fs::read_dir(cache.path().join("brave"))
        .unwrap()
        .count();
    assert_eq!(brave_files, 3);

    // second run: everything from cache, zero requests
    let before = mock.requests().len();
    let out2 = run_bin(
        &[
            "--cache-dir",
            &cache_dir,
            "--providers",
            "brave,gdelt,wikipedia",
        ],
        &env,
        INPUT,
    )
    .await;
    assert!(out2.success);
    assert_eq!(
        mock.requests().len(),
        before,
        "second run must not hit the network"
    );
    assert_eq!(out2.stats["requests"], 0);
    assert_eq!(out2.stats["cache_hits"], 6);
    assert_eq!(out2.stats["estimated_cost_usd"], 0.0);
    let results: Vec<&Value> = out2
        .lines
        .iter()
        .filter(|l| l.get("url").is_some())
        .collect();
    assert_eq!(results.len(), 3 + 2 + 2 + 2);
    assert!(results.iter().all(|l| l["cached"] == true));
    // retrieved_at reports the original fetch time
    let first = select(&out.lines, "912345678:web:1", "brave")[0]["retrieved_at"].clone();
    let second = select(&out2.lines, "912345678:web:1", "brave")[0]["retrieved_at"].clone();
    assert_eq!(first, second);

    // TTL 0 forces a refetch
    let out3 = run_bin(
        &[
            "--cache-dir",
            &cache_dir,
            "--cache-ttl-hours",
            "0",
            "--providers",
            "wikipedia",
        ],
        &env,
        INPUT,
    )
    .await;
    assert_eq!(out3.stats["requests"], 2);
    assert_eq!(out3.stats["cache_hits"], 0);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn no_key_reports_per_query_and_sends_nothing() {
    let mock = start_mock(standard_handler()).await;
    let out = run_bin(&["--providers", "brave"], &provider_env(&mock), INPUT).await;
    assert!(out.success);
    for id in ["912345678:web:1", "912345678:news:1", "999:web:1"] {
        let l = select(&out.lines, id, "brave");
        assert_eq!(l.len(), 1);
        assert_eq!(l[0]["error"], "no_key");
    }
    assert_eq!(mock.requests().len(), 0);
    assert_eq!(out.stats["errors"], 3);
    assert_eq!(out.stats["estimated_cost_usd"], 0.0);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn retries_then_reports_http_errors() {
    let handler: Handler = Arc::new(|req: &Req, _| {
        let q = req.param("q").unwrap_or_default();
        if req.path.ends_with("/news/search") {
            return status(404, "{}");
        }
        match q.as_str() {
            "server-error" => status(500, "{}"),
            "rate" => Resp {
                status: 429,
                body: "{}".into(),
                headers: vec![("Retry-After", "0".into())],
                delay: Duration::ZERO,
            },
            "far-future" => Resp {
                status: 429,
                body: "{}".into(),
                headers: vec![("Retry-After", "3600".into())],
                delay: Duration::ZERO,
            },
            "slow" => Resp {
                status: 200,
                body: r#"{"web":{"results":[]}}"#.into(),
                headers: vec![],
                delay: Duration::from_secs(3),
            },
            _ => ok(fixture("brave_web.json")),
        }
    });
    let mock = start_mock(handler).await;
    let mut env = provider_env(&mock);
    env.push(("BRAVE_SEARCH_API_KEY", KEY.to_string()));
    let input = [
        r#"{"id":"a","query":"server-error"}"#,
        r#"{"id":"b","query":"rate"}"#,
        r#"{"id":"c","query":"x","kind":"news"}"#,
        r#"{"id":"e","query":"slow"}"#,
    ]
    .join("\n");
    let out = run_bin(
        &["--providers", "brave", "--timeout-secs", "1"],
        &env,
        &input,
    )
    .await;
    assert!(out.success);
    let err = |id: &str| select(&out.lines, id, "brave")[0]["error"].clone();
    assert_eq!(err("a"), "http_500");
    assert_eq!(err("b"), "rate_limited");
    assert_eq!(err("c"), "http_404");
    assert_eq!(err("e"), "timeout");

    let by_q = |q: &str| {
        mock.requests()
            .iter()
            .filter(|r| r.param("q").as_deref() == Some(q))
            .count()
    };
    assert_eq!(by_q("server-error"), 3, "5xx retried up to 3 attempts");
    assert_eq!(by_q("rate"), 3, "429 retried up to 3 attempts");
    assert_eq!(by_q("x"), 1, "4xx is not retried");
    assert_eq!(by_q("slow"), 3, "timeouts are retried");
    assert_eq!(out.stats["errors"], 4);
    // failed requests are not billed
    assert_eq!(out.stats["estimated_cost_usd"], 0.0);

    // A Retry-After beyond the 60 s cap opens the circuit: no sleeping, and
    // the remaining queries for that provider fail fast without a request.
    let before = mock.requests().len();
    let input = [
        r#"{"id":"d1","query":"far-future"}"#,
        r#"{"id":"d2","query":"far-future"}"#,
        r#"{"id":"d3","query":"other"}"#,
    ]
    .join("\n");
    let started = std::time::Instant::now();
    let out = run_bin(
        &["--providers", "brave", "--concurrency", "1"],
        &env,
        &input,
    )
    .await;
    assert!(started.elapsed() < Duration::from_secs(10));
    for id in ["d1", "d2", "d3"] {
        assert_eq!(
            select(&out.lines, id, "brave")[0]["error"],
            "rate_limited",
            "{id}"
        );
    }
    assert_eq!(mock.requests().len() - before, 1);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn duplicate_queries_are_fetched_once_and_errors_not_cached() {
    let mock = start_mock(standard_handler()).await;
    let cache = tempfile::tempdir().unwrap();
    let cache_dir = cache.path().to_str().unwrap().to_string();
    let input = [
        r#"{"id":"one","query":"Hannestad barnehage"}"#,
        r#"{"id":"two","query":"Hannestad barnehage"}"#,
    ]
    .join("\n");
    let out = run_bin(
        &["--providers", "wikipedia,brave", "--cache-dir", &cache_dir],
        &provider_env(&mock),
        &input,
    )
    .await;
    assert_eq!(mock.count("/w/api.php"), 1);
    assert_eq!(select(&out.lines, "one", "wikipedia").len(), 2);
    assert_eq!(select(&out.lines, "two", "wikipedia").len(), 2);
    assert_eq!(out.stats["by_provider"]["wikipedia"]["deduped"], 1);
    // brave had no key: error lines, and nothing written to the brave cache
    assert_eq!(select(&out.lines, "one", "brave")[0]["error"], "no_key");
    assert!(!cache.path().join("brave").exists());
}
