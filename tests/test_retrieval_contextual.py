"""Tests for retrieval context_texts plumbing."""

from __future__ import annotations

from trawl import retrieval
from trawl.chunking import Chunk


def _chunk(text: str, heading: str = "") -> Chunk:
    return Chunk(
        text=text,
        heading_path=[heading] if heading else [],
        char_count=len(text),
        embed_text=text,
    )


def test_retrieve_uses_context_texts_for_embedding(monkeypatch):
    chunks = [_chunk("body alpha", "A"), _chunk("body beta", "B")]
    seen_doc_batches: list[list[str]] = []
    query_vec = [[1.0, 0.0]]
    doc_vecs = [[1.0, 0.0], [0.0, 1.0]]
    calls = {"n": 0}

    def _fake_embed(_client, _base_url, _model, texts):
        calls["n"] += 1
        if calls["n"] == 1:
            return query_vec
        seen_doc_batches.append(list(texts))
        return doc_vecs[: len(texts)]

    monkeypatch.setattr(retrieval, "_embed_batch", _fake_embed)

    result = retrieval.retrieve(
        "alpha",
        chunks,
        k=2,
        context_texts=["context one alpha", "context two beta"],
    )

    assert result.error is None
    assert seen_doc_batches == [["context one alpha", "context two beta"]]
    assert result.scored[0].chunk is chunks[0]


def test_retrieve_uses_context_texts_for_bm25_prefilter(monkeypatch):
    chunks = [_chunk("body only one"), _chunk("body only two")]
    seen_doc_batches: list[list[str]] = []
    query_vec = [[1.0, 0.0]]
    doc_vecs = [[1.0, 0.0]]
    calls = {"n": 0}

    def _fake_embed(_client, _base_url, _model, texts):
        calls["n"] += 1
        if calls["n"] == 1:
            return query_vec
        seen_doc_batches.append(list(texts))
        return doc_vecs[: len(texts)]

    monkeypatch.setattr(retrieval, "_embed_batch", _fake_embed)

    result = retrieval.retrieve(
        "needle",
        chunks,
        k=1,
        chunk_budget=1,
        context_texts=["needle appears here", "no match here"],
    )

    assert result.error is None
    assert seen_doc_batches == [["needle appears here"]]
    assert result.scored[0].chunk is chunks[0]
    assert result.n_chunks_embedded == 1


def test_retrieve_rejects_misaligned_context_texts():
    chunks = [_chunk("one"), _chunk("two")]

    result = retrieval.retrieve("query", chunks, k=2, context_texts=["only one"])

    assert result.scored == []
    assert result.embed_calls == 0
    assert result.n_chunks_embedded == 0
    assert result.error == "context_texts length 1 does not match chunks length 2"


def test_retrieve_rejects_context_texts_for_empty_chunks():
    result = retrieval.retrieve("query", [], k=2, context_texts=["x"])

    assert result.scored == []
    assert result.embed_calls == 0
    assert result.n_chunks_embedded == 0
    assert result.error == "context_texts length 1 does not match chunks length 0"
