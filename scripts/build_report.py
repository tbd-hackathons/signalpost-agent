#!/usr/bin/env python3
"""Render a run's envelopes as one self-contained HTML page.

Coverage per information type, then every company as a card: an evidence-only summary whose sentences
link to their sources, signal tags, what is unknown, what changed since the previous run, and a table of
every fact with source and retrieval time. Companies can be searched, filtered by signal, sorted, linked
to directly (#<organisation number>) and compared side by side. Works offline, on desktop and phone.

Usage: python scripts/build_report.py --envelopes out/<run>-envelopes.jsonl --report out/<run>-report.json --output out/<run>.html
"""
from __future__ import annotations

import argparse
import html
import json
from collections import Counter
from pathlib import Path

FAMILIES = [
    ("official_website", "Verified website"),
    ("social_profile", "Social profiles"),
    ("job_posting", "Job ads"),
    ("public_post", "Posts on own site"),
    ("public_activity.procurement", "Public tenders"),
    ("public_activity.news_mention", "News articles"),
]

CSS = """
:root{--bg:#f6f6f3;--card:#fff;--ink:#1c1c1a;--muted:#6a6a64;--line:#e3e3dd;--ok:#1f7a4d;--warn:#a15c00;--bad:#a1262b;--accent:#2f5bd3;--bar:#dfe6fb;--on-accent:#fff}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#131312;--card:#1d1d1b;--ink:#ecebe6;--muted:#9c9b94;--line:#33332f;--ok:#4cc38a;--warn:#e0a24a;--bad:#ef6b6f;--accent:#7d9cff;--bar:#27304d;--on-accent:#0d1430;color-scheme:dark}}
:root[data-theme=dark]{--bg:#131312;--card:#1d1d1b;--ink:#ecebe6;--muted:#9c9b94;--line:#33332f;--ok:#4cc38a;--warn:#e0a24a;--bad:#ef6b6f;--accent:#7d9cff;--bar:#27304d;--on-accent:#0d1430;color-scheme:dark}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,system-ui,"Segoe UI",sans-serif}
main{max-width:1120px;margin:0 auto;padding:24px 16px 96px}h1{font-size:24px;margin:0 0 4px;letter-spacing:-.01em}h2{font-size:15px;margin:24px 0 10px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.04em}
.sub{color:var(--muted);margin:0 0 18px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}.card b{display:block;font-size:24px;font-variant-numeric:tabular-nums}.card span{color:var(--muted);font-size:13px}
.cov{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:6px 14px}.cov div.r{display:grid;grid-template-columns:150px 1fr 220px;gap:10px;align-items:center;padding:6px 0;border-top:1px solid var(--line)}
.cov div.r:first-child{border-top:0}.track{height:10px;background:var(--bar);border-radius:99px;overflow:hidden}.fill{height:100%;background:var(--accent)}.num{font-size:13px;color:var(--muted);text-align:right;font-variant-numeric:tabular-nums}
.note{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--warn);border-radius:8px;padding:10px 14px;margin:16px 0 0}
.tools{position:sticky;top:env(safe-area-inset-top,0px);z-index:5;background:var(--bg);padding:12px 0 8px;display:flex;flex-wrap:wrap;gap:8px;align-items:center}
input[type=search],select{padding:9px 12px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink);font:inherit}input[type=search]{flex:1 1 260px;min-width:0}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:4px 0 12px}.chip{font:inherit;font-size:13px;padding:4px 10px;border-radius:99px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}
.chip[aria-pressed=true]{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}.count{color:var(--muted);font-size:13px}
details.co{background:var(--card);border:1px solid var(--line);border-radius:10px;margin-bottom:8px;scroll-margin-top:80px}details.co[open]{box-shadow:0 1px 8px rgba(0,0,0,.06)}
summary{cursor:pointer;padding:10px 14px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;list-style:none}summary::-webkit-details-marker{display:none}
summary .n{font-weight:600}summary .o{color:var(--muted);font-size:13px;flex:1 1 220px}.tag{font-size:12px;padding:1px 8px;border-radius:99px;border:1px solid var(--line);white-space:nowrap}
.tag.ok{color:var(--ok);border-color:var(--ok)}.tag.sig{color:var(--accent);border-color:var(--accent)}
label.cmp{font-size:13px;color:var(--muted);display:flex;gap:4px;align-items:center;cursor:pointer}
.body{padding:0 14px 14px;overflow-x:auto}.brief{margin:2px 0 8px;max-width:78ch}.src{font-size:11px;text-decoration:none;vertical-align:super}.unk{color:var(--muted);font-size:13px}
.chg{font-size:13px;border-left:3px solid var(--accent);padding:4px 10px;margin:8px 0;background:var(--bar);border-radius:0 6px 6px 0}
table{border-collapse:collapse;width:100%;font-size:13px}td,th{text-align:left;padding:6px 8px;border-top:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:500}
a{color:var(--accent);overflow-wrap:anywhere}.link{display:inline-block;font-size:12px;margin-top:8px}
#tray{position:fixed;left:50%;transform:translateX(-50%);bottom:calc(16px + env(safe-area-inset-bottom,0px));z-index:9;background:var(--ink);color:var(--bg);border-radius:99px;padding:8px 8px 8px 16px;display:none;gap:10px;align-items:center;box-shadow:0 4px 18px rgba(0,0,0,.25)}
#tray button{font:inherit;border:0;border-radius:99px;padding:6px 14px;cursor:pointer}#tray .go{background:var(--accent);color:var(--on-accent)}#tray .clr{background:transparent;color:var(--bg)}
dialog{border:1px solid var(--line);border-radius:12px;background:var(--card);color:var(--ink);padding:0;width:min(1000px,96vw);max-height:90vh}dialog::backdrop{background:rgba(0,0,0,.45)}
.limit{font-size:13px;color:var(--warn)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
dialog header{display:flex;justify-content:space-between;align-items:center;padding:12px 16px;border-bottom:1px solid var(--line)}dialog .x{font:inherit;background:none;border:0;color:var(--ink);font-size:20px;cursor:pointer}
dialog .wrap{overflow:auto;padding:4px 16px 16px}
@media (max-width:640px){.cov div.r{grid-template-columns:1fr}.num{text-align:left}summary .o{flex-basis:100%}}
"""

