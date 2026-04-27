"""PDF fetcher.

Uses httpx for the download and the backend helpers in `pdf_backends`
for text extraction. PyMuPDF remains the default because it has the best
trade-off of speed, quality, and API simplicity among Python PDF libraries.

Also exposes `probe(url)` — a HEAD-only check used by the pipeline to
catch suffix-less PDF URLs (download links, redirects) before paying
for a Playwright render. Mirrors the structure of
`fetchers/passthrough.probe()`.
"""

from __future__ import annotations

import time

import httpx

from . import pdf_backends
from .playwright import FetchResult, make_error_result

HTTP_TIMEOUT_S = 120.0
PROBE_TIMEOUT_S = 3.0
PDF_CONTENT_TYPE = "application/pdf"


def probe(url: str, *, timeout_s: float = PROBE_TIMEOUT_S) -> bool:
    """HEAD `url`; return True iff the Content-Type names a PDF.

    Returns False on every other outcome (non-PDF type, HEAD not
    supported, redirect chain failure, network/timeout error). Caller
    is expected to fall through to the Playwright path on False — a
    failed probe must never make trawl slower than the pre-C7
    behavior.

    Short default timeout keeps the probe from stalling page loads on
    unresponsive origins. The pipeline only invokes `probe()` for
    URLs that already missed the suffix check, so this overhead lands
    on a minority of fetches.
    """
    try:
        with httpx.Client(timeout=timeout_s, follow_redirects=True) as client:
            resp = client.head(url, headers={"User-Agent": "trawl/0.1"})
    except httpx.HTTPError:
        return False
    if resp.status_code >= 400:
        return False
    ct = (resp.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    return ct == PDF_CONTENT_TYPE


def fetch(url: str, *, backend: str = pdf_backends.DEFAULT_BACKEND) -> FetchResult:
    t0 = time.monotonic()
    try:
        with httpx.Client(follow_redirects=True, timeout=HTTP_TIMEOUT_S) as client:
            r = client.get(url, headers={"User-Agent": "trawl/0.1"})
            r.raise_for_status()
            content = r.content
    except httpx.HTTPError as e:
        return make_error_result(url, "pdf", t0, f"{type(e).__name__}: {e}")

    extraction = pdf_backends.extract_pdf_bytes(content, backend=backend)
    if extraction.error:
        return make_error_result(url, "pdf", t0, f"PDF parse error: {extraction.error}")

    return FetchResult(
        url=url,
        html="",
        markdown=extraction.markdown,
        raw_html="",
        fetcher="pdf",
        elapsed_ms=int((time.monotonic() - t0) * 1000),
    )
