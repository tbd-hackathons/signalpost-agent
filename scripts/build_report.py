#!/usr/bin/env python3
"""Render a run's envelopes as one self-contained HTML page: totals, then every company with its sourced facts.

Usage: python scripts/build_report.py --envelopes out/<run>-envelopes.jsonl --report out/<run>-report.json --output out/<run>.html
"""
from __future__ import annotations

import argparse
import html
import json
from collections import Counter
from pathlib import Path

CSS = """
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1b;--muted:#6b6b66;--line:#e4e4df;--ok:#1f7a4d;--warn:#a15c00;--bad:#a1262b;--accent:#2f5bd3}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--card:#1e1e1c;--ink:#ecebe6;--muted:#9c9b94;--line:#33332f;--ok:#4cc38a;--warn:#e0a24a;--bad:#ef6b6f;--accent:#7d9cff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,system-ui,Segoe UI,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 64px}h1{font-size:24px;margin:0 0 4px}.sub{color:var(--muted);margin:0 0 20px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}.card b{display:block;font-size:26px}.card span{color:var(--muted);font-size:13px}
.note{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--warn);border-radius:8px;padding:10px 14px;margin-bottom:20px}
input{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink);font:inherit;margin-bottom:12px}
details{background:var(--card);border:1px solid var(--line);border-radius:8px;margin-bottom:8px}summary{cursor:pointer;padding:10px 14px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
summary .n{font-weight:600}summary .o{color:var(--muted);font-size:13px}.tag{font-size:12px;padding:2px 8px;border-radius:99px;border:1px solid var(--line)}
.tag.ok{color:var(--ok);border-color:var(--ok)}.tag.warn{color:var(--warn);border-color:var(--warn)}.tag.bad{color:var(--bad);border-color:var(--bad)}
.body{padding:0 14px 12px;overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px}td,th{text-align:left;padding:6px 8px;border-top:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:500}a{color:var(--accent);word-break:break-all}
"""


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


def show(value) -> str:
    if isinstance(value, dict):
        if value.get("url"):
            label = value.get("title") or value["url"]
            date = value.get("published") or value.get("date_posted") or ""
            return f'<a href="{esc(value["url"])}" target="_blank" rel="noopener">{esc(label)}</a> {esc(str(date)[:10])}'
        return esc(json.dumps(value, ensure_ascii=False))
    if isinstance(value, str) and value.startswith("http"):
        return f'<a href="{esc(value)}" target="_blank" rel="noopener">{esc(value)}</a>'
    return esc(value)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--envelopes", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--baseline", default="", help='JSON like {"Starter":8,"v1":10}: earlier website counts')
    parser.add_argument("--note", default="")
    args = parser.parse_args()
    envelopes = [json.loads(line) for line in Path(args.envelopes).read_text(encoding="utf-8").splitlines() if line.strip()]
    report = json.loads(Path(args.report).read_text(encoding="utf-8"))

    found = Counter()
    companies_with = Counter()
    for env in envelopes:
        fields = {c["field"].split(".")[0] for c in env["claims"] if c["availability"] == "available"}
        for c in env["claims"]:
            if c["availability"] == "available":
                found[c["field"].split(".")[0]] += 1
        companies_with.update(fields)
    ops = report.get("operations", {})
    cards = [
        (companies_with["official_website"], "verified websites"),
        (found["social_profile"], "company social profiles"),
        (found["job_posting"], f"job ads ({companies_with['job_posting']} companies)"),
        (found["public_post"], f"dated news posts ({companies_with['public_post']} companies)"),
        (ops.get("requests"), "outbound requests"),
        ("US$0", "third-party cost"),
    ]
    baseline = json.loads(args.baseline) if args.baseline else {}
    if baseline:
        cards.insert(1, (" → ".join([*(str(v) for v in baseline.values()), str(companies_with["official_website"])]),
                         "websites: " + " → ".join([*baseline, "now"])))

    rows = []
    for env in envelopes:
        p = env["profile"]
        avail = [c for c in env["claims"] if c["availability"] == "available"]
        other = [c for c in env["claims"] if c["availability"] != "available"]
        evidence = {e["id"]: e for e in env["evidence"]}
        tags = []
        for field, label in (("official_website", "website"), ("social_profile", "social"), ("job_posting", "jobs"), ("public_post", "posts")):
            n = sum(1 for c in avail if c["field"].split(".")[0] == field)
            if n:
                tags.append(f'<span class="tag ok">{label} {n}</span>')
        for c in other:
            if c["availability"] == "failed":
                tags.append(f'<span class="tag bad">{esc(c["field"])} not checked</span>')
        body = ["<table><tr><th>Fact</th><th>Value</th><th>Source</th><th>Retrieved</th></tr>"]
        for c in avail:
            ev = evidence.get((c.get("evidence_ids") or [None])[0]) or {}
            src = f'<a href="{esc(ev.get("source_url"))}" target="_blank" rel="noopener">{esc(ev.get("source_class"))}</a><br>{esc(ev.get("claim_span"))[:160]}' if ev else ""
            body.append(f"<tr><td>{esc(c['field'])}</td><td>{show(c['value'])}</td><td>{src}</td><td>{esc(str(ev.get('retrieved_at') or '')[:16])}</td></tr>")
        for c in other:
            body.append(f"<tr><td>{esc(c['field'])}</td><td><i>{esc(c['availability'])}</i></td><td colspan=2>{esc(c.get('note') or '')}</td></tr>")
        body.append("</table>")
        rows.append(
            f'<details data-q="{esc((p.get("name") or "") + " " + p["organisation_number"]).lower()}"><summary>'
            f'<span class="n">{esc(p.get("name"))}</span><span class="o">{esc(p["organisation_number"])} · {esc(p.get("municipality"))} · '
            f'{esc(p.get("industry_label"))}</span>{"".join(tags)}</summary><div class="body">{"".join(body)}</div></details>')

    rows.sort(key=lambda r: -r.count('class="tag ok"'))
    note = f'<div class="note">{esc(args.note)}</div>' if args.note else ""
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Signalpost Run Report</title><style>{CSS}</style></head><body><main>
<h1>Signalpost agent · run {esc(report.get('run_id'))}</h1>
<p class="sub">{len(envelopes)} companies · {esc(report.get('started_at', '')[:16])} → {esc(report.get('completed_at', '')[:16])} UTC · checks passed: {esc(report.get('validation', {}).get('passed'))}</p>
<div class="cards">{"".join(f'<div class="card"><b>{esc(v)}</b><span>{esc(k)}</span></div>' for v, k in cards)}</div>
{note}<input id="q" placeholder="Filter by company name or organisation number" aria-label="Filter companies">
{"".join(rows)}
</main><script>document.getElementById('q').addEventListener('input',e=>{{const q=e.target.value.toLowerCase();document.querySelectorAll('details').forEach(d=>d.style.display=d.dataset.q.includes(q)?'':'none')}})</script></body></html>"""
    Path(args.output).write_text(page, encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
