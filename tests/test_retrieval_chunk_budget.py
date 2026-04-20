"""Chunk budget prefilter tests with monkeypatched embedding calls.

Covers the BM25 prefilter introduced for longform retrieval cost —
see docs/superpowers/specs/2026-04-20-longform-retrieval-cost-design.md.
"""

from __future__ import annotations

import pytest

from trawl import retrieval
from trawl.chunking import Chunk


def _chunk(text: str, heading: str = "") -> Chunk:
    path = [heading] if heading else []
    return Chunk(
        text=text,
        heading_path=path,
        char_count=len(text),
        embed_text=text,
    )


def _recording_embed_factory(query_vecs, doc_vec_for_index):
    """Stub for `retrieval._embed_batch` that records which chunk texts it sees.

    `query_vecs` is the list of vectors returned for the first (query)
    call. Subsequent calls map each input text back to a vector via
    `doc_vec_for_index(text_position_in_original_corpus)` — but since
    the prefilter changes which texts are passed, we simply look up by
    the text itself to stay order-independent.
    """

    state = {"calls": 0, "doc_texts_seen": []}

    def _stub(_client, _base_url, _model, texts):
        state["calls"] += 1
        if state["calls"] == 1:
            return query_vecs
        state["doc_texts_seen"].extend(texts)
        return [doc_vec_for_index(t) for t in texts]

    return _stub, state


def test_budget_zero_is_no_op(monkeypatch):
    chunks = [_chunk(f"doc {i} alpha") for i in range(5)]
    q = [[1.0, 0.0]]
    stub, state = _recording_embed_factory(q, lambda t: [1.0, 0.0])
    monkeypatch.setattr(retrieval, "_embed_batch", stub)

    result = retrieval.retrieve("alpha", chunks, k=3, chunk_budget=0)
    assert result.n_chunks_embedded == 5
    assert len(state["doc_texts_seen"]) == 5


def test_budget_gte_pool_is_no_op(monkeypatch):
    chunks = [_chunk(f"doc {i} alpha") for i in range(5)]
    q = [[1.0, 0.0]]
    stub, state = _recording_embed_factory(q, lambda t: [1.0, 0.0])
    monkeypatch.setattr(retrieval, "_embed_batch", stub)

    result = retrieval.retrieve("alpha", chunks, k=3, chunk_budget=10)
    assert result.n_chunks_embedded == 5
    assert len(state["doc_texts_seen"]) == 5


def test_budget_below_pool_prefilters(monkeypatch):
    """Budget cap drops the pool to budget size before embedding."""
    # Three docs that match "asyncio" lexically, three that do not.
    chunks = [
        _chunk("asyncio.gather is the right primitive"),
        _chunk("asyncio.run is the entry point"),
        _chunk("asyncio.wait_for applies a timeout"),
        _chunk("unrelated prose about cats"),
        _chunk("unrelated prose about dogs"),
        _chunk("unrelated prose about birds"),
    ]
    q = [[1.0, 0.0]]
    stub, state = _recording_embed_factory(q, lambda t: [1.0, 0.0])
    monkeypatch.setattr(retrieval, "_embed_batch", stub)

    result = retrieval.retrieve("asyncio", chunks, k=3, chunk_budget=3)
    assert result.n_chunks_embedded == 3
    # Only the three lexical matches should reach the embedding stub.
    assert len(state["doc_texts_seen"]) == 3
    for text in state["doc_texts_seen"]:
        assert "asyncio" in text


def test_budget_prefilter_preserves_stable_index_order(monkeypatch):
    """Survivors are passed to the embedding stub in original chunk order.

    The prefilter re-orders BM25 output, then sorts surviving indices
    ascending so downstream embedding / fusion behaves the same way
    whether a chunk was at rank 2 or rank 20 of BM25.
    """
    chunks = [
        _chunk("first alpha chunk"),
        _chunk("unrelated prose"),
        _chunk("second alpha chunk"),
        _chunk("more unrelated"),
        _chunk("third alpha chunk"),
    ]
    q = [[1.0, 0.0]]
    stub, state = _recording_embed_factory(q, lambda t: [1.0, 0.0])
    monkeypatch.setattr(retrieval, "_embed_batch", stub)

    retrieval.retrieve("alpha", chunks, k=3, chunk_budget=3)
    # All three "alpha" chunks should reach the embedding stub in the
    # same relative order they appeared in the input.
    assert state["doc_texts_seen"] == [
        "first alpha chunk",
        "second alpha chunk",
        "third alpha chunk",
    ]


def test_budget_with_hybrid_shares_filtered_pool(monkeypatch):
    """Hybrid fusion runs on the prefiltered pool, not the full chunk set."""
    chunks = [
        _chunk("alpha one"),
        _chunk("alpha two"),
        _chunk("alpha three"),
        _chunk("unrelated one"),
        _chunk("unrelated two"),
    ]
    q = [[1.0, 0.0]]
    stub, state = _recording_embed_factory(q, lambda t: [1.0, 0.0])
    monkeypatch.setattr(retrieval, "_embed_batch", stub)

    result = retrieval.retrieve("alpha", chunks, k=3, hybrid=True, chunk_budget=3)
    assert result.n_chunks_embedded == 3
    # Only 3 survivors embedded, even with hybrid=True.
    assert len(state["doc_texts_seen"]) == 3


def test_budget_empty_chunks_zero_embedded():
    result = retrieval.retrieve("q", [], k=3, chunk_budget=10)
    assert result.n_chunks_embedded == 0
    assert result.scored == []


def test_budget_empty_query_is_no_op(monkeypatch):
    """Empty query → BM25 falls back to original order; budget still caps."""
    chunks = [_chunk(f"doc {i}") for i in range(5)]
    q = [[0.0, 0.0]]
    stub, state = _recording_embed_factory(q, lambda t: [1.0, 0.0])
    monkeypatch.setattr(retrieval, "_embed_batch", stub)

    # BM25 returns the original order fallback on an empty tokenization;
    # budget=3 still caps — top-3 by that fallback = first 3 chunks.
    result = retrieval.retrieve("   ", chunks, k=3, chunk_budget=3)
    assert result.n_chunks_embedded == 3
    assert state["doc_texts_seen"] == ["doc 0", "doc 1", "doc 2"]


def test_retrieval_result_reports_n_chunks_embedded_on_http_error(monkeypatch):
    """Error path still carries the prefiltered count for telemetry."""
    import httpx

    chunks = [_chunk(f"alpha {i}") for i in range(5)]

    def _raise(*_args, **_kwargs):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(retrieval, "_embed_batch", _raise)
    result = retrieval.retrieve("alpha", chunks, k=3, chunk_budget=2)
    assert result.error is not None
    assert result.n_chunks_embedded == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
