"""Retrieval tests for document embedding cache integration."""

from __future__ import annotations

from trawl import retrieval
from trawl.chunking import Chunk


def _chunk(text: str) -> Chunk:
    return Chunk(text=text, embed_text=text, char_count=len(text))


def test_retrieve_reuses_cached_document_embedding(monkeypatch, tmp_path):
    monkeypatch.setenv("TRAWL_EMBED_CACHE_TTL", "60")
    monkeypatch.setenv("TRAWL_EMBED_CACHE_PATH", str(tmp_path))
    monkeypatch.setenv("TRAWL_CONTEXTUAL_RETRIEVAL", "0")
    chunks = [_chunk("alpha body")]
    calls: list[list[str]] = []

    def _fake_embed(_client, _base_url, _model, texts):
        calls.append(list(texts))
        if texts == ["alpha query"]:
            return [[1.0, 0.0]]
        return [[1.0, 0.0]]

    monkeypatch.setattr(retrieval, "_embed_batch", _fake_embed)

    first = retrieval.retrieve("alpha query", chunks, k=1)
    second = retrieval.retrieve("alpha query", chunks, k=1)

    assert first.error is None
    assert second.error is None
    assert calls == [["alpha query"], ["alpha body"], ["alpha query"]]
    assert second.scored[0].chunk is chunks[0]


def test_contextual_mode_invalidates_document_embedding_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("TRAWL_EMBED_CACHE_TTL", "60")
    monkeypatch.setenv("TRAWL_EMBED_CACHE_PATH", str(tmp_path))
    chunks = [_chunk("alpha body")]
    calls: list[list[str]] = []

    def _fake_embed(_client, _base_url, _model, texts):
        calls.append(list(texts))
        if texts == ["alpha query"]:
            return [[1.0, 0.0]]
        return [[1.0, 0.0]]

    monkeypatch.setattr(retrieval, "_embed_batch", _fake_embed)

    monkeypatch.setenv("TRAWL_CONTEXTUAL_RETRIEVAL", "0")
    retrieval.retrieve("alpha query", chunks, k=1)

    monkeypatch.setenv("TRAWL_CONTEXTUAL_RETRIEVAL", "1")
    retrieval.retrieve("alpha query", chunks, k=1, context_texts=["Title: T\n\nalpha body"])

    assert calls == [
        ["alpha query"],
        ["alpha body"],
        ["alpha query"],
        ["Title: T\n\nalpha body"],
    ]


def test_embedding_cache_disabled_keeps_current_embedding_calls(monkeypatch, tmp_path):
    monkeypatch.setenv("TRAWL_EMBED_CACHE_TTL", "0")
    monkeypatch.setenv("TRAWL_EMBED_CACHE_PATH", str(tmp_path))
    chunks = [_chunk("alpha body")]
    calls: list[list[str]] = []

    def _fake_embed(_client, _base_url, _model, texts):
        calls.append(list(texts))
        if texts == ["alpha query"]:
            return [[1.0, 0.0]]
        return [[1.0, 0.0]]

    monkeypatch.setattr(retrieval, "_embed_batch", _fake_embed)

    retrieval.retrieve("alpha query", chunks, k=1)
    retrieval.retrieve("alpha query", chunks, k=1)

    assert calls == [["alpha query"], ["alpha body"], ["alpha query"], ["alpha body"]]
