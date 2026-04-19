"""Tests for `fetchers/pdf.probe()` (C7 HEAD probe).

Mirrors `tests/test_passthrough.py`'s pattern: a tiny ThreadingHTTPServer
fixture that lets each test toggle the response Content-Type, status,
and HEAD behaviour. The pipeline-level integration test uses
monkeypatching to verify the new `pdf-probed` fetcher_used path
without launching Playwright.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from trawl.fetchers import pdf

# ---------------------------------------------------------------- server


class _Handler(BaseHTTPRequestHandler):
    response_ct: str = "application/pdf"
    response_status: int = 200
    head_status: int | None = None
    head_supported: bool = True

    def log_message(self, *a, **kw):
        pass

    def do_GET(self):
        self.send_response(self.response_status)
        self.send_header("Content-Type", self.response_ct)
        self.end_headers()

    def do_HEAD(self):
        if not self.head_supported:
            self.send_response(405)
            self.end_headers()
            return
        status = self.head_status if self.head_status is not None else self.response_status
        self.send_response(status)
        self.send_header("Content-Type", self.response_ct)
        self.end_headers()


@pytest.fixture
def http_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}", _Handler
    server.shutdown()
    server.server_close()


# ---------------------------------------------------------------- probe()


def test_probe_returns_true_on_application_pdf(http_server):
    base, handler = http_server
    handler.response_ct = "application/pdf"
    handler.response_status = 200
    handler.head_status = 200
    handler.head_supported = True
    assert pdf.probe(f"{base}/whitepaper") is True


def test_probe_returns_true_with_charset_param(http_server):
    base, handler = http_server
    handler.response_ct = "application/pdf; charset=binary"
    handler.head_status = 200
    handler.head_supported = True
    assert pdf.probe(f"{base}/doc") is True


def test_probe_returns_false_on_html(http_server):
    base, handler = http_server
    handler.response_ct = "text/html; charset=utf-8"
    handler.head_status = 200
    handler.head_supported = True
    assert pdf.probe(f"{base}/page") is False


def test_probe_returns_false_on_octet_stream(http_server):
    base, handler = http_server
    handler.response_ct = "application/octet-stream"
    handler.head_status = 200
    handler.head_supported = True
    assert pdf.probe(f"{base}/binary") is False


def test_probe_returns_false_on_404(http_server):
    base, handler = http_server
    handler.response_ct = "application/pdf"
    handler.head_status = 404
    handler.head_supported = True
    assert pdf.probe(f"{base}/missing") is False


def test_probe_returns_false_on_405_head_not_supported(http_server):
    base, handler = http_server
    handler.response_ct = "application/pdf"
    handler.head_supported = False
    assert pdf.probe(f"{base}/method-not-allowed") is False


def test_probe_returns_false_on_network_error():
    # Port 1 is reserved; the connection should refuse instantly.
    assert pdf.probe("http://127.0.0.1:1/never") is False


def test_probe_short_timeout_does_not_hang(monkeypatch):
    """If the server hangs on HEAD, the probe must time out promptly."""

    # We don't need a real server — patch httpx.Client.head to raise
    # ReadTimeout so we exercise the except branch deterministically.
    class _Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def head(self, *a, **kw):
            raise httpx.ReadTimeout("simulated")

    monkeypatch.setattr(pdf.httpx, "Client", _Client)
    assert pdf.probe("http://example.com/anything", timeout_s=0.01) is False


# ---------------------------------------------------------------- pipeline integration


def test_pipeline_routes_to_pdf_probed_on_head_hit(monkeypatch):
    """When pdf.probe returns True for a non-suffix URL, the pipeline
    should take the PDF path with fetcher_used='pdf-probed' and skip
    Playwright entirely."""
    from trawl import fetch_relevant
    from trawl.fetchers import pdf as pdf_mod
    from trawl.fetchers.playwright import FetchResult

    url = "https://example.com/whitepaper"  # no .pdf suffix, no /pdf/

    fake_pdf_result = FetchResult(
        url=url,
        html="",
        markdown="# Whitepaper\n\nSome content about the topic.",
        raw_html="",
        fetcher="pdf",
        elapsed_ms=10,
    )

    monkeypatch.setattr(pdf_mod, "probe", lambda u, **kw: True)
    monkeypatch.setattr(pdf_mod, "fetch", lambda u: fake_pdf_result)

    # Minimum stub for retrieval — empty chunks ok, we only assert
    # routing. Block real network/embedding by stubbing _fetch_html
    # too in case probe fails for some reason.
    from trawl import pipeline as pip

    def _should_not_be_called(*a, **kw):
        raise AssertionError("HTML fetcher must not be invoked when probe hits")

    monkeypatch.setattr(pip, "_fetch_html", _should_not_be_called)

    # Stub embedding to avoid hitting the live :8081 endpoint.
    from trawl import retrieval as ret_mod

    def _fake_retrieve(query, chunks, *, k, extra_query_texts=None, hybrid=False):
        from trawl.retrieval import RetrievalResult

        return RetrievalResult(scored=[], elapsed_ms=0, embed_calls=0, error=None)

    monkeypatch.setattr(ret_mod, "retrieve", _fake_retrieve)

    # Avoid telemetry side-effects
    monkeypatch.delenv("TRAWL_TELEMETRY", raising=False)

    result = fetch_relevant(url, "what is this whitepaper about")

    assert result.fetcher_used == "pdf-probed"
    # Markdown was chunked but the stubbed retrieve() returns no scored
    # chunks, so result.chunks is empty. n_chunks_total reflects the
    # pre-retrieval count and proves PDF parsing actually ran.
    assert result.n_chunks_total >= 1
    assert result.page_chars > 0


def test_pipeline_keeps_pdf_for_suffix_url(monkeypatch):
    """`.pdf` suffix path must NOT involve probe (probe only fires on
    suffix-miss). fetcher_used remains 'pdf', not 'pdf-probed'."""
    from trawl import fetch_relevant
    from trawl.fetchers import pdf as pdf_mod
    from trawl.fetchers.playwright import FetchResult

    url = "https://example.com/paper.pdf"
    fake_pdf_result = FetchResult(
        url=url,
        html="",
        markdown="text",
        raw_html="",
        fetcher="pdf",
        elapsed_ms=10,
    )
    monkeypatch.setattr(pdf_mod, "fetch", lambda u: fake_pdf_result)

    probed = []

    def _record_probe(u, **kw):
        probed.append(u)
        return False

    monkeypatch.setattr(pdf_mod, "probe", _record_probe)

    # Stub embedding
    from trawl import retrieval as ret_mod
    from trawl.retrieval import RetrievalResult

    monkeypatch.setattr(
        ret_mod,
        "retrieve",
        lambda q, c, *, k, extra_query_texts=None, hybrid=False: RetrievalResult(
            scored=[], elapsed_ms=0, embed_calls=0, error=None
        ),
    )
    monkeypatch.delenv("TRAWL_TELEMETRY", raising=False)

    result = fetch_relevant(url, "x")

    assert result.fetcher_used == "pdf"
    assert probed == [], "probe must not be called when URL suffix already says PDF"


def test_pipeline_falls_through_to_html_when_probe_false(monkeypatch):
    """probe returns False → fall through to existing _fetch_html. Verify
    fetcher_used reflects the HTML path, not pdf-probed."""
    from trawl import fetch_relevant
    from trawl.fetchers import pdf as pdf_mod
    from trawl.fetchers.playwright import FetchResult

    url = "https://example.com/some-page"

    monkeypatch.setattr(pdf_mod, "probe", lambda u, **kw: False)

    html_result = FetchResult(
        url=url,
        html="<html><body>x</body></html>",
        markdown="x",
        raw_html="<html><body>x</body></html>",
        fetcher="playwright+trafilatura",
        elapsed_ms=20,
    )

    from trawl import pipeline as pip

    monkeypatch.setattr(pip, "_fetch_html", lambda u: (html_result, "x", "playwright+trafilatura"))

    from trawl import retrieval as ret_mod
    from trawl.retrieval import RetrievalResult

    monkeypatch.setattr(
        ret_mod,
        "retrieve",
        lambda q, c, *, k, extra_query_texts=None, hybrid=False: RetrievalResult(
            scored=[], elapsed_ms=0, embed_calls=0, error=None
        ),
    )
    monkeypatch.delenv("TRAWL_TELEMETRY", raising=False)

    result = fetch_relevant(url, "x")
    assert result.fetcher_used == "playwright+trafilatura"
