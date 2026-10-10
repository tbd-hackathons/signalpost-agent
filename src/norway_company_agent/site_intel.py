"""Read more from a website that already passed the identity gate: dated news posts and job ads.

Runs only for a published (exact-entity) company website. Every page is fetched with robots.txt
respected (one robots.txt request per host, cached) and must stay on the company's registered domain,
except job ads that the company's own careers page links to on a known recruiting system.

Signals produced, in the starter's observation schema (`external_footprint.py`):
- public_post: news/blog posts found in the site's sitemap or homepage links, newest first; only posts
  that were fetched and have a title and a date are published.
- job_posting: schema.org JobPosting entries on the careers page, and links from the careers page to
  individual ads on recruiting systems (Webcruiter, Jobbnorge, Recman, Teamtailor, FINN, NAV, ...).
- profile_handle: the company-owned social profiles the website gate already approved.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
import threading
import time
import urllib.parse
import urllib.request
import urllib.robotparser
from typing import Any

import extruct
import tldextract
from bs4 import BeautifulSoup

from .evidence import utc_now
from .website import SAFE_OPENER, USER_AGENT, assert_public_url

CAREER_TERMS = ("karriere", "ledige-stillinger", "ledige stillinger", "stillinger", "jobb", "career", "jobs",
                "vacanc", "work-with-us", "rekruttering")
NEWS_TERMS = ("nyheter", "nyhet", "aktuelt", "news", "blogg", "blog", "artikler", "artikkel", "presse", "press",
              "innlegg", "post")
ATS_AD_PATTERNS = (
    re.compile(r"^https?://(www\.)?finn\.no/(job/.*finnkode=\d+|job/ad/\d+|\d+)"),
    re.compile(r"^https?://arbeidsplassen\.nav\.no/stillinger/stilling/[0-9a-f-]{36}"),
    re.compile(r"^https?://[\w-]+\.teamtailor\.com/jobs/\d+"),
    re.compile(r"^https?://(candidate|recruiter)\.webcruiter\.com/.*(job|advert)\w*[/=]\d+", re.I),
    re.compile(r"^https?://(www\.)?jobbnorge\.no/.*job/\d+"),
    re.compile(r"^https?://[\w-]+\.recman\.page/job/\d+|^https?://app\.recman\.no/(v2/)?job\.php\?job_id=\d+"),
    re.compile(r"^https?://[\w.-]*easycruit\.com/vacancy/\d+"),
    re.compile(r"^https?://[\w.-]*hrmanager\.no/.*(job|stilling)\w*[/=]\d+", re.I),
    re.compile(r"^https?://[\w.-]*jobylon\.com/jobs/\d+"),
    re.compile(r"^https?://[\w.-]*reachmee\.com/.*(job|ad)\w*[/=]\d+", re.I),
)
DATE_META = ("article:published_time", "og:published_time", "datePublished", "pubdate", "date", "dc.date",
             "article:modified_time")
MAX_SITEMAP_URLS = 5000


def registered_domain(url: str) -> str:
    return tldextract.extract(urllib.parse.urlparse(url).hostname or "").top_domain_under_public_suffix


class SiteFetcher:
    """GETs with robots.txt checked once per host. Counts requests for the run report."""

    def __init__(self, timeout: float = 12.0, max_bytes: int = 1_500_000) -> None:
        self.timeout, self.max_bytes = timeout, max_bytes
        self.requests, self.bytes, self.latencies_ms = 0, 0, []
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._lock = threading.Lock()

    def _open(self, url: str) -> tuple[bytes, str, str]:
        assert_public_url(url)
        started = time.monotonic()
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
        self.requests += 1
        with SAFE_OPENER.open(request, timeout=self.timeout) as response:
            raw = response.read(self.max_bytes + 1)
            final_url, ctype = response.geturl(), response.headers.get("content-type", "")
        self.latencies_ms.append(int((time.monotonic() - started) * 1000))
        self.bytes += len(raw)
        if len(raw) > self.max_bytes:
            raise ValueError("response exceeds byte limit")
        return raw, final_url, ctype

    def robots(self, url: str) -> urllib.robotparser.RobotFileParser | None:
        parsed = urllib.parse.urlparse(url)
        key = f"{parsed.scheme}://{parsed.netloc}"
        with self._lock:
            if key in self._robots:
                return self._robots[key]
        parser: urllib.robotparser.RobotFileParser | None = urllib.robotparser.RobotFileParser()
        try:
            raw, _, _ = self._open(key + "/robots.txt")
            parser.parse(raw.decode("utf-8", errors="replace").splitlines())
        except Exception:
            parser = None  # no robots file: ordinary bounded GETs are allowed, as in the starter
        with self._lock:
            self._robots[key] = parser
        return parser

    def allowed(self, url: str) -> bool:
        parser = self.robots(url)
        return True if parser is None else parser.can_fetch(USER_AGENT, url)

    def get(self, url: str) -> tuple[bytes, str, str]:
        if not self.allowed(url):
            raise PermissionError("robots.txt disallows this page")
        return self._open(url)


def parse_sitemap(raw: bytes) -> tuple[list[dict[str, str]], list[str]]:
    """Return (page entries with loc/lastmod, child sitemap URLs)."""
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    text = raw.decode("utf-8", errors="replace")
    children = [m.strip() for m in re.findall(r"<sitemap>.*?<loc>(.*?)</loc>", text, re.S | re.I)]
    entries = []
    for block in re.findall(r"<url>(.*?)</url>", text, re.S | re.I)[:MAX_SITEMAP_URLS]:
        loc = re.search(r"<loc>(.*?)</loc>", block, re.S | re.I)
        if loc:
            lastmod = re.search(r"<lastmod>(.*?)</lastmod>", block, re.S | re.I)
            entries.append({"url": _unescape(loc.group(1).strip()), "lastmod": lastmod.group(1).strip() if lastmod else ""})
    return entries, [_unescape(c) for c in children]


def _unescape(value: str) -> str:
    return value.replace("&amp;", "&").replace("<![CDATA[", "").replace("]]>", "").strip()


def _segments(url: str) -> list[str]:
    return [s for s in urllib.parse.urlparse(url).path.casefold().split("/") if s]


def is_post_url(url: str) -> bool:
    """A single post: a news-like section followed by a slug, not the section index itself."""
    segs = _segments(url)
    for i, seg in enumerate(segs[:-1]):
        if seg in NEWS_TERMS or any(seg.startswith(t) for t in ("nyhet", "aktuelt", "blogg", "artikl")):
            slug = segs[-1]
            return not slug.isdigit() and not slug.startswith("page") and not slug.startswith("side") and len(slug) >= 6
    return False


CAREER_SEGMENT = re.compile(r"^(karriere|jobb|ledig|stilling|career|jobs|vacanc|work-with-us|rekrutt|bli-en-av-oss)")


def is_career_url(url: str, text: str = "") -> bool:
    """A careers section: a short path whose segment starts with a careers word, or short link text naming one."""
    segs = _segments(url)
    if len(segs) <= 2 and any(CAREER_SEGMENT.match(seg) for seg in segs):
        return True
    label = text.casefold().strip()
    return 0 < len(label) <= 40 and len(segs) <= 2 and any(term in label for term in CAREER_TERMS)


def is_ats_ad(url: str) -> bool:
    return any(p.search(url) for p in ATS_AD_PATTERNS)


def _published_date(soup: BeautifulSoup, structured: dict[str, Any]) -> tuple[str | None, str | None]:
    for item in _walk_jsonld(structured.get("json-ld", [])):
        if item.get("datePublished"):
            return str(item["datePublished"]), "json_ld_datePublished"
    for key in DATE_META:
        tag = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key}) or soup.find("meta", attrs={"itemprop": key})
        if tag and tag.get("content"):
            return str(tag["content"]), f"meta_{key}"
    tag = soup.find("time", attrs={"datetime": True})
    if tag:
        return str(tag["datetime"]), "html_time_element"
    return None, None


def _walk_jsonld(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_jsonld(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_jsonld(child)


def _types(item: dict[str, Any]) -> set[str]:
    kind = item.get("@type")
    return set(kind if isinstance(kind, list) else [kind])


def job_postings_from_jsonld(structured: dict[str, Any], page_url: str) -> list[dict[str, Any]]:
    jobs = []
    for item in _walk_jsonld(structured.get("json-ld", [])):
        if "JobPosting" not in _types(item) or not item.get("title"):
            continue
        org = item.get("hiringOrganization")
        org_name = org.get("name") if isinstance(org, dict) else org if isinstance(org, str) else None
        location = item.get("jobLocation")
        locations = location if isinstance(location, list) else [location] if location else []
        places = []
        for loc in locations:
            address = (loc or {}).get("address") if isinstance(loc, dict) else None
            if isinstance(address, dict):
                places.append({k: address.get(k) for k in ("addressLocality", "addressRegion", "postalCode") if address.get(k)})
        jobs.append({"title": str(item["title"])[:300], "url": item.get("url") or page_url,
                     "date_posted": item.get("datePosted"), "valid_through": item.get("validThrough"),
                     "employment_type": item.get("employmentType"), "hiring_organization": org_name,
                     "work_locations": places, "found_by": "schema_org_JobPosting"})
    return jobs


def _name_tokens(name: str) -> set[str]:
    from .site_candidates import name_tokens
    return set(name_tokens(name))


class SiteIntel:
    def __init__(self, fetcher: SiteFetcher | None = None, *, max_posts: int = 3, max_career_pages: int = 1) -> None:
        self.fetcher = fetcher or SiteFetcher()
        self.max_posts, self.max_career_pages = max_posts, max_career_pages

    def _html(self, url: str, domain: str) -> tuple[BeautifulSoup, dict[str, Any], str, str] | None:
        raw, final_url, ctype = self.fetcher.get(url)
        if "html" not in ctype.lower() or registered_domain(final_url) != domain:
            return None
        html = raw.decode("utf-8", errors="replace")
        structured = extruct.extract(html, base_url=final_url, syntaxes=["json-ld"])
        return BeautifulSoup(html, "lxml"), structured, final_url, hashlib.sha256(raw).hexdigest()

    def sitemap_entries(self, home: str) -> tuple[list[dict[str, str]], list[str]]:
        parser = self.fetcher.robots(home)
        base = "{0.scheme}://{0.netloc}".format(urllib.parse.urlparse(home))
        queue = list((parser.site_maps() if parser else None) or [base + "/sitemap.xml"])[:2]
        entries, read, errors = [], 0, []
        while queue and read < 3:
            url = queue.pop(0)
            read += 1
            try:
                raw, _, _ = self.fetcher.get(url)
                found, children = parse_sitemap(raw)
            except Exception as exc:
                errors.append(f"{url}: {type(exc).__name__}")
                continue
            entries.extend(found)
            # From an index, prefer the child sitemap most likely to hold posts.
            children.sort(key=lambda c: (not any(t in c.casefold() for t in ("post", "news", "nyhet", "artik", "blog")), c))
            queue.extend(children[:2])
        return entries, errors

    def collect(self, profile: dict[str, Any], website: dict[str, Any]) -> dict[str, Any]:
        value = website.get("value") or {}
        home = value.get("final_url") or website.get("source_url")
        domain = value.get("registered_domain") or registered_domain(home)
        assessment = value.get("identity_assessment") or {}
        proof = (f"published on {domain}, which passed the website identity gate "
                 f"({assessment.get('method') or 'website_identity'}, score {assessment.get('score')})")
        result: dict[str, Any] = {"posts": [], "jobs": [], "observations": [], "errors": [], "method": "site_intel_v1"}
        links = value.get("site_links") or []

        entries, errors = self.sitemap_entries(home)
        result["errors"] += errors
        result["sitemap_urls"] = len(entries)

        # Dated posts: dated sitemap entries first (newest first), then live homepage links, then undated entries.
        dated: dict[str, str] = {}
        undated: list[str] = []
        for entry in entries:
            if registered_domain(entry["url"]) == domain and is_post_url(entry["url"]):
                if entry["lastmod"]:
                    dated[entry["url"]] = entry["lastmod"]
                else:
                    undated.append(entry["url"])
        linked = [l["url"] for l in links if registered_domain(l["url"]) == domain and is_post_url(l["url"])]
        ordered = sorted(dated.items(), key=lambda kv: kv[1], reverse=True)
        ordered += [(url, "") for url in dict.fromkeys(linked + undated) if url not in dated]
        result["post_candidates"] = len(ordered)
        attempts = 0
        for url, lastmod in ordered:
            if len(result["posts"]) >= self.max_posts or attempts >= self.max_posts + 2:
                break
            attempts += 1
            try:
                page = self._html(url, domain)
            except Exception as exc:
                result["errors"].append(f"{url}: {type(exc).__name__}")
                continue
            if not page:
                continue
            soup, structured, final_url, sha = page
            og = soup.find("meta", attrs={"property": "og:title"})
            h1 = soup.find("h1")
            title = (og.get("content") if og and og.get("content") else h1.get_text(" ", strip=True) if h1 else
                     soup.title.get_text(" ", strip=True) if soup.title else "").strip()[:300]
            published, date_source = _published_date(soup, structured)
            if not published and lastmod:
                published, date_source = lastmod, "sitemap_lastmod"
            if not title or not published:
                continue
            post = {"title": title, "url": final_url, "published": published, "date_source": date_source,
                    "retrieved_at": utc_now(), "content_sha256": sha}
            result["posts"].append(post)
            result["observations"].append(self._observation(profile, "company_site", "public_post", post,
                                                            f"{title} ({published})", proof))

        # Job ads from the careers page.
        career_pages = [l["url"] for l in links if registered_domain(l["url"]) == domain and is_career_url(l["url"], l["text"])]
        career_pages += [e["url"] for e in entries if registered_domain(e["url"]) == domain and is_career_url(e["url"])]
        career_pages = sorted(dict.fromkeys(career_pages), key=lambda u: (len(_segments(u)), u))
        ats_links = {l["url"]: l["text"] for l in links if is_ats_ad(l["url"])}
        result["career_pages"] = career_pages[:3]
        for url in career_pages[: self.max_career_pages]:
            try:
                page = self._html(url, domain)
            except Exception as exc:
                result["errors"].append(f"{url}: {type(exc).__name__}")
                continue
            if not page:
                continue
            soup, structured, final_url, sha = page
            company = _name_tokens(profile.get("name") or "")
            for job in job_postings_from_jsonld(structured, final_url):
                org = _name_tokens(job.get("hiring_organization") or "")
                if org and company and not (org & company):
                    continue  # a sister company's ad on a shared group careers page
                job.update(retrieved_at=utc_now(), content_sha256=sha, listed_on=final_url)
                result["jobs"].append(job)
            for anchor in soup.select("a[href]"):
                href = urllib.parse.urljoin(final_url, str(anchor.get("href") or ""))
                if is_ats_ad(href):
                    ats_links.setdefault(href, anchor.get_text(" ", strip=True))
            for href, text in ats_links.items():
                if text and len(text) >= 4:
                    result["jobs"].append({"title": text[:300], "url": href, "found_by": "careers_page_link_to_recruiting_system",
                                           "listed_on": final_url, "retrieved_at": utc_now(), "content_sha256": sha})
            ats_links = {}
        seen = set()
        jobs = []
        for job in result["jobs"]:
            if job["url"] not in seen:
                seen.add(job["url"])
                jobs.append(job)
        result["jobs"] = jobs
        for job in jobs:
            platform = "company_site" if registered_domain(job["url"]) == domain else "job_board"
            result["observations"].append(self._observation(profile, platform, "job_posting", job, job["title"], proof))

        for link in value.get("social_links") or []:
            item = {"url": link["url"], "retrieved_at": website.get("retrieved_at") or utc_now(),
                    "content_sha256": value.get("content_sha256")}
            obs = self._observation(profile, link["platform"], "profile_handle", item,
                                    f"{link['platform']} link on {domain}", proof)
            result["observations"].append(obs)
        result["operations"] = {"requests": self.fetcher.requests}
        return result

    @staticmethod
    def _observation(profile: dict[str, Any], platform: str, signal: str, item: dict[str, Any], span: str,
                     proof: str) -> dict[str, Any]:
        key = f"{profile['organisation_number']}|{signal}|{item['url']}"
        return {
            "id": "site:" + hashlib.sha256(key.encode()).hexdigest()[:20],
            "organisation_number": str(profile["organisation_number"]),
            "platform": platform,
            "signal_type": signal,
            "source_class": "company_controlled",
            "source_url": item.get("listed_on") or item["url"],
            "item_url": item["url"],
            "retrieved_at": item["retrieved_at"],
            "observed_at": item.get("published") or item.get("date_posted"),
            "content_sha256": item.get("content_sha256"),
            "exact_entity": True,
            "identity_proof": proof,
            "acquisition_mode": "permitted_public_page",
            "rights_status": "approved",
            "evidence_span": span,
            "value": {k: v for k, v in item.items() if k not in {"retrieved_at", "content_sha256"}},
        }


def own_site_reason(profile: dict[str, Any], value: dict[str, Any]) -> str | None:
    """Why this verified website is the company's *own* site, not a manager's, accountant's or parent's.

    The registry often links a condominium or housing co-operative to its manager's site, and a
    subsidiary to its parent's. Such a site is a fine website fact, but its job ads and news belong to
    the other company, so activity is attributed only when one of these holds.
    """
    from .site_discovery import _domain_is_legal_name, _fold

    assessment = value.get("identity_assessment") or {}
    if assessment.get("score") == 1.0:
        return "organisation number on the homepage"
    label = re.sub(r"[^a-z0-9]", "", str(value.get("registered_domain") or "").split(".")[0].casefold())
    if len(label) >= 4 and _domain_is_legal_name(label, profile.get("name") or ""):
        return "domain name spells the legal name"
    words = [_fold(w) for w in re.sub(r"\b(as|asa|ans|da|sa|ba|ks|nuf|enk)\b", " ", str(profile.get("name") or "").casefold()).split()]
    words = [w for w in words if w]
    name = "".join(words)
    if len(name) >= 5 and name in _fold(value.get("title") or ""):
        return "full legal name in the page title"
    if len(words) >= 2 and len(label) >= 5 and any(label == "".join(words[:k]) for k in range(2, len(words))):
        return "domain name spells the start of the legal name"
    # Registry contact details on the site are not enough here: a group often shares one mailbox.
    return None


def site_intel_for(profile: dict[str, Any], website: dict[str, Any], *, max_posts: int = 3) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Run SiteIntel when the website is published and is the company's own; return (result or None, metrics)."""
    value = website.get("value") or {}
    if website.get("status") != "available" or not (value.get("identity_assessment") or {}).get("publishable"):
        return None, {"requests": 0, "bytes": 0, "latencies_ms": []}
    own = own_site_reason(profile, value)
    if not own:
        return ({"posts": [], "jobs": [], "observations": [], "errors": [], "method": "site_intel_v1",
                 "skipped": "verified link, but the site looks like a manager's or parent's: its jobs and posts are not attributed"},
                {"requests": 0, "bytes": 0, "latencies_ms": []})
    intel = SiteIntel(max_posts=max_posts)
    try:
        result = intel.collect(profile, website)
    except Exception as exc:
        result = {"posts": [], "jobs": [], "observations": [], "errors": [f"{type(exc).__name__}: {str(exc)[:160]}"],
                  "method": "site_intel_v1"}
    result["own_site"] = own
    f = intel.fetcher
    return result, {"requests": f.requests, "bytes": f.bytes, "latencies_ms": f.latencies_ms}
