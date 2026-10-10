#!/usr/bin/env python3
"""Run the local-model job extractor on saved careers pages; write raw and verified outputs for review.

Usage: python scripts/eval_local_model.py --pages data-cache/llm-eval --model data-cache/models/<file>.gguf --out out/llm-eval.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from norway_company_agent.llm_extract import LocalExtractor, page_blocks  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    index = json.loads((Path(args.pages) / "index.json").read_text())
    extractor = LocalExtractor(args.model)
    seen = set()
    with open(args.out, "w", encoding="utf-8") as out:
        for item in index:
            if item["key"] in seen:
                continue  # several companies can share one page
            seen.add(item["key"])
            html = (Path(args.pages) / f"{item['key']}.html").read_text(encoding="utf-8", errors="replace")
            text, standalone = page_blocks(html)
            started = time.monotonic()
            verified = extractor.jobs(text, standalone)
            out.write(json.dumps({**item, "seconds": round(time.monotonic() - started, 1), "chars": len(text),
                                  "verified": verified}, ensure_ascii=False) + "\n")
            print(item["name"], "->", [j["title"] for j in verified], flush=True)


if __name__ == "__main__":
    main()
