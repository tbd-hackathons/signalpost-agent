#!/usr/bin/env python3
"""Snapshot Wikidata items that carry a Norwegian organisation number (P2333), limited to the universe.

One SPARQL query to query.wikidata.org; output `data/wikidata-no-orgnr.jsonl.gz` (CC0), one line per
organisation number with item, websites (P856) and social handles, plus a first `_meta` line.

Usage: python scripts/wikidata_snapshot.py [--universe data-cache/signalpost-universe.jsonl.gz]
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.evidence import utc_now  # noqa: E402

ENDPOINT = "https://query.wikidata.org/sparql"
PROPS = {"w": None, "fb": "facebook", "ig": "instagram", "li": "linkedin", "x": "x", "yt": "youtube", "tt": "tiktok"}
QUERY = """SELECT ?i ?o ?w ?fb ?ig ?li ?x ?yt ?tt WHERE { ?i wdt:P2333 ?o .
OPTIONAL{?i wdt:P856 ?w} OPTIONAL{?i wdt:P2013 ?fb} OPTIONAL{?i wdt:P2003 ?ig} OPTIONAL{?i wdt:P4264 ?li}
OPTIONAL{?i wdt:P2002 ?x} OPTIONAL{?i wdt:P2397 ?yt} OPTIONAL{?i wdt:P7085 ?tt} }"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", default=str(ROOT / "data-cache" / "signalpost-universe.jsonl.gz"))
    parser.add_argument("--output", default=str(ROOT / "data" / "wikidata-no-orgnr.jsonl.gz"))
    args = parser.parse_args()
    universe = {json.loads(line)["organisation_number"] for line in gzip.open(args.universe, "rt", encoding="utf-8")}
    request = urllib.request.Request(ENDPOINT + "?" + urllib.parse.urlencode({"query": QUERY}), headers={
        "Accept": "application/sparql-results+json",
        "User-Agent": "signalpost-agent/0.2 (https://github.com/tbd-hackathons/signalpost-agent)"})
    retrieved_at = utc_now()
    bindings = json.loads(urllib.request.urlopen(request, timeout=180).read())["results"]["bindings"]
    rows: dict[str, dict] = {}
    for b in bindings:
        org = b["o"]["value"].replace(" ", "")
        if org not in universe:
            continue
        row = rows.setdefault(org, {"organisation_number": org, "item": b["i"]["value"], "websites": [], "handles": {}})
        for key, platform in PROPS.items():
            if key not in b:
                continue
            value = b[key]["value"]
            target = row["websites"] if platform is None else row["handles"].setdefault(platform, [])
            if value not in target:
                target.append(value)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out, "wt", encoding="utf-8") as handle:
        handle.write(json.dumps({"_meta": {"source": ENDPOINT, "property": "P2333", "license": "CC0-1.0",
                                           "retrieved_at": retrieved_at, "organisations": len(rows)}}) + "\n")
        for org in sorted(rows):
            row = rows[org]
            row["content_sha256"] = hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(f"{len(rows)} organisations -> {out}")


if __name__ == "__main__":
    main()
