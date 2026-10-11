#!/usr/bin/env python3
"""Measure the true addressable set for website discovery, straight from DNS.

The run's own funnel only records attempts when the first candidate succeeds (`value` stays None
otherwise), so this recomputes the funnel from first principles: for every company, what did the
registry offer, does that host actually resolve, and would a wider set of name-derived domains resolve?

DNS-only, so it is cheap and polite: no page is fetched here.

Usage: python scripts/dns_funnel.py --batch data-cache/test-1000.jsonl --bulk data-cache/brreg-enheter.csv.gz
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import socket
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.site_candidates import FREE_MAIL, SHARED_DOMAIN_LIMIT, build_domain_counts, email_domain, guessed_domains  # noqa: E402
from norway_company_agent.website import resolve_host  # noqa: E402


def candidates(name: str, limit: int) -> list[str]:
    """The same name-derived domains the run itself considers."""
    return guessed_domains(name)[:limit]


_probe_cache: dict[str, bool] = {}


def resolves(host: str) -> bool:
    if host in _probe_cache:
        return _probe_cache[host]
    try:
        resolve_host(host)
        ok = True
    except (socket.gaierror, UnicodeError, OSError):
        ok = False
    _probe_cache[host] = ok
    return ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", default="data-cache/test-1000.jsonl")
    parser.add_argument("--bulk", default="data-cache/brreg-enheter.csv.gz")
    parser.add_argument("--per-company", type=int, default=8, help="Name-derived hosts probed per company")
    parser.add_argument("--workers", type=int, default=64)
    args = parser.parse_args()

    meta: dict[str, dict] = {}
    with Path(args.bulk).open("rb") as raw, gzip.open(raw, "rt", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        wanted = {json.loads(line)["organisation_number"] for line in Path(args.batch).read_text().splitlines() if line.strip()}
        for row in reader:
            orgnr = (row.get("organisasjonsnummer") or "").strip()
            if orgnr in wanted:
                meta[orgnr] = {"name": (row.get("navn") or "").strip(),
                               "hjemmeside": (row.get("hjemmeside") or "").strip(),
                               "epost": (row.get("epostadresse") or "").strip()}
    counts = build_domain_counts(args.bulk)

    # Probe every host we might use, in parallel: registry sites, e-mail domains and name guesses.
    probe: list[str] = []
    plan: dict[str, dict] = {}
    for orgnr, info in meta.items():
        host = re.sub(r"^https?://", "", info["hjemmeside"]).split("/")[0].casefold().removeprefix("www.") if info["hjemmeside"] else ""
        dom = email_domain(info["epost"])
        usable_email = bool(dom) and dom not in FREE_MAIL
        guesses = candidates(info["name"], args.per_company)
        plan[orgnr] = {"host": host, "email_domain": dom or "", "usable_email": usable_email,
                       "shared": counts.get(dom, 1) if dom else 0, "guesses": guesses}
        for candidate in [host, dom if usable_email else "", *guesses]:
            if candidate and candidate not in probe:
                probe.append(candidate)

    print(f"comparing {len(meta)} companies · probing {len(probe):,} hostnames", flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(resolves, probe))

    buckets: Counter = Counter()
    recovered: Counter = Counter()
    for orgnr, item in plan.items():
        has_site = bool(item["host"])
        site_live = has_site and resolves(item["host"])
        guess = next((g for g in item["guesses"] if resolves(g)), "")
        if has_site and site_live:
            bucket = "registry website (live)"
        elif has_site:
            bucket = "registry website (dead)"
        elif item["usable_email"] and item["shared"] <= SHARED_DOMAIN_LIMIT:
            bucket = "usable e-mail domain"
        elif item["usable_email"]:
            bucket = "shared e-mail domain"
        elif item["email_domain"]:
            bucket = "free mail (no domain signal)"
        else:
            bucket = "nothing from the registry"
        buckets[bucket] += 1
        if guess and not site_live:
            recovered[bucket] += 1
        recovered["_probe_more"] += 0

    order = ["registry website (live)", "registry website (dead)", "usable e-mail domain",
             "shared e-mail domain", "free mail (no domain signal)", "nothing from the registry"]
    print()
    print(f'{"bucket":34} {"companies":>10} {"name-domain resolves":>22}')
    for bucket in order:
        if buckets[bucket]:
            print(f"{bucket:34} {buckets[bucket]:>10,} {recovered[bucket]:>22,}")
    total = sum(buckets.values())
    dead = buckets["registry website (dead)"]
    print()
    print(f"companies: {total:,}")
    print(f"registry site dead (would block a guess today): {dead:,}")
    print(f"name-derived domains that resolve: {sum(recovered[b] for b in order):,} "
          f"({sum(recovered[b] for b in order) / total:.1%} of companies)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
