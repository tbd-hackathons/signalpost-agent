"""Second pass that uses search results: website candidates and dated news mentions.

Search finds candidates; it never proves identity.
- A website candidate goes through `resolve_website(..., search_websites=...)`, which demands the same strict
  proof as a guessed domain (organisation number, registry contact details, or legal name plus place).
- A news article is published only when its own text names the exact legal entity: the organisation number,
  or the full legal name (with its legal form) together with the registered municipality or postal town.
  Names that are a single common word never qualify on name alone.
"""
from __future__ import annotations

import hashlib
import re
import urllib.parse
from typing import Any

from .evidence import utc_now
from .nav_jobs import LEGAL_SUFFIX, norm_name

DIRECTORY_HOSTS = {
    "proff.no", "purehelp.no", "1881.no", "gulesider.no", "brreg.no", "finn.no", "facebook.com", "linkedin.com",
    "instagram.com", "wikipedia.org", "regnskapstall.no", "forvalt.no", "bedriftsdatabasen.no", "firmasok.no",
    "kompass.com", "dnb.com", "youtube.com", "x.com", "twitter.com", "nav.no", "arbeidsplassen.nav.no",
    "allabolag.se", "opencorporates.com", "enirogule.no", "180.no", "krak.no", "hitta.se", "tiktok.com",
    "proff.dk", "northdata.com", "northdata.de", "infobel.com", "yelp.com", "tripadvisor.com", "google.com",
    "apple.com", "wikidata.org", "seierstad.no", "bisnode.no", "ratsit.se", "dagenshandel.no", "gelbeseiten.de",
    "boligmappa.no", "mittanbud.no", "anbudstorget.no", "doffin.no", "ted.europa.eu", "lovdata.no", "regjeringen.no",
}
NEWS_FORMS = {"AS", "ASA", "SA", "STI", "KF", "IKS", "ANS", "DA", "NUF", "SF", "FLI"}
MAX_NEWS_PER_COMPANY = 5


def _host(url: str) -> str:
    return (urllib.parse.urlparse(url).hostname or "").casefold().removeprefix("www.")


def is_directory(url: str) -> bool:
    host = _host(url)
    return any(host == d or host.endswith("." + d) for d in DIRECTORY_HOSTS)


def distinctive_name(name: str) -> bool:
    """At least two meaningful words, or one long coined word; avoids "Bygg AS" style names."""
    words = [w for w in norm_name(name).split() if len(w) >= 3]
    return len(words) >= 2 or (len(words) == 1 and len(words[0]) >= 8)


def queries_for(profile: dict[str, Any], *, want_website: bool, want_news: bool) -> list[dict[str, Any]]:
    org = str(profile["organisation_number"])
    base = " ".join(LEGAL_SUFFIX.sub(" ", str(profile.get("name") or "")).split())
    out = []
    if want_website and base:
        place = (profile.get("municipality") or "").title()
        out.append({"id": f"{org}:web", "query": f"\"{base}\" {place}".strip(), "kind": "web", "count": 8, "lang": "no"})
    if want_news and distinctive_name(profile.get("name") or "") and (profile.get("legal_form") or "") in NEWS_FORMS:
        out.append({"id": f"{org}:news", "query": f"\"{base}\"", "kind": "news", "count": 10, "lang": "no"})
    return out


def website_hits(hits: list[dict[str, Any]], limit: int = 3) -> list[str]:
    seen, out = set(), []
    for hit in hits:
        host = _host(hit["url"])
        if not host or is_directory(hit["url"]) or host in seen:
            continue
        seen.add(host)
        out.append(hit["url"])
    return out[:limit]


def _places(profile: dict[str, Any]) -> list[str]:
    values = [profile.get("municipality")]
    ev = profile.get("evidence") or {}
    for key in ("registry_live", "registry"):
        value = (ev.get(key) or {}).get("value") or {}
        for address in (value.get("business_address"), value.get("postal_address")):
            if isinstance(address, dict):
                values += [address.get("poststed"), address.get("kommune")]
    name = norm_name(profile.get("name"))
    return [p for p in {str(v).casefold() for v in values if v} if p and p not in name]


def identity_in_text(profile: dict[str, Any], text: str) -> str | None:
    """Return the proof sentence when the article names the exact legal entity, else None."""
    org = str(profile["organisation_number"])
    digits = re.sub(r"\D", "", text)
    if org in digits and re.search(r"\b" + r"\s?".join([org[:3], org[3:6], org[6:]]) + r"\b", text):
        return f"the article states organisation number {org}"
    if not distinctive_name(profile.get("name") or ""):
        return None
    legal = " ".join(str(profile.get("name") or "").split())
    folded = text.casefold()
    if legal.casefold() not in folded:
        return None
    for place in _places(profile):
        if re.search(r"\b" + re.escape(place) + r"\b", folded):
            return f"the article names the legal entity '{legal}' and its registered place '{place.title()}'"
    return None


def verify_news(profile: dict[str, Any], hit: dict[str, Any], fetcher) -> dict[str, Any] | None:
    """Fetch the article (robots.txt respected) and keep it only with exact-entity proof in its own text."""
    import trafilatura

    raw, final_url, ctype = fetcher.get(hit["url"])
    if "html" not in ctype.lower():
        return None
    html = raw.decode("utf-8", errors="replace")
    text = trafilatura.extract(html, include_comments=False) or ""
    proof = identity_in_text(profile, text)
    if not proof:
        return None
    meta = trafilatura.extract_metadata(html)
    published = (meta.date if meta and meta.date else None) or hit.get("published")
    if not published:
        return None
    legal = str(profile.get("name") or "")
    sentence = next((s.strip() for s in re.split(r"(?<=[.!?])\s+", text)
                     if legal.casefold() in s.casefold() or str(profile["organisation_number"]) in re.sub(r"\D", "", s)), "")
    return {"title": (meta.title if meta and meta.title else hit.get("title") or "")[:300], "url": final_url,
            "outlet": _host(final_url), "published": str(published)[:25],
            "date_source": "article_metadata" if meta and meta.date else "search_index",
            "found_by": hit.get("provider"), "identity_proof": proof, "evidence_span": sentence[:400],
            "retrieved_at": utc_now(), "content_sha256": hashlib.sha256(raw).hexdigest()}


def news_observation(profile: dict[str, Any], mention: dict[str, Any]) -> dict[str, Any]:
    key = f"{profile['organisation_number']}|news|{mention['url']}"
    return {
        "id": "news:" + hashlib.sha256(key.encode()).hexdigest()[:20],
        "organisation_number": str(profile["organisation_number"]), "platform": "news",
        "signal_type": "public_mention", "source_class": "public_news", "source_url": mention["url"],
        "item_url": mention["url"], "retrieved_at": mention["retrieved_at"], "observed_at": mention["published"],
        "content_sha256": mention["content_sha256"], "exact_entity": True, "identity_proof": mention["identity_proof"],
        "acquisition_mode": "permitted_public_page", "rights_status": "approved",
        "evidence_span": mention["evidence_span"] or mention["title"], "value": mention,
    }
