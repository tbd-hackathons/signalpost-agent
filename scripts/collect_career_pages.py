#!/usr/bin/env python3
"""Collect careers pages from registry-listed company websites, for testing the local-model extractor.

Samples companies with a registry website (outside the given exclusion lists), reads each homepage,
follows the first careers link, and saves the careers page HTML. robots.txt is respected.

Usage: python scripts/collect_career_pages.py --bulk data-cache/brreg-enheter.csv.gz --out data-cache/llm-eval --want 60
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import random
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from norway_company_agent.site_intel import SiteFetcher, is_career_url, registered_domain  # noqa: E402
from norway_company_agent.website import normalize_homepage  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bulk", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--want", type=int, default=60)
    parser.add_argument("--sample", type=int, default=700)
    parser.add_argument("--exclude", nargs="*", default=["data-cache/dev-100.jsonl", "data-cache/val-100.jsonl"])
    parser.add_argument("--seed", type=int, default=3030)
    args = parser.parse_args()
    excluded = {json.loads(l)["organisation_number"] for f in args.exclude for l in open(f) if l.strip()}
    rows = []
    with gzip.open(args.bulk, "rt", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row.get("hjemmeside") and row["organisasjonsnummer"] not in excluded:
                rows.append({"org": row["organisasjonsnummer"], "name": row["navn"], "site": row["hjemmeside"]})
    random.Random(args.seed).shuffle(rows)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    def visit(row: dict) -> dict | None:
        fetcher = SiteFetcher(timeout=10)
        home = normalize_homepage(row["site"])
        try:
            raw, final, ctype = fetcher.get(home)
            if "html" not in ctype:
                return None
            soup = BeautifulSoup(raw.decode("utf-8", "replace"), "lxml")
            links = [(urllib.parse.urljoin(final, a.get("href") or ""), a.get_text(" ", strip=True)) for a in soup.select("a[href]")]
            careers = [u for u, t in links if registered_domain(u) == registered_domain(final) and is_career_url(u, t)]
            if not careers:
                return None
            raw, page_url, ctype = fetcher.get(careers[0])
            if "html" not in ctype:
                return None
            key = hashlib.sha256(page_url.encode()).hexdigest()[:12]
            (out / f"{key}.html").write_bytes(raw)
            return {"key": key, "org": row["org"], "name": row["name"], "url": page_url}
        except Exception:
            return None

    found = []
    with ThreadPoolExecutor(8) as pool:
        for result in pool.map(visit, rows[: args.sample]):
            if result:
                found.append(result)
                print(len(found), result["name"], result["url"], flush=True)
            if len(found) >= args.want:
                break
    (out / "index.json").write_text(json.dumps(found, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
