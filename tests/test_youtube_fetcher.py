"""Unit tests for YouTube fetcher -- video ID extraction and URL detection."""

from __future__ import annotations


def test_extract_video_id_standard():
    from trawl.fetchers.youtube import _extract_video_id

    assert _extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_extract_video_id_short():
    from trawl.fetchers.youtube import _extract_video_id

    assert _extract_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_extract_video_id_shorts():
    from trawl.fetchers.youtube import _extract_video_id

    assert _extract_video_id("https://www.youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_extract_video_id_live():
    from trawl.fetchers.youtube import _extract_video_id

    assert _extract_video_id("https://www.youtube.com/live/dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_extract_video_id_with_extra_params():
    from trawl.fetchers.youtube import _extract_video_id

    assert (
        _extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=30s&list=PLx")
        == "dQw4w9WgXcQ"
    )


def test_extract_video_id_invalid():
    from trawl.fetchers.youtube import _extract_video_id

    assert _extract_video_id("https://www.example.com/page") is None


def test_extract_video_id_youtube_non_video():
    from trawl.fetchers.youtube import _extract_video_id

    assert _extract_video_id("https://www.youtube.com/channel/UCxyz") is None


def test_fetch_returns_transcript_for_known_video():
    """Integration test: fetch a well-known video that has stable English subtitles."""
    from trawl.fetchers.youtube import fetch

    result = fetch("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert result.ok
    assert result.fetcher == "youtube"
    assert len(result.markdown) > 100
    assert "never gonna give you up" in result.markdown.lower()


def test_is_youtube_url():
    from trawl.pipeline import _is_youtube_url

    assert _is_youtube_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert _is_youtube_url("https://youtu.be/dQw4w9WgXcQ")
    assert _is_youtube_url("https://www.youtube.com/shorts/dQw4w9WgXcQ")
    assert _is_youtube_url("https://www.youtube.com/live/dQw4w9WgXcQ")
    assert not _is_youtube_url("https://www.example.com/page")
    assert not _is_youtube_url("https://www.youtube.com/channel/UCxyz")


def test_fetch_fallback_on_invalid_id():
    """A non-YouTube URL should return an error (no fallback attempted)."""
    from trawl.fetchers.youtube import fetch

    result = fetch("https://www.example.com/page")
    assert not result.ok
    assert "invalid" in (result.error or "").lower()
