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
