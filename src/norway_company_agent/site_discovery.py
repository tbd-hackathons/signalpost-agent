"""Try website candidates in order and publish the first one that proves it is this exact entity.

Publication rules
- registry website / company-specific registry e-mail domain: the starter's identity gate
  (organisation number or legal name on the homepage) OR registry contact details on the site.
- guessed or shared domains (`requires_strict_proof`): organisation number on the homepage OR
  registry contact details on the site. Name similarity alone never publishes a guessed domain.
Rejected candidates are kept in `value.discovery.attempts` with the reason, never published.
"""
from __future__ import annotations

import re
import urllib.parse
from collections import Counter
from difflib import SequenceMatcher
from typing import Any

from .identity import apply_website_identity_gate
from .site_candidates import contact_proof, place_proof, website_candidates
from .website import fetch_website


def _page_text(value: dict[str, Any]) -> str:
    parts = [value.get("title"), value.get("description"), value.get("identity_text_excerpt"), value.get("main_text_excerpt")]
    for page in value.get("pages") or []:
        parts += [page.get("title"), page.get("identity_text_excerpt"), page.get("main_text_excerpt")]
    return " ".join(str(p or "") for p in parts)


def _publish_socials(value: dict[str, Any]) -> None:
    value["social_links"] = [
        {"platform": item["platform"], "url": item["url"]}
        for item in value.get("social_link_assessments") or []
        if item.get("publishable")
    ]


def _fold(text: str) -> str:
    text = text.casefold().replace("å", "aa").replace("ø", "o").replace("æ", "ae")
    return re.sub(r"[^a-z0-9]", "", text)


def _domain_is_legal_name(domain_label: str, legal_name: str) -> bool:
    """The site's domain name spells the legal name (allowing å->aa and similar), not a parent or manager."""
    name = _fold(re.sub(r"\b(as|asa|ans|da|sa|ba|ks|nuf|enk)\b", " ", legal_name.casefold()))
    if not name:
        return False
    variants = {name, name.replace("aa", "a")}
    label = {domain_label, domain_label.replace("aa", "a")}
    return any(a == b or (len(b) >= 5 and b in a) or SequenceMatcher(None, a, b).ratio() >= 0.85
               for a in label for b in variants)


def _accept_domain_handles(value: dict[str, Any], legal_name: str = "") -> None:
    """A profile whose handle contains the verified site's domain name is the company's own.

    The legal-name rule misses brands written differently from the legal name, e.g. GLÅME AS ->
    glaame.no -> facebook.com/glaame.no. The domain itself passed the identity gate, so its name is
    as strong as the legal name. Short or generic domain names (under 5 letters) are not used.
    """
    domain = str(value.get("registered_domain") or "")
    label = re.sub(r"[^a-z0-9]", "", domain.split(".")[0].casefold())
    own_domain = len(label) >= 5 and _domain_is_legal_name(label, legal_name)
    # On a site that passed the identity gate, a handle carrying the legal name's most distinctive word
    # (8+ letters, not a trade word) is the company's own, e.g. BYGGMESTER IVER R. GRESSETH -> ByggmesterGresseth.
    words = [_fold(w) for w in re.findall(r"\w+", legal_name.casefold())]
    distinctive = max((w for w in words if len(w) >= 8 and w not in GENERIC_HANDLE_WORDS), key=len, default="")
    for item in value.get("social_link_assessments") or []:
        if item.get("publishable"):
            continue
        handle = re.sub(r"[^a-z0-9]", "", urllib.parse.unquote(urllib.parse.urlparse(item["url"]).path).casefold())
        if own_domain and label in handle:
            item.update(publishable=True, identity_score=0.95, method="verified_domain_name_in_handle_v1",
                        reason=f"handle contains the verified website's domain name '{label}'")
        elif own_domain and CHANNEL_ID_PATH.match(urllib.parse.urlparse(item["url"]).path):
            # A bare channel id carries no name; on a site whose domain spells the legal name, the site's own link is the proof.
            item.update(publishable=True, identity_score=0.9, method="channel_id_linked_from_named_site_v1",
                        reason=f"channel linked from the company's own site '{domain}', whose domain spells the legal name")
        elif distinctive and distinctive in handle:
            item.update(publishable=True, identity_score=0.92, method="distinctive_name_word_in_handle_v1",
                        reason=f"handle contains the legal name's distinctive word '{distinctive}'")
    _publish_socials(value)


