//! File cache: `<dir>/<provider>/<sha256(provider|kind|lang|count|query)>.json`.
//!
//! Only successful searches (including empty ones) are stored. Writes go to a
//! unique temp file in the same directory and are renamed into place, so a
//! reader never sees a partial file and concurrent writers cannot interleave.

use crate::model::{Hit, Kind};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

/// Identity of one cacheable search.
#[derive(Debug, Clone, Copy)]
pub struct CacheKey<'a> {
    pub provider: &'a str,
    pub kind: Kind,
    pub lang: &'a str,
    pub count: u32,
    pub query: &'a str,
}

impl CacheKey<'_> {
    /// Hex sha256 of `provider|kind|lang|count|query`.
    pub fn digest(&self) -> String {
        let mut h = Sha256::new();
        h.update(
            format!(
                "{}|{}|{}|{}|{}",
                self.provider,
                self.kind.as_str(),
                self.lang,
                self.count,
                self.query
            )
            .as_bytes(),
        );
        hex::encode(h.finalize())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct CacheEntry {
    pub provider: String,
    pub kind: Kind,
    pub lang: String,
    pub count: u32,
    pub query: String,
    /// ISO 8601 UTC time of the original fetch.
    pub fetched_at: String,
    pub results: Vec<Hit>,
}

#[derive(Debug)]
pub struct Cache {
    dir: PathBuf,
    ttl: Duration,
}

static TMP_COUNTER: AtomicU64 = AtomicU64::new(0);

impl Cache {
    pub fn new(dir: impl Into<PathBuf>, ttl: Duration) -> Self {
        Self {
            dir: dir.into(),
            ttl,
        }
    }

    pub fn path_for(&self, key: &CacheKey<'_>) -> PathBuf {
        self.dir
            .join(key.provider)
            .join(format!("{}.json", key.digest()))
    }

    /// Fresh entry for `key`, or `None` on miss, expiry, or a corrupt file.
    pub fn get(&self, key: &CacheKey<'_>) -> Option<CacheEntry> {
        self.get_at(key, Utc::now())
    }

    pub fn get_at(&self, key: &CacheKey<'_>, now: DateTime<Utc>) -> Option<CacheEntry> {
        let bytes = fs::read(self.path_for(key)).ok()?;
        let entry: CacheEntry = serde_json::from_slice(&bytes).ok()?;
        // Guard against a (vanishingly unlikely) digest collision or a
        // hand-edited file.
        if entry.provider != key.provider
            || entry.kind != key.kind
            || entry.lang != key.lang
            || entry.count != key.count
            || entry.query != key.query
        {
            return None;
        }
        let fetched = DateTime::parse_from_rfc3339(&entry.fetched_at)
            .ok()?
            .with_timezone(&Utc);
        let age = now.signed_duration_since(fetched);
        // An entry stamped more than 5 minutes in the future means a clock
        // problem; refetch rather than trust it.
        if age < chrono::Duration::minutes(-5) {
            return None;
        }
        let ttl = chrono::Duration::from_std(self.ttl).unwrap_or(chrono::Duration::MAX);
        if age >= ttl {
            return None;
        }
        Some(entry)
    }

    /// Store a successful result atomically.
    pub fn put(&self, key: &CacheKey<'_>, fetched_at: &str, results: &[Hit]) -> io::Result<()> {
        let path = self.path_for(key);
        let dir = path
            .parent()
            .ok_or_else(|| io::Error::other("cache path has no parent"))?;
        fs::create_dir_all(dir)?;
        let entry = CacheEntry {
            provider: key.provider.to_string(),
            kind: key.kind,
            lang: key.lang.to_string(),
            count: key.count,
            query: key.query.to_string(),
            fetched_at: fetched_at.to_string(),
            results: results.to_vec(),
        };
        let body = serde_json::to_vec(&entry).map_err(io::Error::other)?;
        let tmp = tmp_path(dir, &path);
        let result = write_and_rename(&tmp, &path, &body);
        if result.is_err() {
            let _ = fs::remove_file(&tmp);
        }
        result
    }
}

fn tmp_path(dir: &Path, target: &Path) -> PathBuf {
    let n = TMP_COUNTER.fetch_add(1, Ordering::Relaxed);
    let stem = target
        .file_name()
        .map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_default();
    dir.join(format!(".{stem}.{}.{n}.tmp", std::process::id()))
}

fn write_and_rename(tmp: &Path, path: &Path, body: &[u8]) -> io::Result<()> {
    let mut f = fs::File::create(tmp)?;
    f.write_all(body)?;
    f.sync_all()?;
    drop(f);
    fs::rename(tmp, path)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn key(q: &str) -> CacheKey<'_> {
        CacheKey {
            provider: "wikipedia",
            kind: Kind::Web,
            lang: "no",
            count: 10,
            query: q,
        }
    }

    fn hit() -> Hit {
        Hit {
            url: "https://no.wikipedia.org/wiki/Equinor".into(),
            title: "Equinor".into(),
            snippet: "Norsk energiselskap".into(),
            published: None,
            domain: None,
            language: None,
        }
    }

    #[test]
    fn digest_is_stable_and_field_sensitive() {
        let a = key("Equinor").digest();
        assert_eq!(a.len(), 64);
        assert_eq!(a, key("Equinor").digest());
        let mut k = key("Equinor");
        k.count = 5;
        assert_ne!(a, k.digest());
        let mut k = key("Equinor");
        k.provider = "brave";
        assert_ne!(a, k.digest());
        // the documented formula
        let mut h = Sha256::new();
        h.update(b"wikipedia|web|no|10|Equinor");
        assert_eq!(a, hex::encode(h.finalize()));
    }

    #[test]
    fn miss_then_hit_then_expire() {
        let dir = tempfile::tempdir().unwrap();
        let cache = Cache::new(dir.path(), Duration::from_secs(3600));
        let k = key("Equinor");
        assert!(cache.get(&k).is_none());

        let now = Utc::now();
        let stamp = now.to_rfc3339_opts(chrono::SecondsFormat::Secs, true);
        cache.put(&k, &stamp, &[hit()]).unwrap();
        assert!(cache.path_for(&k).starts_with(dir.path().join("wikipedia")));

        let e = cache
            .get_at(&k, now + chrono::Duration::minutes(30))
            .unwrap();
        assert_eq!(e.results, vec![hit()]);
        assert_eq!(e.fetched_at, stamp);

        assert!(cache
            .get_at(&k, now + chrono::Duration::minutes(61))
            .is_none());
        assert!(cache.get_at(&k, now - chrono::Duration::hours(1)).is_none());
        assert!(cache.get(&key("Other")).is_none());

        // no temp files left behind
        let leftovers: Vec<_> = fs::read_dir(dir.path().join("wikipedia"))
            .unwrap()
            .filter_map(|e| e.ok())
            .filter(|e| e.file_name().to_string_lossy().ends_with(".tmp"))
            .collect();
        assert!(leftovers.is_empty());
    }

    #[test]
    fn empty_results_are_cached() {
        let dir = tempfile::tempdir().unwrap();
        let cache = Cache::new(dir.path(), Duration::from_secs(60));
        let k = key("nothing");
        cache.put(&k, &crate::text::now_iso(), &[]).unwrap();
        assert_eq!(cache.get(&k).unwrap().results, vec![]);
    }

    #[test]
    fn corrupt_file_is_a_miss() {
        let dir = tempfile::tempdir().unwrap();
        let cache = Cache::new(dir.path(), Duration::from_secs(60));
        let k = key("Equinor");
        let p = cache.path_for(&k);
        fs::create_dir_all(p.parent().unwrap()).unwrap();
        fs::write(&p, b"{not json").unwrap();
        assert!(cache.get(&k).is_none());
    }
}
