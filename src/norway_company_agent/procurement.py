"""Dated public-procurement activity from TED (EU Tenders Electronic Daily), matched by organisation number.

Norwegian buyers publish tenders above the EEA thresholds on TED, and contract-award notices name the
winning suppliers by organisation number. A notice is attached to a company only when one of those
numbers equals the company's own number or one of its registered sub-units: name similarity never
attaches a notice. The agent ships a snapshot limited to the Signalpost universe
(`data/ted-no-notices.jsonl.gz`, built by `scripts/ted_snapshot.py`), so a run makes no TED requests.
"""
from __future__ import annotations

import gzip
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from .evidence import evidence
from .nav_jobs import entity_numbers

SNAPSHOT = Path(__file__).resolve().parents[2] / "data" / "ted-no-notices.jsonl.gz"
MAX_NOTICES = 25
AWARD_PREFIXES = ("can-", "veat")


class ProcurementIndex:
    def __init__(self, by_orgnr: dict[str, list[dict[str, Any]]], meta: dict[str, Any]) -> None:
        self.by_orgnr, self.meta = by_orgnr, meta

    @classmethod
    def load(cls, path: str | Path = SNAPSHOT) -> "ProcurementIndex | None":
        path = Path(path)
        if not path.exists():
            return None
        by_orgnr: dict[str, list[dict[str, Any]]] = defaultdict(list)
        meta: dict[str, Any] = {}
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                row = json.loads(line)
                if "_meta" in row:
                    meta = row["_meta"]
                    continue
                for number in set(row.get("buyer_ids") or []) | set(row.get("winner_ids") or []):
                    by_orgnr[number].append(row)
        return cls(dict(by_orgnr), meta)


def _role(row: dict[str, Any], numbers: set[str]) -> str | None:
    if numbers & set(row.get("winner_ids") or []):
        return "supplier_awarded"
    if numbers & set(row.get("buyer_ids") or []):
        return "buyer_awarded" if str(row.get("notice_type") or "").startswith(AWARD_PREFIXES) else "buyer_tender"
    return None


def procurement_record(profile: dict[str, Any], index: ProcurementIndex | None) -> dict[str, Any]:
    if index is None:
        return evidence("procurement", "source_error", "official_procurement_notice", "https://ted.europa.eu/",
                        note="TED snapshot not available for this run")
    numbers, _ = entity_numbers(profile)
    rows = {row["publication_number"]: row for number in numbers for row in index.by_orgnr.get(number, [])}
    as_of = index.meta.get("retrieved_at")
    if not rows:
        return evidence("procurement", "not_found", "official_procurement_notice", "https://ted.europa.eu/",
                        note=f"No TED notice since {index.meta.get('query', '').rsplit('>=', 1)[-1] or 'the snapshot start'} "
                             "names this organisation number as buyer or winner", as_of=as_of)
    notices = []
    for row in sorted(rows.values(), key=lambda r: r.get("published") or "", reverse=True)[:MAX_NOTICES]:
        role = _role(row, numbers)
        matched = sorted(numbers & set((row.get("winner_ids") or []) + (row.get("buyer_ids") or [])))
        notices.append({
            "role": role, "title": row.get("title"), "published": row.get("published"), "url": row["url"],
            "notice_type": row.get("notice_type"), "publication_number": row["publication_number"],
            "counterparties": row.get("buyer_names") if role == "supplier_awarded" else row.get("winner_names"),
            "identity_proof": f"TED notice {row['publication_number']} names organisation number "
                              f"{', '.join(matched)} as {'winner' if role == 'supplier_awarded' else 'buyer'}",
            "retrieved_at": as_of, "content_sha256": row.get("content_sha256"),
        })
    value = {"notices": notices, "notice_count": len(rows),
             "awards_won": sum(1 for n in rows.values() if _role(n, numbers) == "supplier_awarded")}
    return evidence("procurement", "available", "official_procurement_notice", notices[0]["url"], value=value,
                    retrieved_at=as_of, note="EU public-procurement notices (TED), matched by organisation number")
