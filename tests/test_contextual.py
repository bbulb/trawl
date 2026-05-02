"""Unit tests for deterministic contextual retrieval prefixes."""

from __future__ import annotations

from trawl import contextual
from trawl.chunking import Chunk


def _chunk(
    text: str,
    *,
    heading_path: list[str] | None = None,
    index: int = 0,
    record_group_id: int | None = None,
    record_index: int | None = None,
) -> Chunk:
    return Chunk(
        text=text,
        heading_path=heading_path or [],
        char_count=len(text),
        chunk_index=index,
        embed_text=text,
        record_group_id=record_group_id,
        record_index=record_index,
    )


def test_is_enabled_reads_env(monkeypatch):
    monkeypatch.delenv("TRAWL_CONTEXTUAL_RETRIEVAL", raising=False)
    assert contextual.is_enabled() is False

    monkeypatch.setenv("TRAWL_CONTEXTUAL_RETRIEVAL", "1")
    assert contextual.is_enabled() is True

    monkeypatch.setenv("TRAWL_CONTEXTUAL_RETRIEVAL", "true")
    assert contextual.is_enabled() is True

    monkeypatch.setenv("TRAWL_CONTEXTUAL_RETRIEVAL", "0")
    assert contextual.is_enabled() is False


def test_max_prefix_chars_defaults_and_sanitizes(monkeypatch):
    monkeypatch.delenv("TRAWL_CONTEXT_PREFIX_MAX_CHARS", raising=False)
    assert contextual.max_prefix_chars() == 320

    monkeypatch.setenv("TRAWL_CONTEXT_PREFIX_MAX_CHARS", "12")
    assert contextual.max_prefix_chars() == 12

    monkeypatch.setenv("TRAWL_CONTEXT_PREFIX_MAX_CHARS", "bad")
    assert contextual.max_prefix_chars() == 320

    monkeypatch.setenv("TRAWL_CONTEXT_PREFIX_MAX_CHARS", "-10")
    assert contextual.max_prefix_chars() == 0


def test_contextual_text_includes_available_metadata(monkeypatch):
    monkeypatch.setenv("TRAWL_CONTEXT_PREFIX_MAX_CHARS", "500")
    chunk = _chunk(
        "body text about dependency injection",
        heading_path=["Guide", "Testing"],
        index=3,
        record_group_id=1,
        record_index=2,
    )

    result = contextual.build_contextual_text(
        chunk,
        page_title="FastAPI Docs",
        previous_heading="Guide > Basics",
        next_heading="Guide > Advanced",
        total_chunks=9,
    )

    assert result.text.startswith("Title: FastAPI Docs\n")
    assert "Section: Guide > Testing\n" in result.text
    assert "Position: chunk 4 of 9\n" in result.text
    assert "Record: item 3 in repeated group 1\n" in result.text
    assert "Nearby sections: Guide > Basics | Guide > Advanced\n" in result.text
    assert result.text.endswith("\n\nbody text about dependency injection")
    assert result.prefix_chars > 0


def test_contextual_text_omits_missing_metadata(monkeypatch):
    monkeypatch.setenv("TRAWL_CONTEXT_PREFIX_MAX_CHARS", "500")
    chunk = _chunk("plain body", index=0)

    result = contextual.build_contextual_text(
        chunk,
        page_title="",
        previous_heading="",
        next_heading="",
        total_chunks=1,
    )

    assert result.text == "Position: chunk 1 of 1\n\nplain body"
    assert "Title:" not in result.text
    assert "Section:" not in result.text
    assert "Record:" not in result.text
    assert "Nearby sections:" not in result.text


def test_prefix_cap_preserves_body(monkeypatch):
    monkeypatch.setenv("TRAWL_CONTEXT_PREFIX_MAX_CHARS", "20")
    chunk = _chunk("important body", heading_path=["Very Long Heading Name"], index=0)

    result = contextual.build_contextual_text(
        chunk,
        page_title="Long Title",
        previous_heading="Previous Section",
        next_heading="Next Section",
        total_chunks=2,
    )

    prefix, body = result.text.split("\n\n", 1)
    assert len(prefix) <= 20
    assert body == "important body"
    assert result.prefix_chars == len(prefix)


def test_zero_prefix_cap_returns_body_only(monkeypatch):
    monkeypatch.setenv("TRAWL_CONTEXT_PREFIX_MAX_CHARS", "0")
    chunk = _chunk("body only", heading_path=["Section"], index=0)

    result = contextual.build_contextual_text(
        chunk,
        page_title="Title",
        previous_heading="Prev",
        next_heading="Next",
        total_chunks=3,
    )

    assert result.text == "body only"
    assert result.prefix_chars == 0


def test_build_contextual_texts_adds_nearby_heading_stats(monkeypatch):
    monkeypatch.setenv("TRAWL_CONTEXT_PREFIX_MAX_CHARS", "500")
    chunks = [
        _chunk("alpha", heading_path=["A"], index=0),
        _chunk("beta", heading_path=["B"], index=1),
        _chunk("gamma", heading_path=["C"], index=2),
    ]

    batch = contextual.build_contextual_texts(chunks, page_title="Page")

    assert len(batch.texts) == 3
    assert "Nearby sections: B" in batch.texts[0]
    assert "Nearby sections: A | C" in batch.texts[1]
    assert "Nearby sections: B" in batch.texts[2]
    assert batch.prefix_chars_total > 0
    assert batch.prefix_chars_avg == batch.prefix_chars_total / 3
