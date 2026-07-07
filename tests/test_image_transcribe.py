from __future__ import annotations

import struct
import zlib

import fitz

from trawl import image_transcribe, pipeline


def _png(width: int, height: int, color: str = "white") -> bytes:
    colors = {
        "white": b"\xff\xff\xff",
        "blue": b"\x00\x00\xff",
        "green": b"\x00\x80\x00",
        "red": b"\xff\x00\x00",
    }
    pixel = colors[color]
    raw = b"".join(b"\x00" + pixel * width for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _jpeg(width: int, height: int) -> bytes:
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, width, height), 0)
    return pix.tobytes("jpeg")


def test_image_info_and_selection_accept_jpeg_without_pillow(monkeypatch):
    tall = _jpeg(200, 1600)

    assert image_transcribe._image_info(tall, "image/jpeg") == (200, 1600, "image/jpeg")

    monkeypatch.setattr(
        image_transcribe,
        "_download_image",
        lambda _url: (tall, "image/jpeg"),
    )

    assert image_transcribe._selected_image("https://example.com/tall.jpg") == (
        "https://example.com/tall.jpg",
        tall,
        "image/jpeg",
    )


def test_unknown_image_type_defaults_to_jpeg():
    assert image_transcribe._normalized_content_type(None, None) == "image/jpeg"


def test_selection_keeps_tall_images_skips_square_and_respects_max(monkeypatch):
    tall1 = _png(200, 1600, "white")
    tall2 = _png(300, 1200, "blue")
    tall3 = _png(250, 1000, "green")
    square = _png(500, 500, "red")
    payloads = {
        "https://example.com/tall1.png": tall1,
        "https://example.com/square.png": square,
        "https://example.com/tall2.png": tall2,
        "https://example.com/tall3.png": tall3,
    }
    calls: list[bytes] = []

    monkeypatch.setenv("TRAWL_IMAGE_TRANSCRIBE_MAX_IMAGES", "2")
    monkeypatch.setenv("TRAWL_IMAGE_TRANSCRIBE_CACHE_TTL", "0")
    monkeypatch.setattr(
        image_transcribe,
        "_download_image",
        lambda url: (payloads[url], "image/png"),
    )

    def fake_transcribe(image_bytes: bytes, content_type: str) -> str:
        calls.append(image_bytes)
        return f"Transcript {len(calls)} with enough characters to keep."

    monkeypatch.setattr(image_transcribe, "_transcribe_bytes", fake_transcribe)

    result = image_transcribe.transcribe_content_images(list(payloads))

    assert [url for url, _text in result] == [
        "https://example.com/tall1.png",
        "https://example.com/tall2.png",
    ]
    assert calls == [tall1, tall2]


