//! Provider response parsing and request building against fixture files.

use signalpost_search::http::{BodyVerdict, FetchError};
use signalpost_search::model::{Kind, Query};
use signalpost_search::providers::{brave, gdelt, wikipedia};

fn fixture(name: &str) -> String {
    let path = format!("{}/tests/fixtures/{name}", env!("CARGO_MANIFEST_DIR"));
    std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("{path}: {e}"))
}

fn query(q: &str, kind: Kind, count: u32, lang: &str) -> Query {
    Query {
        id: "912345678:web:1".into(),
        query: q.into(),
        kind,
        count,
        lang: lang.into(),
    }
}

// ---------- brave ----------

#[test]
fn brave_web_results() {
    let hits = brave::parse(Kind::Web, &fixture("brave_web.json")).unwrap();
    assert_eq!(hits.len(), 3, "result without url must be skipped");
    let h = &hits[0];
    assert_eq!(h.url, "https://vinsnesbakk-elektro.example.no/");
    assert_eq!(
        h.title,
        "Vinsnesbakk Elektro AS \u{2013} Elektriker i Sandnes"
    );
    assert_eq!(
        h.snippet,
        "Vi er en lokal elektrobedrift i Sandnes & omegn."
    );
    assert_eq!(h.published.as_deref(), Some("2025-11-03T09:12:00Z"));
    assert_eq!(h.domain.as_deref(), Some("vinsnesbakk-elektro.example.no"));
    assert_eq!(h.language.as_deref(), Some("nb"));
    // falls back to `age` when it is an absolute date
    assert_eq!(hits[1].published.as_deref(), Some("2024-03-01"));
    // relative ages are dropped, not guessed
    assert_eq!(hits[2].published, None);
}

#[test]
fn brave_news_results() {
    let hits = brave::parse(Kind::News, &fixture("brave_news.json")).unwrap();
    assert_eq!(hits.len(), 2);
    assert_eq!(
        hits[0].url,
        "https://avis.example.no/nyheter/equinor-havvind"
    );
    assert_eq!(hits[0].title, "Equinor investerer i ny havvind");
    assert_eq!(
        hits[0].snippet,
        "Selskapet melder at \"prosjektet\" starter i 2027."
    );
    assert_eq!(hits[0].published.as_deref(), Some("2026-10-08T06:00:00Z"));
    assert_eq!(hits[1].published, None);
}

