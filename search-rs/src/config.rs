//! Providers and environment-driven configuration.
//!
//! Secrets come only from the environment and are wrapped in [`ApiKey`],
//! whose `Debug` output is redacted.

use crate::model::Kind;
use std::fmt;
use std::str::FromStr;
use std::time::Duration;

pub const ENV_BRAVE_KEY: &str = "BRAVE_SEARCH_API_KEY";
pub const ENV_BRAVE_RPS: &str = "BRAVE_SEARCH_RPS";
pub const ENV_BRAVE_USD_PER_1000: &str = "BRAVE_SEARCH_USD_PER_1000";
pub const ENV_BRAVE_URL: &str = "SIGNALPOST_SEARCH_BRAVE_URL";
pub const ENV_GDELT_URL: &str = "SIGNALPOST_SEARCH_GDELT_URL";
pub const ENV_WIKIPEDIA_URL: &str = "SIGNALPOST_SEARCH_WIKIPEDIA_URL";
/// Test hooks. Honoured only for providers whose base URL points at a
/// loopback host, so they can never be used to hammer a real service.
pub const ENV_TEST_MIN_INTERVAL_MS: &str = "SIGNALPOST_SEARCH_TEST_MIN_INTERVAL_MS";
pub const ENV_TEST_BACKOFF_MS: &str = "SIGNALPOST_SEARCH_TEST_BACKOFF_MS";

pub const DEFAULT_BRAVE_URL: &str = "https://api.search.brave.com/res/v1";
pub const DEFAULT_GDELT_URL: &str = "https://api.gdeltproject.org/api/v2/doc/doc";
pub const DEFAULT_WIKIPEDIA_URL: &str = "https://{lang}.wikipedia.org/w/api.php";

/// GDELT asks for at most one request every 5 s; keep a safety margin.
pub const GDELT_MIN_INTERVAL: Duration = Duration::from_millis(5500);
/// Wikimedia: stay at or below 2 requests per second.
pub const WIKIPEDIA_MIN_INTERVAL: Duration = Duration::from_millis(500);
pub const DEFAULT_BACKOFF: Duration = Duration::from_millis(1000);
pub const MAX_ATTEMPTS: u32 = 3;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Provider {
    Brave,
    Gdelt,
    Wikipedia,
}

impl Provider {
    pub const ALL: [Provider; 3] = [Provider::Brave, Provider::Gdelt, Provider::Wikipedia];

    pub fn name(self) -> &'static str {
        match self {
            Provider::Brave => "brave",
            Provider::Gdelt => "gdelt",
            Provider::Wikipedia => "wikipedia",
        }
    }

    pub fn supports(self, kind: Kind) -> bool {
        match self {
            Provider::Brave => true,
            Provider::Gdelt => kind == Kind::News,
            Provider::Wikipedia => kind == Kind::Web,
        }
    }
}

impl fmt::Display for Provider {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.name())
    }
}

impl FromStr for Provider {
    type Err = String;
    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s.trim().to_ascii_lowercase().as_str() {
            "brave" => Ok(Provider::Brave),
            "gdelt" => Ok(Provider::Gdelt),
            "wikipedia" | "wiki" => Ok(Provider::Wikipedia),
            other => Err(format!(
                "unknown provider {other:?} (expected brave, gdelt, wikipedia)"
            )),
        }
    }
}

/// An API key. Never printed: `Debug` is redacted and there is no `Display`.
#[derive(Clone)]
pub struct ApiKey(String);

impl ApiKey {
    pub fn new(s: String) -> Option<Self> {
        let t = s.trim();
        if t.is_empty() {
            None
        } else {
            Some(Self(t.to_string()))
        }
    }

    pub fn expose(&self) -> &str {
        &self.0
    }
}

impl fmt::Debug for ApiKey {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("ApiKey(<redacted>)")
    }
}

/// Per-provider runtime settings.
#[derive(Debug, Clone)]
pub struct ProviderSettings {
    pub base_url: String,
    pub min_interval: Duration,
    pub backoff: Duration,
}

#[derive(Debug, Clone)]
pub struct Settings {
    pub brave_key: Option<ApiKey>,
    pub brave_usd_per_1000: f64,
    pub brave: ProviderSettings,
    pub gdelt: ProviderSettings,
    pub wikipedia: ProviderSettings,
}

impl Settings {
    pub fn for_provider(&self, p: Provider) -> &ProviderSettings {
        match p {
            Provider::Brave => &self.brave,
            Provider::Gdelt => &self.gdelt,
            Provider::Wikipedia => &self.wikipedia,
        }
    }

