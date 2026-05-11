from __future__ import annotations

import sys
import types

from trawl import pipeline
from trawl.fetchers import scrapling as scrapling_fetcher
from trawl.fetchers.playwright import FetchResult


def test_scrapling_fetcher_lazy_import_maps_response(monkeypatch):
    calls: list[tuple[str, dict]] = []

    class FakeDynamicFetcher:
        @classmethod
        def fetch(cls, url, **kwargs):
            calls.append((url, kwargs))
            return types.SimpleNamespace(
                body=b"<html><head><title>Recovered</title></head><body>alpha body</body></html>",
                status=200,
                headers={"content-type": "text/html", "etag": '"scrapling"'},
                encoding="utf-8",
            )

    fake_fetchers = types.SimpleNamespace(DynamicFetcher=FakeDynamicFetcher)
    monkeypatch.setitem(sys.modules, "scrapling", types.SimpleNamespace())
    monkeypatch.setitem(sys.modules, "scrapling.fetchers", fake_fetchers)
    monkeypatch.setenv("TRAWL_SCRAPLING_FALLBACK", "1")

    result = scrapling_fetcher.fetch("https://example.com/hard", mode="dynamic")

    assert result.ok
    assert result.fetcher == "scrapling-dynamic"
    assert result.html.startswith("<html>")
    assert result.content_type == "text/html"
    assert result.etag == '"scrapling"'
    assert calls[0][1]["timeout"] == 30000


def test_scrapling_fetcher_auto_uses_stealthy_for_antibot(monkeypatch):
    used: list[str] = []

    class FakeDynamicFetcher:
        @classmethod
        def fetch(cls, _url, **_kwargs):
            used.append("dynamic")
            return types.SimpleNamespace(body=b"<html></html>", status=200, headers={})

    class FakeStealthyFetcher:
        @classmethod
        def fetch(cls, _url, **_kwargs):
            used.append("stealthy")
            return types.SimpleNamespace(body=b"<html></html>", status=200, headers={})

    fake_fetchers = types.SimpleNamespace(
        DynamicFetcher=FakeDynamicFetcher,
        StealthyFetcher=FakeStealthyFetcher,
    )
    monkeypatch.setitem(sys.modules, "scrapling", types.SimpleNamespace())
    monkeypatch.setitem(sys.modules, "scrapling.fetchers", fake_fetchers)
    monkeypatch.setenv("TRAWL_SCRAPLING_FALLBACK", "1")

    result = scrapling_fetcher.fetch("https://example.com/hard", mode="auto", reason="anti_bot")

    assert result.ok
    assert result.fetcher == "scrapling-stealthy"
    assert used == ["stealthy"]


def test_scrapling_fetcher_disabled_returns_error(monkeypatch):
    monkeypatch.delenv("TRAWL_SCRAPLING_FALLBACK", raising=False)

    result = scrapling_fetcher.fetch("https://example.com/hard")

    assert not result.ok
    assert "TRAWL_SCRAPLING_FALLBACK=1" in (result.error or "")


def test_pipeline_does_not_call_scrapling_when_disabled(monkeypatch):
    monkeypatch.delenv("TRAWL_SCRAPLING_FALLBACK", raising=False)
    monkeypatch.setattr(
        pipeline.playwright,
        "fetch",
        lambda url: FetchResult(
            url=url,
            html="",
            markdown="",
            raw_html="",
            fetcher="playwright",
            elapsed_ms=1,
            error="PlaywrightTimeoutError: timeout",
        ),
    )

    def explode(*_args, **_kwargs):
        raise AssertionError("scrapling should not be called")

    monkeypatch.setattr(pipeline.scrapling, "fetch", explode)

    fetched, extracted, fetcher_name = pipeline._fetch_html("https://example.com/hard", query="q")

    assert not fetched.ok
    assert extracted.markdown == ""
    assert fetcher_name == "playwright+trafilatura"


def test_pipeline_falls_back_to_scrapling_after_playwright_failure(monkeypatch):
    url = "https://example.com/hard"
    monkeypatch.setenv("TRAWL_SCRAPLING_FALLBACK", "1")
    monkeypatch.setattr(
        pipeline.playwright,
        "fetch",
        lambda _url: FetchResult(
            url=url,
            html="",
            markdown="",
            raw_html="",
            fetcher="playwright",
            elapsed_ms=1,
            error="PlaywrightTimeoutError: timeout",
        ),
    )

    def fake_scrapling_fetch(_url, *, mode="auto", reason=""):
        assert mode == "auto"
        assert reason == "playwright_error"
        return FetchResult(
            url=url,
            html="<html><head><title>Recovered</title></head><body><main>"
            "<h1>Recovered</h1><p>alpha beta body body body body</p>"
            "</main></body></html>",
            markdown="",
            raw_html="",
            fetcher="scrapling-dynamic",
            elapsed_ms=2,
            content_type="text/html",
        )

    monkeypatch.setattr(pipeline.scrapling, "fetch", fake_scrapling_fetch)

    fetched, extracted, fetcher_name = pipeline._fetch_html(url, query="alpha")

    assert fetched.ok
    assert fetched.fetcher == "scrapling-dynamic"
    assert "alpha beta" in extracted.markdown
    assert fetcher_name == "scrapling-dynamic+trafilatura"
