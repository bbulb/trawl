# Stability Speed Remaining Work Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Continue the non-P0 work from `docs/stability-speed-improvement-report-2026-05-04.md` after `docs/superpowers/plans/2026-05-04-p0-stability-foundations.md` is complete.

**Architecture:** Treat the remaining report items as separate, testable slices. Start with P1 measurement and observability so later changes to queues, fetchers, caches, and retrieval modes have data-backed gates. Keep optional external providers and heavy dependencies lazy so the default test suite stays offline.

**Tech Stack:** Python 3.10+, `pytest`, `httpx`, existing `benchmarks/reader_comparison.py`, existing `trawl.embedding_cache`, existing `trawl.telemetry`, MCP server with `ThreadPoolExecutor`, optional Firecrawl API, optional Crawl4AI.

---

## New Session `/goal` Commands

Use the first command for the next implementation session. The later commands are intentionally split because they touch different subsystems and have different risk profiles.

### Goal 1: P1 Speed Observability And Benchmark Adapters

```text
/goal Implement the next P1 speed/observability slice for trawl using docs/stability-speed-improvement-report-2026-05-04.md and docs/superpowers/plans/2026-05-04-stability-speed-remaining-work.md. Add embedding cache hit/miss metadata through RetrievalResult, PipelineResult, telemetry, and reader-comparison output. Add a warm-repeat benchmark mode that can compare cold vs warm repeated queries with TRAWL_EMBED_CACHE_TTL=86400. Implement mocked Firecrawl and Crawl4AI reader-comparison adapters that skip cleanly when credentials/packages are unavailable. Update README/docs and verify with targeted tests plus `mamba run -n trawl python -m pytest`.
```

### Goal 2: P1 MCP Browser Queue Separation

```text
/goal Implement MCP/browser queue separation for trawl using docs/stability-speed-improvement-report-2026-05-04.md and docs/superpowers/plans/2026-05-04-stability-speed-remaining-work.md. Route browser-free fetch_page calls through a general executor while keeping Playwright/profile work on the single browser executor. Add concurrency tests showing passthrough/API/PDF-style calls are not blocked by one slow Playwright/profile call, preserve greenlet/thread safety, update docs, and verify with targeted tests plus `mamba run -n trawl python -m pytest`.
```

### Goal 3: P2 Fetch Recovery And Cache Revalidation

```text
/goal Implement the P2 fetch recovery slice for trawl using docs/stability-speed-improvement-report-2026-05-04.md and docs/superpowers/plans/2026-05-04-stability-speed-remaining-work.md. Add optional lazy Scrapling fallback behind TRAWL_SCRAPLING_FALLBACK=1, add HTTP cache revalidation fields and mocked 304/200/stale tests, keep default dependencies unchanged, update docs, and verify with targeted tests plus `mamba run -n trawl python -m pytest`.
```

### Goal 4: P2 Retrieval Mode Re-Measurement

```text
/goal Implement cache-controlled hybrid/contextual retrieval re-measurement for trawl using docs/stability-speed-improvement-report-2026-05-04.md and docs/superpowers/plans/2026-05-04-stability-speed-remaining-work.md. Add benchmark/report support for dense, hybrid, contextual-auto, and contextual-forced modes; record flipped-to-fail, rank movement, retrieval p50/p95, and token output; do not change defaults unless gates pass; update docs and verify with targeted tests plus `mamba run -n trawl python -m pytest`.
```

## Execution Order

1. Goal 1 first. It adds measurement and optional provider scaffolding needed to judge later speed work.
2. Goal 2 second. Queue separation changes runtime behavior and should rely on the observability from Goal 1.
3. Goal 3 third. Scrapling and cache revalidation are optional recovery paths with higher dependency and staleness risk.
4. Goal 4 last. Retrieval default changes need cache-controlled benchmark data before any rollout decision.

## File Structure For Goal 1

