"""Unit tests for extract_title()."""

from trawl.extraction import extract_title


def test_html_title_tag():
    html = "<html><head><title>  The Real Title  </title></head><body>x</body></html>"
    assert extract_title(html=html, markdown="") == "The Real Title"


def test_html_title_tag_beats_markdown_h1():
    html = "<html><head><title>HTML Title</title></head><body>x</body></html>"
    markdown = "# Markdown H1\n\nbody"
    assert extract_title(html=html, markdown=markdown) == "HTML Title"


def test_markdown_h1_fallback():
    assert extract_title(html="", markdown="# My Doc\n\nbody") == "My Doc"


def test_markdown_h1_fallback_when_html_has_no_title():
    html = "<html><body>no title tag</body></html>"
    markdown = "# Fallback H1\n\nbody"
    assert extract_title(html=html, markdown=markdown) == "Fallback H1"


def test_markdown_h1_skips_h2():
    md = "## Not This\n\nbody\n\n# This One"
    assert extract_title(html="", markdown=md) == "This One"


def test_empty_inputs():
    assert extract_title(html="", markdown="") == ""


def test_whitespace_only_title():
    html = "<html><head><title>   </title></head><body>x</body></html>"
    assert extract_title(html=html, markdown="") == ""


def test_malformed_html_does_not_raise():
    # BeautifulSoup must tolerate unclosed tags etc.
    html = "<title>Broken"
    # Either returns "Broken" or "" -- both acceptable; must not raise.
    out = extract_title(html=html, markdown="")
    assert isinstance(out, str)


def test_html_title_with_nested_tags():
    # <title> with mixed content must return clean text, not raw markup.
    html = "<html><head><title><b>Site</b> - Page</title></head><body>x</body></html>"
    assert extract_title(html=html, markdown="") == "Site - Page"
