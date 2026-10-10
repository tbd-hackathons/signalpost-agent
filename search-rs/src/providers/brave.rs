//! Brave Search API (web and news). Needs `BRAVE_SEARCH_API_KEY`.

use crate::http::{BodyVerdict, FetchError};
use crate::model::{Hit, Kind, Query};
use crate::text::{build_url, normalize_date, strip_html};
use serde_json::Value;

/// Brave caps `count` at 20 for web and 50 for news.
pub fn clamp_count(kind: Kind, count: u32) -> u32 {
    match kind {
        Kind::Web => count.clamp(1, 20),
        Kind::News => count.clamp(1, 50),
    }
}

/// Brave's `search_lang` uses `nb` for Norwegian Bokmål.
pub fn search_lang(lang: &str) -> &str {
    match lang {
        "no" | "nb" | "nn" => "nb",
        other => other,
    }
}

pub fn build_request_url(base: &str, q: &Query) -> String {
    let path = match q.kind {
        Kind::Web => "/web/search",
        Kind::News => "/news/search",
    };
    let mut params = vec![
        ("q", q.query.clone()),
        ("count", clamp_count(q.kind, q.count).to_string()),
        ("country", "NO".to_string()),
        ("search_lang", search_lang(&q.lang).to_string()),
    ];
    if q.kind == Kind::Web {
        params.push(("result_filter", "web".to_string()));
        params.push(("text_decorations", "false".to_string()));
    }
    build_url(&format!("{base}{path}"), &params)
}

pub fn check_body(body: &str) -> BodyVerdict {
    if body.trim_start().starts_with('{') {
        BodyVerdict::Ok
    } else {
        BodyVerdict::Fail(FetchError::BadResponse("brave: non-JSON body".into()))
    }
}

/// Parse a Brave web (`web.results[]`) or news (`results[]`) response.
pub fn parse(kind: Kind, body: &str) -> Result<Vec<Hit>, FetchError> {
    let v: Value = serde_json::from_str(body)
        .map_err(|e| FetchError::BadResponse(format!("brave: invalid JSON: {e}")))?;
    let results = match kind {
        Kind::Web => v.get("web").and_then(|w| w.get("results")),
        Kind::News => v.get("results"),
    };
    let Some(arr) = results.and_then(Value::as_array) else {
        return Ok(Vec::new());
    };
    Ok(arr
        .iter()
        .filter_map(|r| {
            let url = r.get("url")?.as_str()?.trim();
            if url.is_empty() {
                return None;
            }
            let s = |k: &str| r.get(k).and_then(Value::as_str).unwrap_or("");
            let published = [s("page_age"), s("age")]
                .iter()
                .find_map(|d| normalize_date(d));
            let domain = r
                .get("meta_url")
                .and_then(|m| m.get("hostname"))
                .and_then(Value::as_str)
                .map(str::to_string);
            Some(Hit {
                url: url.to_string(),
                title: strip_html(s("title")),
                snippet: strip_html(s("description")),
                published,
                domain,
                language: r
                    .get("language")
                    .and_then(Value::as_str)
                    .map(str::to_string),
            })
        })
        .collect())
}