JS = r"""
const D=JSON.parse(document.getElementById('data').textContent);
const q=document.getElementById('q'),sort=document.getElementById('sort'),list=document.getElementById('list'),shown=document.getElementById('shown');
const chips=[...document.querySelectorAll('.chip')];const picked=new Set();
function apply(){const t=q.value.trim().toLowerCase();const on=chips.filter(c=>c.getAttribute('aria-pressed')==='true').map(c=>c.dataset.k);let n=0;
 for(const el of list.children){const d=D[el.id];const ok=(!t||d.q.includes(t))&&on.every(k=>d.k.includes(k));el.hidden=!ok;if(ok)n++}shown.textContent=n+' shown'}
function order(){const k=sort.value;const els=[...list.children];const v=e=>{const d=D[e.id];return k==='name'?d.name:k==='facts'?-d.facts:-(d.f[k]??-1e18)};
 els.sort((a,b)=>{const x=v(a),y=v(b);return x<y?-1:x>y?1:0});els.forEach(e=>list.appendChild(e))}
chips.forEach(c=>c.addEventListener('click',()=>{c.setAttribute('aria-pressed',c.getAttribute('aria-pressed')==='true'?'false':'true');apply()}));
q.addEventListener('input',apply);sort.addEventListener('change',()=>{order();apply()});
const tray=document.getElementById('tray'),tn=document.getElementById('tn');
function upd(){tn.textContent=picked.size+' selected';tray.style.display=picked.size?'flex':'none';if(!picked.size)lim.hidden=true}
document.querySelectorAll('label.cmp').forEach(l=>l.addEventListener('click',e=>e.stopPropagation()));
const lim=document.getElementById('lim');
document.querySelectorAll('input.pick').forEach(b=>b.addEventListener('change',()=>{if(b.checked&&picked.size>=4){b.checked=false;lim.hidden=false;return}
 lim.hidden=true;b.checked?picked.add(b.value):picked.delete(b.value);upd()}));
document.getElementById('clr').onclick=()=>{picked.clear();document.querySelectorAll('input.pick').forEach(b=>b.checked=false);upd()};
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const nok=v=>v==null?'—':(Math.abs(v)>=1e6?(v/1e6).toFixed(1)+' m':Math.abs(v)>=1e3?(v/1e3).toFixed(0)+' k':String(v));
const pct=v=>v==null?'—':v.toFixed(0)+'%';
document.getElementById('go').onclick=()=>{const ids=[...picked];const rows=[['Organisation number',d=>esc(d.org)],['Municipality',d=>esc(d.place)],['Industry',d=>esc(d.industry)],
 ['Employees',d=>esc(d.f.employees??'—')],['Accounts year',d=>esc(d.f.year||'—')],['Revenue (NOK)',d=>nok(d.f.revenue)],['Result (NOK)',d=>nok(d.f.result)],
 ['Revenue growth',d=>pct(d.f.revenue_growth_pct)],['Equity ratio',d=>pct(d.f.equity_ratio_pct)],
 ['Website',d=>d.web?`<a href="${esc(d.web)}" target="_blank" rel="noopener">${esc(d.web)}</a>`:'—'],['Social profiles',d=>d.c.social_profile||0],
 ['Active job ads',d=>d.c.job_posting||0],['Posts on own site',d=>d.c.public_post||0],['Public tenders',d=>d.c['public_activity.procurement']||0],
 ['News articles',d=>d.c['public_activity.news_mention']||0],['Signals',d=>esc(d.signals.join(', ')||'—')],['Not found or not checked',d=>esc(d.unknown.join(', ')||'—')]];
 let h='<table><tr><th></th>'+ids.map(i=>`<th><a href="#${esc(i)}">${esc(D[i].name)}</a></th>`).join('')+'</tr>';
 for(const [label,fn] of rows)h+=`<tr><th>${label}</th>`+ids.map(i=>`<td>${fn(D[i])}</td>`).join('')+'</tr>';
 const box=document.getElementById('cmpbody');box.innerHTML=h+'</table>';box.querySelectorAll('a[href^="#"]').forEach(a=>a.addEventListener('click',()=>document.getElementById('cmp').close()));
 document.getElementById('cmp').showModal()};
function openHash(){const id=decodeURIComponent(location.hash.slice(1));const el=id&&document.getElementById(id);if(el&&el.tagName==='DETAILS'){el.hidden=false;el.open=true;el.scrollIntoView()}}
window.addEventListener('hashchange',openHash);order();apply();openHash();
"""


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


