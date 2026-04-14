"""Stack Exchange API fetcher.

Uses the Stack Exchange API v2.3 to fetch questions and answers
without rendering the page. Falls back to Playwright when the API
fails or for unrecognized SE domains.

Public API mirrors the PDF/YouTube fetchers: fetch(url) -> FetchResult.
"""

from __future__ import annotations

import logging
import os
import re
import time
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from . import playwright as pw
from .playwright import FetchResult, make_error_result

logger = logging.getLogger(__name__)

_SE_API_BASE = "https://api.stackexchange.com/2.3"
_SE_API_KEY = os.environ.get("TRAWL_SE_API_KEY", "")

_SE_DOMAINS = {
    "stackoverflow.com": "stackoverflow",
    "superuser.com": "superuser",
    "serverfault.com": "serverfault",
    "askubuntu.com": "askubuntu",
    "mathoverflow.net": "mathoverflow",
}

_QUESTION_RE = re.compile(r"^/(?:questions|q)/(\d+)")
_ANSWER_RE = re.compile(r"^/a/(\d+)")


def matches(url: str) -> bool:
    """Return True if `url` is a Stack Exchange URL the API fetcher handles."""
    return _parse_se_url(url) is not None


def _parse_se_url(url: str) -> tuple[str, str] | None:
    """Parse a Stack Exchange URL into (site, question_id), or None."""
    parts = urlsplit(url)
    host = parts.hostname or ""

    site = _SE_DOMAINS.get(host) or _SE_DOMAINS.get(host.removeprefix("www."))
    if not site:
        if host.endswith(".stackexchange.com"):
            site = host.removesuffix(".stackexchange.com")
        else:
            return None

    m = _QUESTION_RE.match(parts.path)
    if m:
        return (site, m.group(1))

    m = _ANSWER_RE.match(parts.path)
    if m:
        return (site, m.group(1))

    return None


def _html_to_text(html: str) -> str:
    """Convert HTML body from the SE API to plain text."""
    soup = BeautifulSoup(html, "lxml")
    for pre in soup.find_all("pre"):
        pre.insert_before("\n```\n")
        pre.insert_after("\n```\n")
    return soup.get_text(separator="\n", strip=True)


def fetch(url: str) -> FetchResult:
    """Fetch a Stack Exchange question + answers via the API.

    Returns the question and its answers as markdown, sorted by score
    with accepted answer first. Falls back to Playwright on API failure.

    Never raises -- errors land in FetchResult.error.
    """
    t0 = time.monotonic()

    parsed = _parse_se_url(url)
    if parsed is None:
        return make_error_result(url, "stackexchange", t0, f"invalid Stack Exchange URL: {url}")

    site, question_id = parsed
    params = {
        "site": site,
        "filter": "withbody",
        "order": "desc",
        "sort": "votes",
    }
    if _SE_API_KEY:
        params["key"] = _SE_API_KEY

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(
                f"{_SE_API_BASE}/questions/{question_id}",
                params={**params, "filter": "withbody"},
            )
            resp.raise_for_status()
            data = resp.json()

            items = data.get("items", [])
            if not items:
                logger.info("SE API returned no items for %s/%s", site, question_id)
                return pw.fetch(url)

            question = items[0]
            title = question.get("title", "")
            q_body = _html_to_text(question.get("body", ""))
            tags = ", ".join(question.get("tags", []))

            resp_a = client.get(
                f"{_SE_API_BASE}/questions/{question_id}/answers",
                params=params,
            )
            resp_a.raise_for_status()
            answers = resp_a.json().get("items", [])

        parts = [f"# {title}\n\nTags: {tags}\n\n{q_body}"]

        answers.sort(key=lambda a: (not a.get("is_accepted", False), -a.get("score", 0)))
        for ans in answers:
            score = ans.get("score", 0)
            accepted = " accepted" if ans.get("is_accepted") else ""
            a_body = _html_to_text(ans.get("body", ""))
            parts.append(f"---\n\n## Answer (score: {score},{accepted})\n\n{a_body}")

        markdown = "\n\n".join(parts)
        return FetchResult(
            url=url,
            html="",
            markdown=markdown,
            raw_html="",
            fetcher="stackexchange",
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )
    except Exception as e:
        logger.warning(
            "SE API error for %s/%s (%s: %s), falling back to playwright",
            site,
            question_id,
            type(e).__name__,
            e,
        )

    return pw.fetch(url)
