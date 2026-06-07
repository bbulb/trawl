"""Hybrid retrieval integration tests with monkeypatched embedding calls."""

from __future__ import annotations

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


def _fake_embed_factory(query_vecs, doc_vecs):
    """Build a stub for `retrieval._embed_batch` that returns canned vectors.

    The first invocation returns `query_vecs`; subsequent invocations pop
    slices off `doc_vecs` in batch order. Call counts align with the
    production flow (1 query embed call + N/EMBEDDING_BATCH chunk calls).
    """

    calls = {"n": 0}

    def _stub(_client, _base_url, _model, texts):
        calls["n"] += 1
        if calls["n"] == 1:
            return [query_vecs[i] for i, _ in enumerate(texts)]
        start = (calls["n"] - 2) * retrieval.EMBEDDING_BATCH
        return doc_vecs[start : start + len(texts)]

    return _stub


def test_hybrid_off_matches_baseline(monkeypatch):
    """hybrid=False must preserve bit-for-bit behaviour vs. the default call."""
    chunks = [_chunk("alpha beta"), _chunk("gamma delta"), _chunk("epsilon zeta")]
    q = [[1.0, 0.0]]
    docs = [[0.9, 0.1], [0.0, 1.0], [0.5, 0.5]]
    monkeypatch.setattr(retrieval, "_embed_batch", _fake_embed_factory(q, docs))

    result_default = retrieval.retrieve("query", chunks, k=3)
    monkeypatch.setattr(retrieval, "_embed_batch", _fake_embed_factory(q, docs))
    result_explicit_off = retrieval.retrieve("query", chunks, k=3, hybrid=False)

    order_default = [s.chunk.text for s in result_default.scored]
    order_off = [s.chunk.text for s in result_explicit_off.scored]
    assert order_default == order_off
    assert [round(s.score, 6) for s in result_default.scored] == [
        round(s.score, 6) for s in result_explicit_off.scored
    ]


def test_hybrid_on_returns_top_k_shape(monkeypatch):
    chunks = [
        _chunk("alpha beta gamma"),
        _chunk("delta epsilon zeta"),
        _chunk("eta theta iota"),
        _chunk("kappa lambda mu"),
    ]
    q = [[1.0, 0.0]]
    docs = [[0.9, 0.1], [0.1, 0.9], [0.5, 0.5], [0.0, 1.0]]
    monkeypatch.setattr(retrieval, "_embed_batch", _fake_embed_factory(q, docs))

    result = retrieval.retrieve("alpha", chunks, k=2, hybrid=True)
    assert len(result.scored) == 2
    assert len({id(s.chunk) for s in result.scored}) == 2


def test_hybrid_preserves_score_field_as_cosine(monkeypatch):
    """RRF rank order can differ from dense, but score must still be cosine."""
    chunks = [_chunk("lexical match FastAPI dependency"), _chunk("semantic only")]
    q = [[1.0, 0.0]]
    docs = [[0.6, 0.8], [1.0, 0.0]]
    monkeypatch.setattr(retrieval, "_embed_batch", _fake_embed_factory(q, docs))

    result = retrieval.retrieve("FastAPI dependency", chunks, k=2, hybrid=True)
    # Dense cosine: doc 0 = 0.6, doc 1 = 1.0 → doc 1 wins on dense.
    # BM25 on tokens: query=["fastapi","dependency"], doc 0 has both,
    # doc 1 has neither → doc 0 wins on sparse.
    # RRF fusion of [1,0] and [0,1] is a tie; insertion order keeps doc 1 first.
    scores_by_text = {s.chunk.text: s.score for s in result.scored}
    assert scores_by_text["semantic only"] == 1.0
    assert round(scores_by_text["lexical match FastAPI dependency"], 2) == 0.6


def test_hybrid_on_lexical_match_rises(monkeypatch):
    """A lexical-match chunk should rise in the hybrid ordering, even if RRF
    k=60 is too conservative to flip top-1 on a 3-doc toy fixture."""
    chunks = [
        _chunk("semantic neighbour passage — talks about request handling"),
        _chunk("vaguely related prose about nothing in particular here"),
        _chunk("async def gather tasks concurrently via asyncio.gather"),
    ]
    q = [[1.0, 0.0]]
    # Dense ranks [0, 1, 2]: doc 2 is last. Hybrid should promote it.
    docs = [[0.95, 0.05], [0.85, 0.15], [0.05, 0.99]]

    def run(hybrid):
        monkeypatch.setattr(retrieval, "_embed_batch", _fake_embed_factory(q, docs))
        return retrieval.retrieve("asyncio.gather", chunks, k=3, hybrid=hybrid)

    dense_order = [s.chunk.text for s in run(False).scored]
    hybrid_order = [s.chunk.text for s in run(True).scored]
    # Dense puts the lexical-match chunk last.
    assert "asyncio.gather" in dense_order[-1]
    # Query-aware hybrid promotes it out of the last slot.
    assert any("asyncio.gather" in text for text in hybrid_order[:2])


def test_hybrid_ordering_differs_from_dense(monkeypatch):
    """Hybrid must produce a different ordering than dense-only when BM25
    gives a ranking the dense side didn't already agree with."""
    chunks = [
        _chunk("generic dense neighbour one"),
        _chunk("generic dense neighbour two"),
        _chunk("the exact lexical match asyncio.gather here"),
        _chunk("generic dense neighbour three"),
    ]
    q = [[1.0, 0.0]]
    docs = [[0.90, 0.10], [0.85, 0.15], [0.50, 0.50], [0.80, 0.20]]

    def run(hybrid):
        monkeypatch.setattr(retrieval, "_embed_batch", _fake_embed_factory(q, docs))
        return retrieval.retrieve("asyncio.gather", chunks, k=4, hybrid=hybrid)

    dense_order = [s.chunk.text for s in run(False).scored]
    hybrid_order = [s.chunk.text for s in run(True).scored]
    assert dense_order != hybrid_order


