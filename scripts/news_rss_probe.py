#!/usr/bin/env python3
"""How much dated news can a keyless Norwegian news feed actually give us?

VERDICT: real yield, not safe to publish. Measured on 140 of the 1,000-company batch, 20 companies (14%)
had a headline naming the company, projecting to ~140 companies and ~1,000 articles — against 0 today,
because GDELT returns nothing for this batch. But roughly 30% of those matches are the wrong company,
always for the same reason: the legal name is an ordinary Norwegian word or phrase. "SMARAGD AS" matched a
story about a stolen emerald (*smaragd*), "FLINKE JENTER AS" matched "«Flinke jenter»", "LIVESHOW AS"
matched the word "liveshow". Word-boundary matching removes only 9% of these (the glued-substring test let
"A. MESTAD AS" match "Ola Mestad", which boundaries do fix) because the rest match whole words legitimately.

The fix would be to verify the article body against the full name and registered place, but the feed's link
is a Google redirect that answers with a captcha, so the body is not reachable. A wrong-company match costs
the precision block and can stop a run becoming official, so this stays a probe until either a Norwegian word
frequency list can reject generic names or the publisher article can be read. The same reasoning retired the
GDELT cap increase: see out/HANDOVER.md.

Google News RSS is free, keyless and indexes Norwegian outlets. The article link is a Google redirect that
answers with a captcha, so the feed item is the evidence: its title, outlet and publication date. That makes
the identity test a title test, and a title is a weaker place to prove identity than article text — so this
measures how often the company's *complete* legal name really appears in the headline.

Usage: python scripts/news_rss_probe.py --sample 40 --workers 3
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
from pathlib import Path
from collections import Counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.site_candidates import name_tokens  # noqa: E402

FEED = "https://news.google.com/rss/search?q={query}&hl=no&gl=NO&ceid=NO:no"
UA = "SignalpostResearchPOC/1.0 (https://builderr.ai; bounded qualification run)"
LEGAL_FORM = re.compile(r"\b(as|asa|ans|da|sa|ba|ks|nuf|enk|sti|iks)\b", re.I)


def fold(value: str) -> str:
    value = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode().casefold()
    return re.sub(r"[^a-z0-9]", "", value)


def query_for(name: str) -> str:
    """The distinctive words of the legal name, without the legal form."""
    words = [w for w in re.findall(r"[a-z0-9æøå]+", LEGAL_FORM.sub(" ", (name or "").casefold())) if len(w) > 1]
    return " ".join(words[:5])


def probe(company: dict) -> dict:
    name = company.get("name") or ""
    org = str(company.get("organisation_number") or "")
    query = query_for(name)
    full = "".join(name_tokens(name))
    out = {"organisation_number": org, "name": name, "query": query, "items": 0, "hits": []}
    if len(full) < 5 or not query:
        out["skipped"] = "name too short or generic"
        return out
    url = FEED.format(query=urllib.parse.quote(query))
    try:
        request = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(request, timeout=25) as response:
            raw = response.read(1_500_000)
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"[:80]
        return out
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        out["error"] = f"parse: {exc}"
        return out
    for item in root.iter("item"):
        out["items"] += 1
        title = (item.findtext("title") or "").strip()
        # Google appends " - Outlet" to the headline; the headline is what has to name the company.
        headline = title.rsplit(" - ", 1)[0]
        outlet = (item.findtext("source") or "").strip()
        link = (item.findtext("link") or "").strip()
        published = item.findtext("pubDate") or ""
        try:
            published = parsedate_to_datetime(published).date().isoformat()
        except Exception:
            published = ""
        if full and full in fold(headline):
            out["hits"].append({"headline": headline, "outlet": outlet, "published": published, "link": link[:80]})
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", default="data-cache/test-1000.jsonl")
    parser.add_argument("--sample", type=int, default=40)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--json", default="out/mine/news-rss-probe.json")
    args = parser.parse_args()

    import random

    companies = [json.loads(line) for line in Path(args.batch).read_text().splitlines() if line.strip()]
    random.Random(args.seed).shuffle(companies)
    sample = companies[: args.sample]
    print(f"probing {len(sample)} companies against Google News RSS (keyless)", flush=True)

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for result in pool.map(probe, sample):
            results.append(result)

    with_hits = [r for r in results if r.get("hits")]
    total_hits = sum(len(r["hits"]) for r in results)
    companies_with_items = sum(1 for r in results if r["items"])
    print()
    print(f"companies probed            : {len(results)}")
    print(f"  feed returned any item    : {companies_with_items}")
    print(f"  a headline names the company exactly: {len(with_hits)} ({len(with_hits)/max(1,len(results)):.0%})")
    print(f"  headlines verified        : {total_hits}")
    print(f"  projection over 1,000     : ~{int(len(with_hits)/max(1,len(results))*1000)} companies, "
          f"~{int(total_hits/max(1,len(results))*1000)} articles")
    print()
    dates = Counter(h["published"][:4] for r in results for h in r.get("hits", []))
    print("by year:", dict(sorted(dates.items())))
    print()
    for r in with_hits[:14]:
        h = r["hits"][0]
        print(f"  {r['organisation_number']} {str(r['name'])[:26]:26} {h['published']} {str(h['outlet'])[:18]:18} {h['headline'][:44]}")
    Path(args.json).write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in results) + "\n", encoding="utf-8")
    print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
