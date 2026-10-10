//! HTTP GET with rate limiting, bounded retries, exponential backoff with
//! jitter, and `Retry-After` support.

use crate::config::{Provider, MAX_ATTEMPTS};
use crate::ratelimit::RateLimiter;
use crate::stats::Stats;
use reqwest::header::{HeaderMap, HeaderValue, ACCEPT, RETRY_AFTER};
use std::collections::hash_map::RandomState;
use std::hash::{BuildHasher, Hasher};
use std::time::{Duration, SystemTime};
use thiserror::Error;

/// The longest `Retry-After` we are willing to sleep for inside one query.
/// Longer requests are reported as `rate_limited` instead of stalling the run.
pub const MAX_RETRY_AFTER: Duration = Duration::from_secs(60);

/// Error codes written to the `error` field of an output line.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum FetchError {
    #[error("rate_limited")]
    RateLimited,
    #[error("http_{0}")]
    Http(u16),
    #[error("timeout")]
    Timeout,
    #[error("network")]
    Network,
    #[error("bad_response")]
    BadResponse(String),
    #[error("bad_query")]
    BadQuery(String),
    #[error("no_key")]
    NoKey,
    #[error("unsupported_kind")]
    UnsupportedKind,
    #[error("internal")]
    Internal(String),
}

impl FetchError {
    pub fn code(&self) -> String {
        self.to_string()
    }

    /// Human-readable detail safe to emit (never contains secrets or URLs).
    pub fn detail(&self) -> Option<&str> {
        match self {
            FetchError::BadResponse(d) | FetchError::BadQuery(d) | FetchError::Internal(d) => {
                Some(d)
            }
            _ => None,
        }
    }
}

/// What a provider says about a 2xx body before it is parsed.
#[derive(Debug, PartialEq, Eq)]
pub enum BodyVerdict {
    Ok,
    /// The provider answered 200 but with a "slow down" message.
    RateLimited,
    Fail(FetchError),
}

pub struct Request<'a> {
    pub provider: Provider,
    pub url: &'a str,
    pub headers: HeaderMap,
    pub limiter: &'a RateLimiter,
    pub backoff: Duration,
    pub stats: &'a Stats,
    pub check_body: fn(&str) -> BodyVerdict,
}

enum Attempt {
    Done(String),
    Retry(FetchError, Option<Duration>),
    Fail(FetchError),
}

pub fn accept_json() -> HeaderMap {
    let mut h = HeaderMap::new();
    h.insert(ACCEPT, HeaderValue::from_static("application/json"));
    h
}

/// GET `req.url`, retrying 429, 5xx, timeouts and connection errors up to
/// [`MAX_ATTEMPTS`] times. Every attempt waits for the provider's limiter.
pub async fn get_text(client: &reqwest::Client, req: Request<'_>) -> Result<String, FetchError> {
    let mut last = FetchError::Network;
    for attempt in 1..=MAX_ATTEMPTS {
        if !req.limiter.acquire().await {
            // Provider asked us to stay away longer than MAX_RETRY_AFTER.
            return Err(FetchError::RateLimited);
        }
        req.stats.request(req.provider);
        match one_attempt(client, &req).await {
            Attempt::Done(body) => {
                req.stats.success_response(req.provider);
                return Ok(body);
            }
            Attempt::Fail(e) => return Err(e),
            Attempt::Retry(e, retry_after) => {
                if let Some(ra) = retry_after {
                    if ra > MAX_RETRY_AFTER {
                        req.limiter.trip(ra);
                        return Err(e);
                    }
                }
                if attempt == MAX_ATTEMPTS {
                    if e == FetchError::RateLimited {
                        // Let the next queries for this provider breathe too.
                        req.limiter.penalize(backoff_delay(req.backoff, attempt));
                    }
                    return Err(e);
                }
                let wait = retry_after.unwrap_or_else(|| backoff_delay(req.backoff, attempt));
                if e == FetchError::RateLimited {
                    req.limiter.penalize(wait);
                }
                req.stats.retry(req.provider);
                tokio::time::sleep(wait).await;
                last = e;
            }
        }
    }
    Err(last)
}

