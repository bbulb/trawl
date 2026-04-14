"""YouTube transcript fetcher.

Uses youtube_transcript_api to extract video transcripts without
rendering the page. Falls back to Playwright when transcripts are
unavailable so the caller still gets title/description metadata.

Public API mirrors the PDF fetcher: fetch(url) -> FetchResult.
"""

from __future__ import annotations

import logging
import re
import time
from urllib.parse import parse_qs, urlsplit

from . import playwright as pw
from .playwright import FetchResult, make_error_result

logger = logging.getLogger(__name__)

_YT_HOSTS = {"www.youtube.com", "youtube.com", "m.youtube.com", "youtu.be"}

_PATH_ID_RE = re.compile(r"^/(?:shorts|live)/([A-Za-z0-9_-]{11})(?:[/?#]|$)")


def matches(url: str) -> bool:
    """Return True if `url` is a YouTube video URL the API fetcher handles."""
    return _extract_video_id(url) is not None


def _extract_video_id(url: str) -> str | None:
    """Extract the YouTube video ID from a URL, or None if not a YouTube video."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if host not in _YT_HOSTS:
        return None

    # youtu.be/ID
    if host == "youtu.be":
        segment = parts.path.lstrip("/").split("/")[0]
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", segment):
            return segment
        return None

    # youtube.com/watch?v=ID
    if parts.path in ("/watch", "/watch/"):
        qs = parse_qs(parts.query)
        v = qs.get("v", [None])[0]
        if v and re.fullmatch(r"[A-Za-z0-9_-]{11}", v):
            return v
        return None

    # youtube.com/shorts/ID or youtube.com/live/ID
    m = _PATH_ID_RE.match(parts.path)
    if m:
        return m.group(1)

    return None


def fetch(url: str) -> FetchResult:
    """Fetch a YouTube video's transcript text.

    On the happy path (transcript available), returns the transcript as
    markdown with fetcher="youtube" -- no Playwright needed.

    When no transcript is available, falls back to playwright.fetch(url)
    so the caller still gets title/description metadata.

    Never raises -- errors land in FetchResult.error.
    """
    t0 = time.monotonic()

    video_id = _extract_video_id(url)
    if video_id is None:
        return make_error_result(
            url,
            "youtube",
            t0,
            f"invalid YouTube URL: could not extract video ID from {url}",
        )

    try:
        from youtube_transcript_api import (
            CouldNotRetrieveTranscript,
            YouTubeTranscriptApi,
        )
    except ImportError:
        return make_error_result(
            url,
            "youtube",
            t0,
            "youtube-transcript-api not installed (pip install youtube-transcript-api)",
        )

    try:
        ytt = YouTubeTranscriptApi()
        transcript_list = ytt.list(video_id)
        # Prefer manual subtitles over auto-generated; accept any language.
        manual = [t for t in transcript_list if not t.is_generated]
        chosen = manual[0] if manual else list(transcript_list)[0]
        transcript = chosen.fetch()
        text = " ".join(snippet.text for snippet in transcript)
        if not text.strip():
            logger.info("empty transcript for %s, falling back to playwright", video_id)
        else:
            return FetchResult(
                url=url,
                html="",
                markdown=text,
                raw_html="",
                fetcher="youtube",
                elapsed_ms=int((time.monotonic() - t0) * 1000),
            )
    except CouldNotRetrieveTranscript as e:
        logger.info(
            "no transcript for %s (%s), falling back to playwright",
            video_id,
            e,
        )
    except Exception as e:
        logger.warning(
            "youtube_transcript_api error for %s (%s: %s), falling back to playwright",
            video_id,
            type(e).__name__,
            e,
        )

    # Fallback: render the page with Playwright to get title/description.
    return pw.fetch(url)
