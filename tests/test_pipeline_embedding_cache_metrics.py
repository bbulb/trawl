from __future__ import annotations

from trawl import pipeline, retrieval
from trawl.chunking import Chunk
from trawl.fetchers.playwright import FetchResult


def test_pipeline_serializes_embedding_cache_metrics(monkeypatch):
    monkeypatch.setenv("TRAWL_FETCH_CACHE_TTL", "0")

    fetched = FetchResult(
        url="https://example.test/cache",
        html="<html><title>Cache</title><body>alpha body repeated content</body></html>",
        markdown="alpha body repeated content for embedding cache metrics",
        raw_html="",
        fetcher="test",
        elapsed_ms=1,
    )

    def fake_fetch_html(_url, query=None):
        return (
            fetched,
            pipeline.extraction.ExtractedContent(
                markdown=fetched.markdown,
                extractor="test",
            ),
            "test",
        )

    def fake_retrieve(*_args, **_kwargs):
        chunk = Chunk(
            text="alpha body repeated content for embedding cache metrics",
            embed_text="alpha body repeated content for embedding cache metrics",
            char_count=55,
            chunk_index=0,
        )
        return retrieval.RetrievalResult(
            scored=[retrieval.ScoredChunk(chunk=chunk, score=1.0)],
            elapsed_ms=2,
            embed_calls=1,
            n_chunks_embedded=1,
            embed_cache_hits=3,
            embed_cache_misses=4,
        )

    monkeypatch.setattr(pipeline, "_fetch_html", fake_fetch_html)
    monkeypatch.setattr(retrieval, "retrieve", fake_retrieve)

    result = pipeline.fetch_relevant(
        "https://example.test/cache",
        "alpha cache metrics",
        use_rerank=False,
    )

    payload = pipeline.to_dict(result)
    assert payload["embed_cache_hits"] == 3
    assert payload["embed_cache_misses"] == 4
