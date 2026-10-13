"""Client for NAV's public job-ad feed (pam-stilling-feed.nav.no).

The feed needs a public bearer token, published by NAV at /api/publicToken. It is fetched on demand,
kept in memory only, and refreshed when NAV rejects it.
"""
from __future__ import annotations

import http.client
import json
import re
import time
import urllib.error
import urllib.request

BASE = "https://pam-stilling-feed.nav.no"
USER_AGENT = "signalpost-agent/0.2 (+https://github.com/tbd-hackathons/signalpost-agent)"


class NavFeed:
    def __init__(self, timeout: float = 60.0) -> None:
        self.timeout = timeout
        self.token: str | None = None
        self.requests = 0

    def _refresh_token(self) -> None:
        req = urllib.request.Request(BASE + "/api/publicToken", headers={"User-Agent": USER_AGENT})
        raw = urllib.request.urlopen(req, timeout=self.timeout).read().decode()
        self.requests += 1
        match = re.search(r"eyJ[\w-]+\.[\w-]+\.[\w-]+", raw)
        if not match:
            raise RuntimeError("NAV public token not found")
        self.token = match.group(0)

    def get(self, path: str, retries: int = 4, backoff: float = 2.0) -> dict:
        if self.token is None:
            self._refresh_token()
        for attempt in range(retries):
            req = urllib.request.Request(BASE + path, headers={
                "User-Agent": USER_AGENT, "Accept": "application/json", "Authorization": f"Bearer {self.token}"})
            try:
                self.requests += 1
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                # Refresh only on an auth failure; a 5xx means NAV is struggling, so back off instead.
                if exc.code in {401, 403}:
                    self._refresh_token()
                if attempt == retries - 1:
                    raise
            except (urllib.error.URLError, http.client.HTTPException, OSError):
                if attempt == retries - 1:
                    raise
            time.sleep(backoff * (attempt + 1))
        raise RuntimeError("unreachable")


def compact(item: dict) -> dict:
    entry = item.get("_feed_entry") or {}
    return {"uuid": entry.get("uuid") or item.get("id"), "status": entry.get("status"),
            "title": entry.get("title") or item.get("title"), "business_name": entry.get("businessName"),
            "municipal": entry.get("municipal"), "modified": entry.get("sistEndret") or item.get("date_modified")}
