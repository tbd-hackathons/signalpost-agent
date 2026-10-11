#!/usr/bin/env python3
"""Build the active NAV job-ad index used by the agent: one line per active ad, keyed by employer number.

The active ads are listed in arbeidsplassen.nav.no's public sitemap (robots.txt allows everything).
For each listed ad not yet cached, or changed since (sitemap lastmod), the detail is read from NAV's
public feed API, which names the employer's organisation number. Writes:
  <cache>/details.jsonl         append-only detail cache (non-personal fields only)
  <cache>/active-ads.jsonl.gz   active, unexpired ads with employer organisation number
  <cache>/active-ads.meta.json  build time, feed cursor and counts
Inactive ads are left out of the active index, as NAV's feed terms require.

Usage: python scripts/nav_active_index.py --cache data-cache/nav-feed [--workers 4] [--limit N]
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from norway_company_agent.evidence import utc_now  # noqa: E402
from norway_company_agent.nav_jobs import NavDetailClient, NavWebClient, _expired, ad_summary  # noqa: E402


SITEMAP = "https://arbeidsplassen.nav.no/stillinger/sitemap.xml"


def sitemap_active() -> dict[str, dict]:
    """uuid -> {"modified": lastmod} for every ad in NAV's public job sitemap."""
    import re
    import urllib.request
    from norway_company_agent.nav_feed import USER_AGENT
    request = urllib.request.Request(SITEMAP, headers={"User-Agent": USER_AGENT})
    text = urllib.request.urlopen(request, timeout=60).read().decode("utf-8", errors="replace")
    active = {}
    for block in re.findall(r"<url>(.*?)</url>", text, re.S):
        loc = re.search(r"/stillinger/stilling/([0-9a-f-]{36})", block)
        if loc:
            lastmod = re.search(r"<lastmod>(.*?)</lastmod>", block)
            active[loc.group(1)] = {"status": "ACTIVE", "modified": lastmod.group(1) if lastmod else None}
    return active


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cache", required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--pause", type=float, default=0.3, help="seconds each worker waits between requests")
    parser.add_argument("--source", choices=["web", "feed"], default="web",
                        help="web: the public ad pages (fast, robots.txt allows all); feed: the job-feed API")
    parser.add_argument("--limit", type=int, default=0, help="fetch at most N new details (0 = all)")
    args = parser.parse_args()
    cache = Path(args.cache)
    cache.mkdir(parents=True, exist_ok=True)
    active = sitemap_active()
    print(f"active ads in sitemap={len(active)}", flush=True)

    details_path = cache / "details.jsonl"
    details: dict[str, dict] = {}
    if details_path.exists():
        for line in details_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue  # a line cut short by an interrupted build
            details[row["uuid"]] = row
    todo = [uuid for uuid, row in active.items()
            if uuid not in details or str(details[uuid].get("feed_modified")) != str(row["modified"])]
    if args.limit:
        todo = todo[: args.limit]
    print(f"cached details={len(details)} to fetch={len(todo)}", flush=True)

    client = NavWebClient(pause=args.pause) if args.source == "web" else NavDetailClient()
    # Feed cursor for run-time catch-up: the newest feed page as of this build.
    try:
        if args.source == "web":
            raise RuntimeError("feed not used")
        last_page = client.feed.get("/api/v1/feed?last=true")
        cursor = f"/api/v1/feed/{last_page['id']}" if last_page.get("id") else None
    except Exception:  # keep the previous build's cursor rather than fail the whole build
        meta_path = cache / "active-ads.meta.json"
        cursor = json.loads(meta_path.read_text()).get("feed_next_url") if meta_path.exists() else None
    started, done, failed = time.monotonic(), 0, 0
    with details_path.open("a", encoding="utf-8") as out, ThreadPoolExecutor(args.workers) as pool:
        def paced(uuid: str) -> dict:
            if args.source == "feed":
                time.sleep(args.pause)
            return client(uuid)

        futures = {pool.submit(paced, uuid): uuid for uuid in todo}
        for future in as_completed(futures):
            uuid = futures[future]
            try:
                detail = future.result()
                summary = ad_summary(detail, utc_now())
            except Exception:
                failed += 1
                continue
            done += 1
            if summary is None and (detail.get("ad_content") or {}):
                # Read, but NAV holds no employer number (mostly ads imported from other boards): it can never be
                # matched to an organisation, so it is cached as resolved rather than counted as a gap.
                summary = {"uuid": uuid, "status": detail.get("status"), "employer_orgnr": None, "no_employer_number": True}
            if summary:
                summary["feed_modified"] = active[uuid]["modified"]
                details[uuid] = summary
                out.write(json.dumps(summary, ensure_ascii=False, separators=(",", ":")) + "\n")
                out.flush()  # every ad reaches disk at once, so an interrupted build loses nothing
            if done % 500 == 0:
                print(f"details {done}/{len(todo)} failed={failed} {time.monotonic() - started:.0f}s", flush=True)

    today = datetime.now(timezone.utc).date()
    rows = [details[uuid] for uuid in active if uuid in details and details[uuid].get("employer_orgnr")
            and details[uuid].get("status") == "ACTIVE" and not _expired(details[uuid], today)]
    tmp = cache / "active-ads.jsonl.gz.tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    tmp.replace(cache / "active-ads.jsonl.gz")
    missing = sum(1 for uuid in active if uuid not in details)
    meta = {"built_at": utc_now(), "source": SITEMAP, "sitemap_ads": len(active), "ads_without_details": missing,
            "ads_without_employer_number": sum(1 for uuid in active if (details.get(uuid) or {}).get("no_employer_number")),
            # Complete only when at most 1% of live ads lack details; otherwise "no jobs" can never be asserted.
            "partial": missing > max(10, len(active) // 100), "feed_next_url": cursor,
            "active_ads": len(rows), "employers": len({row["employer_orgnr"] for row in rows}),
            "detail_requests": len(todo), "detail_failures": failed, "requests": getattr(client, "requests", None) or client.feed.requests, "detail_source": args.source}
    (cache / "active-ads.meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
