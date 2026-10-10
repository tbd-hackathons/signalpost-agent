//! Reads queries, fans them out to providers, and writes output lines.

use crate::cache::{Cache, CacheKey};
use crate::config::{Provider, Settings};
use crate::http::FetchError;
use crate::model::{parse_query_line, BadInputLine, EmptyLine, ErrorLine, Hit, Query, ResultLine};
use crate::providers::{self, ProviderCtx};
use crate::ratelimit::RateLimiter;
use crate::stats::Stats;
use crate::text::now_iso;
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::io::{AsyncBufRead, AsyncBufReadExt, AsyncWrite, AsyncWriteExt};
use tokio::sync::{mpsc, OnceCell, Semaphore};
use tokio::task::JoinSet;

#[derive(Debug, Clone)]
pub struct RunOptions {
    pub cache_dir: Option<PathBuf>,
    pub cache_ttl: Duration,
    /// Max in-flight live requests per provider.
    pub concurrency: usize,
    pub providers: Vec<Provider>,
    pub timeout: Duration,
}

#[derive(Debug, Clone)]
struct Found {
    hits: Arc<Vec<Hit>>,
    retrieved_at: String,
    cached: bool,
}

type Outcome = Result<Found, FetchError>;

struct ProviderState {
    limiter: RateLimiter,
    semaphore: Semaphore,
}

struct Runner {
    client: reqwest::Client,
    settings: Settings,
    cache: Option<Cache>,
    stats: Arc<Stats>,
    state: HashMap<Provider, ProviderState>,
    inflight: Mutex<HashMap<String, Arc<OnceCell<Outcome>>>>,
    tx: mpsc::UnboundedSender<String>,
}

/// Build the HTTP client: rustls, fixed User-Agent, per-request timeout.
///
/// Redirects are followed only within the same host (max 5), so the Brave
/// `X-Subscription-Token` header can never be forwarded to another host.
pub fn build_client(timeout: Duration) -> anyhow::Result<reqwest::Client> {
    let same_host = reqwest::redirect::Policy::custom(|attempt| {
        let origin = attempt.previous().first().and_then(|u| u.host_str());
        if attempt.previous().len() > 5 {
            attempt.error("too many redirects")
        } else if origin.is_some() && attempt.url().host_str() == origin {
            attempt.follow()
        } else {
            attempt.stop()
        }
    });
    Ok(reqwest::Client::builder()
        .user_agent(crate::USER_AGENT)
        .timeout(timeout)
        .connect_timeout(timeout)
        .redirect(same_host)
        .use_rustls_tls()
        .build()?)
}

/// Process every line of `input`, write output lines to `output`, and return
/// the run statistics.
pub async fn run<R, W>(
    opts: RunOptions,
    settings: Settings,
    input: R,
    output: W,
) -> anyhow::Result<Arc<Stats>>
where
    R: AsyncBufRead + Unpin,
    W: AsyncWrite + Unpin + Send + 'static,
{
    let client = build_client(opts.timeout)?;
    let (tx, rx) = mpsc::unbounded_channel::<String>();
    let writer = tokio::spawn(write_lines(rx, output));

    let concurrency = opts.concurrency.max(1);
    let state = opts
        .providers
        .iter()
        .map(|&p| {
            let ps = settings.for_provider(p);
            (
                p,
                ProviderState {
                    limiter: RateLimiter::new(ps.min_interval),
                    semaphore: Semaphore::new(concurrency),
                },
            )
        })
        .collect();
    let stats = Arc::new(Stats::default());
    let runner = Arc::new(Runner {
        client,
        settings,
        cache: opts
            .cache_dir
            .as_ref()
            .map(|d| Cache::new(d.clone(), opts.cache_ttl)),
        stats: stats.clone(),
        state,
        inflight: Mutex::new(HashMap::new()),
        tx: tx.clone(),
    });

    let mut tasks = JoinSet::new();
    let mut task_ids = HashMap::new();
    let mut input = input;
    let mut buf = Vec::new();
    let mut line_no = 0usize;
    loop {
        buf.clear();
        let n = input.read_until(b'\n', &mut buf).await?;
        if n == 0 {
            break;
        }
        line_no += 1;
        let line = match std::str::from_utf8(&buf) {
            Ok(s) => s.trim(),
            Err(_) => {
                runner.bad_input(line_no, None, "line is not valid UTF-8");
                continue;
            }
        };
        if line.is_empty() {
            continue;
        }
        let q = match parse_query_line(line) {
            Ok(q) => Arc::new(q),
            Err(b) => {
                runner.bad_input(line_no, b.id.as_deref(), &b.detail);
                continue;
            }
        };
        stats.query();
        for &p in &opts.providers {
            let r = runner.clone();
            let q2 = q.clone();
            let handle = tasks.spawn(async move { r.handle(q2, p).await });
            task_ids.insert(handle.id(), (q.id.clone(), p));
        }
        // Reap finished tasks as we go so memory stays flat on long inputs.
        while let Some(res) = tasks.try_join_next_with_id() {
            reap(&runner, &mut task_ids, res);
        }
    }
    while let Some(res) = tasks.join_next_with_id().await {
        reap(&runner, &mut task_ids, res);
    }

    drop(runner);
    drop(tx);
    writer.await??;
    Ok(stats)
}

