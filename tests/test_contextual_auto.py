"""Tests for contextual retrieval mode and auto policy."""

from __future__ import annotations

from trawl import contextual
from trawl.chunking import Chunk


def _chunk(text: str, *, index: int = 0, record_group_id: int | None = None) -> Chunk:
    return Chunk(
        text=text,
        embed_text=text,
        char_count=len(text),
        chunk_index=index,
        record_group_id=record_group_id,
        record_index=0 if record_group_id is not None else None,
    )


def test_mode_defaults_to_off(monkeypatch):
    monkeypatch.delenv("TRAWL_CONTEXTUAL_RETRIEVAL", raising=False)
    assert contextual.mode() == "off"


def test_mode_accepts_on_values(monkeypatch):
    for value in ("1", "true", "yes", "on"):
        monkeypatch.setenv("TRAWL_CONTEXTUAL_RETRIEVAL", value)
        assert contextual.mode() == "on"


def test_mode_accepts_auto(monkeypatch):
    monkeypatch.setenv("TRAWL_CONTEXTUAL_RETRIEVAL", "auto")
    assert contextual.mode() == "auto"


def test_mode_treats_unknown_as_off(monkeypatch):
    monkeypatch.setenv("TRAWL_CONTEXTUAL_RETRIEVAL", "surprise")
    assert contextual.mode() == "off"


def test_is_enabled_stays_backward_compatible(monkeypatch):
    monkeypatch.setenv("TRAWL_CONTEXTUAL_RETRIEVAL", "1")
    assert contextual.is_enabled() is True

    monkeypatch.setenv("TRAWL_CONTEXTUAL_RETRIEVAL", "auto")
    assert contextual.is_enabled() is False


def test_should_use_contextual_on_and_off(monkeypatch):
    chunks = [_chunk("alpha")]

    monkeypatch.setenv("TRAWL_CONTEXTUAL_RETRIEVAL", "1")
    assert contextual.should_use_contextual(query="alpha", chunks=chunks, page_title="") is True

    monkeypatch.setenv("TRAWL_CONTEXTUAL_RETRIEVAL", "0")
    assert contextual.should_use_contextual(query="alpha", chunks=chunks, page_title="") is False


def test_auto_disables_tiny_pages(monkeypatch):
    monkeypatch.setenv("TRAWL_CONTEXTUAL_RETRIEVAL", "auto")
    chunks = [_chunk("alpha"), _chunk("beta", index=1)]

    assert contextual.should_use_contextual(query="simple question", chunks=chunks, page_title="") is False


def test_auto_enables_identifier_queries(monkeypatch):
    monkeypatch.setenv("TRAWL_CONTEXTUAL_RETRIEVAL", "auto")
    chunks = [_chunk("x", index=i) for i in range(3)]

    assert (
        contextual.should_use_contextual(
            query="how does asyncio.gather() handle exceptions",
            chunks=chunks,
            page_title="Python docs",
        )
        is True
    )


def test_auto_enables_large_pages(monkeypatch):
    monkeypatch.setenv("TRAWL_CONTEXTUAL_RETRIEVAL", "auto")
    chunks = [_chunk("x", index=i) for i in range(16)]

    assert contextual.should_use_contextual(query="concept query", chunks=chunks, page_title="Docs") is True


def test_auto_enables_repeated_records(monkeypatch):
    monkeypatch.setenv("TRAWL_CONTEXTUAL_RETRIEVAL", "auto")
    chunks = [_chunk("x", index=0), _chunk("y", index=1, record_group_id=2)]

    assert contextual.should_use_contextual(query="jobs", chunks=chunks, page_title="Listings") is True


def test_auto_disabled_when_prefix_cap_zero(monkeypatch):
    monkeypatch.setenv("TRAWL_CONTEXTUAL_RETRIEVAL", "auto")
    monkeypatch.setenv("TRAWL_CONTEXT_PREFIX_MAX_CHARS", "0")
    chunks = [_chunk("x", index=i) for i in range(16)]

    assert contextual.should_use_contextual(query="asyncio.gather()", chunks=chunks, page_title="Docs") is False
