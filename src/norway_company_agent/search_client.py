"""Runs the Rust `signalpost-search` binary over a batch of queries (JSON Lines in, JSON Lines out).

Search results are candidates only: a website still has to pass the website identity gate, and a news
article has to name the exact legal entity before anything is published. The binary reads provider keys
from environment variables (BRAVE_SEARCH_API_KEY); without a key that provider reports `no_key`, which the
agent records as "not checked", never as "nothing found".
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
BINARY_CANDIDATES = (
    ROOT / "bin" / "signalpost-search",
    ROOT / "search-rs" / "target" / "release" / "signalpost-search",
)


def find_binary() -> str | None:
    override = os.environ.get("SIGNALPOST_SEARCH_BIN")
    if override and Path(override).is_file():
        return override
    for path in BINARY_CANDIDATES:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return shutil.which("signalpost-search")


class SearchBatch:
    """Starts the binary on a list of queries in the background; `results()` waits and groups them by id."""

    def __init__(self, queries: list[dict[str, Any]], *, cache_dir: str | Path, providers: str,
                 binary: str | None = None, concurrency: int = 4) -> None:
        self.queries = queries
        self.binary = binary or find_binary()
        self.stats: dict[str, Any] = {}
        self.error: str | None = None
        self._by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._done = threading.Event()
        if not self.binary:
            self.error = "search binary not installed"
            self._done.set()
            return
        args = [self.binary, "--cache-dir", str(cache_dir), "--providers", providers, "--concurrency", str(concurrency)]
        self._thread = threading.Thread(target=self._run, args=(args,), daemon=True)
        self._thread.start()

    def _run(self, args: list[str]) -> None:
        try:
            payload = "".join(json.dumps(q, ensure_ascii=False) + "\n" for q in self.queries)
            proc = subprocess.run(args, input=payload, capture_output=True, text=True, timeout=6 * 3600, check=False)
            for line in proc.stdout.splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("id"):
                    self._by_id[row["id"]].append(row)
            for line in proc.stderr.splitlines():
                if line.startswith('{"stats"'):
                    self.stats = json.loads(line)["stats"]
            if proc.returncode != 0:
                self.error = f"search binary exited {proc.returncode}"
        except Exception as exc:  # the run continues; search-backed fields report "not checked"
            self.error = f"{type(exc).__name__}: {str(exc)[:120]}"
        finally:
            self._done.set()

    def results(self, query_id: str, timeout: float | None = None) -> tuple[str, list[dict[str, Any]]]:
        """(state, hits): state is "searched" (hits may be empty), or "not_checked" when no provider answered."""
        self._done.wait(timeout)
        rows = self._by_id.get(query_id) or []
        hits = [r for r in rows if r.get("url")]
        if hits or any(r.get("empty") for r in rows):
            return "searched", sorted(hits, key=lambda r: (r.get("provider") or "", r.get("rank") or 0))
        return "not_checked", []
