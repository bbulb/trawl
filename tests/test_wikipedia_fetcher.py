"""Unit tests for Wikipedia fetcher -- URL parsing and API fetch."""

from __future__ import annotations


def test_parse_english():
    from trawl.fetchers.wikipedia import _parse_wikipedia_url

    r = _parse_wikipedia_url("https://en.wikipedia.org/wiki/Python_(programming_language)")
    assert r == ("en", "Python_(programming_language)")


def test_parse_korean():
    from trawl.fetchers.wikipedia import _parse_wikipedia_url

    r = _parse_wikipedia_url("https://ko.wikipedia.org/wiki/%EC%9D%B4%EC%88%9C%EC%8B%A0")
    assert r == ("ko", "\uc774\uc21c\uc2e0")


def test_parse_japanese():
    from trawl.fetchers.wikipedia import _parse_wikipedia_url

    r = _parse_wikipedia_url("https://ja.wikipedia.org/wiki/%E5%AF%BF%E5%8F%B8")
    assert r == ("ja", "\u5bff\u53f8")


def test_parse_mobile():
    from trawl.fetchers.wikipedia import _parse_wikipedia_url

    r = _parse_wikipedia_url("https://en.m.wikipedia.org/wiki/Python_(programming_language)")
    assert r == ("en", "Python_(programming_language)")


def test_parse_non_wikipedia():
    from trawl.fetchers.wikipedia import _parse_wikipedia_url

    assert _parse_wikipedia_url("https://www.example.com/page") is None


def test_parse_non_article():
    from trawl.fetchers.wikipedia import _parse_wikipedia_url

    assert _parse_wikipedia_url("https://en.wikipedia.org/wiki/Special:Random") is None


def test_fetch_korean_article():
    """Integration test: fetch the Yi Sun-sin article via MediaWiki API."""
    from trawl.fetchers.wikipedia import fetch

    result = fetch("https://ko.wikipedia.org/wiki/%EC%9D%B4%EC%88%9C%EC%8B%A0")
    assert result.ok
    assert result.fetcher == "wikipedia"
    assert len(result.markdown) > 1000
    assert "1545" in result.markdown


def test_fetch_invalid_url():
    from trawl.fetchers.wikipedia import fetch

    result = fetch("https://www.example.com/page")
    assert not result.ok
    assert "invalid" in (result.error or "").lower()