CHANNEL_ID_PATH = re.compile(r"^/channel/UC[\w-]{22}/?$")
GENERIC_HANDLE_WORDS = {
    "borettslag", "boligsameie", "eiendom", "eiendommer", "barnehage", "byggmester", "entreprenor", "regnskap",
    "regnskapskontor", "transport", "elektro", "installasjon", "consulting", "holding", "invest", "investering",
    "forvaltning", "utvikling", "tannlege", "tannhelse", "fysioterapi", "advokatfirma", "maskinentreprenor",
    "byggservice", "rorleggerfirma", "rorlegger", "eiendomsmegling", "restaurant", "frisorsalong", "treningssenter",
    "boligbyggelag", "forsikring", "logistikk", "teknologi", "technology", "solutions", "services", "management",
}


CONTACT_LINK = re.compile(r"(?i)(kontakt|contact|om-oss|om_oss|omoss|\bom oss\b|about|hvem-er-vi|firmainfo|impressum)")


def _deeper_proof(profile: dict[str, Any], value: dict[str, Any], metrics: dict[str, Any]) -> str | None:
    """Look for proof on up to two contact/about pages of the same site (robots.txt respected).

    Used only for a near-miss: the domain spells the legal name and the homepage shows it, but the homepage
    has no organisation number, registry contact or registered place. The proof rules are unchanged.
    """
    from bs4 import BeautifulSoup

    from .site_intel import SiteFetcher, registered_domain

    home = value.get("final_url") or ""
    domain = registered_domain(home)
    urls = [l["url"] for l in value.get("site_links") or []
            if registered_domain(l["url"]) == domain and (CONTACT_LINK.search(l["url"]) or CONTACT_LINK.search(l.get("text") or ""))]
    if not urls:
        base = "{0.scheme}://{0.netloc}".format(urllib.parse.urlparse(home))
        urls = [base + "/kontakt", base + "/om-oss"]
    fetcher = SiteFetcher()
    org = str(profile["organisation_number"])
    found = None
    for url in list(dict.fromkeys(urls))[:2]:
        try:
            raw, final_url, ctype = fetcher.get(url)
        except Exception:
            continue
        if "html" not in ctype.lower() or registered_domain(final_url) != domain:
            continue
        text = BeautifulSoup(raw.decode("utf-8", errors="replace"), "lxml").get_text(" ", strip=True)
        if org in re.sub(r"\D", "", text) and re.search(r"\b" + r"\s?".join([org[:3], org[3:6], org[6:]]) + r"\b", text):
            found = f"exact organisation number appears on {final_url}"
        else:
            proof = contact_proof(profile, text) or place_proof(profile, text)
            found = f"{proof} ({final_url})" if proof else None
        if found:
            break
    metrics["requests"] += fetcher.requests
    metrics["bytes"] += fetcher.bytes
    metrics["latencies_ms"] += fetcher.latencies_ms
    return found


DECLARED_SOURCES = {"registry_website", "organisation_number_keyed_source"}
STRONG_OWN_REASONS = {"organisation number on the homepage", "domain name spells the legal name"}


PLACEHOLDER_PAGE = re.compile(
    r"\b(?:domene(?:t)?|domain|nettsted(?:et)?|website)\b[^.\n]{0,30}\b(?:er |is )?(?:parkert|parked|til salgs|for sale)\b|"
    r"\bis parked\b|kjøp (?:dette )?domenet|buy this domain|default web page|welcome to nginx|account suspended", re.I)


