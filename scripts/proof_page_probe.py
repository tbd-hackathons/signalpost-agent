#!/usr/bin/env python3
"""Is the proof we are missing simply on a page we never read?

A name-derived domain is rejected when the homepage carries no organisation number, no registry contact and
no full legal name plus place. Norwegian company sites almost always print the organisation number somewhere
— GDPR makes the data controller identify itself, and the footer of a privacy or contact page is where it goes.

This samples companies whose candidate domain was rejected and reads a few more of that site's own pages,
then re-runs the same proof rules. It only reports; it changes nothing.

Usage: python scripts/proof_page_probe.py --sample 60 --workers 6
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.site_candidates import contact_proof, place_proof  # noqa: E402
from norway_company_agent.site_intel import SiteFetcher, registered_domain  # noqa: E402

# Pages a company uses to identify itself. Privacy first: it is required to name the data controller.
EXTRA_PATHS = ("/personvern", "/personvernserklaring", "/privacy", "/privacy-policy", "/kontakt", "/om-oss", "/contact")


def digits(text: str) -> str:
    return re.sub(r"\D", "", text or "")


def has_orgnr(profile: dict, text: str) -> bool:
    org = digits(str(profile.get("organisation_number") or ""))
    return bool(org) and org in digits(text)


def probe(profile: dict, url: str, paths: tuple[str, ...]) -> dict:
    """Read the extra pages of one candidate site and report whether any of them proves identity."""
    org = str(profile.get("organisation_number"))
    base = re.match(r"^(https?://[^/]+)", url)
    if not base:
        return {"orgnr": org, "name": profile.get("name"), "url": url, "result": "bad_url", "tried": []}
    origin = base.group(1)
    domain = registered_domain(url)
    fetcher = SiteFetcher()
    tried, found = [], None
    for path in paths:
        target = origin + path
        try:
            raw, final_url, ctype = fetcher.get(target)
        except Exception:
            continue
        if registered_domain(final_url) != domain:
            continue
        if "html" not in ctype.lower() and "text" not in ctype.lower():
            continue
        text = raw.decode("utf-8", errors="replace")
        tried.append(path)
        if has_orgnr(profile, text):
            found = ("organisation number", path)
        elif contact_proof(profile, text):
            found = (contact_proof(profile, text), path)
        elif place_proof(profile, text):
            found = (place_proof(profile, text), path)
        if found:
            break
    return {"orgnr": org, "name": profile.get("name"), "url": url,
            "result": "proved" if found else "no_proof", "proof": found[0] if found else None,
            "path": found[1] if found else None, "tried": tried}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--envelopes", default="out/final-envelopes.jsonl")
    parser.add_argument("--sample", type=int, default=60)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--json", default="out/mine/proof-page-probe.json")
    args = parser.parse_args()

    import random

    targets = []
    for line in Path(args.envelopes).read_text().splitlines():
        if not line.strip():
            continue
        env = json.loads(line)
        profile = env.get("profile") or {}
        record = ((profile.get("evidence") or {}).get("website") or {})
        if record.get("status") != "available":
            continue
        value = record.get("value") or {}
        if (value.get("identity_assessment") or {}).get("publishable"):
            continue
        discovery = (value.get("discovery") or {})
        rejected = [a for a in discovery.get("attempts") or []
                    if a.get("outcome") == "rejected" and a.get("identity") == "related_or_uncertain"]
        if not rejected:
            continue
        targets.append((profile, rejected[0]["url"]))

    random.Random(args.seed).shuffle(targets)
    sample = targets[: args.sample]
    print(f"rejected candidates with a live page: {len(targets):,} · probing {len(sample)}", flush=True)

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for result in pool.map(lambda item: probe(item[0], item[1], EXTRA_PATHS), sample):
            results.append(result)

    proved = [r for r in results if r["result"] == "proved"]
    kinds = Counter(r.get("proof") for r in proved)
    pages = Counter(r.get("path") for r in proved)
    print()
    print(f"probed              : {len(results)}")
    print(f"now prove identity  : {len(proved)} ({len(proved) / max(1, len(results)):.0%})")
    print(f"  projected over    : {int(len(proved) / max(1, len(results)) * len(targets)):,} of {len(targets):,} rejected candidates")
    print()
    print("proof kind:")
    for kind, count in kinds.most_common():
        print(f"  {count:>4}  {kind}")
    print("page that carried it:")
    for page, count in pages.most_common():
        print(f"  {count:>4}  {page}")
    print()
    for r in proved[:12]:
        print(f"  {r['orgnr']} {str(r['name'])[:30]:30} {r['path']:18} {str(r['proof'])[:44]}")
    Path(args.json).write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in results) + "\n", encoding="utf-8")
    print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
