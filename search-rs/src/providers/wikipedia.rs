//! MediaWiki full-text search on no.wikipedia.org / en.wikipedia.org
//! (web only, keyless).

use crate::http::{BodyVerdict, FetchError};
use crate::model::{Hit, Query};
use crate::text::{build_url, encode_wiki_title, strip_html};
use serde_json::Value;

pub fn clamp_count(count: u32) -> u32 {
    count.clamp(1, 50)
}

/// `en` for English, Norwegian (`no`) for everything else.
pub fn wiki_lang(lang: &str) -> &'static str {
    if lang == "en" {
        "en"
    } else {
        "no"
    }
}

/// API endpoint for a language: `{lang}` in the template is substituted.
pub fn endpoint(template: &str, lang: &str) -> String {
    template.replace("{lang}", wiki_lang(lang))
}

/// Article root derived from the endpoint: `https://no.wikipedia.org/w/api.php`
/// gives `https://no.wikipedia.org`.
pub fn site_root(endpoint: &str) -> &str {
    endpoint
        .strip_suffix("/w/api.php")
        .or_else(|| endpoint.strip_suffix("/api.php"))
        .unwrap_or(endpoint)
        .trim_end_matches('/')
}

pub fn build_request_url(template: &str, q: &Query) -> String {
    build_url(
        &endpoint(template, &q.lang),
        &[
            ("action", "query".into()),
            ("list", "search".into()),
            ("srsearch", q.query.clone()),
            ("format", "json".into()),
            ("formatversion", "2".into()),
            ("srlimit", clamp_count(q.count).to_string()),
            ("srprop", "snippet".into()),
        ],
    )
}

pub fn check_body(body: &str) -> BodyVerdict {
    if body.trim_start().starts_with('{') {
        BodyVerdict::Ok
    } else {
        BodyVerdict::Fail(FetchError::BadResponse("wikipedia: non-JSON body".into()))
    }
}

pub fn parse(body: &str, site_root: &str) -> Result<Vec<Hit>, FetchError> {
    let v: Value = serde_json::from_str(body)
        .map_err(|e| FetchError::BadResponse(format!("wikipedia: invalid JSON: {e}")))?;
    if let Some(err) = v.get("error") {
        let code = err.get("code").and_then(Value::as_str).unwrap_or("unknown");
        return Err(FetchError::BadQuery(format!("wikipedia: {code}")));
    }
    let Some(arr) = v
        .get("query")
        .and_then(|q| q.get("search"))
        .and_then(Value::as_array)
    else {
        return Ok(Vec::new());
    };
    Ok(arr
        .iter()
        .filter_map(|r| {
            let title = r.get("title")?.as_str()?.trim();
            if title.is_empty() {
                return None;
            }
            let snippet = r.get("snippet").and_then(Value::as_str).unwrap_or("");
            Some(Hit {
                url: format!("{site_root}/wiki/{}", encode_wiki_title(title)),
                title: title.to_string(),
                snippet: strip_html(snippet),
                published: None,
                domain: None,
                language: None,
            })
        })
        .collect())
}
