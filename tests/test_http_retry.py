from __future__ import annotations

import http.client
import socket
import ssl
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from norway_company_agent.website import is_transient, open_with_retry  # noqa: E402


def test_transient_failures_are_retried_and_real_ones_are_not():
    """A batch run provokes connection and TLS resets. Retrying those recovers live sites; retrying a
    certificate that does not verify just wastes the run's budget on a site that will never work."""
    retry = [
        ConnectionResetError(54, "Connection reset by peer"),
        ConnectionAbortedError(53, "Software caused connection abort"),
        TimeoutError("The read operation timed out"),
        socket.timeout("timed out"),
        ssl.SSLError(1, "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol"),
        ssl.SSLError(1, "[SSL: TLSV1_ALERT_INTERNAL_ERROR] tlsv1 alert internal error"),
        http.client.RemoteDisconnected("Remote end closed connection without response"),
        urllib.error.URLError(ConnectionResetError(54, "Connection reset by peer")),
        urllib.error.HTTPError("u", 503, "busy", None, None),
        urllib.error.HTTPError("u", 429, "slow down", None, None),
    ]
    never = [
        ssl.SSLCertVerificationError(1, "certificate verify failed: self-signed certificate"),
        urllib.error.URLError(ssl.SSLCertVerificationError(1, "certificate verify failed")),
        urllib.error.HTTPError("u", 404, "Not Found", None, None),
        urllib.error.HTTPError("u", 403, "Forbidden", None, None),
        ValueError("Private, loopback, link-local, multicast, and reserved addresses are blocked"),
    ]
    for exc in retry:
        assert is_transient(exc), f"{type(exc).__name__}({exc}) should be retried"
    for exc in never:
        assert not is_transient(exc), f"{type(exc).__name__}({exc}) must not be retried"


def test_open_with_retry_gives_up_and_reraises(monkeypatch):
    """A permanently failing host must surface its error after the attempts are spent, not hang."""
    import norway_company_agent.website as website

    calls = {"n": 0}

    class Opener:
        def open(self, request, timeout=None):
            calls["n"] += 1
            raise ConnectionResetError(54, "Connection reset by peer")

    monkeypatch.setattr(website, "SAFE_OPENER", Opener())
    monkeypatch.setattr(website.time, "sleep", lambda _s: None)
    request = website.urllib.request.Request("https://example.no/")
    try:
        open_with_retry(request, timeout=1.0, attempts=2)
    except ConnectionResetError:
        pass
    else:  # pragma: no cover - the call must raise
        raise AssertionError("expected the error to propagate once attempts are spent")
    assert calls["n"] == 2


def test_open_with_retry_recovers_after_a_transient_failure(monkeypatch):
    import norway_company_agent.website as website

    calls = {"n": 0}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class Opener:
        def open(self, request, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionResetError(54, "Connection reset by peer")
            return Response()

    monkeypatch.setattr(website, "SAFE_OPENER", Opener())
    monkeypatch.setattr(website.time, "sleep", lambda _s: None)
    request = website.urllib.request.Request("https://example.no/")
    with open_with_retry(request, timeout=1.0, attempts=3) as response:
        assert response is not None
    assert calls["n"] == 2
