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
from norway_company_agent.evidence import evidence, utc_now  # noqa: E402
from norway_company_agent.identity import apply_website_identity_gate  # noqa: E402
from norway_company_agent.official import fetch_official_modules  # noqa: E402
from norway_company_agent.website import fetch_website  # noqa: E402
from norway_company_agent.site_discovery import resolve_website  # noqa: E402
from norway_company_agent.site_candidates import build_domain_counts  # noqa: E402
from norway_company_agent.site_intel import SiteFetcher, site_intel_for  # noqa: E402
from norway_company_agent.claims_view import contract_view  # noqa: E402
from norway_company_agent.brief import build_brief  # noqa: E402
from norway_company_agent.changes import changes_since  # noqa: E402
from norway_company_agent.search_client import SearchBatch  # noqa: E402
from norway_company_agent.search_enrich import MAX_NEWS_PER_COMPANY, is_directory, queries_for, verify_news, website_hits  # noqa: E402
from norway_company_agent.procurement import ProcurementIndex, procurement_record  # noqa: E402
from norway_company_agent.wikidata import WikidataIndex, wikidata_record  # noqa: E402
from norway_company_agent.nav_jobs import NavDetailClient, NavWebClient, NavJobIndex, catch_up, jobs_for_profile  # noqa: E402


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
    parser.add_argument("--modules", default="registry,accounting_obligation,registry_live,financials,roles,group,locations,wikidata,procurement,website,jobs,search")
    parser.add_argument("--max-site-candidates", type=int, default=4)
    parser.add_argument("--max-site-guesses", type=int, default=3,
                        help="Name-derived domains fetched per company, after DNS keeps only the ones that exist")
    parser.add_argument("--no-domain-guess", action="store_true")
    parser.add_argument("--no-site-intel", dest="site_intel", action="store_false", help="skip news/careers reading on verified sites")
    parser.add_argument("--max-site-posts", type=int, default=3)
    parser.add_argument("--llm-model", help="local GGUF model for unlabelled careers pages (needs `uv sync --extra llm`)")
    parser.add_argument("--previous", help="profiles JSONL from an earlier run; envelopes then list what changed")
    parser.add_argument("--nav-cache", default=str(ROOT / "data-cache" / "nav-feed"), help="NAV active job-ad index directory")
    parser.add_argument("--search-providers", default="brave,gdelt",
                        help="providers for the search pass; keys come from environment variables (BRAVE_SEARCH_API_KEY)")
    parser.add_argument("--max-news-queries", type=int, default=250)
    parser.add_argument("--news-min-employees", type=int, default=5)
    parser.add_argument("--search-cache", default=str(ROOT / "data-cache" / "search"))
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
    fetch_modules = set(requested_modules) - {"registry", "accounting_obligation", "website", "jobs", "wikidata", "procurement"}
    operations = {"requests": 0, "bytes": 0, "latencies_ms": []}
    domain_counts = build_domain_counts(args.bulk) if "website" in requested_modules else None
    extractor = None
    if args.llm_model:
        from norway_company_agent.llm_extract import LocalExtractor
        extractor = LocalExtractor(args.llm_model)
    wikidata_index = WikidataIndex.load() if "wikidata" in requested_modules else None
    procurement_index = ProcurementIndex.load() if "procurement" in requested_modules else None
    nav_index = NavJobIndex.load(args.nav_cache) if "jobs" in requested_modules else None
    nav_client = NavWebClient(pause=1.0) if nav_index else None  # live re-check via NAV's public ad pages
    nav_catch_up = None
    if nav_index and args.nav_catch_up_pages:
        meta_path = Path(args.nav_cache) / "active-ads.meta.json"
        cursor = json.loads(meta_path.read_text()).get("feed_next_url") if meta_path.exists() else None
        feed_client = NavDetailClient()
        try:
            nav_catch_up = catch_up(nav_index, feed_client.feed, cursor, max_pages=args.nav_catch_up_pages)
        except Exception as exc:  # the index alone still works; every ad is re-checked anyway
            nav_catch_up = {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}
        operations["requests"] += feed_client.feed.requests

    def enrich(profile: dict) -> tuple[dict, dict]:
        records, metrics = fetch_official_modules(profile["organisation_number"], fetch_modules)
        profile["evidence"].update(records)
        website_metrics = {"requests": 0, "bytes": 0, "latencies_ms": []}
        if "wikidata" in requested_modules:
            profile["evidence"]["wikidata"] = wikidata_record(profile, wikidata_index)
        if "procurement" in requested_modules:
            profile["evidence"]["procurement"] = procurement_record(profile, procurement_index)
        wikidata_sites = (profile["evidence"].get("wikidata", {}).get("value") or {}).get("websites") or []
        nav_sites = nav_index.homepages(profile["organisation_number"]) if nav_index else []
        if "website" in requested_modules:
            website_record, website_metrics = resolve_website(
                profile, domain_counts, max_candidates=args.max_site_candidates, max_guesses=args.max_site_guesses,
                guess=not args.no_domain_guess, extra_websites=wikidata_sites + nav_sites)
            profile["evidence"]["website"] = website_record
        intel, intel_metrics = None, {"requests": 0, "bytes": 0, "latencies_ms": []}
        if "website" in requested_modules and args.site_intel:
            intel, intel_metrics = site_intel_for(profile, profile["evidence"]["website"], max_posts=args.max_site_posts,
                                                   extractor=extractor)
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
            # The search pass runs after every company's first pass, so a run stopped in between keeps its
            # first-pass profiles and only repeats the search pass for them.
            if profile_complete_for_modules(item, [m for m in requested_modules if m != "search"])
        }
        resumed_profiles = len(state)
    pending_profiles = [profile for profile in profiles if profile["organisation_number"] not in state]
    search_pending = [profile for profile in profiles if profile["organisation_number"] not in state
                      or not profile_complete_for_modules(state[profile["organisation_number"]], ["search"])]
    search_batch = None
    if "search" in requested_modules:
        # News search is paced by its provider (GDELT: one request per 5.5 s), so the largest employers go first.
        by_size = sorted([state.get(p["organisation_number"], p) for p in search_pending],
                         key=lambda p: -(p.get("employees") or 0))
        news_orgs = {p["organisation_number"] for p in by_size
                     if (p.get("employees") or 0) >= args.news_min_employees}
        news_orgs = set([p["organisation_number"] for p in by_size if p["organisation_number"] in news_orgs][: args.max_news_queries])
        queries = [q for profile in by_size for q in queries_for(
            profile, want_website=not profile.get("website"), want_news=profile["organisation_number"] in news_orgs)]
        search_batch = SearchBatch(queries, cache_dir=args.search_cache, providers=args.search_providers)

    def search_pass(profile: dict) -> tuple[dict, dict]:
        """Second pass: search-found website candidates (strict proof) and news articles naming the exact entity."""
        org = profile["organisation_number"]
        metric = {"requests": 0, "bytes": 0, "latencies_ms": []}
        if search_batch is None or search_batch.error:
            profile["evidence"]["search"] = evidence("search", "source_error", "search_api_candidates", "about:search",
                                                     note=(search_batch.error if search_batch else None) or "search not run")
            return profile, metric
        web_state, web_hits = search_batch.results(f"{org}:web")
        news_state, news_hits = search_batch.results(f"{org}:news")
        value = {"web": web_state if not profile.get("website") else "not_needed", "news": news_state,
                 "website_candidates": [], "mentions": []}
        site = profile["evidence"].get("website") or {}
        verified = site.get("status") == "available" and ((site.get("value") or {}).get("identity_assessment") or {}).get("publishable")
        if "website" in requested_modules and not verified and web_hits:
            value["website_candidates"] = website_hits(web_hits)
            if value["website_candidates"]:
                record, m = resolve_website(profile, domain_counts, search_websites=value["website_candidates"])
                for key in metric:
                    metric[key] = metric[key] + m[key]
                if record.get("status") == "available" and ((record.get("value") or {}).get("identity_assessment") or {}).get("publishable"):
                    profile["evidence"]["website"] = record
                    value["website_from_search"] = record["value"].get("final_url")
                    if args.site_intel:
                        intel, m = site_intel_for(profile, record, max_posts=args.max_site_posts, extractor=extractor)
                        for key in metric:
                            metric[key] = metric[key] + m[key]
                        if intel is not None:
                            record["value"]["activity"] = intel
        fetcher = SiteFetcher()
        for hit in [h for h in news_hits if not is_directory(h["url"])][:MAX_NEWS_PER_COMPANY * 2]:
            if len(value["mentions"]) >= MAX_NEWS_PER_COMPANY:
                break
            try:
                mention = verify_news(profile, hit, fetcher)
            except Exception:
                continue  # unreachable or disallowed article: skipped, never guessed
            if mention and mention["url"] not in {m["url"] for m in value["mentions"]}:
                value["mentions"].append(mention)
        metric["requests"] += fetcher.requests
        metric["bytes"] += fetcher.bytes
        metric["latencies_ms"] += fetcher.latencies_ms
        status = "available" if value["mentions"] or value.get("website_from_search") else \
            "not_found" if "searched" in (value["web"], value["news"]) else "source_error"
        profile["evidence"]["search"] = evidence("search", status, "search_api_candidates", "about:search", value=value,
                                                 note="Search results are candidates; published items carry their own identity proof")
        return profile, metric

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

    if search_batch is not None:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for profile, metric in pool.map(search_pass, [state[p["organisation_number"]] for p in search_pending]):
                state[profile["organisation_number"]] = profile
                operations["requests"] += metric["requests"]
                operations["bytes"] += metric["bytes"]
                operations["latencies_ms"].extend(metric["latencies_ms"])
        operations["search"] = search_batch.stats or {"error": search_batch.error}
        operations["third_party_cost_usd"] = (search_batch.stats or {}).get("estimated_cost_usd", 0)
        write_jsonl(profiles_output, [state[org] for org in orgs if org in state])

    completed_at = utc_now()
    ordered_profiles = [state[org] for org in orgs]
    previous: dict[str, dict] = {}
    if args.previous and Path(args.previous).exists():
        for line in Path(args.previous).read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                previous[row["organisation_number"]] = row
    envelopes = []
    for profile in ordered_profiles:
        envelope = terminal_envelope(profile, run_id=args.run_id, modules=requested_modules,
                                     started_at=started_at, completed_at=completed_at)
        envelope.update(contract_view(profile), brief=build_brief(profile))
        prior = previous.get(profile["organisation_number"])
        envelope["changes"] = changes_since(prior, profile) if prior else []
        envelope["refresh"] = {"previous_run_found": bool(prior), "previous_completed_at": (prior or {}).get("completed_at")}
        envelopes.append(envelope)
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
        "local_model": {"model": extractor.model_id, "calls": extractor.calls} if extractor else None,
        "nav_jobs": {"index_built_at": nav_index.built_at if nav_index else None, "catch_up": nav_catch_up},
        "validation": validation,
    }
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if validation["passed"] else 1)


if __name__ == "__main__":
    main()
