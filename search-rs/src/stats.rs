//! Run counters, printed as one JSON line on stderr at the end of a run.

use crate::config::Provider;
use serde_json::{json, Map, Value};
use std::collections::BTreeMap;
use std::sync::Mutex;

#[derive(Debug, Default, Clone, Copy)]
pub struct ProviderCounters {
    pub queries: u64,
    pub requests: u64,
    pub retries: u64,
    pub success_responses: u64,
    pub cache_hits: u64,
    pub deduped: u64,
    pub results: u64,
    pub empty: u64,
    pub errors: u64,
    pub skipped: u64,
    pub cache_write_errors: u64,
}

#[derive(Debug, Default)]
pub struct Stats {
    inner: Mutex<Inner>,
}

#[derive(Debug, Default)]
struct Inner {
    queries: u64,
    bad_input: u64,
    by_provider: BTreeMap<Provider, ProviderCounters>,
}

impl Stats {
    fn with<F: FnOnce(&mut ProviderCounters)>(&self, p: Provider, f: F) {
        let mut g = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        f(g.by_provider.entry(p).or_default());
    }

    pub fn query(&self) {
        self.inner.lock().unwrap_or_else(|e| e.into_inner()).queries += 1;
    }
    pub fn bad_input(&self) {
        self.inner
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .bad_input += 1;
    }
    pub fn provider_query(&self, p: Provider) {
        self.with(p, |c| c.queries += 1);
    }
    pub fn request(&self, p: Provider) {
        self.with(p, |c| c.requests += 1);
    }
    pub fn retry(&self, p: Provider) {
        self.with(p, |c| c.retries += 1);
    }
    pub fn success_response(&self, p: Provider) {
        self.with(p, |c| c.success_responses += 1);
    }
    pub fn cache_hit(&self, p: Provider) {
        self.with(p, |c| c.cache_hits += 1);
    }
    pub fn deduped(&self, p: Provider) {
        self.with(p, |c| c.deduped += 1);
    }
    pub fn results(&self, p: Provider, n: usize) {
        self.with(p, |c| c.results += n as u64);
    }
    pub fn empty(&self, p: Provider) {
        self.with(p, |c| c.empty += 1);
    }
    pub fn error(&self, p: Provider) {
        self.with(p, |c| c.errors += 1);
    }
    pub fn skipped(&self, p: Provider) {
        self.with(p, |c| c.skipped += 1);
    }
    pub fn cache_write_error(&self, p: Provider) {
        self.with(p, |c| c.cache_write_errors += 1);
    }

    pub fn counters(&self, p: Provider) -> ProviderCounters {
        let g = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        g.by_provider.get(&p).copied().unwrap_or_default()
    }

    /// The `{"stats": {...}}` object. Brave cost counts successful (2xx)
    /// responses only; cache hits and failed requests are free.
    pub fn to_json(&self, brave_usd_per_1000: f64, elapsed_secs: f64) -> Value {
        let g = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        let mut by = Map::new();
        let (mut requests, mut cache_hits, mut errors) = (0, 0, 0);
        for (p, c) in &g.by_provider {
            requests += c.requests;
            cache_hits += c.cache_hits;
            errors += c.errors;
            by.insert(
                p.name().to_string(),
                json!({
                    "queries": c.queries,
                    "requests": c.requests,
                    "retries": c.retries,
                    "cache_hits": c.cache_hits,
                    "deduped": c.deduped,
                    "results": c.results,
                    "empty": c.empty,
                    "errors": c.errors,
                    "skipped": c.skipped,
                    "cache_write_errors": c.cache_write_errors,
                }),
            );
        }
        let brave_billable = g
            .by_provider
            .get(&Provider::Brave)
            .map(|c| c.success_responses)
            .unwrap_or(0);
        let cost =
            (brave_billable as f64 * brave_usd_per_1000 / 1000.0 * 10_000.0).round() / 10_000.0;
        json!({
            "stats": {
                "queries": g.queries,
                "bad_input": g.bad_input,
                "requests": requests,
                "cache_hits": cache_hits,
                "errors": errors,
                "by_provider": by,
                "estimated_cost_usd": cost,
                "elapsed_secs": (elapsed_secs * 100.0).round() / 100.0,
            }
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn totals_and_cost() {
        let s = Stats::default();
        s.query();
        s.query();
        s.bad_input();
        for _ in 0..3 {
            s.request(Provider::Brave);
        }
        s.success_response(Provider::Brave);
        s.success_response(Provider::Brave);
        s.cache_hit(Provider::Wikipedia);
        s.error(Provider::Gdelt);
        let v = s.to_json(5.0, 1.234);
        let st = &v["stats"];
        assert_eq!(st["queries"], 2);
        assert_eq!(st["bad_input"], 1);
        assert_eq!(st["requests"], 3);
        assert_eq!(st["cache_hits"], 1);
        assert_eq!(st["errors"], 1);
        assert_eq!(st["estimated_cost_usd"], 0.01);
        assert_eq!(st["by_provider"]["brave"]["requests"], 3);
    }
}
