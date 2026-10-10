"""A short, decision-useful brief per company, written only from published evidence.

Every sentence carries the source URLs it rests on. Nothing is inferred beyond simple arithmetic on
filed figures (growth, margin, equity ratio); anything not found is listed under `unknown`, never
written as zero.
"""
from __future__ import annotations

from typing import Any


def _nok(value: float | None) -> str:
    if value is None:
        return "unknown"
    sign = "-" if value < 0 else ""
    value = abs(value)
    if value >= 1e9:
        return f"{sign}NOK {value / 1e9:.1f} bn"
    if value >= 1e6:
        return f"{sign}NOK {value / 1e6:.1f} m"
    if value >= 1e3:
        return f"{sign}NOK {value / 1e3:.0f} k"
    return f"{sign}NOK {value:.0f}"


def _pct(new: float | None, old: float | None) -> float | None:
    if new is None or old in (None, 0):
        return None
    return (new - old) / abs(old) * 100


def build_brief(profile: dict[str, Any]) -> dict[str, Any]:
    ev = profile.get("evidence") or {}
    sentences: list[dict[str, Any]] = []
    signals: list[str] = []
    figures: dict[str, Any] = {}
    unknown: list[str] = []

    def say(text: str, *records: dict[str, Any] | None) -> None:
        sources = [r.get("source_url") for r in records if r and r.get("source_url")]
        sentences.append({"text": text, "sources": list(dict.fromkeys(sources))})

    live = ev.get("registry_live") or {}
    reg = live.get("value") or {}
    name = profile.get("name") or reg.get("name")
    form = profile.get("legal_form") or reg.get("legal_form") or ""
    industry = (profile.get("industry_label") or (reg.get("industry") or {}).get("beskrivelse") or "").strip()
    place = profile.get("municipality") or ((reg.get("business_address") or {}).get("kommune"))
    first = f"{name} ({form}, org. no. {profile['organisation_number']})"
    if industry:
        first += f" is registered for {industry.casefold()}"
    if place:
        first += f" in {str(place).title()}"
    employees = reg.get("employees") if reg else profile.get("employees")
    first += f", with {employees} registered employees." if employees else "."
    say(first, live or ev.get("registry"))
    if profile.get("bankrupt") or reg.get("bankrupt"):
        signals.append("bankrupt")
        say("The registry marks the company as bankrupt.", live)
    if profile.get("liquidating") or reg.get("liquidating"):
        signals.append("in liquidation")
        say("The registry marks the company as under liquidation.", live)
    figures["employees"] = employees
    if not employees:
        unknown.append("employee count")

    fin = ev.get("financials") or {}
    records = sorted(((fin.get("value") or {}).get("records") or []),
                     key=lambda r: (r.get("period") or {}).get("tilDato") or "")
    if records:
        last = records[-1]
        year = ((last.get("period") or {}).get("tilDato") or "")[:4]
        text = f"In {year} it reported revenue of {_nok(last.get('revenue'))} and a result of {_nok(last.get('annual_result'))}"
        if len(records) >= 2:
            growth = _pct(last.get("revenue"), records[-2].get("revenue"))
            if growth is not None:
                text += f"; revenue {'grew' if growth >= 0 else 'fell'} {abs(growth):.0f}% on the year before"
                if growth >= 20:
                    signals.append("revenue growing fast")
                elif growth <= -20:
                    signals.append("revenue falling")
        say(text + ".", fin)
        equity, assets = last.get("equity"), last.get("assets")
        figures.update(year=year, revenue=last.get("revenue"), result=last.get("annual_result"), equity=equity,
                       assets=assets, revenue_growth_pct=round(growth, 1) if len(records) >= 2 and growth is not None else None,
                       equity_ratio_pct=round(equity / assets * 100, 1) if equity is not None and assets else None)
        if equity is not None and assets:
            ratio = equity / assets * 100
            say(f"Equity was {_nok(equity)}, {ratio:.0f}% of total assets of {_nok(assets)}.", fin)
            if equity < 0:
                signals.append("negative equity")
            elif ratio < 10:
                signals.append("thin equity")
        losses = [r for r in records[-3:] if (r.get("annual_result") or 0) < 0]
        if len(records) >= 3 and len(losses) == 3:
            signals.append("loss in each of the last three years")
        elif (last.get("annual_result") or 0) > 0:
            signals.append("profitable last year")
    else:
        unknown.append("filed accounts")

    roles = ((ev.get("roles") or {}).get("value") or {}).get("roles") or []
    active = [r for r in roles if not r.get("inactive")]

    def names(code: str) -> list[str]:
        out = []
        for r in active:
            if r.get("role_code") == code:
                n = r.get("name")
                out.append(", ".join(n) if isinstance(n, list) else str(n))
        return out

    ceo, chair, auditor = names("DAGL"), names("LEDE"), names("REVI")
    parts = []
    if ceo:
        parts.append(f"managing director {ceo[0]}")
    if chair:
        parts.append(f"board chair {chair[0]}")
    if auditor:
        parts.append(f"auditor {auditor[0]}")
    if parts:
        say("Leadership on record: " + ", ".join(parts) + ".", ev.get("roles"))
    else:
        unknown.append("leadership")

    locations = ((ev.get("locations") or {}).get("value") or {}).get("locations") or []
    if len(locations) > 1:
        towns = sorted({str((u.get("address") or {}).get("poststed") or "").title() for u in locations} - {""})
        say(f"It has {len(locations)} registered business locations" + (f" ({', '.join(towns[:5])})." if towns else "."),
            ev.get("locations"))
        signals.append("multi-site")

    jobs = ev.get("jobs") or {}
    jv = jobs.get("value") or {}
    count = jv.get("active_job_count") or 0
    if jobs.get("status") == "available" and count:
        newest = (jv.get("postings") or jv.get("company_site_postings") or [{}])[0]
        title = newest.get("title") or newest.get("job_title")
        say(f"It is hiring: {count} active job ad{'s' if count != 1 else ''}"
            + (f", most recently “{title}”" if title else "") + ".", jobs)
        signals.append("hiring")
    elif jobs.get("status") == "not_found":
        say("No active job ad names this company or its branches on NAV's job board.", jobs)
    else:
        unknown.append("job ads (source could not be checked)")

    proc = ev.get("procurement") or {}
    pv = proc.get("value") or {}
    if proc.get("status") == "available" and pv.get("notices"):
        latest = pv["notices"][0]
        won = pv.get("awards_won") or 0
        lead = (f"It has won {won} public contract{'s' if won != 1 else ''} published on TED" if won
                else f"It appears in {pv.get('notice_count')} public-procurement notice{'s' if pv.get('notice_count') != 1 else ''} on TED")
        say(f"{lead}; the latest, on {latest.get('published')}, is “{latest.get('title')}”.", proc)
        signals.append("public-sector supplier" if won else "public buyer")

    mentions = ((ev.get("search") or {}).get("value") or {}).get("mentions") or []
    if mentions:
        latest = max(mentions, key=lambda m: str(m.get("published") or ""))
        sentences.append({"text": f"It is named in {len(mentions)} news article{'s' if len(mentions) != 1 else ''}; "
                                  f"the latest, on {str(latest.get('published'))[:10]} in {latest.get('outlet')}, is “{latest.get('title')}”.",
                          "sources": [m["url"] for m in mentions][:3]})
        signals.append("in the news")
    elif ((ev.get("search") or {}).get("value") or {}).get("news") not in (None, "searched"):
        unknown.append("news coverage (search not checked)")

    web = ev.get("website") or {}
    wv = web.get("value") or {}
    from .site_intel import own_site_reason

    verified_site = web.get("status") == "available" and (wv.get("identity_assessment") or {}).get("publishable")
    if verified_site and not own_site_reason(profile, wv):
        say(f"Its registry contact details lead to {wv.get('final_url')}, a manager's, parent's or group site rather than its own.", web)
        unknown.append("own website")
        signals.append("part of a group or managed")
    elif verified_site:
        socials = sorted({link["platform"] for link in wv.get("social_links") or []})
        wd = (ev.get("wikidata") or {}).get("value") or {}
        socials = sorted(set(socials) | {link["platform"] for link in wd.get("social_profiles") or []})
        text = f"Its website {wv.get('final_url')} is verified as its own"
        if socials:
            text += f", linking to {', '.join(socials)}"
        posts = (wv.get("activity") or {}).get("posts") or []
        if posts:
            text += f"; latest dated post {str(posts[0].get('published'))[:10]}"
        say(text + ".", web, ev.get("wikidata") if wd.get("social_profiles") else None)
        signals.append("verified website")
    else:
        unknown.append("website")
        signals.append("no verified web presence")

    headline = sentences[0]["text"] if sentences else name
    return {"headline": headline, "sentences": sentences, "signals": signals, "unknown": unknown,
            "key_figures": figures, "method": "evidence_only_brief_v1"}
