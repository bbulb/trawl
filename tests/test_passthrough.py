"""Tests for raw-passthrough handling of JSON/XML responses."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from trawl import fetch_relevant
from trawl.fetchers import passthrough
from trawl.fetchers.playwright import FetchResult
from trawl.pipeline import PipelineResult, to_dict


def test_pipeline_result_has_passthrough_fields():
    r = PipelineResult(
        url="https://x/y.json",
        query="",
        fetcher_used="passthrough",
        fetch_ms=0,
        chunk_ms=0,
        retrieval_ms=0,
        total_ms=0,
        page_chars=0,
        n_chunks_total=0,
        structured_path=False,
        hyde_used=False,
        hyde_text="",
        chunks=[],
    )
    assert r.content_type is None
    assert r.truncated is False
    d = to_dict(r)
    assert "content_type" in d
    assert "truncated" in d


def test_matches_url_suffix_positive():
    assert passthrough.matches("https://api.example.com/data.json")
    assert passthrough.matches("https://feed.example.com/rss.xml")
    assert passthrough.matches("https://example.com/a.rss")
    assert passthrough.matches("https://example.com/a.atom")
    assert passthrough.matches("https://example.com/a.json?x=1")


def test_matches_url_suffix_negative():
    assert not passthrough.matches("https://example.com/index.html")
    assert not passthrough.matches("https://example.com/")
    assert not passthrough.matches("https://example.com/doc.pdf")


def test_is_passthrough_content_type():
    f = passthrough.is_passthrough_content_type
    assert f("application/json")
    assert f("application/json; charset=utf-8")
    assert f("application/vnd.api+json")
    assert f("application/xml")
    assert f("text/xml")
    assert f("application/rss+xml")
    assert f("application/atom+xml")
    assert f("application/problem+json; charset=utf-8")
    assert not f("text/html")
    assert not f("application/pdf")
    assert not f("application/octet-stream")
    assert not f("image/png")
    assert not f(None)
    assert not f("")


class _Handler(BaseHTTPRequestHandler):
    response_body: bytes = b""
    response_ct: str = "application/json"
    response_status: int = 200
    head_status: int | None = None

    def log_message(self, *a, **kw):
        pass

    def do_GET(self):
        self.send_response(self.response_status)
        self.send_header("Content-Type", self.response_ct)
        self.send_header("Content-Length", str(len(self.response_body)))
        self.end_headers()
        self.wfile.write(self.response_body)

    def do_HEAD(self):
        status = self.head_status if self.head_status is not None else self.response_status
        self.send_response(status)
        self.send_header("Content-Type", self.response_ct)
        self.send_header("Content-Length", str(len(self.response_body)))
        self.end_headers()


@pytest.fixture
def http_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}", _Handler
    server.shutdown()
    server.server_close()


def test_fetch_json_ok(http_server):
    base, handler = http_server
    handler.response_body = json.dumps({"a": 1}).encode("utf-8")
    handler.response_ct = "application/json"
    handler.response_status = 200

    r = passthrough.fetch(f"{base}/data.json")
    assert r.ok
    assert r.content_type == "application/json"
    assert r.raw_bytes == b'{"a": 1}'
    assert r.truncated is False


def test_fetch_content_type_mismatch(http_server):
    base, handler = http_server
    handler.response_body = b"<html></html>"
    handler.response_ct = "text/html"
    handler.response_status = 200
    r = passthrough.fetch(f"{base}/data.json")
    assert not r.ok
    assert "content-type mismatch" in (r.error or "")


def test_fetch_http_error(http_server):
    base, handler = http_server
    handler.response_body = b""
    handler.response_status = 404
    handler.response_ct = "application/json"
    r = passthrough.fetch(f"{base}/missing.json")
    assert not r.ok
    assert "HTTP 404" in (r.error or "")


def test_fetch_truncation(http_server, monkeypatch):
    monkeypatch.setattr(passthrough, "PASSTHROUGH_MAX_BYTES", 1024)
    base, handler = http_server
    handler.response_body = b"x" * 2048
    handler.response_ct = "application/json"
    handler.response_status = 200
    r = passthrough.fetch(f"{base}/big.json")
    assert r.ok
    assert r.truncated is True
    assert len(r.raw_bytes) == 1024


def test_probe_head_ok_json(http_server):
    base, handler = http_server
    handler.response_body = b'{"a": 1}'
    handler.response_ct = "application/json"
    handler.response_status = 200
    handler.head_status = 200
    ct = passthrough.probe(f"{base}/v1/forecast")
    assert ct is not None
    assert ct.startswith("application/json")


def test_probe_head_405_returns_none(http_server):
    base, handler = http_server
    handler.response_body = b'{"a": 1}'
    handler.response_ct = "application/json"
    handler.head_status = 405
    assert passthrough.probe(f"{base}/v1/forecast") is None


def test_probe_head_non_passthrough_ct(http_server):
    base, handler = http_server
    handler.response_body = b"<html></html>"
    handler.response_ct = "text/html"
    handler.response_status = 200
    handler.head_status = 200
    assert passthrough.probe(f"{base}/page") is None


def test_probe_network_error_returns_none():
    # Port 1 is reserved; no listener, httpx will error quickly.
    assert passthrough.probe("http://127.0.0.1:1/nope", timeout_s=0.5) is None


def test_playwright_fetch_result_has_content_type():
    r = FetchResult(
        url="https://x/",
        html="",
        markdown="",
        raw_html="",
        fetcher="playwright",
        elapsed_ms=0,
    )
    assert r.content_type is None
    r2 = FetchResult(
        url="https://x/",
        html="",
        markdown="",
        raw_html="",
        fetcher="playwright",
        elapsed_ms=0,
        content_type="application/json",
    )
    assert r2.content_type == "application/json"


def test_fetch_relevant_passthrough_json(http_server):
    base, handler = http_server
    body = b'{"hello": "world"}'
    handler.response_body = body
    handler.response_ct = "application/json"
    handler.response_status = 200

    r = fetch_relevant(f"{base}/data.json")
    assert r.error is None, r.error
    assert r.path == "raw_passthrough"
    assert r.content_type == "application/json"
    assert r.truncated is False
    assert r.fetcher_used == "passthrough"
    assert len(r.chunks) == 1
    assert r.chunks[0]["text"] == body.decode("utf-8")
    assert r.chunks[0]["chunk_index"] == 0
    assert r.n_chunks_total == 1


def test_fetch_relevant_passthrough_truncated(http_server, monkeypatch):
    from trawl.fetchers import passthrough as pt_mod

    monkeypatch.setattr(pt_mod, "PASSTHROUGH_MAX_BYTES", 32)
    base, handler = http_server
    handler.response_body = b'{"k":"' + b"x" * 100 + b'"}'
    handler.response_ct = "application/json"
    handler.response_status = 200
    r = fetch_relevant(f"{base}/big.json")
    assert r.truncated is True
    assert len(r.chunks[0]["text"]) == 32
    assert r.error is None  # truncation is not an error


def test_fetch_relevant_head_probed_passthrough(http_server):
    """Suffix-less API endpoint answering HEAD with a JSON Content-Type
    should bypass Playwright and return the raw body."""
    base, handler = http_server
    body = b'{"temp": 21}'
    handler.response_body = body
    handler.response_ct = "application/json"
    handler.response_status = 200
    handler.head_status = 200

    r = fetch_relevant(f"{base}/v1/forecast?lat=0&lon=0")
    assert r.error is None, r.error
    assert r.path == "raw_passthrough"
    assert r.fetcher_used == "passthrough-probed"
    assert r.content_type == "application/json"
    assert r.chunks[0]["text"] == body.decode("utf-8")


def test_pipeline_post_detection_passthrough(http_server, monkeypatch):
    base, handler = http_server
    body = b'{"post": "detect"}'
    handler.response_body = body
    handler.response_ct = "application/json"
    handler.response_status = 200
    # This test covers the case where HEAD isn't supported, so the pre-probe
    # must fail and the pipeline must fall through to the Playwright render
    # before the Content-Type post-check salvages the body.
    handler.head_status = 405

    # Simulate Playwright: return a FetchResult with content_type set but
    # a garbled HTML body (as Chromium's JSON viewer would produce).
    from trawl import pipeline as pipeline_mod
    from trawl.fetchers.playwright import FetchResult as PwFetchResult

    def fake_fetch_html(url: str):
        fr = PwFetchResult(
            url=url,
            html="<html><pre>{&quot;post&quot;: &quot;detect&quot;}</pre></html>",
            markdown="",
            raw_html="",
            fetcher="playwright",
            elapsed_ms=5,
            content_type="application/json; charset=utf-8",
        )
        return fr, "garbage-markdown", "playwright+trafilatura"

    monkeypatch.setattr(pipeline_mod, "_fetch_html", fake_fetch_html)

    # URL has no passthrough suffix (no .json/.xml/.rss/.atom), so
    # fetch_relevant proceeds past the URL-hint short-circuit and
    # enters _run_full_pipeline, which calls _fetch_html (monkeypatched).
    r = fetch_relevant(f"{base}/api/weather", query="anything")
    assert r.error is None, r.error
    assert r.path == "raw_passthrough"
    assert r.fetcher_used == "playwright+passthrough"
    assert r.chunks[0]["text"] == body.decode("utf-8")
    assert r.content_type == "application/json; charset=utf-8"


def test_pipeline_post_detection_passthrough_fetch_fails(monkeypatch):
    """When re-fetch fails, return terminal error rather than falling back."""
    from trawl import pipeline as pipeline_mod
    from trawl.fetchers import passthrough as pt_mod
    from trawl.fetchers.playwright import FetchResult as PwFetchResult

    def fake_fetch_html(url: str):
        return (
            PwFetchResult(
                url=url,
                html="<html></html>",
                markdown="",
                raw_html="",
                fetcher="playwright",
                elapsed_ms=5,
                content_type="application/json",
            ),
            "",
            "playwright+trafilatura",
        )

    def fake_raw(url, *, timeout_s=15.0):
        return pt_mod.PassthroughResult(
            url=url,
            raw_bytes=b"",
            content_type=None,
            elapsed_ms=1,
            error="ConnectError: boom",
        )

    monkeypatch.setattr(pipeline_mod, "_fetch_html", fake_fetch_html)
    monkeypatch.setattr(pt_mod, "fetch_raw_body", fake_raw)

    r = fetch_relevant("https://example.test/api/x", query="anything")
    assert r.path == "raw_passthrough"
    assert r.error and "passthrough raw body fetch failed" in r.error
    assert r.chunks == []