def family(field: str) -> str:
    return field if field.startswith("public_activity.") else field.split(".")[0]


def show(value) -> str:
    if isinstance(value, dict):
        if value.get("url"):
            label = value.get("title") or value["url"]
            date = value.get("published") or value.get("date_posted") or ""
            extra = f" · {esc(str(value['role']).replace('_', ' '))}" if value.get("role") else ""
            return f'<a href="{esc(value["url"])}" target="_blank" rel="noopener">{esc(label)}</a> {esc(str(date)[:10])}{extra}'
        return esc(json.dumps(value, ensure_ascii=False))
    if isinstance(value, str) and value.startswith("http"):
        return f'<a href="{esc(value)}" target="_blank" rel="noopener">{esc(value)}</a>'
    return esc(value)


def change_text(change: dict) -> str:
    field = change.get("field") or change.get("path") or "field"
    before, after = change.get("before", change.get("previous")), change.get("after", change.get("current"))
    if before is None and after is not None:
        return f"{field}: added {json.dumps(after, ensure_ascii=False)[:120]}"
    if after is None and before is not None:
        return f"{field}: removed {json.dumps(before, ensure_ascii=False)[:120]}"
    return f"{field}: {json.dumps(before, ensure_ascii=False)[:80]} → {json.dumps(after, ensure_ascii=False)[:80]}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--envelopes", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--note", default="")
    parser.add_argument("--fragment", action="store_true", help="omit the document skeleton (for hosting that adds its own)")
    args = parser.parse_args()
    envelopes = [json.loads(line) for line in Path(args.envelopes).read_text(encoding="utf-8").splitlines() if line.strip()]
    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    total = len(envelopes) or 1

    found, companies_with, failed_with, signal_counts = Counter(), Counter(), Counter(), Counter()
    data, rows = {}, []
    for env in envelopes:
        p = env["profile"]
        org = p["organisation_number"]
        avail = [c for c in env["claims"] if c["availability"] == "available"]
        other = [c for c in env["claims"] if c["availability"] != "available"]
        counts = Counter(family(c["field"]) for c in avail)
        found.update(counts)
        companies_with.update(set(counts))
        failed_with.update({family(c["field"]) for c in other if c["availability"] == "failed"} - set(counts))
        brief = env.get("brief") or {}
        signals = list(dict.fromkeys(brief.get("signals") or []))
        changes = env.get("changes") or []
        signal_counts.update(signals)
        website = next((c["value"] for c in avail if c["field"] == "official_website"), None)
        data[org] = {"org": org, "name": p.get("name") or org, "place": (p.get("municipality") or "").title(),
                     "industry": p.get("industry_label") or "", "f": brief.get("key_figures") or {}, "c": dict(counts),
                     "facts": len(avail), "signals": signals, "unknown": brief.get("unknown") or [], "web": website,
                     "k": signals + [f"has:{f}" for f in counts] + (["changed"] if changes else []),
                     "q": f"{p.get('name') or ''} {org} {p.get('municipality') or ''} {p.get('industry_label') or ''}".lower()}

        evidence = {e["id"]: e for e in env["evidence"]}
        tags = [f'<span class="tag ok">{label.split()[-1].lower()} {counts[f]}</span>' for f, label in FAMILIES if counts[f]]
        if changes:
            tags.append(f'<span class="tag sig">{len(changes)} change{"s" if len(changes) != 1 else ""}</span>')
        body = []
        if brief:
            text = " ".join(
                esc(s["text"]) + "".join(f' <a class="src" href="{esc(u)}" target="_blank" rel="noopener" title="{esc(u)}">[{i + 1}]</a>'
                                         for i, u in enumerate(s.get("sources") or []))
                for s in brief.get("sentences") or [])
            body.append(f'<p class="brief">{text}</p>')
            if signals:
                body.append("<p>" + " ".join(f'<span class="tag sig">{esc(x)}</span>' for x in signals) + "</p>")
            if brief.get("unknown"):
                body.append(f'<p class="unk">Not found or not checked: {esc(", ".join(brief["unknown"]))}</p>')
        if changes:
            body.append('<div class="chg"><b>Changed since the previous run</b><br>' +
                        "<br>".join(esc(change_text(c)) for c in changes[:12]) + "</div>")
        body.append("<table><tr><th>Fact</th><th>Value</th><th>Source</th><th>Retrieved</th></tr>")
        for c in avail:
            ev = evidence.get((c.get("evidence_ids") or [None])[0]) or {}
            src = (f'<a href="{esc(ev.get("source_url"))}" target="_blank" rel="noopener">{esc(ev.get("source_class"))}</a>'
                   f'<br>{esc(str(ev.get("claim_span") or "")[:160])}') if ev else ""
            body.append(f"<tr><td>{esc(c['field'])}</td><td>{show(c['value'])}</td><td>{src}</td><td>{esc(str(ev.get('retrieved_at') or '')[:16])}</td></tr>")
        for c in other:
            body.append(f"<tr><td>{esc(c['field'])}</td><td><i>{esc(c['availability'].replace('_', ' '))}</i></td><td colspan=2>{esc(c.get('note') or '')}</td></tr>")
        body.append("</table>")
        rows.append(
            f'<details class="co" id="{esc(org)}"><summary><span class="n">{esc(p.get("name"))}</span>'
            f'<span class="o">{esc(org)} · {esc((p.get("municipality") or "").title())} · {esc(p.get("industry_label"))}</span>'
            f'{"".join(tags)}<label class="cmp"><input type="checkbox" class="pick" value="{esc(org)}"> compare</label></summary>'
            f'<div class="body">{"".join(body)}<a class="link" href="#{esc(org)}">Link to this company</a></div></details>')

    ops = report.get("operations", {})
    passed = (report.get("validation") or {}).get("passed")
    cards = [(len(envelopes), "companies"), ("passed" if passed else "failed", "output contract checks"),
             (sum(found.values()), "sourced external facts"), (ops.get("requests"), "outbound requests"),
             (f"US${ops.get('third_party_cost_usd') or 0:g}", "third-party cost")]
    coverage = "".join(
        f'<div class="r"><span>{label}</span><div class="track"><div class="fill" style="width:{companies_with[f] / total * 100:.1f}%"></div></div>'
        f'<span class="num">{companies_with[f]} compan{"y" if companies_with[f] == 1 else "ies"} · {found[f]} fact{"" if found[f] == 1 else "s"}{f" · {failed_with[f]} not checked" if failed_with[f] else ""}</span></div>'
        for f, label in FAMILIES)
    chip_keys = [(f"has:{f}", label) for f, label in FAMILIES if companies_with[f]]
    chip_keys += [(s, s) for s, _ in signal_counts.most_common(10)]
    if any(env.get("changes") for env in envelopes):
        chip_keys.append(("changed", "changed since last run"))
    chips = "".join(f'<button class="chip" type="button" aria-pressed="false" data-k="{esc(k)}">{esc(label)}</button>' for k, label in chip_keys)
    note = f'<div class="note">{esc(args.note)}</div>' if args.note else ""
    started, completed = str(report.get("started_at", ""))[:16], str(report.get("completed_at", ""))[:16]
    head = "" if args.fragment else ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
                                     '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">')
    page = f"""{head}<title>Signalpost Company Profiles</title><style>{CSS}</style>{"" if args.fragment else "</head><body>"}<main>
<h1>Signalpost company profiles</h1>
<p class="sub">Run {esc(report.get('run_id'))} · {started.replace('T', ' ')} → {completed[11:]} UTC · every fact links to its source; “not checked” is never shown as zero.</p>
<div class="cards">{"".join(f'<div class="card"><b>{esc(v)}</b><span>{esc(k)}</span></div>' for v, k in cards)}</div>
<h2>Coverage by information type</h2><div class="cov">{coverage}</div>{note}
<h2>Companies</h2>
<div class="tools"><input id="q" type="search" placeholder="Search name, organisation number, place or industry" aria-label="Search companies">
<select id="sort" aria-label="Sort companies"><option value="facts">Most facts first</option><option value="revenue">Highest revenue</option>
<option value="employees">Most employees</option><option value="revenue_growth_pct">Fastest revenue growth</option><option value="name">Name A–Z</option></select>
<span class="count" id="shown"></span></div>
<div class="chips" role="group" aria-label="Filter by signal">{chips}</div>
<div id="list">{"".join(rows)}</div>
</main>
<div id="tray" role="region" aria-label="Compare"><span id="tn"></span><span class="limit" id="lim" hidden>up to 4</span><button class="go" id="go" type="button">Compare</button><button class="clr" id="clr" type="button">Clear</button></div>
<dialog id="cmp"><header><b>Compare companies</b><button class="x" type="button" onclick="this.closest('dialog').close()" aria-label="Close">×</button></header><div class="wrap" id="cmpbody"></div></dialog>
<script id="data" type="application/json">{json.dumps(data, ensure_ascii=False).replace("</", "<\\/")}</script>
<script>{JS}</script>{"" if args.fragment else "</body></html>"}"""
    Path(args.output).write_text(page, encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
