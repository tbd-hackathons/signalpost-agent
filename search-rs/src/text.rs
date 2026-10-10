//! Small text helpers: HTML stripping, URL encoding, date normalisation.

use chrono::{DateTime, NaiveDate, NaiveDateTime, Utc};
use percent_encoding::{utf8_percent_encode, AsciiSet, NON_ALPHANUMERIC};

/// RFC 3986 query component encoding: everything except unreserved characters
/// (`A-Z a-z 0-9 - . _ ~`) is percent-encoded, so spaces become `%20` and
/// double quotes become `%22`.
const QUERY_VALUE: &AsciiSet = &NON_ALPHANUMERIC
    .remove(b'-')
    .remove(b'.')
    .remove(b'_')
    .remove(b'~');

/// Encoding for a MediaWiki page title in a `/wiki/<title>` path.
const WIKI_TITLE: &AsciiSet = &NON_ALPHANUMERIC
    .remove(b'-')
    .remove(b'.')
    .remove(b'_')
    .remove(b'~')
    .remove(b'(')
    .remove(b')')
    .remove(b',')
    .remove(b':')
    .remove(b'!')
    .remove(b'*')
    .remove(b'\'');

pub fn encode_query_value(s: &str) -> String {
    utf8_percent_encode(s, QUERY_VALUE).to_string()
}

/// Build `base?k1=v1&k2=v2` with every value percent-encoded.
pub fn build_url(base: &str, params: &[(&str, String)]) -> String {
    let mut out = String::with_capacity(base.len() + 64);
    out.push_str(base);
    let mut sep = if base.contains('?') { '&' } else { '?' };
    for (k, v) in params {
        out.push(sep);
        out.push_str(k);
        out.push('=');
        out.push_str(&encode_query_value(v));
        sep = '&';
    }
    out
}

pub fn encode_wiki_title(title: &str) -> String {
    utf8_percent_encode(&title.replace(' ', "_"), WIKI_TITLE).to_string()
}

/// Remove HTML tags, decode the common entities, and collapse whitespace.
pub fn strip_html(input: &str) -> String {
    let mut no_tags = String::with_capacity(input.len());
    let mut in_tag = false;
    for c in input.chars() {
        match c {
            '<' => in_tag = true,
            '>' if in_tag => in_tag = false,
            _ if !in_tag => no_tags.push(c),
            _ => {}
        }
    }
    let decoded = decode_entities(&no_tags);
    decoded.split_whitespace().collect::<Vec<_>>().join(" ")
}

fn decode_entities(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut rest = s;
    while let Some(pos) = rest.find('&') {
        out.push_str(&rest[..pos]);
        let after = &rest[pos..];
        match after.find(';') {
            Some(end) if end <= 10 => {
                let entity = &after[1..end];
                if let Some(c) = decode_entity(entity) {
                    out.push(c);
                    rest = &after[end + 1..];
                    continue;
                }
                out.push('&');
                rest = &after[1..];
            }
            _ => {
                out.push('&');
                rest = &after[1..];
            }
        }
    }
    out.push_str(rest);
    out
}

fn decode_entity(e: &str) -> Option<char> {
    match e {
        "amp" => Some('&'),
        "lt" => Some('<'),
        "gt" => Some('>'),
        "quot" => Some('"'),
        "apos" => Some('\''),
        "nbsp" => Some(' '),
        "ndash" => Some('\u{2013}'),
        "mdash" => Some('\u{2014}'),
        "hellip" => Some('\u{2026}'),
        "laquo" => Some('\u{ab}'),
        "raquo" => Some('\u{bb}'),
        "aelig" => Some('æ'),
        "oslash" => Some('ø'),
        "aring" => Some('å'),
        "AElig" => Some('Æ'),
        "Oslash" => Some('Ø'),
        "Aring" => Some('Å'),
        _ => {
            let num = e.strip_prefix('#')?;
            let code = if let Some(hex) = num.strip_prefix('x').or_else(|| num.strip_prefix('X')) {
                u32::from_str_radix(hex, 16).ok()?
            } else {
                num.parse::<u32>().ok()?
            };
            char::from_u32(code)
        }
    }
}

/// Normalise a provider date to ISO 8601. Returns `None` for relative or
/// unparseable values such as "2 days ago" rather than guessing.
pub fn normalize_date(raw: &str) -> Option<String> {
    let s = raw.trim();
    if s.is_empty() {
        return None;
    }
    if let Ok(dt) = DateTime::parse_from_rfc3339(s) {
        return Some(
            dt.with_timezone(&Utc)
                .to_rfc3339_opts(chrono::SecondsFormat::Secs, true),
        );
    }
    for fmt in [
        "%Y-%m-%dT%H:%M:%S%.f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
    ] {
        if let Ok(ndt) = NaiveDateTime::parse_from_str(s, fmt) {
            return Some(
                ndt.and_utc()
                    .to_rfc3339_opts(chrono::SecondsFormat::Secs, true),
            );
        }
    }
    // GDELT seendate: 20261001T120000Z
    if let Ok(ndt) = NaiveDateTime::parse_from_str(s, "%Y%m%dT%H%M%SZ") {
        return Some(
            ndt.and_utc()
                .to_rfc3339_opts(chrono::SecondsFormat::Secs, true),
        );
    }
    for fmt in ["%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%d %B %Y"] {
        if let Ok(d) = NaiveDate::parse_from_str(s, fmt) {
            return Some(d.format("%Y-%m-%d").to_string());
        }
    }
    None
}

/// Current time as ISO 8601 UTC with second precision.
pub fn now_iso() -> String {
    Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, true)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn encodes_quotes_and_spaces() {
        assert_eq!(
            encode_query_value("\"Vinsnesbakk Elektro\" Sandnes"),
            "%22Vinsnesbakk%20Elektro%22%20Sandnes"
        );
        assert_eq!(
            encode_query_value("Ålesund & co"),
            "%C3%85lesund%20%26%20co"
        );
        assert_eq!(
            build_url("https://x/y", &[("q", "a b".into()), ("n", "3".into())]),
            "https://x/y?q=a%20b&n=3"
        );
    }

    #[test]
    fn wiki_titles() {
        assert_eq!(
            encode_wiki_title("Hannestad barnehage"),
            "Hannestad_barnehage"
        );
        assert_eq!(encode_wiki_title("Tromsø (by)"), "Troms%C3%B8_(by)");
    }

    #[test]
    fn strips_html() {
        assert_eq!(
            strip_html("<span class=\"searchmatch\">Equinor</span> ASA &amp; co&#39;s  <b>x</b>"),
            "Equinor ASA & co's x"
        );
        assert_eq!(strip_html("a &unknown; b & c"), "a &unknown; b & c");
    }

    #[test]
    fn normalizes_dates() {
        assert_eq!(
            normalize_date("20261001T120000Z").unwrap(),
            "2026-10-01T12:00:00Z"
        );
        assert_eq!(
            normalize_date("2024-03-01T12:00:00").unwrap(),
            "2024-03-01T12:00:00Z"
        );
        assert_eq!(
            normalize_date("2024-03-01T12:00:00+02:00").unwrap(),
            "2024-03-01T10:00:00Z"
        );
        assert_eq!(normalize_date("March 1, 2024").unwrap(), "2024-03-01");
        assert_eq!(normalize_date("2 days ago"), None);
        assert_eq!(normalize_date(""), None);
    }
}
