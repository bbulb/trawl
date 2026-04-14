"""GitHub REST API fetcher.

Uses the GitHub REST API to fetch READMEs, issues, PRs, and file
contents without rendering the page. Falls back to Playwright when
the API fails or for unsupported GitHub URL patterns.

Public API mirrors the PDF/YouTube fetchers: fetch(url) -> FetchResult.
"""

from __future__ import annotations

import base64
import logging
import os
import time
from urllib.parse import urlsplit

import httpx

from . import playwright as pw
from .playwright import FetchResult, make_error_result

logger = logging.getLogger(__name__)

_GH_API_BASE = "https://api.github.com"
_GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")


def matches(url: str) -> bool:
    """Return True if `url` is a GitHub URL the API fetcher handles."""
    return _parse_github_url(url) is not None


def _parse_github_url(url: str) -> tuple[str, str, str, dict] | None:
    """Parse a GitHub URL into (owner, repo, kind, params), or None.

    kind is one of: "readme", "issue", "pull", "blob".
    Returns None for unsupported paths (discussions, actions, etc.).
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    if host not in ("github.com", "www.github.com"):
        return None

    segments = [s for s in parts.path.split("/") if s]
    if len(segments) < 2:
        return None

    owner, repo = segments[0], segments[1]

    if len(segments) == 2:
        return (owner, repo, "readme", {})
    if segments[2] == "tree":
        return (owner, repo, "readme", {})
    if segments[2] == "issues" and len(segments) >= 4 and segments[3].isdigit():
        return (owner, repo, "issue", {"number": segments[3]})
    if segments[2] == "pull" and len(segments) >= 4 and segments[3].isdigit():
        return (owner, repo, "pull", {"number": segments[3]})
    if segments[2] == "blob" and len(segments) >= 5:
        ref = segments[3]
        path = "/".join(segments[4:])
        return (owner, repo, "blob", {"ref": ref, "path": path})

    return None


def _gh_headers() -> dict[str, str]:
    """Build headers for GitHub API requests."""
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "trawl/0.1",
    }
    if _GH_TOKEN:
        headers["Authorization"] = f"Bearer {_GH_TOKEN}"
    return headers


def _fetch_readme(client: httpx.Client, owner: str, repo: str) -> str:
    resp = client.get(
        f"{_GH_API_BASE}/repos/{owner}/{repo}/readme",
        headers={**_gh_headers(), "Accept": "application/vnd.github.raw"},
    )
    resp.raise_for_status()
    return resp.text


def _fetch_issue_or_pr(client: httpx.Client, owner: str, repo: str, kind: str, number: str) -> str:
    endpoint = "issues" if kind == "issue" else "pulls"
    resp = client.get(
        f"{_GH_API_BASE}/repos/{owner}/{repo}/{endpoint}/{number}",
        headers=_gh_headers(),
    )
    resp.raise_for_status()
    data = resp.json()
    title = data.get("title", "")
    body = data.get("body", "") or ""
    state = data.get("state", "")
    labels = ", ".join(lb.get("name", "") for lb in data.get("labels", []))
    header = f"# {title}\n\nState: {state}"
    if labels:
        header += f"  Labels: {labels}"
    return f"{header}\n\n{body}"


def _fetch_blob(client: httpx.Client, owner: str, repo: str, ref: str, path: str) -> str:
    resp = client.get(
        f"{_GH_API_BASE}/repos/{owner}/{repo}/contents/{path}",
        params={"ref": ref},
        headers=_gh_headers(),
    )
    resp.raise_for_status()
    data = resp.json()
    content_b64 = data.get("content", "")
    return base64.b64decode(content_b64).decode("utf-8")


def fetch(url: str) -> FetchResult:
    """Fetch GitHub content via the REST API.

    Supports repo READMEs, issues, PRs, and file blobs.
    Falls back to Playwright for unsupported paths or API failures.

    Never raises -- errors land in FetchResult.error.
    """
    t0 = time.monotonic()

    parsed = _parse_github_url(url)
    if parsed is None:
        return make_error_result(url, "github", t0, f"invalid GitHub URL: {url}")

    owner, repo, kind, params = parsed

    try:
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            if kind == "readme":
                markdown = _fetch_readme(client, owner, repo)
            elif kind in ("issue", "pull"):
                markdown = _fetch_issue_or_pr(client, owner, repo, kind, params["number"])
            elif kind == "blob":
                markdown = _fetch_blob(client, owner, repo, params["ref"], params["path"])
            else:
                return pw.fetch(url)

        if not markdown.strip():
            logger.info("empty content from GitHub API for %s, falling back", url)
            return pw.fetch(url)

        return FetchResult(
            url=url,
            html="",
            markdown=markdown,
            raw_html="",
            fetcher="github",
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )
    except Exception as e:
        logger.warning(
            "GitHub API error for %s (%s: %s), falling back to playwright",
            url,
            type(e).__name__,
            e,
        )

    return pw.fetch(url)
