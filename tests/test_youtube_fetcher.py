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


# ---------- transcript language selection (_choose_transcript)


class _FakeTranscript:
    def __init__(self, language_code, is_generated):
        self.language_code = language_code
        self.is_generated = is_generated


class _FakeTranscriptList:
    """Mimics youtube_transcript_api's TranscriptList: iterable +
    find_transcript(langs) raising NoTranscriptFound when absent."""

    def __init__(self, transcripts):
        self._transcripts = transcripts

    def __iter__(self):
        return iter(self._transcripts)

    def find_transcript(self, language_codes):
        from youtube_transcript_api._errors import NoTranscriptFound

        for code in language_codes:
            for t in self._transcripts:
                if t.language_code == code:
                    return t
        raise NoTranscriptFound("vid", language_codes, self._transcripts)


def test_choose_transcript_prefers_english_over_arbitrary_manual():
    # The 3blue1brown shape: manual subtitles in many languages, Arabic
    # first. English must win over "first manual".
    from trawl.fetchers.youtube import _choose_transcript

    tl = _FakeTranscriptList(
        [
            _FakeTranscript("ar", False),
            _FakeTranscript("zh", False),
            _FakeTranscript("en", False),
            _FakeTranscript("en", True),
        ]
    )
    assert _choose_transcript(tl).language_code == "en"


def test_choose_transcript_no_english_prefers_generated_original():
    # Korean lecture: no English; the generated transcript is the
    # original audio language, preferred over arbitrary manual translations.
    from trawl.fetchers.youtube import _choose_transcript

    tl = _FakeTranscriptList(
        [
            _FakeTranscript("ar", False),  # arbitrary manual translation
            _FakeTranscript("ko", True),  # generated, original language
        ]
    )
    chosen = _choose_transcript(tl)
    assert chosen.language_code == "ko"
    assert chosen.is_generated


def test_choose_transcript_falls_back_to_first_manual():
    from trawl.fetchers.youtube import _choose_transcript

    tl = _FakeTranscriptList([_FakeTranscript("ja", False), _FakeTranscript("fr", False)])
    assert _choose_transcript(tl).language_code == "ja"