def test_identifier_query_uses_query_aware_weights(monkeypatch):
    chunks = [
        _chunk("generic dense neighbour one"),
        _chunk("generic dense neighbour two"),
        _chunk("the exact lexical match asyncio.gather here"),
    ]
    q = [[1.0, 0.0]]
    docs = [[0.90, 0.10], [0.85, 0.15], [0.50, 0.50]]
    monkeypatch.setattr(retrieval, "_embed_batch", _fake_embed_factory(q, docs))

    result = retrieval.retrieve("asyncio.gather", chunks, k=3, hybrid=True)

    assert result.query_type == "identifier"
    assert result.retrieval_mode == "hybrid"
    assert result.fusion_weights["bm25"] > result.fusion_weights["dense"]
    assert "asyncio.gather" in result.scored[0].chunk.text
    assert result.rank_diagnostics
    top_diag = result.rank_diagnostics[0]
    assert top_diag["chunk_index"] == result.scored[0].chunk.chunk_index
    assert "bm25" in top_diag["ranks"]
    assert "bm25" in top_diag["contributions"]


def test_concept_query_keeps_dense_weight_highest(monkeypatch):
    chunks = [
        _chunk("overview of dependency injection concepts"),
        _chunk("exact token dependency injection but less useful"),
    ]
    q = [[1.0, 0.0]]
    docs = [[1.0, 0.0], [0.5, 0.5]]
    monkeypatch.setattr(retrieval, "_embed_batch", _fake_embed_factory(q, docs))

    result = retrieval.retrieve(
        "how dependency injection improves testing", chunks, k=2, hybrid=True
    )

    assert result.query_type == "concept"
    assert result.fusion_weights["dense"] > result.fusion_weights["bm25"]


def test_bge_m3_sparse_rank_participates_when_configured(monkeypatch):
    chunks = [
        _chunk("dense neighbour"),
        _chunk("native sparse exact match"),
        _chunk("bm25 neighbour sparse"),
    ]
    q = [[1.0, 0.0]]
    docs = [[0.95, 0.05], [0.25, 0.75], [0.80, 0.20]]
    monkeypatch.setattr(retrieval, "_embed_batch", _fake_embed_factory(q, docs))
    monkeypatch.setenv("TRAWL_BGE_M3_SPARSE_URL", "http://sparse.example/rank")

    def _fake_sparse_rank(_query, _documents, *, endpoint, model):
        assert endpoint == "http://sparse.example/rank"
        assert model == retrieval.DEFAULT_EMBEDDING_MODEL
        return retrieval.SparseRankResult(ranking=[1, 2, 0])

    monkeypatch.setattr(retrieval, "_bge_m3_sparse_rank", _fake_sparse_rank)

    result = retrieval.retrieve("native sparse", chunks, k=3, hybrid=True)

    assert result.sparse_rank_error is None
    assert "bge_m3_sparse" in result.fusion_weights
    assert "native sparse" in result.scored[0].chunk.text
    assert "bge_m3_sparse" in result.rank_diagnostics[0]["ranks"]


def test_bge_m3_sparse_error_falls_back_to_available_rankers(monkeypatch):
    chunks = [_chunk("alpha beta"), _chunk("gamma delta")]
    q = [[1.0, 0.0]]
    docs = [[0.9, 0.1], [0.1, 0.9]]
    monkeypatch.setattr(retrieval, "_embed_batch", _fake_embed_factory(q, docs))
    monkeypatch.setenv("TRAWL_BGE_M3_SPARSE_URL", "http://sparse.example/rank")

    def _fake_sparse_rank(_query, _documents, *, endpoint, model):
        return retrieval.SparseRankResult(ranking=[], error="HTTPStatusError: 500")

    monkeypatch.setattr(retrieval, "_bge_m3_sparse_rank", _fake_sparse_rank)

    result = retrieval.retrieve("alpha", chunks, k=2, hybrid=True)

    assert result.scored
    assert result.sparse_rank_error == "HTTPStatusError: 500"
    assert "bge_m3_sparse" not in result.fusion_weights


def test_hybrid_on_empty_chunks():
    result = retrieval.retrieve("q", [], k=3, hybrid=True)
    assert result.scored == []
    assert result.embed_calls == 0


def test_hybrid_on_all_empty_text_falls_back_to_dense(monkeypatch):
    """All-empty chunk texts → bm25_rank returns original order, dense dominates."""
    chunks = [_chunk(""), _chunk(""), _chunk("")]
    q = [[1.0, 0.0]]
    docs = [[0.9, 0.1], [0.3, 0.7], [0.7, 0.3]]
    monkeypatch.setattr(retrieval, "_embed_batch", _fake_embed_factory(q, docs))

    result = retrieval.retrieve("query", chunks, k=3, hybrid=True)
    # Dense ranks docs [0 (high cosine), 2, 1]; sparse returns [0, 1, 2]
    # (original order fallback). Doc 0 is first in both so it wins the
    # fusion without ties.
    assert len(result.scored) == 3
    # All three chunks have empty text so we can't identify by text —
    # verify instead that the top score is the highest cosine of the set.
    assert result.scored[0].score == max(s.score for s in result.scored)
