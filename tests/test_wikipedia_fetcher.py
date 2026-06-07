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


# _preserve_headings — guards against the 2024+ MediaWiki HTML where
# `<div class="mw-heading">` + `<span class="mw-editsection">` siblings
# cause Trafilatura to drop the entire heading as boilerplate.


def test_preserve_headings_basic_h2():
    from trawl.fetchers.wikipedia import _preserve_headings

    out = _preserve_headings("<h2>Section</h2><p>Body.</p>")
    assert "## Section" in out
    assert "Body." in out


def test_preserve_headings_mw_heading_wrapper():
    """The exact 2024+ MediaWiki pattern that broke `korean_wiki_person`."""
    from trawl.fetchers.wikipedia import _preserve_headings

    html = (
        '<div class="mw-heading mw-heading2">'
        '<h2 id="x"><span id="x_anchor"></span>임진왜란</h2>'
        '<span class="mw-editsection">[edit]</span>'
        "</div>"
        "<p>Body.</p>"
    )
    out = _preserve_headings(html)
    assert "## 임진왜란" in out
    assert "[edit]" not in out  # editsection span was decomposed


def test_preserve_headings_all_levels():
    from trawl.fetchers.wikipedia import _preserve_headings

    html = "<h1>A</h1><h2>B</h2><h3>C</h3><h4>D</h4><h5>E</h5><h6>F</h6>"
    out = _preserve_headings(html)
    assert "# A" in out
    assert "## B" in out
    assert "### C" in out
    assert "#### D" in out
    assert "##### E" in out
    assert "###### F" in out


def test_preserve_headings_empty_heading_skipped():
    from trawl.fetchers.wikipedia import _preserve_headings

    out = _preserve_headings("<h2></h2><h2>Real</h2>")
    assert "## Real" in out
    # The empty heading should not produce a stray `## ` prefix line.
    assert "## \n" not in out and not out.strip().startswith("## \n")


def test_preserve_headings_malformed_returns_input():
    """Defensive fallback: parse failures must not propagate."""
    from trawl.fetchers.wikipedia import _preserve_headings

    # An empty string is trivially "malformed" for this purpose — and any
    # other unparseable bytes should just round-trip back through the
    # function without raising.
    assert _preserve_headings("") == ""
