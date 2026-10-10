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
from pathlib import Path
from typing import Any

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
        socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        return True
    except (socket.gaierror, UnicodeError, OSError):
        return False


def guessed_domains(name: str) -> list[str]:
    tokens = name_tokens(name)
    distinctive = [t for t in tokens if t not in GENERIC]
    if not distinctive or len(tokens) > 4:
        return []
    joined = "".join(tokens)
    if len(joined) < 5:
        return []
    guesses = [f"{joined}.no"]
    if len(tokens) > 1:
        guesses.append(f"{'-'.join(tokens)}.no")
    if tokens[-1] in GENERIC and len(tokens) > 1:
        guesses.append(f"{''.join(tokens[:-1])}.no")
    return list(dict.fromkeys(guesses))


def website_candidates(profile: dict[str, Any], domain_counts: Counter | None, *, guess: bool = True) -> list[dict[str, Any]]:
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

    if guess and not out:
        for host in guessed_domains(profile.get("name") or raw.get("navn") or ""):
            if _resolves(host):
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


def place_proof(profile: dict[str, Any], text: str) -> str | None:
    """Registered post town or street address on the site. Only used together with an exact legal-name match."""
    raw = ((profile.get("evidence") or {}).get("registry") or {}).get("value") or {}
    folded = " ".join(re.findall(r"[a-z0-9]+", _fold(text or "")))
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