- Modify `src/trawl/retrieval.py`: add document embedding cache hit/miss counters to `RetrievalResult` and return them from `_embed_documents_with_cache`.
- Modify `src/trawl/pipeline.py`: add `embed_cache_hits` and `embed_cache_misses` to `PipelineResult` and propagate retrieval counters on full/profile retrieval paths.
- Modify `src/trawl/telemetry.py`: add cache counters to opt-in JSONL events.
- Modify `benchmarks/reader_comparison.py`: add provider adapters for Firecrawl/Crawl4AI, warm repeat arguments, cache metadata fields, and report summary rows.
- Modify `README.md`: document warm-repeat benchmark command, optional provider setup, and cache metric fields.
- Modify `docs/stability-speed-improvement-report-2026-05-04.md`: append implementation results for Goal 1.
- Create or modify `tests/test_reader_comparison.py`: mocked provider adapter and warm-repeat tests.
- Modify `tests/test_retrieval_embedding_cache.py`: assert cache hit/miss counters.
- Modify `tests/test_pipeline.py` or create `tests/test_pipeline_embedding_cache_metrics.py`: assert pipeline serialization includes cache counters.
- Modify `tests/test_telemetry.py`: assert telemetry includes cache counters and still omits raw query/chunks.

## Goal 1 Task Breakdown

### Task 1: Retrieval-Level Embedding Cache Counters

**Files:**
- Modify: `src/trawl/retrieval.py`
- Modify: `tests/test_retrieval_embedding_cache.py`

- [ ] **Step 1: Write failing retrieval cache counter assertions**

Add assertions to `tests/test_retrieval_embedding_cache.py::test_retrieve_reuses_cached_document_embedding`:

```python
    assert first.embed_cache_hits == 0
    assert first.embed_cache_misses == 1
    assert second.embed_cache_hits == 1
    assert second.embed_cache_misses == 0
```

Add assertions to `tests/test_retrieval_embedding_cache.py::test_embedding_cache_disabled_keeps_current_embedding_calls`:

```python
    assert result.embed_cache_hits == 0
    assert result.embed_cache_misses == 0
```

- [ ] **Step 2: Run retrieval cache tests to verify failure**

```bash
mamba run -n trawl python -m pytest tests/test_retrieval_embedding_cache.py -q
```

Expected: FAIL with `AttributeError` for missing `embed_cache_hits` or `embed_cache_misses`.

- [ ] **Step 3: Add counters to retrieval result**

In `src/trawl/retrieval.py`, extend `RetrievalResult`:

```python
    embed_cache_hits: int = 0
    embed_cache_misses: int = 0
```

Change `_embed_documents_with_cache` to return:

```python
) -> tuple[list[list[float]], int, int, int]:
```

Inside `_embed_documents_with_cache`, initialize `cache_hits = 0`. Increment it when `cached is not None`. After the miss loop, return:

```python
    return (
        [embedding for embedding in embeddings if embedding is not None],
        embed_calls,
        cache_hits,
        len(misses),
    )
```

When `embedding_cache.is_enabled()` is false, report both counters as zero by treating gets as unmeasured disabled-cache behavior.

- [ ] **Step 4: Propagate counters from retrieve**

In `retrieve()`, unpack:

```python
            chunk_embs, doc_embed_calls, embed_cache_hits, embed_cache_misses = (
                _embed_documents_with_cache(
                    client,
                    base_url,
                    model,
                    chunk_texts,
                    contextual_mode=contextual_mode,
                )
            )
```

Initialize `embed_cache_hits = 0` and `embed_cache_misses = 0` before the `try`. Include both fields in the final `RetrievalResult(...)`.

For `_bm25_fallback_result(...)`, pass through the current counter values if the query embedding succeeded before document embedding failed. If the query embedding failed first, both counters stay zero.

- [ ] **Step 5: Run retrieval cache tests**

```bash
mamba run -n trawl python -m pytest tests/test_retrieval_embedding_cache.py -q
```

Expected: PASS.

### Task 2: Pipeline And Telemetry Cache Metrics

**Files:**
- Modify: `src/trawl/pipeline.py`
- Modify: `src/trawl/telemetry.py`
- Create: `tests/test_pipeline_embedding_cache_metrics.py`
- Modify: `tests/test_telemetry.py`

- [ ] **Step 1: Write failing pipeline serialization test**

Create `tests/test_pipeline_embedding_cache_metrics.py`:

```python
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
            pipeline.extraction.ExtractedContent(markdown=fetched.markdown, extractor="test"),
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
```

- [ ] **Step 2: Write failing telemetry assertion**

Add to `tests/test_telemetry.py::test_build_event_fields`:

```python
    assert event["embed_cache_hits"] == 0
    assert event["embed_cache_misses"] == 0
```

Add a focused test:

```python
def test_build_event_includes_embedding_cache_metrics():
    r = _sample_result()
    r.embed_cache_hits = 2
    r.embed_cache_misses = 5

    event = telemetry._build_event(r)

    assert event["embed_cache_hits"] == 2
    assert event["embed_cache_misses"] == 5
```

