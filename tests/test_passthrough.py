"""Tests for raw-passthrough handling of JSON/XML responses."""
from __future__ import annotations

from trawl.pipeline import PipelineResult, to_dict
from trawl.fetchers import passthrough


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


import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class _Handler(BaseHTTPRequestHandler):
    response_body: bytes = b""
    response_ct: str = "application/json"
    response_status: int = 200

    def log_message(self, *a, **kw):
        pass

    def do_GET(self):
        self.send_response(self.response_status)
        self.send_header("Content-Type", self.response_ct)
        self.send_header("Content-Length", str(len(self.response_body)))
        self.end_headers()
        self.wfile.write(self.response_body)


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
