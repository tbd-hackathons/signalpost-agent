#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.batch import profile_complete_for_modules, profiles_from_bulk, read_organisation_inputs, terminal_envelope, validate_envelopes  # noqa: E402
from norway_company_agent.evidence import utc_now  # noqa: E402
from norway_company_agent.identity import apply_website_identity_gate  # noqa: E402
from norway_company_agent.official import fetch_official_modules  # noqa: E402
from norway_company_agent.website import fetch_website  # noqa: E402
from norway_company_agent.site_discovery import resolve_website  # noqa: E402
from norway_company_agent.site_candidates import build_domain_counts  # noqa: E402
from norway_company_agent.site_intel import site_intel_for  # noqa: E402
from norway_company_agent.claims_view import contract_view  # noqa: E402
from norway_company_agent.nav_jobs import NavDetailClient, NavJobIndex, catch_up, jobs_for_profile  # noqa: E402


NAV_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluator-owned Signalpost batch contract")
    parser.add_argument("--organisations", required=True, help="JSON, JSONL, or text organisation-number list")
    parser.add_argument("--bulk", required=True, help="Frozen Brreg entity snapshot")
    parser.add_argument("--output", required=True, help="Terminal envelope JSONL")
    parser.add_argument("--profiles-output", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--expected-count", type=int, default=100)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--checkpoint-every", type=int, default=25)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--modules", default="registry,accounting_obligation,registry_live,financials,roles,group,locations,website,jobs")
    parser.add_argument("--max-site-candidates", type=int, default=3)
    parser.add_argument("--no-domain-guess", action="store_true")
    parser.add_argument("--no-site-intel", dest="site_intel", action="store_false", help="skip news/careers reading on verified sites")
    parser.add_argument("--max-site-posts", type=int, default=3)
    parser.add_argument("--nav-cache", default=str(ROOT / "data-cache" / "nav-feed"), help="NAV active job-ad index directory")
    parser.add_argument("--nav-catch-up-pages", type=int, default=60, help="feed pages read for ads newer than the index")
    args = parser.parse_args()

    started_at = utc_now()
    organisation_inputs = read_organisation_inputs(args.organisations)
    orgs = [item["organisation_number"] for item in organisation_inputs]
    if len(orgs) != args.expected_count:
        raise SystemExit(f"Expected {args.expected_count} organisations, received {len(orgs)}")
    profiles, registry_metadata = profiles_from_bulk(args.bulk, orgs)
    annotations = {item["organisation_number"]: item for item in organisation_inputs}
    for profile in profiles:
        for key in ("evaluation_split", "sample_slice"):
            if key in annotations[profile["organisation_number"]]:
                profile[key] = annotations[profile["organisation_number"]][key]
    requested_modules = [item.strip() for item in args.modules.split(",") if item.strip()]
    fetch_modules = set(requested_modules) - {"registry", "accounting_obligation", "website", "jobs"}
    operations = {"requests": 0, "bytes": 0, "latencies_ms": []}
    domain_counts = build_domain_counts(args.bulk) if "website" in requested_modules else None
    nav_index = NavJobIndex.load(args.nav_cache) if "jobs" in requested_modules else None
    nav_client = NavDetailClient() if nav_index else None
    nav_catch_up = None
    if nav_index and args.nav_catch_up_pages:
        meta_path = Path(args.nav_cache) / "active-ads.meta.json"
        cursor = json.loads(meta_path.read_text()).get("feed_next_url") if meta_path.exists() else None
        try:
            nav_catch_up = catch_up(nav_index, nav_client.feed, cursor, max_pages=args.nav_catch_up_pages)
        except Exception as exc:  # the index alone still works; every ad is re-checked anyway
            nav_catch_up = {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}
        operations["requests"] += nav_client.feed.requests

    def enrich(profile: dict) -> tuple[dict, dict]:
        records, metrics = fetch_official_modules(profile["organisation_number"], fetch_modules)
        profile["evidence"].update(records)
        website_metrics = {"requests": 0, "bytes": 0, "latencies_ms": []}
        if "website" in requested_modules:
            website_record, website_metrics = resolve_website(
                profile, domain_counts, max_candidates=args.max_site_candidates, guess=not args.no_domain_guess)
            profile["evidence"]["website"] = website_record
        intel, intel_metrics = None, {"requests": 0, "bytes": 0, "latencies_ms": []}
        if "website" in requested_modules and args.site_intel:
            intel, intel_metrics = site_intel_for(profile, profile["evidence"]["website"], max_posts=args.max_site_posts)
            if intel is not None:
                profile["evidence"]["website"]["value"]["activity"] = intel
        jobs_metrics = {"requests": 0, "bytes": 0, "latencies_ms": []}
        if "jobs" in requested_modules:
            site_jobs = (intel or {}).get("jobs") or []
            linked = [m.group(0) for job in site_jobs for m in [NAV_UUID.search(job["url"])] if m and "arbeidsplassen.nav.no" in job["url"]]
            profile["evidence"]["jobs"], jobs_metrics = jobs_for_profile(
                profile, nav_index, nav_client, linked_uuids=linked, site_jobs=site_jobs,
                site_observations=(intel or {}).get("observations"))
        metric = {
            "requests": len(metrics) + website_metrics["requests"] + intel_metrics["requests"] + jobs_metrics["requests"],
            "bytes": sum(item.bytes_received for item in metrics) + website_metrics["bytes"] + intel_metrics["bytes"],
            "latencies_ms": [item.elapsed_ms for item in metrics] + website_metrics["latencies_ms"] + intel_metrics["latencies_ms"],
        }
        profile["run_metrics"] = metric
        return profile, metric

    state: dict[str, dict] = {}
    resumed_profiles = 0
    profiles_output = Path(args.profiles_output)
    if args.resume and profiles_output.exists():
        prior = [json.loads(line) for line in profiles_output.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not set(item["organisation_number"] for item in prior).issubset(set(orgs)):
            raise SystemExit("Resume profile membership is not a subset of this batch")
        state = {
            item["organisation_number"]: item
            for item in prior
            if profile_complete_for_modules(item, requested_modules)
        }
        resumed_profiles = len(state)
    pending_profiles = [profile for profile in profiles if profile["organisation_number"] not in state]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(enrich, profile): profile["organisation_number"] for profile in pending_profiles}
        for index, future in enumerate(as_completed(futures), 1):
            profile, metric = future.result()
            state[profile["organisation_number"]] = profile
            operations["requests"] += metric["requests"]
            operations["bytes"] += metric["bytes"]
            operations["latencies_ms"].extend(metric["latencies_ms"])
            if index % args.checkpoint_every == 0 or index == len(pending_profiles):
                checkpoint = [state[org] for org in orgs if org in state]
                write_jsonl(profiles_output, checkpoint)

    completed_at = utc_now()
    ordered_profiles = [state[org] for org in orgs]
    envelopes = [
        {**terminal_envelope(profile, run_id=args.run_id, modules=requested_modules, started_at=started_at, completed_at=completed_at),
         **contract_view(profile)}
        for profile in ordered_profiles
    ]
    validation = validate_envelopes(envelopes, args.expected_count)
    write_jsonl(profiles_output, ordered_profiles)
    write_jsonl(Path(args.output), envelopes)
    latencies = sorted(operations.pop("latencies_ms"))
    operations["p50_ms"] = latencies[len(latencies) // 2] if latencies else None
    operations["p95_ms"] = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] if latencies else None
    report = {
        "run_id": args.run_id,
        "started_at": started_at,
        "completed_at": completed_at,
        "expected_count": args.expected_count,
        "emitted_envelopes": len(envelopes),
        "resumed_profiles": resumed_profiles,
        "profiles_fetched_this_run": len(pending_profiles),
        "modules": requested_modules,
        "registry": registry_metadata,
        "operations": operations,
        "nav_jobs": {"index_built_at": nav_index.built_at if nav_index else None, "catch_up": nav_catch_up},
        "validation": validation,
    }
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if validation["passed"] else 1)


if __name__ == "__main__":
    main()