def _related_link_holds(cand: dict[str, Any], value: dict[str, Any]) -> bool:
    """A declared site is only a related site when the page is still on the declared domain and is a real site.

    A lapsed registry website often redirects to the registrar's parking page (webhuset.no/parkering): that
    is neither the company nor a manager, parent or group, so labelling it related would invent a relationship.
    """
    from .site_intel import registered_domain

    final_url = str(value.get("final_url") or "")
    declared = registered_domain("https://" + str(cand.get("host") or "") + "/")
    if not final_url or registered_domain(final_url) != declared:
        return False
    page_text = " ".join(str(value.get(k) or "") for k in ("title", "main_text_excerpt"))[:2000]
    return not PLACEHOLDER_PAGE.search(page_text)


def resolve_website(profile: dict[str, Any], domain_counts: Counter | None, *, max_candidates: int = 3,
                    max_guesses: int = 3, guess: bool = True, extra_websites: list[str] | None = None,
                    search_websites: list[str] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """`extra_websites`: sites an organisation-number-keyed source (Wikidata, NAV employer) records for this company.

    `search_websites`: domains a search API returned for the company name. They are tried alone (the other
    candidates already failed in the first pass) and need the same strict proof as a guessed domain.
    """
    metrics: dict[str, Any] = {"requests": 0, "bytes": 0, "latencies_ms": []}
    candidates = [] if search_websites else website_candidates(profile, domain_counts, guess=guess,
                                                              max_guesses=max_guesses)
    for url in search_websites or []:
        host = (urllib.parse.urlparse(url).hostname or "").casefold().removeprefix("www.")
        if host and host not in {c["host"] for c in candidates}:
            candidates.append({"url": f"https://{host}/", "host": host, "source": "search_api_candidate",
                               "reason": "a search API returned this site for the legal name",
                               "requires_strict_proof": True})
    hosts = {c["host"] for c in candidates}
    for url in extra_websites or []:
        host = (urllib.parse.urlparse(url if "://" in url else "https://" + url).hostname or "").casefold().removeprefix("www.")
        if host and host not in hosts:
            hosts.add(host)
            # Ahead of name guesses, behind the registry's own website and e-mail domain.
            position = sum(1 for c in candidates if c["source"] != "legal_name_domain_guess")
            candidates.insert(position, {"url": url if "://" in url else "https://" + url, "host": host,
                                         "source": "organisation_number_keyed_source",
                                         "reason": "Wikidata or NAV records this website for the organisation number",
                                         "requires_strict_proof": False})
    # Declared and guessed candidates get their own budget: a company with several registry candidates must
    # not use up the slots that a name-derived domain needs, and guesses are capped because each one costs a fetch.
    declared = [c for c in candidates if c["source"] != "legal_name_domain_guess"]
    guesses = [c for c in candidates if c["source"] == "legal_name_domain_guess"]
    candidates = declared[:max_candidates] + guesses[:max_guesses]
    attempts: list[dict[str, Any]] = []
    fallback: dict[str, Any] | None = None
    # A website the registry records for this organisation number that is reachable but does not present
    # itself as the company's own: a housing co-operative's manager, a subsidiary's parent, an accountant.
    # The registry links them, so the link is worth reporting — as a labelled related site, never collapsed
    # into the company's own website.
    related_fallback: dict[str, Any] | None = None

    for cand in candidates:
        record, m = fetch_website(cand["url"])
        metrics["requests"] += m.get("requests", 0)
        metrics["bytes"] += m.get("bytes", 0)
        metrics["latencies_ms"] += m.get("latencies_ms", [])
        attempt = {k: cand[k] for k in ("url", "source", "reason", "requires_strict_proof")}
        if record.get("status") != "available":
            attempt.update(outcome="unreachable", status=record.get("status"), note=record.get("note"))
            attempts.append(attempt)
            fallback = fallback or record
            continue

        gated = apply_website_identity_gate(profile, record)
        site, assessment = gated["website"], gated["assessment"] or {}
        value = site.get("value") or {}
        org_on_page = assessment.get("score") == 1.0
        proof = contact_proof(profile, _page_text(value))
        if cand["requires_strict_proof"]:
            if not org_on_page and not proof and assessment.get("score", 0) >= 0.95:
                # Full legal name on the homepage plus the registered town is enough for a guessed domain.
                proof = place_proof(profile, _page_text(value))
            if not org_on_page and not proof and assessment.get("score", 0) >= 0.95:
                proof = _deeper_proof(profile, value, metrics)
            accept = org_on_page or bool(proof)
        else:
            accept = bool(assessment.get("publishable")) or bool(proof)
            if not accept and cand["source"] in DECLARED_SOURCES and assessment.get("score", 0) >= 0.3:
                # The company itself declared this site (registry entry) or an organisation-number-keyed source
                # records it; that, plus a site that presents itself under the company's name, is enough.
                from .site_intel import own_site_reason

                own = own_site_reason(profile, value)
                if own:
                    accept, proof = True, f"declared for this organisation number ({cand['source']}), and {own}"
                    if own not in STRONG_OWN_REASONS:
                        # A parent, umbrella body or fund manager often shares the name words: publish as related.
                        value["related_only"] = True

        if accept and not assessment.get("publishable"):
            # Contact proof upgrades an otherwise uncertain name match.
            assessment = {**assessment, "status": "exact", "score": 0.97, "publishable": True,
                          "reasons": [*assessment.get("reasons", []), proof],
                          "method": "declared_site_named_v1" if str(proof).startswith("declared") else "registry_contact_on_site_v1"}
            value["identity_assessment"] = assessment
            _publish_socials(value)
        elif not accept and assessment.get("publishable"):
            # Strict candidate with only a name match: do not publish.
            assessment = {**assessment, "status": "related_or_uncertain", "publishable": False,
                          "reasons": [*assessment.get("reasons", []),
                                      "guessed/shared domain needs organisation number or registry contact on the site"]}
            value["identity_assessment"] = assessment
            value["social_links"] = []

        attempt.update(outcome="accepted" if accept else "rejected", identity=assessment.get("status"),
                       score=assessment.get("score"), proof=proof)
        attempts.append(attempt)
        if accept:
            _accept_domain_handles(value, profile.get("name") or "")
            value["discovery"] = {"selected": attempt, "attempts": attempts, "method": "site_discovery_v1"}
            site["value"] = value
            return site, metrics
        if (not accept and record.get("status") == "available" and not cand["requires_strict_proof"]
                and cand["source"] in DECLARED_SOURCES and related_fallback is None
                and _related_link_holds(cand, value)):
            reasons = [str(r) for r in assessment.get("reasons") or []]
            reasons.append(f"the registry records {cand['host']} for this organisation number, but the site "
                           "does not present itself as this company's own (a manager, parent or group site)")
            value["related_only"] = True
            value["identity_assessment"] = {**assessment, "publishable": False, "reasons": reasons}
            value["social_links"] = []
            record["value"] = value
            related_fallback = record
        fallback = fallback or site

    if related_fallback is not None:
        # Nothing of the company's own was proven, but the registry does link it to this reachable site:
        # report the link as related rather than dropping what the register itself declares. Reaching here
        # means no candidate was accepted, so this is the most informative record left.
        value = related_fallback["value"]
        value["discovery"] = {"selected": None, "attempts": attempts, "method": "site_discovery_v1",
                              "related_fallback": True}
        return related_fallback, metrics
    if fallback is None:
        fallback, _ = fetch_website(None)  # the starter's standard not_found evidence record
        fallback["note"] = "No website candidate from registry website, registry e-mail domain or legal-name domain"
    elif fallback.get("status") == "available":
        # A candidate was reachable but not proven: keep it as not published.
        value = fallback.get("value") or {}
        value["social_links"] = []
    if not isinstance(fallback.get("value"), dict):
        # `setdefault` leaves an explicit None in place, which would silently drop the attempts on a blocked or
        # failed candidate. Every attempt is kept, accepted or not (agent playbook, "save every attempt").
        fallback["value"] = {}
    fallback["value"]["discovery"] = {"selected": None, "attempts": attempts, "method": "site_discovery_v1"}
    return fallback, metrics
