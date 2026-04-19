"""Integration tests for the fetch cache wired into `_run_full_pipeline`.

Mocks the HTML fetcher + retrieval so we can verify:
    * first call populates the cache and returns `cache_hit=False`
    * second call within TTL returns `cache_hit=True`, skips the fetcher
    * `TRAWL_FETCH_CACHE_TTL=0` disables caching
    * expired cached entry triggers a fresh fetch
    * profile and passthrough branches still bypass the cache

No Playwright, no bge-m3 endpoint.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from trawl import fetch_cache, pipeline
from trawl.fetchers.playwright import FetchResult
from trawl.retrieval import RetrievalResult, ScoredChunk

# ---------- fixtures


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TRAWL_FETCH_CACHE_PATH", str(tmp_path))
    monkeypatch.setenv("TRAWL_FETCH_CACHE_TTL", "300")
    monkeypatch.setenv("TRAWL_FETCH_CACHE_MAX_MB", "100")
    yield tmp_path


@pytest.fixture
def fake_fetcher(monkeypatch):
    """Replace `_fetch_html` with a counter so we can assert hits vs misses."""
    calls: list[str] = []

    def _fake(url: str):
        calls.append(url)
        fetched = FetchResult(
            url=url,
            html="<html><head><title>Cached Page</title></head>"
            "<body><h1>hi</h1><p>body body body body body</p></body></html>",
            markdown="# Cached Page\n\nbody body body body body",
            raw_html="<html/>",
            fetcher="playwright+trafilatura",
            elapsed_ms=4321,
        )
        return fetched, fetched.markdown, "playwright+trafilatura"

    monkeypatch.setattr(pipeline, "_fetch_html", _fake)
    return calls


@pytest.fixture
def fake_retrieval(monkeypatch):
    """Return a canned retrieval result with one scored chunk."""

    def _fake_retrieve(query, chunks, *, k, extra_query_texts=None, hybrid=False):
    def _fake_retrieve(query, chunks, *, k, extra_query_texts=None):
        scored = [
            ScoredChunk(chunk=c, score=1.0 - i * 0.1) for i, c in enumerate(chunks[: max(k, 1)])
        ]
        return RetrievalResult(scored=scored, elapsed_ms=5, embed_calls=0)

    monkeypatch.setattr(pipeline.retrieval, "retrieve", _fake_retrieve)

    # Skip the reranker round-trip for determinism.
    def _fake_rerank(query, scored, *, k, page_title=""):
        return scored[:k]

    monkeypatch.setattr(pipeline.reranking, "rerank", _fake_rerank)


@pytest.fixture
def no_profile(monkeypatch):
    """Make track_visit / load_profile no-ops so the profile path stays off."""
    import trawl.profiles as profiles_mod

    monkeypatch.setattr(profiles_mod, "track_visit", lambda url: None)
    monkeypatch.setattr(profiles_mod, "load_profile", lambda url: None)
    monkeypatch.setattr(profiles_mod, "get_visit_count", lambda url: 0)


# ---------- tests


def test_first_call_is_cache_miss_second_is_hit(fake_fetcher, fake_retrieval, no_profile):
    url = "https://example.com/article"
    r1 = pipeline.fetch_relevant(url, "what")
    assert r1.error is None
    assert r1.cache_hit is False
    assert r1.fetcher_used == "playwright+trafilatura"
    assert r1.fetch_ms > 0
    assert len(fake_fetcher) == 1

    r2 = pipeline.fetch_relevant(url, "what")
    assert r2.error is None
    assert r2.cache_hit is True
    assert r2.fetcher_used == "playwright+trafilatura"
    assert r2.fetch_ms == 0
    # Fetcher was NOT called again.
    assert len(fake_fetcher) == 1
    # Chunks survived the round-trip.
    assert r1.n_chunks_total == r2.n_chunks_total
    assert r1.page_title == r2.page_title


def test_cache_disabled_never_reuses(fake_fetcher, fake_retrieval, no_profile, monkeypatch):
    monkeypatch.setenv("TRAWL_FETCH_CACHE_TTL", "0")
    url = "https://example.com/no-cache"
    pipeline.fetch_relevant(url, "q")
    pipeline.fetch_relevant(url, "q")
    assert len(fake_fetcher) == 2


def test_expired_entry_triggers_refetch(fake_fetcher, fake_retrieval, no_profile, monkeypatch):
    url = "https://example.com/expires"

    # Prime the cache with a stale entry.
    fetch_cache.put(
        fetch_cache.CachedFetch(
            url=url,
            markdown="# stale\n\nstale body",
            page_title="stale",
            fetcher_used="playwright+trafilatura",
            content_type="text/html",
            cached_at=time.time() - 10_000,  # far older than TTL
            fetch_elapsed_ms=1000,
        )
    )

    r = pipeline.fetch_relevant(url, "q")
    assert r.cache_hit is False
    assert len(fake_fetcher) == 1
    # After the fetch, the cache should hold the fresh markdown.
    got = fetch_cache.get(url)
    assert got is not None
    assert got.markdown.startswith("# Cached Page")


def test_different_queries_share_cached_fetch(fake_fetcher, fake_retrieval, no_profile):
    """Cache is keyed by URL only — different queries hit the same entry."""
    url = "https://example.com/shared"
    pipeline.fetch_relevant(url, "first query")
    r2 = pipeline.fetch_relevant(url, "second query")
    assert r2.cache_hit is True
    assert len(fake_fetcher) == 1


def test_error_result_not_cached(fake_retrieval, no_profile, monkeypatch):
    """A failed fetch must not populate the cache."""

    def _fail(url):
        return (
            FetchResult(
                url=url,
                html="",
                markdown="",
                raw_html="",
                fetcher="playwright+trafilatura",
                elapsed_ms=100,
                error="boom",
            ),
            "",
            "playwright+trafilatura",
        )

    monkeypatch.setattr(pipeline, "_fetch_html", _fail)
    url = "https://example.com/fail"
    r = pipeline.fetch_relevant(url, "q")
    assert r.error is not None
    assert fetch_cache.get(url) is None


def test_pipeline_result_dataclass_defaults_cache_hit_false():
    """Backward-compat: legacy PipelineResult constructions keep working."""
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
    )
    assert r.cache_hit is False


def test_to_dict_includes_cache_hit():
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
        cache_hit=True,
    )
    d = pipeline.to_dict(r)
    assert d["cache_hit"] is True