#[test]
fn brave_missing_sections_are_empty_and_garbage_is_an_error() {
    assert!(brave::parse(Kind::Web, r#"{"type":"search"}"#)
        .unwrap()
        .is_empty());
    assert!(brave::parse(Kind::News, r#"{"results":[]}"#)
        .unwrap()
        .is_empty());
    assert!(matches!(
        brave::parse(Kind::Web, "<html>"),
        Err(FetchError::BadResponse(_))
    ));
    assert!(matches!(brave::check_body("<html>"), BodyVerdict::Fail(_)));
}

#[test]
fn brave_request_urls() {
    let u = brave::build_request_url(
        "https://api.search.brave.com/res/v1",
        &query("\"Vinsnesbakk Elektro\" Sandnes", Kind::Web, 30, "no"),
    );
    assert_eq!(
        u,
        "https://api.search.brave.com/res/v1/web/search?q=%22Vinsnesbakk%20Elektro%22%20Sandnes&count=20&country=NO&search_lang=nb&result_filter=web&text_decorations=false"
    );
    let u = brave::build_request_url("http://x", &query("Equinor", Kind::News, 50, "en"));
    assert_eq!(
        u,
        "http://x/news/search?q=Equinor&count=50&country=NO&search_lang=en"
    );
}

// ---------- gdelt ----------

#[test]
fn gdelt_articles() {
    let hits = gdelt::parse(&fixture("gdelt_artlist.json")).unwrap();
    assert_eq!(hits.len(), 2);
    assert_eq!(hits[0].url, "https://www.example.no/nyheter/equinor-1");
    assert_eq!(hits[0].title, "Equinor øker produksjonen på Johan Sverdrup");
    assert_eq!(hits[0].published.as_deref(), Some("2026-10-01T12:00:00Z"));
    assert_eq!(hits[0].domain.as_deref(), Some("example.no"));
    assert_eq!(hits[0].language.as_deref(), Some("Norwegian"));
    assert_eq!(hits[0].snippet, "");
    assert_eq!(hits[1].published.as_deref(), Some("2026-09-30T08:15:00Z"));
}

#[test]
fn gdelt_repairs_invalid_escapes() {
    let body = fixture("gdelt_bad_escape.json");
    assert!(serde_json::from_str::<serde_json::Value>(&body).is_err());
    let hits = gdelt::parse(&body).unwrap();
    assert_eq!(hits.len(), 1);
    assert_eq!(hits[0].title, "Equinor's new field");
}

#[test]
fn gdelt_empty_and_plain_text_bodies() {
    assert!(gdelt::parse(&fixture("gdelt_empty.json"))
        .unwrap()
        .is_empty());
    assert!(gdelt::parse("").unwrap().is_empty());
    assert_eq!(
        gdelt::check_body(&fixture("gdelt_rate_limited.txt")),
        BodyVerdict::RateLimited
    );
    match gdelt::check_body(&fixture("gdelt_short_phrase.txt")) {
        BodyVerdict::Fail(FetchError::BadQuery(d)) => {
            assert_eq!(d, "gdelt: The specified phrase is too short.")
        }
        other => panic!("unexpected {other:?}"),
    }
    assert_eq!(
        gdelt::check_body(&fixture("gdelt_artlist.json")),
        BodyVerdict::Ok
    );
}

#[test]
fn gdelt_request_url() {
    let u = gdelt::build_request_url(
        "https://api.gdeltproject.org/api/v2/doc/doc",
        &query("\"Vinsnesbakk Elektro\"", Kind::News, 500, "no"),
    );
    assert_eq!(
        u,
        "https://api.gdeltproject.org/api/v2/doc/doc?query=%22Vinsnesbakk%20Elektro%22&mode=artlist&format=json&maxrecords=250&timespan=12m&sort=datedesc"
    );
}

// ---------- wikipedia ----------

#[test]
fn wikipedia_results() {
    let hits = wikipedia::parse(
        &fixture("wikipedia_search.json"),
        "https://no.wikipedia.org",
    )
    .unwrap();
    assert_eq!(hits.len(), 2);
    assert_eq!(
        hits[0].url,
        "https://no.wikipedia.org/wiki/Hannestad_barnehage"
    );
    assert_eq!(hits[0].title, "Hannestad barnehage");
    assert_eq!(
        hits[0].snippet,
        "Hannestad barnehage er en barnehage i Sarpsborg & omegn."
    );
    assert_eq!(hits[0].published, None);
    assert_eq!(
        hits[1].url,
        "https://no.wikipedia.org/wiki/Troms%C3%B8_(by)"
    );
}

#[test]
fn wikipedia_empty_and_error() {
    assert!(
        wikipedia::parse(&fixture("wikipedia_empty.json"), "https://x")
            .unwrap()
            .is_empty()
    );
    assert!(matches!(
        wikipedia::parse(&fixture("wikipedia_error.json"), "https://x"),
        Err(FetchError::BadQuery(d)) if d == "wikipedia: nosrsearch"
    ));
}

#[test]
fn wikipedia_urls_and_lang() {
    let t = "https://{lang}.wikipedia.org/w/api.php";
    assert_eq!(
        wikipedia::endpoint(t, "no"),
        "https://no.wikipedia.org/w/api.php"
    );
    assert_eq!(
        wikipedia::endpoint(t, "en"),
        "https://en.wikipedia.org/w/api.php"
    );
    assert_eq!(
        wikipedia::endpoint(t, "sv"),
        "https://no.wikipedia.org/w/api.php"
    );
    assert_eq!(
        wikipedia::site_root("https://no.wikipedia.org/w/api.php"),
        "https://no.wikipedia.org"
    );
    assert_eq!(
        wikipedia::site_root("http://127.0.0.1:9/w/api.php"),
        "http://127.0.0.1:9"
    );
    let u = wikipedia::build_request_url(t, &query("Hannestad barnehage", Kind::Web, 5, "no"));
    assert_eq!(
        u,
        "https://no.wikipedia.org/w/api.php?action=query&list=search&srsearch=Hannestad%20barnehage&format=json&formatversion=2&srlimit=5&srprop=snippet"
    );
}