- [ ] **Step 3: Run tests to verify failure**

```bash
mamba run -n trawl python -m pytest tests/test_pipeline_embedding_cache_metrics.py tests/test_telemetry.py -q
```

Expected: FAIL because `PipelineResult` and telemetry do not expose the new fields yet.

- [ ] **Step 4: Add PipelineResult fields and propagation**

In `src/trawl/pipeline.py`, add defaults near `n_chunks_embedded`:

```python
    embed_cache_hits: int = 0
    embed_cache_misses: int = 0
```

When building profile retrieval and full pipeline results, pass:

```python
        embed_cache_hits=retrieved.embed_cache_hits,
        embed_cache_misses=retrieved.embed_cache_misses,
```

On retrieval error paths, pass the same values from `retrieved`.

- [ ] **Step 5: Add telemetry fields**

In `src/trawl/telemetry.py::_build_event`, add:

```python
        "embed_cache_hits": result.embed_cache_hits,
        "embed_cache_misses": result.embed_cache_misses,
```

- [ ] **Step 6: Run pipeline and telemetry tests**

```bash
mamba run -n trawl python -m pytest tests/test_pipeline_embedding_cache_metrics.py tests/test_telemetry.py -q
```

Expected: PASS.

### Task 3: Reader Comparison Warm Repeat Mode

**Files:**
- Modify: `benchmarks/reader_comparison.py`
- Modify: `tests/test_reader_comparison.py`

- [ ] **Step 1: Add failing unit test for repeat expansion**

Add to `tests/test_reader_comparison.py`:

```python
def test_expand_repeated_cases_marks_repeat_index():
    cases = [{"id": "a", "category": "docs", "url": "https://example.test", "query": "q", "expected_facts": [{"id": "f", "all_of": ["x"]}], "failure_class": {}}]

    expanded = reader_comparison.expand_repeated_cases(cases, repeats=2)

    assert [case["_repeat_index"] for case in expanded] == [0, 1]
    assert [case["_cache_phase"] for case in expanded] == ["cold", "warm"]
```

- [ ] **Step 2: Add failing test for trawl metadata fields**

Add a test that monkeypatches `reader_comparison.run_trawl_provider` dependencies and verifies the result includes:

```python
    assert result["retrieval_ms"] == 11
    assert result["cache_hit"] is False
    assert result["n_chunks_embedded"] == 1
    assert result["embed_cache_hits"] == 2
    assert result["embed_cache_misses"] == 3
    assert result["repeat_index"] == 1
    assert result["cache_phase"] == "warm"
```

- [ ] **Step 3: Run reader comparison tests to verify failure**

```bash
mamba run -n trawl python -m pytest tests/test_reader_comparison.py -q
```

Expected: FAIL because repeat expansion and metadata fields do not exist.

- [ ] **Step 4: Implement repeat expansion and CLI flag**

In `benchmarks/reader_comparison.py`, extend `RESULT_FIELDS`:

```python
    "repeat_index",
    "cache_phase",
    "retrieval_ms",
    "cache_hit",
    "n_chunks_embedded",
    "embed_cache_hits",
    "embed_cache_misses",
```

Add:

```python
def expand_repeated_cases(cases: list[dict[str, Any]], *, repeats: int) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    for case in cases:
        for repeat_index in range(repeats):
            item = dict(case)
            item["_repeat_index"] = repeat_index
            item["_cache_phase"] = "cold" if repeat_index == 0 else "warm"
            expanded.append(item)
    return expanded
```

Add parser arguments:

```python
    parser.add_argument("--repeat", type=int, default=1, help="Repeat each case N times.")
    parser.add_argument(
        "--warm-repeat-embed-cache-ttl",
        type=int,
        default=None,
        help="Set TRAWL_EMBED_CACHE_TTL while running repeated trawl cases.",
    )
```

In `main()`, call:

```python
    cases = expand_repeated_cases(cases, repeats=max(args.repeat, 1))
```

When `args.warm_repeat_embed_cache_ttl is not None`, set `os.environ["TRAWL_EMBED_CACHE_TTL"]` before provider execution and restore the previous value in a `finally`.

- [ ] **Step 5: Populate trawl metadata**

In `run_trawl_provider`, add fields from `payload` into the result:

