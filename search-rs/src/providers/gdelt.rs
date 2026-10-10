//! GDELT DOC 2.0 article list (news only, keyless).
//!
//! GDELT allows one request every 5 seconds. When exceeded, or when the query
//! is rejected, it answers HTTP 200 with a plain-text message instead of JSON.

use crate::http::{BodyVerdict, FetchError};
use crate::model::{Hit, Query};
use crate::text::{build_url, normalize_date, strip_html};
use serde_json::Value;

pub fn clamp_count(count: u32) -> u32 {
    count.clamp(1, 250)
}

pub fn build_request_url(endpoint: &str, q: &Query) -> String {
    build_url(
        endpoint,
        &[
            ("query", q.query.clone()),
            ("mode", "artlist".into()),
            ("format", "json".into()),
            ("maxrecords", clamp_count(q.count).to_string()),
            ("timespan", "12m".into()),
            ("sort", "datedesc".into()),
        ],
    )
}

pub fn check_body(body: &str) -> BodyVerdict {
    let t = body.trim_start();
    if t.starts_with('{') {
        return BodyVerdict::Ok;
    }
    // GDELT returns an empty body for some no-result queries.
    if t.is_empty() {
        return BodyVerdict::Ok;
    }
    let lower = t.to_ascii_lowercase();
    if lower.contains("limit requests")
        || lower.contains("one every 5 seconds")
        || lower.contains("rate limit")
    {
        return BodyVerdict::RateLimited;
    }
    let msg: String = t.chars().take(160).collect();
    BodyVerdict::Fail(FetchError::BadQuery(format!(
        "gdelt: {}",
        msg.split_whitespace().collect::<Vec<_>>().join(" ")
    )))
}

/// GDELT occasionally emits invalid JSON escapes (e.g. `\'`). Turn `\'`
/// into `'` and double any other backslash that does not start a valid JSON
/// escape.
pub fn repair_json(body: &str) -> String {
    let mut out = String::with_capacity(body.len() + 8);
    let mut chars = body.chars().peekable();
    while let Some(c) = chars.next() {
        if c == '\\' {
            match chars.peek() {
                Some('"' | '\\' | '/' | 'b' | 'f' | 'n' | 'r' | 't' | 'u') => {
                    out.push('\\');
                    out.push(chars.next().unwrap_or('\\'));
                }
                Some('\'') => {
                    out.push('\'');
                    chars.next();
                }
                _ => out.push_str("\\\\"),
            }
        } else {
            out.push(c);
        }
    }
    out
}

pub fn parse(body: &str) -> Result<Vec<Hit>, FetchError> {
    if body.trim().is_empty() {
        return Ok(Vec::new());
    }
    let v: Value = match serde_json::from_str(body) {
        Ok(v) => v,
        Err(_) => serde_json::from_str(&repair_json(body))
            .map_err(|e| FetchError::BadResponse(format!("gdelt: invalid JSON: {e}")))?,
    };
    let Some(arr) = v.get("articles").and_then(Value::as_array) else {
        return Ok(Vec::new());
    };
    Ok(arr
        .iter()
        .filter_map(|a| {
            let url = a.get("url")?.as_str()?.trim();
            if url.is_empty() {
                return None;
            }
            let s = |k: &str| a.get(k).and_then(Value::as_str).unwrap_or("");
            let opt = |k: &str| {
                a.get(k)
                    .and_then(Value::as_str)
                    .map(str::trim)
                    .filter(|v| !v.is_empty())
                    .map(str::to_string)
            };
            Some(Hit {
                url: url.to_string(),
                title: strip_html(s("title")),
                snippet: String::new(),
                published: normalize_date(s("seendate")),
                domain: opt("domain"),
                language: opt("language"),
            })
        })
        .collect())
}