def test_byte_cap_skips_oversize_download(monkeypatch):
    class FakeResponse:
        status_code = 200
        headers = {"content-type": "image/png"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def iter_bytes(self):
            yield b"x" * 11

    def fake_stream(method: str, url: str, **kwargs):
        return FakeResponse()

    monkeypatch.setenv("TRAWL_IMAGE_TRANSCRIBE_MAX_BYTES", "10")
    monkeypatch.setattr(image_transcribe.httpx, "stream", fake_stream)
    monkeypatch.setattr(
        image_transcribe,
        "_transcribe_bytes",
        lambda _image_bytes, _content_type: (_ for _ in ()).throw(AssertionError("called")),
    )

    assert image_transcribe.transcribe_content_images(["https://example.com/large.png"]) == []


def test_no_text_and_short_transcripts_are_dropped(monkeypatch):
    tall1 = _png(200, 1600, "white")
    tall2 = _png(200, 1600, "blue")
    tall3 = _png(200, 1600, "green")
    payloads = {
        "https://example.com/one.png": tall1,
        "https://example.com/two.png": tall2,
        "https://example.com/three.png": tall3,
    }
    outputs = iter(
        [
            "NO_TEXT",
            "too short",
            "This transcript is long enough to be retained.",
        ]
    )

    monkeypatch.setenv("TRAWL_IMAGE_TRANSCRIBE_CACHE_TTL", "0")
    monkeypatch.setattr(
        image_transcribe,
        "_download_image",
        lambda url: (payloads[url], "image/png"),
    )
    monkeypatch.setattr(
        image_transcribe,
        "_transcribe_bytes",
        lambda _image_bytes, _content_type: next(outputs),
    )

    assert image_transcribe.transcribe_content_images(list(payloads)) == [
        ("https://example.com/three.png", "This transcript is long enough to be retained.")
    ]


def test_transcript_cache_reuses_same_bytes_and_ttl_zero_disables(monkeypatch, tmp_path):
    tall = _png(200, 1600, "white")
    calls = 0

    monkeypatch.setenv("TRAWL_IMAGE_TRANSCRIBE_CACHE_PATH", str(tmp_path))
    monkeypatch.setenv("TRAWL_IMAGE_TRANSCRIBE_CACHE_TTL", "604800")
    monkeypatch.setattr(
        image_transcribe,
        "_download_image",
        lambda _url: (tall, "image/png"),
    )

    def fake_transcribe(_image_bytes: bytes, _content_type: str) -> str:
        nonlocal calls
        calls += 1
        return "Cached transcript with enough characters."

    monkeypatch.setattr(image_transcribe, "_transcribe_bytes", fake_transcribe)

    assert image_transcribe.transcribe_content_images(["https://example.com/one.png"])
    assert image_transcribe.transcribe_content_images(["https://example.com/two.png"])
    assert calls == 1

    calls = 0
    monkeypatch.setenv("TRAWL_IMAGE_TRANSCRIBE_CACHE_TTL", "0")
    assert image_transcribe.transcribe_content_images(["https://example.com/one.png"])
    assert image_transcribe.transcribe_content_images(["https://example.com/two.png"])
    assert calls == 2


def test_is_enabled_defaults_off_and_override_wins(monkeypatch):
    monkeypatch.delenv("TRAWL_IMAGE_TRANSCRIBE", raising=False)
    assert image_transcribe.is_enabled(None) is False
    assert image_transcribe.is_enabled(True) is True

    monkeypatch.setenv("TRAWL_IMAGE_TRANSCRIBE", "1")
    assert image_transcribe.is_enabled(None) is True
    assert image_transcribe.is_enabled(False) is False


def test_scan_and_transcribe_images_appends_sections_when_dominant_and_enabled(monkeypatch):
    urls = [
        "https://example.com/assets/banner-one.png?cache=1",
        "https://example.com/assets/banner-two.jpg",
    ]

    def fake_scan(html: str, markdown_chars: int, base_url: str):
        assert markdown_chars == len("base markdown")
        return True, urls

    monkeypatch.setattr(pipeline.images, "scan_content_images", fake_scan)
    monkeypatch.setattr(pipeline.image_transcribe, "is_enabled", lambda override: override is True)
    monkeypatch.setattr(
        pipeline.image_transcribe,
        "transcribe_content_images",
        lambda seen: [
            (seen[0], "First transcript with enough characters."),
            (seen[1], "Second transcript with enough characters."),
        ],
    )

    md, dominant, content_images, n_transcribed = pipeline._scan_and_transcribe_images(
        "<html></html>",
        "base markdown",
        "https://example.com/page",
        True,
    )

    assert dominant is True
    assert content_images == urls
    assert n_transcribed == 2
    assert "## Image text (banner-one.png)" in md
    assert "## Image text (banner-two.jpg)" in md


def test_scan_and_transcribe_images_skips_non_dominant_without_calling_transcriber(monkeypatch):
    monkeypatch.setattr(
        pipeline.images,
        "scan_content_images",
        lambda _html, _markdown_chars, _base_url: (False, []),
    )
    monkeypatch.setattr(pipeline.image_transcribe, "is_enabled", lambda _override: True)
    monkeypatch.setattr(
        pipeline.image_transcribe,
        "transcribe_content_images",
        lambda _urls: (_ for _ in ()).throw(AssertionError("called")),
    )

    assert pipeline._scan_and_transcribe_images(
        "<html></html>", "markdown", "https://e.test", True
    ) == (
        "markdown",
        False,
        [],
        0,
    )


def test_scan_and_transcribe_images_never_raises_when_transcription_explodes(monkeypatch):
    urls = ["https://example.com/banner.png"]
    monkeypatch.setattr(
        pipeline.images,
        "scan_content_images",
        lambda _html, _markdown_chars, _base_url: (True, urls),
    )
    monkeypatch.setattr(pipeline.image_transcribe, "is_enabled", lambda _override: True)
    monkeypatch.setattr(
        pipeline.image_transcribe,
        "transcribe_content_images",
        lambda _urls: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    assert pipeline._scan_and_transcribe_images(
        "<html></html>", "markdown", "https://e.test", True
    ) == (
        "markdown",
        True,
        urls,
        0,
    )


def test_pipeline_result_to_dict_includes_images_transcribed():
    r = pipeline.PipelineResult(
        url="https://example.com",
        query="q",
        fetcher_used="x",
        fetch_ms=0,
        chunk_ms=0,
        retrieval_ms=0,
        total_ms=0,
        page_chars=0,
        n_chunks_total=0,
        structured_path=False,
        hyde_used=False,
        hyde_text="",
        chunks=[],
        images_transcribed=2,
    )

    assert pipeline.to_dict(r)["images_transcribed"] == 2