```python
        result = build_scored_result(...)
        result.update(
            {
                "repeat_index": case.get("_repeat_index", 0),
                "cache_phase": case.get("_cache_phase", "cold"),
                "retrieval_ms": payload.get("retrieval_ms"),
                "cache_hit": payload.get("cache_hit"),
                "n_chunks_embedded": payload.get("n_chunks_embedded"),
                "embed_cache_hits": payload.get("embed_cache_hits"),
                "embed_cache_misses": payload.get("embed_cache_misses"),
            }
        )
        return result
```

Make `build_scored_result` set those keys to neutral values for non-trawl providers:

```python
        "repeat_index": case.get("_repeat_index", 0),
        "cache_phase": case.get("_cache_phase", "cold"),
        "retrieval_ms": None,
        "cache_hit": None,
        "n_chunks_embedded": n_chunks_total,
        "embed_cache_hits": None,
        "embed_cache_misses": None,
```

- [ ] **Step 6: Update report rendering**

In `render_report`, add a section when any active row has `cache_phase == "warm"`:

```markdown
## Warm repeat summary

| Provider | Phase | Rows | Avg retrieval ms | Avg embed cache hits | Avg embed cache misses |
|---|---:|---:|---:|---:|---:|
```

Compute averages only for numeric values.

- [ ] **Step 7: Run reader comparison tests**

```bash
mamba run -n trawl python -m pytest tests/test_reader_comparison.py -q
```

Expected: PASS.

### Task 4: Firecrawl Reader Adapter With Mocked Tests

**Files:**
- Modify: `benchmarks/reader_comparison.py`
- Modify: `tests/test_reader_comparison.py`

- [ ] **Step 1: Write failing Firecrawl skip and success tests**

Add tests:

```python
def test_firecrawl_provider_skips_without_api_key(monkeypatch):
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)

    result = reader_comparison.run_firecrawl_provider(_case())

    assert result["status"] == "skipped"
    assert result["failure_phase"] == "not_configured"


def test_firecrawl_provider_uses_markdown_response(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"success": True, "data": {"markdown": "alpha fact"}}

    def fake_post(url, *, headers, json, timeout):
        assert url == "https://api.firecrawl.dev/v2/scrape"
        assert headers["Authorization"] == "Bearer fc-test"
        assert json == {"url": "https://example.test", "formats": ["markdown"]}
        return Response()

    monkeypatch.setattr(reader_comparison.httpx, "post", fake_post)

    result = reader_comparison.run_firecrawl_provider(_case())

    assert result["provider"] == "firecrawl"
    assert result["status"] == "ok"
    assert result["answer_grounding_hit"] is True
```

- [ ] **Step 2: Run tests to verify failure**

```bash
mamba run -n trawl python -m pytest tests/test_reader_comparison.py -q
```

Expected: FAIL because Firecrawl still returns "adapter is not implemented".

- [ ] **Step 3: Implement Firecrawl HTTP adapter**

Use the official Firecrawl scrape shape checked on 2026-05-04: `Firecrawl.scrape(url=..., formats=["markdown", "html"])` is documented, and the public API endpoint is `https://api.firecrawl.dev/v2/scrape`.

In `benchmarks/reader_comparison.py`, add constants:

```python
FIRECRAWL_SCRAPE_URL = os.environ.get(
    "FIRECRAWL_SCRAPE_URL", "https://api.firecrawl.dev/v2/scrape"
)
FIRECRAWL_TIMEOUT = float(os.environ.get("FIRECRAWL_TIMEOUT", "60"))
```

Replace `run_firecrawl_provider` with:

```python
def run_firecrawl_provider(case: dict[str, Any]) -> dict[str, Any]:
    api_key = os.environ.get("FIRECRAWL_API_KEY")
    if not api_key:
        return build_skip_result(case, "firecrawl", "FIRECRAWL_API_KEY not set")

    started = time.monotonic()
    try:
        response = httpx.post(
            FIRECRAWL_SCRAPE_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"url": case["url"], "formats": ["markdown"]},
            timeout=FIRECRAWL_TIMEOUT,
        )
        response.raise_for_status()
        body = response.json()
        data = body.get("data") if isinstance(body, dict) else None
        markdown = ""
        if isinstance(data, dict):
            markdown = str(data.get("markdown") or "")
        elif isinstance(body, dict):
            markdown = str(body.get("markdown") or "")
        elapsed = int((time.monotonic() - started) * 1000)
        return build_scored_result(
            case=case,
            provider="firecrawl",
            status="ok",
            latency_ms=elapsed,
            ranked_texts=[markdown] if markdown else [],
            n_chunks_total=None,
            error=None,
        )
    except Exception as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        return build_scored_result(
            case=case,
            provider="firecrawl",
            status="error",
            latency_ms=elapsed,
            ranked_texts=[],
            n_chunks_total=None,
            error=f"{type(exc).__name__}: {exc}",
        )
```

