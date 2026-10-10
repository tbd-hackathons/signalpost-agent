//! Input queries, provider-neutral hits, and the output line shapes.

use serde::{Deserialize, Serialize};
use serde_json::Value;

/// Default number of results per query when the input omits `count`.
pub const DEFAULT_COUNT: u32 = 10;
/// Upper bound on `count`; providers clamp further to their own limits.
pub const MAX_COUNT: u32 = 50;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Kind {
    Web,
    News,
}

impl Kind {
    pub fn as_str(self) -> &'static str {
        match self {
            Kind::Web => "web",
            Kind::News => "news",
        }
    }
}

/// A validated input query.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Query {
    pub id: String,
    pub query: String,
    pub kind: Kind,
    pub count: u32,
    pub lang: String,
}

/// Why an input line was rejected. `id` is kept when it could be read.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BadInput {
    pub id: Option<String>,
    pub detail: String,
}

fn bad(id: Option<String>, detail: impl Into<String>) -> BadInput {
    BadInput {
        id,
        detail: detail.into(),
    }
}

/// Parse and validate one input line.
pub fn parse_query_line(line: &str) -> Result<Query, BadInput> {
    let v: Value = serde_json::from_str(line).map_err(|_| bad(None, "invalid JSON"))?;
    let obj = v
        .as_object()
        .ok_or_else(|| bad(None, "line is not a JSON object"))?;

    let id = match obj.get("id") {
        Some(Value::String(s)) if !s.trim().is_empty() => s.clone(),
        Some(Value::Number(n)) => n.to_string(),
        _ => return Err(bad(None, "missing or empty \"id\"")),
    };

    let query = match obj.get("query") {
        Some(Value::String(s)) if !s.trim().is_empty() => s.trim().to_string(),
        _ => return Err(bad(Some(id), "missing or empty \"query\"")),
    };

    let kind = match obj.get("kind") {
        None | Some(Value::Null) => Kind::Web,
        Some(Value::String(s)) => match s.to_ascii_lowercase().as_str() {
            "web" => Kind::Web,
            "news" => Kind::News,
            _ => return Err(bad(Some(id), "\"kind\" must be \"web\" or \"news\"")),
        },
        Some(_) => return Err(bad(Some(id), "\"kind\" must be a string")),
    };

    let count = match obj.get("count") {
        None | Some(Value::Null) => DEFAULT_COUNT,
        Some(Value::Number(n)) => match n.as_u64() {
            Some(c) if c >= 1 => c.min(MAX_COUNT as u64) as u32,
            _ => return Err(bad(Some(id), "\"count\" must be a positive integer")),
        },
        Some(_) => return Err(bad(Some(id), "\"count\" must be a positive integer")),
    };

    let lang = match obj.get("lang") {
        None | Some(Value::Null) => "no".to_string(),
        Some(Value::String(s))
            if (2..=3).contains(&s.len()) && s.chars().all(|c| c.is_ascii_alphabetic()) =>
        {
            s.to_ascii_lowercase()
        }
        Some(_) => return Err(bad(Some(id), "\"lang\" must be a 2-3 letter code")),
    };

    Ok(Query {
        id,
        query,
        kind,
        count,
        lang,
    })
}

/// One provider-neutral search hit.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Hit {
    pub url: String,
    pub title: String,
    pub snippet: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub published: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub domain: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub language: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct ResultLine<'a> {
    pub id: &'a str,
    pub provider: &'a str,
    pub kind: &'a str,
    pub rank: usize,
    pub url: &'a str,
    pub title: &'a str,
    pub snippet: &'a str,
    pub published: Option<&'a str>,
    pub retrieved_at: &'a str,
    pub cached: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub domain: Option<&'a str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub language: Option<&'a str>,
}

#[derive(Debug, Serialize)]
pub struct EmptyLine<'a> {
    pub id: &'a str,
    pub provider: &'a str,
    pub kind: &'a str,
    pub empty: bool,
    pub retrieved_at: &'a str,
    pub cached: bool,
}

#[derive(Debug, Serialize)]
pub struct ErrorLine<'a> {
    pub id: &'a str,
    pub provider: &'a str,
    pub error: &'a str,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub detail: Option<&'a str>,
}

#[derive(Debug, Serialize)]
pub struct BadInputLine<'a> {
    pub id: Option<&'a str>,
    pub error: &'static str,
    pub line: usize,
    pub detail: &'a str,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_full_line() {
        let q = parse_query_line(
            r#"{"id":"912345678:web:1","query":"\"Vinsnesbakk Elektro\" Sandnes","kind":"web","count":10,"lang":"no"}"#,
        )
        .unwrap();
        assert_eq!(q.id, "912345678:web:1");
        assert_eq!(q.query, "\"Vinsnesbakk Elektro\" Sandnes");
        assert_eq!(q.kind, Kind::Web);
        assert_eq!(q.count, 10);
        assert_eq!(q.lang, "no");
    }

    #[test]
    fn applies_defaults_and_clamps() {
        let q = parse_query_line(r#"{"id":"a","query":"x","count":500,"lang":"EN"}"#).unwrap();
        assert_eq!(q.kind, Kind::Web);
        assert_eq!(q.count, MAX_COUNT);
        assert_eq!(q.lang, "en");
        let q = parse_query_line(r#"{"id":7,"query":"x","kind":"NEWS"}"#).unwrap();
        assert_eq!(q.id, "7");
        assert_eq!(q.kind, Kind::News);
        assert_eq!(q.count, DEFAULT_COUNT);
    }

    #[test]
    fn rejects_bad_lines() {
        assert_eq!(parse_query_line("not json").unwrap_err().id, None);
        assert_eq!(parse_query_line("[1,2]").unwrap_err().id, None);
        assert_eq!(parse_query_line(r#"{"query":"x"}"#).unwrap_err().id, None);
        let e = parse_query_line(r#"{"id":"a","query":"  "}"#).unwrap_err();
        assert_eq!(e.id.as_deref(), Some("a"));
        assert!(parse_query_line(r#"{"id":"a","query":"x","kind":"images"}"#).is_err());
        assert!(parse_query_line(r#"{"id":"a","query":"x","count":0}"#).is_err());
        assert!(parse_query_line(r#"{"id":"a","query":"x","lang":"norsk!"}"#).is_err());
    }
}
