"""Raw-passthrough fetcher for structured data responses.

Bypasses trawl's extraction/chunking/retrieval when the target URL
returns JSON, XML, RSS, or Atom. Detection is three-stage:

1. URL-suffix hint (this module's `matches`) — cheap, lets the pipeline
   take an httpx-only fast path without launching Playwright.
2. HEAD pre-probe (`probe`) — for suffix-less URLs like
   `api.open-meteo.com/v1/forecast`. Small HEAD request to learn the
   Content-Type before paying for a headless browser render.
3. Content-Type post-check (`is_passthrough_content_type`) — last-resort
   check on the Playwright response, used when HEAD isn't supported by
   the origin but GET succeeded and revealed structured data.
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


import time
from dataclasses import dataclass

import httpx


@dataclass
class PassthroughResult:
    """Result of a passthrough fetch. Mirrors FetchResult where it matters
    (url, error, elapsed_ms) but carries raw bytes and content_type
    instead of rendered HTML, since passthrough skips extraction entirely.
    """
    url: str
    raw_bytes: bytes
    content_type: str | None
    elapsed_ms: int
    truncated: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def probe(url: str, *, timeout_s: float = 3.0) -> str | None:
    """HEAD `url` and return its Content-Type when it names a passthrough
    media type. Returns None for every other outcome (non-passthrough
    Content-Type, HEAD not supported, redirect chain, network error).

    The caller is expected to fall through to the normal HTML fetcher
    when None is returned. A short default timeout keeps the probe from
    stalling page loads on unresponsive origins — failure of the probe
    must not make trawl slower than before.
    """
    try:
        with httpx.Client(timeout=timeout_s, follow_redirects=True) as client:
            resp = client.head(url)
    except httpx.HTTPError:
        return None
    if resp.status_code >= 400:
        return None
    ct = resp.headers.get("content-type")
    return ct if is_passthrough_content_type(ct) else None


def fetch(url: str, *, timeout_s: float = 15.0) -> PassthroughResult:
    """GET `url` via httpx, stream-capped at PASSTHROUGH_MAX_BYTES.

    Returns `ok=False` (with an error string) when:
      - the request fails (network, timeout)
      - HTTP status is >= 400
      - Content-Type does not pass `is_passthrough_content_type`
      - the body is empty

    The caller is expected to fall through to another fetcher when `ok`
    is False, except in the terminal "empty body" case which is a real
    error (raw fetch worked, server returned nothing).
    """
    t0 = time.monotonic()
    try:
        with httpx.stream(
            "GET",
            url,
            follow_redirects=True,
            timeout=timeout_s,
        ) as resp:
            if resp.status_code >= 400:
                return PassthroughResult(
                    url=url,
                    raw_bytes=b"",
                    content_type=resp.headers.get("content-type"),
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                    error=f"HTTP {resp.status_code}",
                )
            ct = resp.headers.get("content-type")
            if not is_passthrough_content_type(ct):
                return PassthroughResult(
                    url=url,
                    raw_bytes=b"",
                    content_type=ct,
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                    error=f"content-type mismatch: {ct!r}",
                )
            buf = bytearray()
            truncated = False
            for chunk in resp.iter_bytes():
                remaining = PASSTHROUGH_MAX_BYTES - len(buf)
                if remaining <= 0:
                    truncated = True
                    break
                if len(chunk) > remaining:
                    buf.extend(chunk[:remaining])
                    truncated = True
                    break
                buf.extend(chunk)
            if not buf and not truncated:
                return PassthroughResult(
                    url=url,
                    raw_bytes=b"",
                    content_type=ct,
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                    error="empty body",
                )
            return PassthroughResult(
                url=url,
                raw_bytes=bytes(buf),
                content_type=ct,
                elapsed_ms=int((time.monotonic() - t0) * 1000),
                truncated=truncated,
            )
    except httpx.HTTPError as e:
        return PassthroughResult(
            url=url,
            raw_bytes=b"",
            content_type=None,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
            error=f"{type(e).__name__}: {e}",
        )


def fetch_raw_body(
    url: str,
    *,
    timeout_s: float = 15.0,
) -> PassthroughResult:
    """Re-fetch `url` via httpx to recover the original bytes.

    Used after Playwright has detected a passthrough Content-Type on
    the navigation response. Playwright's rendered HTML at that point
    is Chromium's JSON/XML viewer DOM, not the raw body, so we issue a
    direct httpx GET. Unlike `fetch`, this does not enforce the URL-
    hint gate or fail on Content-Type mismatch — the caller already
    decided the body is structured data.
    """
    t0 = time.monotonic()
    try:
        with httpx.stream(
            "GET",
            url,
            follow_redirects=True,
            timeout=timeout_s,
        ) as resp:
            if resp.status_code >= 400:
                return PassthroughResult(
                    url=url,
                    raw_bytes=b"",
                    content_type=resp.headers.get("content-type"),
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                    error=f"HTTP {resp.status_code}",
                )
            ct = resp.headers.get("content-type")
            buf = bytearray()
            truncated = False
            for chunk in resp.iter_bytes():
                remaining = PASSTHROUGH_MAX_BYTES - len(buf)
                if remaining <= 0:
                    truncated = True
                    break
                if len(chunk) > remaining:
                    buf.extend(chunk[:remaining])
                    truncated = True
                    break
                buf.extend(chunk)
            if not buf and not truncated:
                return PassthroughResult(
                    url=url,
                    raw_bytes=b"",
                    content_type=ct,
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                    error="empty body",
                )
            return PassthroughResult(
                url=url,
                raw_bytes=bytes(buf),
                content_type=ct,
                elapsed_ms=int((time.monotonic() - t0) * 1000),
                truncated=truncated,
            )
    except httpx.HTTPError as e:
        return PassthroughResult(
            url=url,
            raw_bytes=b"",
            content_type=None,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
            error=f"{type(e).__name__}: {e}",
        )