- [ ] **Step 4: Run Firecrawl tests**

```bash
mamba run -n trawl python -m pytest tests/test_reader_comparison.py -q
```

Expected: PASS.

### Task 5: Crawl4AI Reader Adapter With Mocked Tests

**Files:**
- Modify: `benchmarks/reader_comparison.py`
- Modify: `tests/test_reader_comparison.py`

- [ ] **Step 1: Write failing Crawl4AI skip and success tests**

Add a helper import wrapper so tests can monkeypatch without installing Crawl4AI:

```python
def test_crawl4ai_provider_skips_when_package_missing(monkeypatch):
    monkeypatch.setattr(reader_comparison, "_load_crawl4ai", lambda: None)

    result = reader_comparison.run_crawl4ai_provider(_case())

    assert result["status"] == "skipped"
    assert result["failure_phase"] == "not_configured"
```

Add an async fake class that returns an object with `markdown = "alpha fact"` and assert status `ok`.

- [ ] **Step 2: Run tests to verify failure**

```bash
mamba run -n trawl python -m pytest tests/test_reader_comparison.py -q
```

Expected: FAIL because `_load_crawl4ai` does not exist and Crawl4AI still returns "adapter is not implemented".

- [ ] **Step 3: Implement lazy Crawl4AI adapter**

Use the official Crawl4AI v0.8 flow checked on 2026-05-04: `AsyncWebCrawler` is the crawler entry point and `crawler.arun(url=..., config=CrawlerRunConfig(...))` returns a result with markdown content.

Add:

```python
def _load_crawl4ai():
    try:
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig
    except ImportError:
        return None
    return {
        "AsyncWebCrawler": AsyncWebCrawler,
        "BrowserConfig": BrowserConfig,
        "CacheMode": CacheMode,
        "CrawlerRunConfig": CrawlerRunConfig,
    }
```

Implement:

```python
async def _run_crawl4ai_async(case: dict[str, Any], api: dict[str, Any]) -> str:
    browser_config = api["BrowserConfig"](headless=True)
    run_config = api["CrawlerRunConfig"](cache_mode=api["CacheMode"].BYPASS)
    async with api["AsyncWebCrawler"](config=browser_config) as crawler:
        result = await crawler.arun(url=case["url"], config=run_config)
    markdown = getattr(result, "markdown", "") or ""
    if hasattr(markdown, "fit_markdown"):
        markdown = markdown.fit_markdown or getattr(markdown, "raw_markdown", "")
    return str(markdown or "")
```

Replace `run_crawl4ai_provider` with a wrapper that loads the API, returns a skip if unavailable, runs `asyncio.run(_run_crawl4ai_async(case, api))`, then returns `build_scored_result(...)`.

- [ ] **Step 4: Run Crawl4AI tests**

```bash
mamba run -n trawl python -m pytest tests/test_reader_comparison.py -q
```

Expected: PASS.

### Task 6: Docs And Verification For Goal 1

**Files:**
- Modify: `README.md`
- Modify: `docs/stability-speed-improvement-report-2026-05-04.md`

- [ ] **Step 1: Document warm repeat benchmark**

Add to README benchmark/testing area:

````markdown
For repeated-query cache measurement:

```bash
mamba run -n trawl python benchmarks/reader_comparison.py \
  --provider trawl \
  --repeat 2 \
  --warm-repeat-embed-cache-ttl 86400
```

The report includes cold/warm phases, retrieval latency, chunk budget counts, and embedding-cache hit/miss counters for trawl rows.
````

- [ ] **Step 2: Document optional provider setup**

Add:

```markdown
Optional reader-comparison providers:

- Firecrawl: set `FIRECRAWL_API_KEY`; unavailable credentials produce skipped rows.
- Crawl4AI: install the optional package in your environment; unavailable imports produce skipped rows.
```

- [ ] **Step 3: Update stability report**

Append under the P1 embedding cache and reader benchmark sections:

```markdown
구현 결과:

- reader comparison에 cold/warm repeat mode와 embedding-cache hit/miss metadata를 추가했다.
- Firecrawl/Crawl4AI provider adapter는 credential/package가 없으면 skipped row를 기록하고, mocked tests로 schema를 검증한다.
```

