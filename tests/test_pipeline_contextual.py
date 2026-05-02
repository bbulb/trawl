"""Pipeline tests for contextual retrieval wiring."""

from __future__ import annotations

from trawl import pipeline
from trawl.fetchers.playwright import FetchResult
from trawl.retrieval import RetrievalResult, ScoredChunk


def _disable_profiles(monkeypatch):
    import trawl.profiles as profiles_mod

    monkeypatch.setattr(profiles_mod, "track_visit", lambda url: None)
    monkeypatch.setattr(profiles_mod, "load_profile", lambda url: None)
    monkeypatch.setattr(profiles_mod, "get_visit_count", lambda url: 0)


def test_full_pipeline_passes_context_texts_when_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("TRAWL_CONTEXTUAL_RETRIEVAL", "1")
    monkeypatch.setenv("TRAWL_FETCH_CACHE_PATH", str(tmp_path))
    monkeypatch.setenv("TRAWL_FETCH_CACHE_TTL", "0")
    _disable_profiles(monkeypatch)
    seen: dict[str, object] = {}

    def _fake_fetch_html(url: str, query: str | None = None):
        html = (
            "<html><head><title>Context Page</title></head>"
            "<body><h1>Alpha</h1><p>alpha body text enough words</p>"
            "<h1>Beta</h1><p>beta body text enough words</p></body></html>"
        )
        fetched = FetchResult(
            url=url,
            html=html,
            markdown="# Alpha\n\nalpha body text enough words\n\n# Beta\n\nbeta body text enough words",
            raw_html=html,
            fetcher="playwright",
            elapsed_ms=10,
        )
        extracted = pipeline.extraction.ExtractedContent(
            markdown=fetched.markdown,
            extractor="test",
            source_selector="document",
            source_xpath="/",
        )
        return fetched, extracted, "playwright+trafilatura"

    def _fake_retrieve(query, chunks, *, k, context_texts=None, **_kwargs):
        seen["context_texts"] = context_texts
        scored = [ScoredChunk(chunk=chunks[0], score=1.0)]
        return RetrievalResult(scored=scored, elapsed_ms=1, embed_calls=0, n_chunks_embedded=1)

    monkeypatch.setattr(pipeline, "_fetch_html", _fake_fetch_html)
    monkeypatch.setattr(pipeline.retrieval, "retrieve", _fake_retrieve)
    monkeypatch.setattr(
        pipeline.reranking,
        "rerank",
        lambda _q, scored, *, k, page_title="": (scored[:k], False),
    )

    result = pipeline.fetch_relevant("https://example.com/context", "alpha")

    assert result.error is None
    context_texts = seen["context_texts"]
    assert isinstance(context_texts, list)
    assert context_texts
    assert context_texts[0].startswith("Title: Context Page\n")
    assert "Section: Alpha" in context_texts[0]
    assert result.contextual_retrieval_used is True
    assert result.context_prefix_chars_total > 0
    assert result.context_prefix_chars_avg > 0


def test_full_pipeline_omits_context_texts_when_disabled(monkeypatch, tmp_path):
    monkeypatch.delenv("TRAWL_CONTEXTUAL_RETRIEVAL", raising=False)
    monkeypatch.setenv("TRAWL_FETCH_CACHE_PATH", str(tmp_path))
    monkeypatch.setenv("TRAWL_FETCH_CACHE_TTL", "0")
    _disable_profiles(monkeypatch)
    seen: dict[str, object] = {}

    def _fake_fetch_html(url: str, query: str | None = None):
        html = (
            "<html><head><title>No Context</title></head><body>"
            "<h1>A</h1><p>body body body body body</p></body></html>"
        )
        fetched = FetchResult(
            url=url,
            html=html,
            markdown="# A\n\nbody body body body body",
            raw_html=html,
            fetcher="playwright",
            elapsed_ms=10,
        )
        extracted = pipeline.extraction.ExtractedContent(
            markdown=fetched.markdown,
            extractor="test",
        )
        return fetched, extracted, "playwright+trafilatura"

    def _fake_retrieve(query, chunks, *, k, context_texts=None, **_kwargs):
        seen["context_texts"] = context_texts
        return RetrievalResult(
            scored=[ScoredChunk(chunk=chunks[0], score=1.0)],
            elapsed_ms=1,
            embed_calls=0,
            n_chunks_embedded=1,
        )

    monkeypatch.setattr(pipeline, "_fetch_html", _fake_fetch_html)
    monkeypatch.setattr(pipeline.retrieval, "retrieve", _fake_retrieve)
    monkeypatch.setattr(
        pipeline.reranking,
        "rerank",
        lambda _q, scored, *, k, page_title="": (scored[:k], False),
    )

    result = pipeline.fetch_relevant("https://example.com/no-context", "body")

    assert result.error is None
    assert seen["context_texts"] is None
    assert result.contextual_retrieval_used is False
    assert result.context_prefix_chars_total == 0
    assert result.context_prefix_chars_avg == 0.0


def test_pipeline_result_defaults_contextual_fields():
    result = pipeline.PipelineResult(
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
    )

    assert result.contextual_retrieval_used is False
    assert result.context_prefix_chars_total == 0
    assert result.context_prefix_chars_avg == 0.0