async fn one_attempt(client: &reqwest::Client, req: &Request<'_>) -> Attempt {
    let resp = match client
        .get(req.url)
        .headers(req.headers.clone())
        .send()
        .await
    {
        Ok(r) => r,
        Err(e) => return Attempt::Retry(classify_reqwest(&e), None),
    };
    let status = resp.status();
    let retry_after = resp
        .headers()
        .get(RETRY_AFTER)
        .and_then(|v| v.to_str().ok())
        .and_then(parse_retry_after);
    let body = match resp.text().await {
        Ok(b) => b,
        Err(e) => return Attempt::Retry(classify_reqwest(&e), None),
    };
    let code = status.as_u16();
    if code == 429 {
        return Attempt::Retry(FetchError::RateLimited, retry_after);
    }
    if status.is_server_error() {
        return Attempt::Retry(FetchError::Http(code), retry_after);
    }
    if !status.is_success() {
        return Attempt::Fail(FetchError::Http(code));
    }
    match (req.check_body)(&body) {
        BodyVerdict::Ok => Attempt::Done(body),
        BodyVerdict::RateLimited => Attempt::Retry(FetchError::RateLimited, retry_after),
        BodyVerdict::Fail(e) => Attempt::Fail(e),
    }
}

fn classify_reqwest(e: &reqwest::Error) -> FetchError {
    if e.is_timeout() {
        FetchError::Timeout
    } else {
        FetchError::Network
    }
}

/// Parse `Retry-After` as delta-seconds or an HTTP date.
pub fn parse_retry_after(v: &str) -> Option<Duration> {
    let v = v.trim();
    if let Ok(secs) = v.parse::<u64>() {
        return Some(Duration::from_secs(secs));
    }
    let when = httpdate::parse_http_date(v).ok()?;
    Some(
        when.duration_since(SystemTime::now())
            .unwrap_or(Duration::ZERO),
    )
}

/// `base * 2^(attempt-1)` plus up to `base` of random jitter.
pub fn backoff_delay(base: Duration, attempt: u32) -> Duration {
    let exp = base.saturating_mul(1u32 << (attempt.saturating_sub(1)).min(10));
    let base_ms = base.as_millis() as u64;
    let jitter = if base_ms == 0 {
        0
    } else {
        random_u64() % (base_ms + 1)
    };
    exp + Duration::from_millis(jitter)
}

fn random_u64() -> u64 {
    // RandomState is seeded from OS randomness per instance; good enough for
    // jitter without pulling in a RNG crate.
    let mut h = RandomState::new().build_hasher();
    h.write_u128(
        SystemTime::now()
            .duration_since(SystemTime::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0),
    );
    h.finish()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn retry_after_forms() {
        assert_eq!(parse_retry_after("7"), Some(Duration::from_secs(7)));
        assert_eq!(parse_retry_after(" 0 "), Some(Duration::ZERO));
        assert_eq!(
            parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT"),
            Some(Duration::ZERO)
        );
        let future = httpdate::fmt_http_date(SystemTime::now() + Duration::from_secs(30));
        let d = parse_retry_after(&future).unwrap();
        assert!(d > Duration::from_secs(25) && d <= Duration::from_secs(30));
        assert_eq!(parse_retry_after("soon"), None);
    }

    #[test]
    fn backoff_grows_with_bounded_jitter() {
        let base = Duration::from_millis(100);
        for attempt in 1..=3 {
            let d = backoff_delay(base, attempt);
            let floor = base * (1 << (attempt - 1));
            assert!(d >= floor && d <= floor + base, "attempt {attempt}: {d:?}");
        }
        assert_eq!(backoff_delay(Duration::ZERO, 2), Duration::ZERO);
    }

    #[test]
    fn error_codes() {
        assert_eq!(FetchError::Http(500).code(), "http_500");
        assert_eq!(FetchError::RateLimited.code(), "rate_limited");
        assert_eq!(FetchError::NoKey.code(), "no_key");
        assert_eq!(FetchError::BadQuery("x".into()).detail(), Some("x"));
    }
}
