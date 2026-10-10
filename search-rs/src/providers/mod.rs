//! Provider request building, response parsing, and dispatch.

pub mod brave;
pub mod gdelt;
pub mod wikipedia;

use crate::config::{Provider, Settings};
use crate::http::{self, FetchError, Request};
use crate::model::{Hit, Query};
use crate::ratelimit::RateLimiter;
use crate::stats::Stats;
use reqwest::header::HeaderValue;

/// Shared state for one provider.
pub struct ProviderCtx<'a> {
    pub provider: Provider,
    pub settings: &'a Settings,
    pub limiter: &'a RateLimiter,
    pub stats: &'a Stats,
}

/// Run one live search (no cache) and return its hits, truncated to `count`.
pub async fn search(
    client: &reqwest::Client,
    ctx: &ProviderCtx<'_>,
    q: &Query,
) -> Result<Vec<Hit>, FetchError> {
    let ps = ctx.settings.for_provider(ctx.provider);
    let mut headers = http::accept_json();
    let (url, check_body): (String, fn(&str) -> http::BodyVerdict) = match ctx.provider {
        Provider::Brave => {
            let key = ctx.settings.brave_key.as_ref().ok_or(FetchError::NoKey)?;
            let mut v = HeaderValue::from_str(key.expose())
                .map_err(|_| FetchError::Internal("brave key is not a valid header".into()))?;
            v.set_sensitive(true);
            headers.insert("X-Subscription-Token", v);
            (brave::build_request_url(&ps.base_url, q), brave::check_body)
        }
        Provider::Gdelt => (gdelt::build_request_url(&ps.base_url, q), gdelt::check_body),
        Provider::Wikipedia => (
            wikipedia::build_request_url(&ps.base_url, q),
            wikipedia::check_body,
        ),
    };
    let body = http::get_text(
        client,
        Request {
            provider: ctx.provider,
            url: &url,
            headers,
            limiter: ctx.limiter,
            backoff: ps.backoff,
            stats: ctx.stats,
            check_body,
        },
    )
    .await?;
    let mut hits = match ctx.provider {
        Provider::Brave => brave::parse(q.kind, &body)?,
        Provider::Gdelt => gdelt::parse(&body)?,
        Provider::Wikipedia => {
            let ep = wikipedia::endpoint(&ps.base_url, &q.lang);
            wikipedia::parse(&body, wikipedia::site_root(&ep))?
        }
    };
    hits.truncate(q.count as usize);
    Ok(hits)
}
