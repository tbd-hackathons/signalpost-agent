"""Website candidates beyond the registry `hjemmeside` field.

Order, strongest first:
  1. registry website (hjemmeside)
  2. domain of the registry e-mail address (epostadresse), unless it is free mail
     or shared by several unrelated entities (ISP, accountant, group mailbox)
  3. .no domains derived from the legal name, kept only if DNS resolves

Every candidate is only a candidate. Publication still goes through the website
identity gate; guessed domains face a stricter gate (see `requires_strict_proof`).
"""
from __future__ import annotations

import csv
import gzip
import re
import socket
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .website import resolve_host

FREE_MAIL = {
    "gmail.com", "googlemail.com", "hotmail.com", "hotmail.no", "outlook.com", "outlook.no", "live.com",
    "live.no", "msn.com", "yahoo.com", "yahoo.no", "icloud.com", "me.com", "mac.com", "aol.com",
    "mail.com", "gmx.com", "gmx.net", "protonmail.com", "proton.me", "online.no", "frisurf.no",
    "broadpark.no", "getmail.no", "c2i.net", "start.no", "lyse.net", "altibox.no", "bluezone.no",
    "enivest.net", "tele2.no", "haugnett.no", "telia.no", "netcom.no", "chello.no", "kvinesdal.net",
    "hesbynett.no", "vikenfiber.no", "tdcnet.no", "nextgentel.com", "ngtel.no", "eunet.no",
    "loqal.no", "ostfoldnett.no", "pc.nu", "sensewave.com", "mimer.no", "nordnet.no", "tussa.com",
}
LEGAL_SUFFIX = {"as", "asa", "ans", "da", "enk", "sa", "nuf", "ba", "sti", "iks", "kf", "fli", "brl", "esek"}
GENERIC = {"og", "and", "the", "norge", "norway", "company", "holding", "invest", "eiendom", "service", "services"}
SHARED_DOMAIN_LIMIT = 3  # more distinct entities than this on one e-mail domain = not company-specific


def _fold(value: str) -> str:
    value = value.translate(str.maketrans({"ø": "o", "Ø": "O", "å": "a", "Å": "A", "æ": "ae", "Æ": "AE"}))
    return unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().casefold()


