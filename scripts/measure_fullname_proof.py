"""Measure the full-name .no proof on exactly the companies it can affect."""
import json, re, sys, collections
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, 'src')
from norway_company_agent.site_candidates import build_domain_counts, name_tokens
from norway_company_agent.site_discovery import resolve_website

targets = []
for line in open('out/final-envelopes.jsonl'):
    env = json.loads(line); p = env.get('profile') or {}
    w = ((p.get('evidence') or {}).get('website') or {})
    wv = w.get('value') if isinstance(w.get('value'), dict) else {}
    if w.get('status') == 'available' and (wv.get('identity_assessment') or {}).get('publishable'):
        continue
    full = ''.join(name_tokens(p.get('name') or ''))
    if len(full) < 5:
        continue
    for a in ((wv or {}).get('discovery') or {}).get('attempts') or []:
        if a.get('outcome') != 'rejected':
            continue
        label = re.sub(r'[^a-z0-9]', '', str(a.get('url') or '').split('//')[-1].split('/')[0].split('.')[0].casefold())
        if label in (full, full.replace('aa', 'a')):
            targets.append(p)
            break
print(f'companies that had a rejected candidate spelling the complete legal name: {len(targets)}', flush=True)
counts = build_domain_counts('data-cache/brreg-enheter.csv.gz')

def again(p):
    try:
        rec, _ = resolve_website(p, counts, max_candidates=4, max_guesses=3)
        v = rec.get('value') if isinstance(rec.get('value'), dict) else {}
        a = (v.get('identity_assessment') or {})
        sel = (v.get('discovery') or {}).get('selected') or {}
        return {'orgnr': p.get('organisation_number'), 'name': p.get('name'), 'url': v.get('final_url'),
                'ok': bool(a.get('publishable')), 'proof': str(sel.get('proof') or '')}
    except Exception as exc:
        return {'orgnr': p.get('organisation_number'), 'name': p.get('name'), 'url': None, 'ok': False,
                'proof': f'{type(exc).__name__}: {exc}'}

out = []
with ThreadPoolExecutor(max_workers=8) as pool:
    for r in pool.map(again, targets): out.append(r)
newly = [r for r in out if r['ok']]
by = collections.Counter('full-name .no proof' if 'complete legal name' in r['proof'] else r['proof'][:34] or 'other' for r in newly)
print(f'NOW publishable: {len(newly)} of {len(targets)}')
for k, v in by.most_common(): print(f'   {v:>4}  {k}')
print()
for r in newly[:30]:
    print(f"   {r['orgnr']} {str(r['name'])[:28]:28} {str(r['url'])[:38]}")
json.dump(out, open('out/mine/website-gain.json','w'), ensure_ascii=False, indent=1)
