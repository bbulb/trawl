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


def html_to_markdown(html: str) -> str:
    """Extract the main content of an HTML page as markdown (or plain text).

    Runs Trafilatura in precision and recall modes and BeautifulSoup with a
    minimal boilerplate strip, and returns whichever is longest. See the
    module docstring for rationale.

    When ``TRAWL_RECORDS`` is enabled (default), repeating sibling groups in
    the rendered DOM are annotated with invisible-separator sentinels before
    extraction so the downstream chunker can keep each record atomic.
    """
    if not html:
        return ""

    if _RECORDS_ENABLED:
        try:
            html, _groups = records.annotate_records(html)
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

    precise = _safe_trafilatura(html, favor_precision=True, **common_kwargs)
    recall = _safe_trafilatura(html, favor_recall=True, **common_kwargs)
    bs = _bs_fallback(html)

    candidates = [c for c in (recall, precise, bs) if c]
    if not candidates:
        return ""
    return max(candidates, key=len)


def _safe_trafilatura(html: str, **kwargs) -> str:
    try:
        return trafilatura.extract(html, **kwargs) or ""
    except Exception:
        return ""


def _bs_fallback(html: str) -> str:
    """Extract all visible text from the HTML minus obvious boilerplate tags.

    Returns plain text with double-newlines between block-level elements so
    the chunker can split sensibly. Not strictly markdown, but the chunker
    and embedder don't care about markdown syntax — they care about text.
    """
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        return ""
    for tag in soup(_NOISE_TAGS):
        tag.decompose()
    body = soup.body or soup
    # `get_text("\n", strip=True)` collapses each element's text runs while
    # preserving block boundaries. Good enough for chunking; the noise
    # filter in chunking.py drops any chunks that are mostly whitespace.
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
