"""Wikipedia MediaWiki API fetcher.

Uses the MediaWiki parse API to fetch article HTML without rendering
the page in a browser. The HTML is then fed through the existing
extraction.html_to_markdown() pipeline for consistency.

Falls back to Playwright when the API fails.

Public API mirrors the PDF/YouTube fetchers: fetch(url) -> FetchResult.
"""

from __future__ import annotations

import logging
import re
import time
from urllib.parse import unquote, urlsplit

import httpx

from trawl import extraction

from . import playwright as pw
from .playwright import FetchResult, make_error_result

logger = logging.getLogger(__name__)

_WIKI_HOST_RE = re.compile(r"^([a-z]{2,3})(?:\.m)?\.wikipedia\.org$")
_SPECIAL_PREFIXES = (
    "Special:",
    "Wikipedia:",
    "Help:",
    "Talk:",
    "User:",
    "File:",
    "Category:",
    "Template:",
    "Portal:",
)


def matches(url: str) -> bool:
    """Return True if `url` is a Wikipedia URL the API fetcher handles."""
    return _parse_wikipedia_url(url) is not None


def _parse_wikipedia_url(url: str) -> tuple[str, str] | None:
    """Parse a Wikipedia URL into (lang, title), or None."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    m = _WIKI_HOST_RE.match(host)
    if not m:
        return None
    lang = m.group(1)

    path = parts.path
    if not path.startswith("/wiki/"):
        return None

    title = unquote(path[len("/wiki/") :])
    if not title:
        return None

    for prefix in _SPECIAL_PREFIXES:
        if title.startswith(prefix):
            return None

    return (lang, title)


def fetch(url: str) -> FetchResult:
    """Fetch a Wikipedia article via the MediaWiki parse API.

    Returns the article HTML converted to markdown using the existing
    extraction pipeline. Falls back to Playwright on API failure.

    Never raises -- errors land in FetchResult.error.
    """
    t0 = time.monotonic()

    parsed = _parse_wikipedia_url(url)
    if parsed is None:
        return make_error_result(url, "wikipedia", t0, f"invalid Wikipedia URL: {url}")

    lang, title = parsed
    api_url = f"https://{lang}.wikipedia.org/w/api.php"
    params = {
        "action": "parse",
        "page": title,
        "prop": "text",
        "format": "json",
        "redirects": "1",
    }

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(
                api_url,
                params=params,
                headers={"User-Agent": "trawl/0.1 (selective web extraction)"},
            )
            resp.raise_for_status()
            data = resp.json()

        if "error" in data:
            logger.info(
                "MediaWiki API error for %s/%s: %s",
                lang,
                title,
                data["error"].get("info", ""),
            )
            return pw.fetch(url)

        html = data.get("parse", {}).get("text", {}).get("*", "")
        if not html:
            logger.info("empty HTML from MediaWiki API for %s/%s", lang, title)
            return pw.fetch(url)

        markdown = extraction.html_to_markdown(html)
        if not markdown:
            return pw.fetch(url)

        return FetchResult(
            url=url,
            html="",
            markdown=markdown,
            raw_html="",
            fetcher="wikipedia",
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )
    except Exception as e:
        logger.warning(
            "MediaWiki API error for %s/%s (%s: %s), falling back to playwright",
            lang,
            title,
            type(e).__name__,
            e,
        )

    return pw.fetch(url)
