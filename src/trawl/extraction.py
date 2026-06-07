"""HTML → markdown extraction.

Two-stage approach:

1. **Trafilatura** in both precision and recall modes. Strong on article-shaped
   content (news, Wikipedia, blog posts); sometimes too aggressive on list /
   pricing / marketing pages.
2. **BeautifulSoup fallback** that strips only obvious non-content tags
   (`script`, `style`, `nav`, `header`, `footer`, `aside`, `form`, `iframe`,
   `svg`, `noscript`) and returns everything else as plain text. This catches
   pages where Trafilatura filters out the main content because it doesn't
   look "articley" to the heuristic — the canonical example is a SaaS pricing
   page where the prices are in styled card `<div>`s that Trafilatura classifies
   as decoration.

We return whichever of Trafilatura or BS produces more text; the downstream
chunker + embedding top-k filter out the extra noise on article pages, and
the extra content lets pricing / list pages work.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from math import log1p

import trafilatura
from bs4 import BeautifulSoup

from . import records

_RECORDS_ENABLED = os.environ.get("TRAWL_RECORDS", "1") != "0"

_NOISE_TAGS = [
    "script",
    "style",
    "noscript",
    "header",
    "footer",
    "nav",
    "aside",
    "form",
    "iframe",
    "svg",
    "menu",
    "dialog",
    "template",
]

_BOILERPLATE_MARKERS = {
    "advertisement",
    "cookie",
    "cookies",
    "footer",
    "login",
    "menu",
    "newsletter",
    "privacy",
    "share",
    "sign in",
    "subscribe",
}
_WORD_RE = re.compile(r"\w+", re.UNICODE)
_MD_LINK_RE = re.compile(r"\[[^\]]+\]\([^)]+\)")
_URL_RE = re.compile(r"https?://\S+")
_MD_HEADING_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_MD_TABLE_LINE_RE = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
_MD_CODE_FENCE_RE = re.compile(r"```")


@dataclass(frozen=True)
class ExtractedContent:
    """Markdown plus extractor provenance for downstream chunks."""

    markdown: str
    extractor: str
    source_selector: str | None = None
    source_xpath: str | None = None
    score: float = 0.0


@dataclass(frozen=True)
class _Candidate:
    name: str
    markdown: str
    source_selector: str | None
    source_xpath: str | None


def html_to_markdown(html: str, *, query: str | None = None) -> str:
    """Extract the main content of an HTML page as markdown (or plain text).

    Runs Trafilatura in precision and recall modes and BeautifulSoup with a
    minimal boilerplate strip, and returns the best-scoring candidate. See the
    module docstring for rationale.

    When ``TRAWL_RECORDS`` is enabled (default), repeating sibling groups in
    the rendered DOM are annotated with invisible-separator sentinels before
    extraction so the downstream chunker can keep each record atomic.
    """
    return extract_html(html, query=query).markdown


def extract_html(html: str, *, query: str | None = None) -> ExtractedContent:
    """Extract HTML into markdown and report the winning extractor.

    Candidate selection is score-based rather than raw-length based. The
    scorer rewards query term coverage, heading/code/table preservation, and
    enough text to be useful; it penalizes link-heavy or obvious boilerplate
    output. Mozilla Readability-compatible Python packages are used when
    installed, but remain optional.
    """
    if not html:
        return ExtractedContent(markdown="", extractor="")

    records_present = False
    if _RECORDS_ENABLED:
        try:
            html, _groups = records.annotate_records(html)
            records_present = bool(_groups)
        except Exception:
            # Annotation is a best-effort enhancement; never fail extraction.
            pass

    common_kwargs = dict(
        output_format="markdown",
        include_links=True,
        include_images=False,
        include_tables=True,
        include_comments=False,
    )

    candidates = [
        _Candidate(
            "trafilatura-recall",
            _safe_trafilatura(html, favor_recall=True, **common_kwargs),
            "document",
            "/",
        ),
        _Candidate(
            "trafilatura-precision",
            _safe_trafilatura(html, favor_precision=True, **common_kwargs),
            "document",
            "/",
        ),
        _Candidate("beautifulsoup", _bs_fallback(html), "body", "/html/body"),
        _Candidate("readability", _readability(html), "readability", None),
    ]
    candidates = [c for c in candidates if c.markdown]

    if not candidates:
        return ExtractedContent(markdown="", extractor="")

    # When records were annotated, prefer a candidate that preserved the
    # sentinels over a longer candidate that stripped them. Aladin is the
    # canonical case: the book list sits inside a <form>, which
    # ``_bs_fallback`` decomposes as a noise tag — the bs output is
    # longer than the trafilatura recall output but contains none of the
    # 50 book records.
    if records_present:
        sentinel_bearing = [c for c in candidates if records.SENTINEL_PREFIX in c.markdown]
        if sentinel_bearing:
            candidates = sentinel_bearing

    best = max(candidates, key=lambda c: _score_candidate(c.markdown, query=query))
    return ExtractedContent(
        markdown=best.markdown,
        extractor=best.name,
        source_selector=best.source_selector,
        source_xpath=best.source_xpath,
        score=_score_candidate(best.markdown, query=query),
    )


def _safe_trafilatura(html: str, **kwargs) -> str:
    try:
        return trafilatura.extract(html, **kwargs) or ""
    except Exception:
        return ""


def _readability(html: str) -> str:
    """Optional Readability extractor.

    The Python ecosystem exposes Mozilla-Readability-like behavior through
    packages such as ``readability-lxml``. Keep this optional so the default
    install does not gain a hard dependency or fail when the package is absent.
    """
    try:
        from readability import Document  # type: ignore[import-not-found]
    except Exception:
        return ""
    try:
        summary_html = Document(html).summary(html_partial=True)
    except Exception:
        return ""
    return _bs_fallback(summary_html)


def _score_candidate(markdown: str, *, query: str | None = None) -> float:
    text = markdown.strip()
    if not text:
        return 0.0

    words = _WORD_RE.findall(text.lower())
    n_words = max(len(words), 1)
    lines = [line for line in text.splitlines() if line.strip()]
    n_lines = max(len(lines), 1)

    query_score = _query_coverage(text, query) * 120.0
    length_score = min(log1p(len(text)) * 8.0, 70.0)
    heading_score = min(len(_MD_HEADING_RE.findall(text)) / n_lines, 0.25) * 60.0
    code_score = min(text.count("`") + len(_MD_CODE_FENCE_RE.findall(text)) * 6, 24) * 1.5
    table_score = min(len(_MD_TABLE_LINE_RE.findall(text)), 12) * 2.0

    link_density = (len(_MD_LINK_RE.findall(text)) + len(_URL_RE.findall(text))) / n_words
    boilerplate_ratio = _boilerplate_marker_ratio(text)

    return (
        query_score
        + length_score
        + heading_score
        + code_score
        + table_score
        - link_density * 80.0
        - boilerplate_ratio * 140.0
    )


def _query_coverage(text: str, query: str | None) -> float:
    if not query:
        return 0.0
    text_terms = set(_WORD_RE.findall(text.lower()))
    query_terms = {
        term for term in _WORD_RE.findall(query.lower()) if len(term) > 1 and not term.isdigit()
    }
    if not query_terms:
        return 0.0
    return len(query_terms & text_terms) / len(query_terms)


def _boilerplate_marker_ratio(text: str) -> float:
    lowered = text.lower()
    hits = sum(lowered.count(marker) for marker in _BOILERPLATE_MARKERS)
    return hits / max(len(_WORD_RE.findall(lowered)), 1)


def _bs_fallback(html: str) -> str:
    """Extract all visible text from the HTML minus obvious boilerplate tags.

    Returns plain text with double-newlines between block-level elements so
    the chunker can split sensibly. Not strictly markdown, but the chunker
    and embedder don't care about markdown syntax — they care about text.

    Noise-tag decomposition is skipped for any subtree that contains a
    record sentinel. The canonical example is Aladin: the 50 book cards
    live inside a ``<form>`` (used for wishlist add), which is normally a
    noise tag; stripping it would delete every record we just annotated.
    """
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        return ""
    for tag in soup(_NOISE_TAGS):
        # Preserve tags that wrap annotated records. Checking the string
        # representation is cheap because the sentinel is ASCII and rare.
        if records.SENTINEL_PREFIX in str(tag):
            continue
        tag.decompose()
    body = soup.body or soup
    return body.get_text(separator="\n", strip=True)


_MD_H1_RE = re.compile(r"^# +(.+?)\s*$", re.MULTILINE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def extract_title(*, html: str, markdown: str) -> str:
    """Return a best-effort page title.

    Resolution order:
      1. HTML <title> tag content, whitespace-stripped.
      2. First markdown H1 line (`# ...`), whitespace-stripped.
      3. Empty string.

    Never raises. Callers should treat "" as "no title available".
    """
    if html:
        try:
            soup = BeautifulSoup(html, "html.parser")
            if soup.title:
                text = _HTML_TAG_RE.sub("", soup.title.get_text(strip=True)).strip()
                if text:
                    return text
        except Exception:
            pass

    if markdown:
        m = _MD_H1_RE.search(markdown)
        if m:
            return m.group(1).strip()

    return ""