def name_tokens(name: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", _fold(name or "")) if t not in LEGAL_SUFFIX]


def email_domain(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    domain = email.rsplit("@", 1)[1].strip().strip(".").casefold()
    return domain if re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", domain) else None


def build_domain_counts(bulk_path: str | Path) -> Counter:
    """How many registry entities use each e-mail domain, from the bulk snapshot.

    Cached next to the snapshot as `<snapshot>.email-domains.json`, keyed by the snapshot's size and
    mtime, so a run only pays the full scan once per snapshot."""
    import json, os
    bulk_path = Path(bulk_path)
    stat = os.stat(bulk_path)
    key = f"{stat.st_size}:{int(stat.st_mtime)}"
    cache = bulk_path.with_name(bulk_path.name + ".email-domains.json")
    if cache.exists():
        data = json.loads(cache.read_text())
        if data.get("snapshot_key") == key:
            return Counter(data["counts"])
    counts: Counter = Counter()
    with gzip.open(bulk_path, "rt", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            domain = email_domain(row.get("epostadresse"))
            if domain:
                counts[domain] += 1
    cache.write_text(json.dumps({"snapshot_key": key, "counts": counts}))
    return counts


def _resolves(host: str) -> bool:
    try:
        resolve_host(host, attempts=2)
        return True
    except (socket.gaierror, UnicodeError, OSError):
        return False


_DNS_CACHE: dict[str, bool] = {}
# Six at a time. A wider pool outruns the local resolver, which then reports live domains as missing.
_DNS_WIDTH = 6


def _resolve_all(hosts: list[str]) -> list[str]:
    """The hostnames that resolve, memoised for the process and probed in parallel.

    DNS is the cheap filter that makes a wide guess list affordable: a name yields ten plausible domains and
    usually only one or two exist, so probing is cheaper than fetching and rejecting. Parallel because a
    batch of a thousand companies is otherwise ten thousand serial lookups.
    """
    todo = [host for host in hosts if host not in _DNS_CACHE]
    if todo:
        with ThreadPoolExecutor(max_workers=min(_DNS_WIDTH, len(todo))) as pool:
            for host, ok in zip(todo, pool.map(_resolves, todo)):
                _DNS_CACHE[host] = ok
    return [host for host in hosts if _DNS_CACHE.get(host)]


# Trade words that rarely form part of a domain, though they are common in legal names.
TRADE = {
    "eiendom", "eiendommer", "holding", "invest", "investering", "forvaltning", "service", "services",
    "group", "gruppen", "norge", "norway", "company", "drift", "bygg", "entreprenor", "consulting",
    "teknologi", "technology", "solutions", "management", "partner", "partners", "nordic", "nordisk",
    "avdeling", "filial", "av", "og", "med", "for", "til", "borettslaget", "boligsameiet", "sameiet",
}
GUESS_TLDS = ("no", "com")


def _name_variants(name: str) -> list[str]:
    """The legal name in the spellings a domain could use: the plain one, and a with å written aa."""
    name = str(name or "")
    variants = [_fold(name)]
    if "å" in name.casefold():
        variants.append(_fold(name.replace("å", "aa").replace("Å", "AA")))
    return list(dict.fromkeys(variants))


def _slug_labels(name: str) -> list[str]:
    """Domain labels the legal name could plausibly own, best first.

    "NORDLYS EIENDOM BERGEN AS" yields nordlysbergen, nordlyseiendombergen, nordlyseiendom, nordlys. Ordering
    matters more than recall here: the caller fetches in order and stops at the first site that proves identity.
    """
    labels: list[str] = []

    def add(*parts: str) -> None:
        label = "".join(parts)
        if 3 <= len(label) <= 40 and label not in labels:
            labels.append(label)

    for variant in _name_variants(name):
        tokens = [t for t in re.findall(r"[a-z0-9]+", variant) if t not in LEGAL_SUFFIX]
        if not any(len(t) >= 3 for t in tokens):
            continue  # "A B C D E F AS": no word to build a domain from
        core = [t for t in tokens if t not in TRADE and t not in GENERIC]
        if not core:
            continue  # "EIENDOM AS": every word is a trade word, so a guess only finds a stranger's site
        add(*core)
        if len(core) > 1:
            add(core[0], *core[2:])  # drop the second word, often the trade word that did survive filtering
            add(*core[:2])
            add(*core[-2:])
            add(core[0])
            add("-".join(core))  # hyphenated form, tried after the joined ones
        if tokens != core and len(tokens) <= 6:
            add(*tokens)
    return labels


def guessed_domains(name: str) -> list[str]:
    """Ordered domain guesses from the legal name, best first. Candidates only: the site must still prove identity."""
    return [f"{label}.{tld}" for label in _slug_labels(name) for tld in GUESS_TLDS]


def website_candidates(profile: dict[str, Any], domain_counts: Counter | None, *, guess: bool = True,
                       max_guesses: int = 3) -> list[dict[str, Any]]:
    raw = ((profile.get("evidence") or {}).get("registry") or {}).get("value") or {}
    seen: set[str] = set()
    out: list[dict[str, Any]] = []

    def add(url: str, source: str, reason: str, strict: bool) -> None:
        host = re.sub(r"^https?://", "", url).split("/")[0].casefold().removeprefix("www.")
        if host and host not in seen:
            seen.add(host)
            out.append({"url": url, "host": host, "source": source, "reason": reason, "requires_strict_proof": strict})

    homepage = profile.get("website") or raw.get("hjemmeside")
    if homepage:
        add(homepage, "registry_website", "Brønnøysund registry website field", False)

    email = raw.get("epostadresse")
    domain = email_domain(email)
    if domain and domain not in FREE_MAIL:
        shared = (domain_counts or {}).get(domain, 1)
        if shared <= SHARED_DOMAIN_LIMIT:
            add(f"https://{domain}/", "registry_email_domain", f"domain of registry e-mail ({shared} entities use it)", False)
        else:
            add(f"https://{domain}/", "registry_email_domain_shared", f"registry e-mail domain shared by {shared} entities", True)

    if guess:
        # Always considered, not only when the registry is silent. A registry website that is dead or fails
        # the identity gate would otherwise end the search for a company whose own domain is a plain variant
        # of its name. Declared candidates are tried first, so this only adds work when they do not publish.
        name = profile.get("name") or raw.get("navn") or ""
        live = _resolve_all(guessed_domains(name))
        for host in live[:max_guesses]:
            add(f"https://{host}/", "legal_name_domain_guess", "domain derived from the legal name resolves in DNS", True)
    return out


def contact_proof(profile: dict[str, Any], text: str) -> str | None:
    """Registry contact details printed on the site prove the site belongs to this entity."""
    raw = ((profile.get("evidence") or {}).get("registry") or {}).get("value") or {}
    folded = _fold(text or "")
    email = (raw.get("epostadresse") or "").strip().casefold()
    if email and email in folded:
        return "registry e-mail address appears on the site"
    digits = re.sub(r"\D", "", text or "")
    for key in ("telefon", "mobil"):
        phone = re.sub(r"\D", "", raw.get(key) or "")[-8:]
        if len(phone) == 8 and phone in digits:
            return f"registry {key} number appears on the site"
    return None


def _full_name_present(profile: dict[str, Any], folded_text: str) -> bool:
    """Every word of the legal name, not just one of them.

    "ARKWRIGHT X AS" reduces to a single distinctive token, so a name-only gate cannot tell it apart from
    "Arkwright Consulting", which also has an Oslo office. Requiring the whole legal name — short tokens
    included — is what makes a place match mean something.
    """
    tokens = name_tokens(profile.get("name") or "")
    return bool(tokens) and all(
        re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", folded_text) for token in tokens)


def place_proof(profile: dict[str, Any], text: str) -> str | None:
    """Registered post town or street address on the site. Only used together with an exact legal-name match."""
    raw = ((profile.get("evidence") or {}).get("registry") or {}).get("value") or {}
    folded = " ".join(re.findall(r"[a-z0-9]+", _fold(text or "")))
    if not _full_name_present(profile, folded):
        return None  # one shared word and a town is not an identity; the whole name has to be there
    street = " ".join(re.findall(r"[a-z0-9]+", _fold(raw.get("forretningsadresse.adresse") or "")))
    if street and len(street) >= 6 and street in folded:
        return "registered street address appears on the site"
    postcode = str(raw.get("forretningsadresse.postnummer") or "").strip()
    if len(postcode) == 4 and re.search(rf"(?<!\d){postcode}(?!\d)", folded):
        return f"registered postcode {postcode} appears on the site"
    name = " ".join(name_tokens(profile.get("name") or ""))
    for key in ("forretningsadresse.poststed", "postadresse.poststed", "forretningsadresse.kommune"):
        town = " ".join(re.findall(r"[a-z0-9]+", _fold(raw.get(key) or "")))
        if re.search(rf"(?<![a-z0-9]){re.escape(town)}(?![a-z0-9])", name):
            continue  # the town is part of the name ("Oslo Science City"): finding it proves nothing more
        if len(town) >= 3 and re.search(rf"(?<![a-z0-9]){re.escape(town)}(?![a-z0-9])", folded):
            return f"registered {key.split('.')[-1]} '{town}' appears on the site with the full legal name"
    return None