fn reap(
    runner: &Runner,
    ids: &mut HashMap<tokio::task::Id, (String, Provider)>,
    res: Result<(tokio::task::Id, ()), tokio::task::JoinError>,
) {
    match res {
        Ok((id, ())) => {
            ids.remove(&id);
        }
        Err(e) => {
            if let Some((qid, p)) = ids.remove(&e.id()) {
                runner.stats.error(p);
                runner.emit_error(&qid, p, &FetchError::Internal("task failed".into()));
            }
        }
    }
}

async fn write_lines<W: AsyncWrite + Unpin>(
    mut rx: mpsc::UnboundedReceiver<String>,
    out: W,
) -> std::io::Result<()> {
    let mut w = tokio::io::BufWriter::new(out);
    while let Some(line) = rx.recv().await {
        w.write_all(line.as_bytes()).await?;
        w.write_all(b"\n").await?;
        while let Ok(more) = rx.try_recv() {
            w.write_all(more.as_bytes()).await?;
            w.write_all(b"\n").await?;
        }
        // Flush whenever the queue drains so the caller can stream results.
        w.flush().await?;
    }
    w.flush().await
}

impl Runner {
    fn send<T: serde::Serialize>(&self, v: &T) {
        if let Ok(s) = serde_json::to_string(v) {
            // A closed channel means the writer failed; the run reports that.
            let _ = self.tx.send(s);
        }
    }

    fn bad_input(&self, line: usize, id: Option<&str>, detail: &str) {
        self.stats.bad_input();
        self.send(&BadInputLine {
            id,
            error: "bad_input",
            line,
            detail,
        });
    }

    fn emit_error(&self, id: &str, p: Provider, e: &FetchError) {
        let code = e.code();
        self.send(&ErrorLine {
            id,
            provider: p.name(),
            error: &code,
            detail: e.detail(),
        });
    }

    async fn handle(self: Arc<Self>, q: Arc<Query>, p: Provider) {
        self.stats.provider_query(p);
        if !p.supports(q.kind) {
            self.stats.skipped(p);
            self.emit_error(&q.id, p, &FetchError::UnsupportedKind);
            return;
        }
        let key = CacheKey {
            provider: p.name(),
            kind: q.kind,
            lang: &q.lang,
            count: q.count,
            query: &q.query,
        };
        let cell = {
            let mut map = self.inflight.lock().unwrap_or_else(|e| e.into_inner());
            map.entry(key.digest()).or_default().clone()
        };
        let mut leader = false;
        let outcome = cell
            .get_or_init(|| {
                leader = true;
                self.resolve(&q, p)
            })
            .await
            .clone();

        match outcome {
            Err(e) => {
                self.stats.error(p);
                self.emit_error(&q.id, p, &e);
            }
            Ok(found) => {
                if !leader {
                    self.stats.deduped(p);
                }
                let cached = found.cached || !leader;
                if found.hits.is_empty() {
                    self.stats.empty(p);
                    self.send(&EmptyLine {
                        id: &q.id,
                        provider: p.name(),
                        kind: q.kind.as_str(),
                        empty: true,
                        retrieved_at: &found.retrieved_at,
                        cached,
                    });
                    return;
                }
                self.stats.results(p, found.hits.len());
                for (i, h) in found.hits.iter().enumerate() {
                    self.send(&ResultLine {
                        id: &q.id,
                        provider: p.name(),
                        kind: q.kind.as_str(),
                        rank: i + 1,
                        url: &h.url,
                        title: &h.title,
                        snippet: &h.snippet,
                        published: h.published.as_deref(),
                        retrieved_at: &found.retrieved_at,
                        cached,
                        domain: h.domain.as_deref(),
                        language: h.language.as_deref(),
                    });
                }
            }
        }
    }

    async fn resolve(&self, q: &Query, p: Provider) -> Outcome {
        let key = CacheKey {
            provider: p.name(),
            kind: q.kind,
            lang: &q.lang,
            count: q.count,
            query: &q.query,
        };
        if let Some(cache) = &self.cache {
            if let Some(entry) = cache.get(&key) {
                self.stats.cache_hit(p);
                return Ok(Found {
                    hits: Arc::new(entry.results),
                    retrieved_at: entry.fetched_at,
                    cached: true,
                });
            }
        }
        if p == Provider::Brave && self.settings.brave_key.is_none() {
            return Err(FetchError::NoKey);
        }
        let st = self
            .state
            .get(&p)
            .ok_or_else(|| FetchError::Internal("provider not initialised".into()))?;
        let _permit = st
            .semaphore
            .acquire()
            .await
            .map_err(|_| FetchError::Internal("semaphore closed".into()))?;
        let ctx = ProviderCtx {
            provider: p,
            settings: &self.settings,
            limiter: &st.limiter,
            stats: &self.stats,
        };
        let hits = providers::search(&self.client, &ctx, q).await?;
        let retrieved_at = now_iso();
        if let Some(cache) = &self.cache {
            if cache.put(&key, &retrieved_at, &hits).is_err() {
                self.stats.cache_write_error(p);
            }
        }
        Ok(Found {
            hits: Arc::new(hits),
            retrieved_at,
            cached: false,
        })
    }
}
