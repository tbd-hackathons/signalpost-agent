//! signalpost-search: candidate discovery for signalpost-agent.
//!
//! Reads search queries as JSON Lines, fans each one out to the selected
//! providers, and writes one JSON line per result, per error, or per empty
//! result set. Results are candidates only; the caller verifies identity.

pub mod cache;
pub mod config;
pub mod http;
pub mod model;
pub mod providers;
pub mod ratelimit;
pub mod runner;
pub mod stats;
pub mod text;

/// User-Agent sent on every outbound request.
pub const USER_AGENT: &str =
    "signalpost-agent/0.2 (+https://github.com/tbd-hackathons/signalpost-agent)";
