"""PDF fetcher.

Uses httpx for the download and PyMuPDF (`pymupdf`) for text extraction.
PyMuPDF has the best trade-off of speed, quality, and API simplicity
among Python PDF libraries — a single `page.get_text()` per page gives
us reasonable reading order for most academic and technical PDFs.
"""

from __future__ import annotations

import time

import httpx

from .playwright import FetchResult, make_error_result

HTTP_TIMEOUT_S = 120.0


def fetch(url: str) -> FetchResult:
    t0 = time.monotonic()
    try:
        with httpx.Client(follow_redirects=True, timeout=HTTP_TIMEOUT_S) as client:
            r = client.get(url, headers={"User-Agent": "trawl/0.1"})
            r.raise_for_status()
            content = r.content
    except httpx.HTTPError as e:
        return make_error_result(url, "pdf", t0, f"{type(e).__name__}: {e}")

    try:
        import pymupdf  # noqa: PLC0415  -- lazy import
    except ImportError:
        return make_error_result(url, "pdf", t0, "pymupdf not installed (pip install pymupdf)")

    try:
        doc = pymupdf.open(stream=content, filetype="pdf")
        pages: list[str] = []
        for page in doc:
            # 'text' reading order is good enough for chunking; 'blocks' or
            # 'dict' give structure but add complexity we don't need here.
            pages.append(page.get_text("text"))
        doc.close()
    except Exception as e:
        return make_error_result(url, "pdf", t0, f"PDF parse error: {type(e).__name__}: {e}")

    # Join pages with a blank line. The chunker's sentence fallback splitter
    # will still break each paragraph sensibly even when pages have no
    # explicit newlines inside them.
    markdown = "\n\n".join(p.strip() for p in pages if p.strip())
    return FetchResult(
        url=url,
        html="",
        markdown=markdown,
        raw_html="",
        fetcher="pdf",
        elapsed_ms=int((time.monotonic() - t0) * 1000),
    )
