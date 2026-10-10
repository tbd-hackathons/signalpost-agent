#!/usr/bin/env python3
"""Snapshot Norwegian public-procurement notices from TED (EU Tenders Electronic Daily), keyed by organisation number.

TED's search API is open (no key) and its notices are free to reuse. Each notice names buyers and, for
contract awards, winners by Norwegian organisation number, so matching is exact. Only notices that name an
organisation in the Signalpost universe are kept.

Usage: python scripts/ted_snapshot.py --universe data-cache/signalpost-universe.jsonl.gz --since 20240101 \
         --output data/ted-no-notices.jsonl.gz
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

API = "https://api.ted.europa.eu/v3/notices/search"
USER_AGENT = "signalpost-agent/0.2 (+https://github.com/tbd-hackathons/signalpost-agent)"
FIELDS = ["publication-number", "publication-date", "notice-type", "notice-title",
          "buyer-identifier", "buyer-name", "winner-identifier", "winner-name"]


def pick_lang(value) -> str:
    if isinstance(value, dict):
        for lang in ("nor", "nob", "eng"):
            if value.get(lang):
                text = value[lang]
                return text[0] if isinstance(text, list) else text
        first = next(iter(value.values()), "")
        return first[0] if isinstance(first, list) else first
    return value or ""


def names(value) -> list[str]:
    if isinstance(value, dict):
        for lang in ("nor", "nob", "eng"):
            if value.get(lang):
                return list(dict.fromkeys(value[lang]))
        return list(dict.fromkeys(next(iter(value.values()), [])))
    return list(value or [])


def compact(notice: dict) -> dict:
    title = pick_lang(notice.get("notice-title"))
    title = title.split(" – ", 2)[-1] if title.count(" – ") >= 2 else title  # drop "Country – CPV –" prefix
    number = notice["publication-number"]
    return {
        "publication_number": number,
        "published": str(notice.get("publication-date") or "")[:10],
        "notice_type": notice.get("notice-type"),
        "title": title,
        "url": f"https://ted.europa.eu/en/notice/-/detail/{number}",
        "buyer_ids": [str(x).replace(" ", "") for x in notice.get("buyer-identifier") or []],
        "buyer_names": names(notice.get("buyer-name")),
        "winner_ids": [str(x).replace(" ", "") for x in notice.get("winner-identifier") or []],
        "winner_names": names(notice.get("winner-name")),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", required=True)
    parser.add_argument("--since", default="20240101", help="publication date YYYYMMDD")
    parser.add_argument("--output", required=True)
    parser.add_argument("--pause", type=float, default=1.0)
    args = parser.parse_args()

    with gzip.open(args.universe, "rt", encoding="utf-8") as handle:
        universe = {json.loads(line)["organisation_number"] for line in handle if line.strip()}
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Content-Type": "application/json"})
    body = {"query": f"buyer-country=NOR AND publication-date>={args.since}", "fields": FIELDS,
            "limit": 250, "paginationMode": "ITERATION"}
    retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    seen = kept = pages = 0
    rows = []
    while True:
        for attempt in range(5):
            response = session.post(API, json=body, timeout=60)
            if response.status_code == 200:
                break
            time.sleep(args.pause * 2 ** (attempt + 1))
        response.raise_for_status()
        data = response.json()
        pages += 1
        for notice in data.get("notices") or []:
            seen += 1
            row = compact(notice)
            if universe & set(row["buyer_ids"] + row["winner_ids"]):
                row["content_sha256"] = hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
                rows.append(row)
                kept += 1
        token = data.get("iterationNextToken")
        if pages % 20 == 0:
            print(f"pages={pages} notices={seen} kept={kept}", flush=True)
        if not token or not data.get("notices"):
            break
        body["iterationNextToken"] = token
        time.sleep(args.pause)

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(args.output, "wt", encoding="utf-8") as out:
        out.write(json.dumps({"_meta": {"source": API, "query": body["query"], "retrieved_at": retrieved_at,
                                        "notices_scanned": seen, "notices_kept": kept,
                                        "licence": "TED data, free reuse (Commission Decision 2011/833/EU)"}}) + "\n")
        for row in rows:
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"done pages={pages} notices={seen} kept={kept} -> {args.output}")


if __name__ == "__main__":
    main()