- [ ] **Step 4: Run focused verification**

```bash
mamba run -n trawl python -m pytest \
  tests/test_retrieval_embedding_cache.py \
  tests/test_pipeline_embedding_cache_metrics.py \
  tests/test_telemetry.py \
  tests/test_reader_comparison.py \
  -q
```

Expected: PASS.

- [ ] **Step 5: Run full verification**

```bash
mamba run -n trawl python -m pytest
```

Expected: all tests pass.

## Goal 2 Planning Notes

Use these constraints when turning Goal 2 into a full implementation plan:

- Keep `profile_page` and Playwright-rendered full pipeline calls on a single browser executor.
- Add a small browser-free route classifier in `src/trawl_mcp/server.py`; classify passthrough suffixes, known API fetchers, and direct PDF URL/probe-safe paths before dispatch.
- Do not call Playwright from the general executor.
- Test by monkeypatching `fetch_relevant` or a new dispatch helper so one slow browser-classified call does not block five browser-free calls.
- Preserve existing MCP payload shape and content boundary.

## Goal 3 Planning Notes

Use these constraints when turning Goal 3 into a full implementation plan:

- Scrapling must be lazy-imported and guarded by `TRAWL_SCRAPLING_FALLBACK=1`.
- Scrapling should only supply HTML; extraction, chunking, retrieval, warnings, and telemetry stay in trawl.
- Fetch-cache revalidation should extend `fetch_cache.CachedFetch` with `etag`, `last_modified`, and `content_hash` while continuing to read old cache records.
- Tests must mock 304 reuse, 200 replacement, missing validators, and stale dynamic-page behavior.

Goal 3 implementation result:

- Added `src/trawl/fetchers/scrapling.py` as a lazy optional fallback. It is disabled unless `TRAWL_SCRAPLING_FALLBACK=1`, supports `TRAWL_SCRAPLING_MODE=auto|dynamic|stealthy`, and returns regular trawl `FetchResult` HTML for the existing extraction path.
- Added optional `scrapling` extra in `pyproject.toml`; default dependencies remain unchanged.
- Added fetch-cache validator metadata and conditional revalidation. `304` reuses cached markdown after refreshing metadata; `200`, missing validators, and revalidation errors fall through to a fresh fetch.
- Added tests in `tests/test_scrapling_fallback.py`, `tests/test_fetch_cache.py`, and `tests/test_pipeline_cache.py`.

## Goal 4 Planning Notes

Use these constraints when turning Goal 4 into a full implementation plan:

- Measurement comes before default changes.
- Required report fields: mode, query type, retrieval p50/p95, rank-1 identity, flipped-to-fail count, token output, and cache setting.
- Do not change `TRAWL_HYBRID_RETRIEVAL` or `TRAWL_CONTEXTUAL_RETRIEVAL` defaults unless flipped-to-fail is 0 and retrieval p95 increase is `<= 20%`.

Goal 4 implementation result:

- Added `--retrieval-mode dense|hybrid|contextual-auto|contextual-forced` to `benchmarks/reader_comparison.py`. Mode expansion applies only to `trawl` rows so external/full-page provider baselines are not duplicated.
- Each mode sets `TRAWL_HYBRID_RETRIEVAL` and `TRAWL_CONTEXTUAL_RETRIEVAL` only around its single provider call, then restores the previous environment. Existing runtime defaults remain unchanged.
- Reader-comparison CSV/JSONL rows now include requested/observed retrieval mode, retrieval query type, contextual-use flag, cache TTL, rank-1 identity hash, first satisfied fact rank, dense-baseline rank movement, flipped-to-fail, and token output.
- Markdown reports now include a retrieval-mode summary grouped by mode, query type, and cache TTL, with flipped-to-fail counts plus retrieval p50/p95.
- Tests cover mode expansion, environment restoration, dense-baseline comparison annotation, and report rendering in `tests/test_reader_comparison.py`.

## Sources Checked For Optional Provider APIs

- Firecrawl scrape docs: `https://docs.firecrawl.dev/features/scrape`
- Firecrawl repository API example: `https://github.com/firecrawl/firecrawl`
- Crawl4AI AsyncWebCrawler docs: `https://docs.crawl4ai.com/api/async-webcrawler/`
- Crawl4AI `arun()` docs: `https://docs.crawl4ai.com/api/arun/`