    /// Read settings from an environment lookup function (`std::env::var`
    /// in production, a map in tests).
    pub fn from_env_with(get: impl Fn(&str) -> Option<String>) -> Self {
        let brave_key = get(ENV_BRAVE_KEY).and_then(ApiKey::new);
        let brave_rps = get(ENV_BRAVE_RPS)
            .and_then(|v| v.trim().parse::<f64>().ok())
            .filter(|v| v.is_finite() && *v > 0.0)
            .unwrap_or(1.0);
        let brave_usd_per_1000 = get(ENV_BRAVE_USD_PER_1000)
            .and_then(|v| v.trim().parse::<f64>().ok())
            .filter(|v| v.is_finite() && *v >= 0.0)
            .unwrap_or(5.0);
        let test_interval = get(ENV_TEST_MIN_INTERVAL_MS)
            .and_then(|v| v.trim().parse::<u64>().ok())
            .map(Duration::from_millis);
        let test_backoff = get(ENV_TEST_BACKOFF_MS)
            .and_then(|v| v.trim().parse::<u64>().ok())
            .map(Duration::from_millis);

        let build = |env: &str, default_url: &str, interval: Duration, backoff: Duration| {
            let base_url = get(env)
                .map(|s| s.trim().trim_end_matches('/').to_string())
                .filter(|s| !s.is_empty())
                .unwrap_or_else(|| default_url.to_string());
            let local = is_loopback_url(&base_url);
            ProviderSettings {
                min_interval: if local {
                    test_interval.unwrap_or(interval)
                } else {
                    interval
                },
                backoff: if local {
                    test_backoff.unwrap_or(backoff)
                } else {
                    backoff
                },
                base_url,
            }
        };

        Settings {
            brave_key,
            brave_usd_per_1000,
            brave: build(
                ENV_BRAVE_URL,
                DEFAULT_BRAVE_URL,
                Duration::from_secs_f64(1.0 / brave_rps),
                DEFAULT_BACKOFF,
            ),
            // GDELT keeps answering 429 if retried quickly; back off from
            // its own interval rather than from one second.
            gdelt: build(
                ENV_GDELT_URL,
                DEFAULT_GDELT_URL,
                GDELT_MIN_INTERVAL,
                GDELT_MIN_INTERVAL,
            ),
            wikipedia: build(
                ENV_WIKIPEDIA_URL,
                DEFAULT_WIKIPEDIA_URL,
                WIKIPEDIA_MIN_INTERVAL,
                DEFAULT_BACKOFF,
            ),
        }
    }

    pub fn from_env() -> Self {
        Self::from_env_with(|k| std::env::var(k).ok())
    }
}

/// True when the URL's host is `localhost`, `127.0.0.1` or `[::1]`.
pub fn is_loopback_url(url: &str) -> bool {
    let rest = match url.split_once("://") {
        Some((_, r)) => r,
        None => return false,
    };
    let authority = rest.split(['/', '?', '#']).next().unwrap_or("");
    let host_port = authority.rsplit('@').next().unwrap_or("");
    let host = if let Some(stripped) = host_port.strip_prefix('[') {
        stripped.split(']').next().unwrap_or("")
    } else {
        host_port.split(':').next().unwrap_or("")
    };
    matches!(
        host.to_ascii_lowercase().as_str(),
        "localhost" | "127.0.0.1" | "::1"
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn env(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> {
        let m: HashMap<String, String> = pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect();
        move |k| m.get(k).cloned()
    }

    #[test]
    fn defaults() {
        let s = Settings::from_env_with(env(&[]));
        assert!(s.brave_key.is_none());
        assert_eq!(s.brave.base_url, DEFAULT_BRAVE_URL);
        assert_eq!(s.brave.min_interval, Duration::from_secs(1));
        assert_eq!(s.gdelt.min_interval, GDELT_MIN_INTERVAL);
        assert_eq!(s.wikipedia.min_interval, WIKIPEDIA_MIN_INTERVAL);
        assert_eq!(s.gdelt.backoff, GDELT_MIN_INTERVAL);
        assert_eq!(s.brave.backoff, DEFAULT_BACKOFF);
        assert_eq!(s.brave_usd_per_1000, 5.0);
    }

    #[test]
    fn blank_key_is_no_key_and_key_is_redacted() {
        let s = Settings::from_env_with(env(&[(ENV_BRAVE_KEY, "  ")]));
        assert!(s.brave_key.is_none());
        let s = Settings::from_env_with(env(&[(ENV_BRAVE_KEY, "sekret-123")]));
        assert_eq!(s.brave_key.as_ref().unwrap().expose(), "sekret-123");
        assert!(!format!("{s:?}").contains("sekret-123"));
    }

    #[test]
    fn test_hooks_only_apply_to_loopback() {
        let s = Settings::from_env_with(env(&[
            (ENV_TEST_MIN_INTERVAL_MS, "0"),
            (ENV_TEST_BACKOFF_MS, "1"),
            (ENV_GDELT_URL, "http://127.0.0.1:9999/gdelt"),
            (ENV_WIKIPEDIA_URL, "https://wiki.example.org/w/api.php"),
        ]));
        assert_eq!(s.gdelt.min_interval, Duration::ZERO);
        assert_eq!(s.gdelt.backoff, Duration::from_millis(1));
        assert_eq!(s.wikipedia.min_interval, WIKIPEDIA_MIN_INTERVAL);
        assert_eq!(s.brave.min_interval, Duration::from_secs(1));
        assert_eq!(s.brave.backoff, DEFAULT_BACKOFF);
    }

    #[test]
    fn brave_rps() {
        let s = Settings::from_env_with(env(&[(ENV_BRAVE_RPS, "4")]));
        assert_eq!(s.brave.min_interval, Duration::from_millis(250));
        let s = Settings::from_env_with(env(&[(ENV_BRAVE_RPS, "-1")]));
        assert_eq!(s.brave.min_interval, Duration::from_secs(1));
    }

    #[test]
    fn loopback_detection() {
        assert!(is_loopback_url("http://127.0.0.1:8080/x"));
        assert!(is_loopback_url("http://localhost/x"));
        assert!(is_loopback_url("http://[::1]:80/"));
        assert!(!is_loopback_url("https://api.gdeltproject.org/api"));
        assert!(!is_loopback_url("http://127.0.0.1.evil.com/"));
        assert!(!is_loopback_url("http://localhost@evil.com/"));
    }

    #[test]
    fn provider_parsing() {
        assert_eq!("Brave".parse::<Provider>().unwrap(), Provider::Brave);
        assert!("bing".parse::<Provider>().is_err());
        assert!(Provider::Gdelt.supports(Kind::News));
        assert!(!Provider::Gdelt.supports(Kind::Web));
        assert!(!Provider::Wikipedia.supports(Kind::News));
    }
}
