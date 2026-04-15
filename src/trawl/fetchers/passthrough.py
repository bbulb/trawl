"""Raw-passthrough fetcher for structured data responses.

Bypasses trawl's extraction/chunking/retrieval when the target URL
returns JSON, XML, RSS, or Atom. Detection is two-stage:

1. URL-suffix hint (this module's `matches`) — cheap, lets the pipeline
   take an httpx-only fast path without launching Playwright.
2. Content-Type post-check (`is_passthrough_content_type`) — used by
   the pipeline after Playwright has already loaded a suffix-less URL,
   to discover API endpoints like `/api/weather` that still return JSON.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

PASSTHROUGH_CONTENT_TYPES: tuple[str, ...] = (
    "application/json",
    "application/xml",
    "text/xml",
    "application/rss+xml",
    "application/atom+xml",
)

PASSTHROUGH_URL_SUFFIXES: tuple[str, ...] = (".json", ".xml", ".rss", ".atom")

PASSTHROUGH_MAX_BYTES: int = int(
    os.environ.get("TRAWL_PASSTHROUGH_MAX_BYTES", "262144")
)


def matches(url: str) -> bool:
    """True when the URL path ends with a structured-data suffix."""
    path = urlsplit(url).path.lower()
    return path.endswith(PASSTHROUGH_URL_SUFFIXES)


def is_passthrough_content_type(ct: str | None) -> bool:
    """True when `ct` names a passthrough-eligible media type.

    Accepts explicit allow-list entries plus any `+json` / `+xml`
    structured-syntax suffix (RFC 6838 §4.2.8).
    """
    if not ct:
        return False
    base = ct.split(";", 1)[0].strip().lower()
    if base in PASSTHROUGH_CONTENT_TYPES:
        return True
    if base.endswith("+json") or base.endswith("+xml"):
        return True
    return False
