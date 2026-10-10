"""Website and social profiles recorded in Wikidata against the Norwegian organisation number (P2333).

Wikidata is CC0. The agent ships a snapshot limited to the Signalpost universe
(`data/wikidata-no-orgnr.jsonl.gz`, built by `scripts/wikidata_snapshot.py`), so a run makes no
Wikidata requests. Identity is the organisation number itself; the website still goes through the
normal website identity gate before it is published.
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

from .evidence import evidence

SNAPSHOT = Path(__file__).resolve().parents[2] / "data" / "wikidata-no-orgnr.jsonl.gz"
HANDLE_URLS = {
    "facebook": "https://facebook.com/{}",
    "instagram": "https://instagram.com/{}",
    "linkedin": "https://linkedin.com/company/{}",
    "x": "https://x.com/{}",
    "youtube": "https://youtube.com/channel/{}",
    "tiktok": "https://tiktok.com/@{}",
}


class WikidataIndex:
    def __init__(self, rows: dict[str, dict[str, Any]], meta: dict[str, Any]) -> None:
        self.rows, self.meta = rows, meta

    @classmethod
    def load(cls, path: str | Path = SNAPSHOT) -> "WikidataIndex | None":
        path = Path(path)
        if not path.exists():
            return None
        rows, meta = {}, {}
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                row = json.loads(line)
                if "_meta" in row:
                    meta = row["_meta"]
                else:
                    rows[row["organisation_number"]] = row
        return cls(rows, meta)

    def get(self, organisation_number: str) -> dict[str, Any] | None:
        return self.rows.get(str(organisation_number))


def wikidata_record(profile: dict[str, Any], index: WikidataIndex | None) -> dict[str, Any]:
    if index is None:
        return evidence("wikidata", "source_error", "wikidata_cc0", "https://www.wikidata.org/",
                        note="Wikidata snapshot not available for this run")
    row = index.get(profile["organisation_number"])
    if not row:
        return evidence("wikidata", "not_found", "wikidata_cc0", "https://www.wikidata.org/",
                        note="No Wikidata item records this organisation number (P2333)",
                        as_of=index.meta.get("retrieved_at"))
    profiles = [{"platform": platform, "url": HANDLE_URLS[platform].format(handle), "handle": handle}
                for platform, handles in sorted((row.get("handles") or {}).items()) for handle in handles
                if platform in HANDLE_URLS]
    value = {"item": row["item"], "websites": row.get("websites") or [], "social_profiles": profiles,
             "identity_proof": f"Wikidata item {row['item'].rsplit('/', 1)[-1]} records organisation number "
                               f"{profile['organisation_number']} (property P2333)"}
    return evidence("wikidata", "available", "wikidata_cc0", row["item"], value=value,
                    retrieved_at=index.meta.get("retrieved_at"), content_sha256=row.get("content_sha256"),
                    note="Community-maintained open data (CC0), matched by organisation number")
