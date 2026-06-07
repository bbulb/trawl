# Contextual Retrieval Prefix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in deterministic contextual prefix for retrieval inputs without changing returned chunk text or public payload shape.

**Architecture:** Add a focused `trawl.contextual` module that builds ranking-only strings from existing chunk/page metadata. Pipeline code builds contextual texts when `TRAWL_CONTEXTUAL_RETRIEVAL=1` and passes them to `retrieval.retrieve()`, which uses them for dense embedding and BM25 ranking. Telemetry records only contextual usage and prefix length statistics, never raw text.

**Tech Stack:** Python 3.10+, dataclasses, pytest, monkeypatch, existing trawl pipeline/retrieval/telemetry modules.

---

## File Structure

- Create `src/trawl/contextual.py`: pure helpers for contextual feature flag, prefix cap parsing, per-chunk context construction, and batch statistics.
- Create `tests/test_contextual.py`: unit tests for prefix content, truncation, metadata omission, and body preservation.
- Create `tests/test_retrieval_contextual.py`: retrieval tests that verify `context_texts` are used for embedding/BM25 and alignment errors are explicit.
- Modify `src/trawl/retrieval.py`: accept `context_texts`, validate alignment, use them as `chunk_texts` when present, and retain existing behavior when absent.
- Modify `src/trawl/pipeline.py`: add `PipelineResult` contextual telemetry fields and build/pass contextual texts in full/profile retrieval paths.
- Modify `src/trawl/telemetry.py`: emit contextual diagnostic fields.
- Modify `README.md`: document the two new environment variables.

---

### Task 1: Contextual Prefix Module

**Files:**
- Create: `src/trawl/contextual.py`
- Test: `tests/test_contextual.py`

- [ ] **Step 1: Write failing tests for contextual prefix construction**

Create `tests/test_contextual.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_contextual.py -v
```

Expected: FAIL during import with `ImportError` or `ModuleNotFoundError` because `trawl.contextual` does not exist.

- [ ] **Step 3: Implement `src/trawl/contextual.py`**

Create `src/trawl/contextual.py`:

```python
"""Deterministic contextual text for retrieval inputs.

The returned strings are ranking-only inputs. They must not replace
``Chunk.text`` or any public payload text.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .chunking import Chunk

DEFAULT_MAX_PREFIX_CHARS = 320


@dataclass(frozen=True)
class ContextualText:
    text: str
    prefix_chars: int


@dataclass(frozen=True)
class ContextualTextBatch:
    texts: list[str]
    prefix_chars_total: int
    prefix_chars_avg: float


def is_enabled() -> bool:
    """Return True when contextual retrieval is enabled by environment."""
    return os.environ.get("TRAWL_CONTEXTUAL_RETRIEVAL", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def max_prefix_chars() -> int:
    """Return the configured prefix cap, falling back to the default."""
    raw = os.environ.get("TRAWL_CONTEXT_PREFIX_MAX_CHARS", str(DEFAULT_MAX_PREFIX_CHARS))
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_PREFIX_CHARS
    return max(0, value)


def build_contextual_text(
    chunk: Chunk,
    *,
    page_title: str,
    previous_heading: str,
    next_heading: str,
    total_chunks: int,
) -> ContextualText:
    """Build one ranking-only contextual string for ``chunk``."""
    body = chunk.embed_text or chunk.text
    prefix_limit = max_prefix_chars()
    if prefix_limit <= 0:
        return ContextualText(text=body, prefix_chars=0)

    lines: list[str] = []
    title = page_title.strip()
    if title:
        lines.append(f"Title: {title}")
    if chunk.heading:
        lines.append(f"Section: {chunk.heading}")
    if total_chunks > 0:
        lines.append(f"Position: chunk {chunk.chunk_index + 1} of {total_chunks}")
    if chunk.record_group_id is not None and chunk.record_index is not None:
        lines.append(
            f"Record: item {chunk.record_index + 1} in repeated group {chunk.record_group_id}"
        )

    nearby = [h for h in (previous_heading, next_heading) if h]
    if nearby:
        lines.append(f"Nearby sections: {' | '.join(nearby)}")

    prefix = "\n".join(lines).strip()
    if len(prefix) > prefix_limit:
        prefix = prefix[:prefix_limit].rstrip()
    if not prefix:
        return ContextualText(text=body, prefix_chars=0)
    return ContextualText(text=f"{prefix}\n\n{body}", prefix_chars=len(prefix))


def build_contextual_texts(chunks: list[Chunk], *, page_title: str) -> ContextualTextBatch:
    """Build contextual retrieval inputs aligned with ``chunks``."""
    texts: list[str] = []
    prefix_total = 0
    total_chunks = len(chunks)
    headings = [c.heading for c in chunks]

    for index, chunk in enumerate(chunks):
        previous_heading = _nearest_heading(headings, index, step=-1)
        next_heading = _nearest_heading(headings, index, step=1)
        item = build_contextual_text(
            chunk,
            page_title=page_title,
            previous_heading=previous_heading,
            next_heading=next_heading,
            total_chunks=total_chunks,
        )
        texts.append(item.text)
        prefix_total += item.prefix_chars

    avg = prefix_total / total_chunks if total_chunks else 0.0
    return ContextualTextBatch(
        texts=texts,
        prefix_chars_total=prefix_total,
        prefix_chars_avg=avg,
    )


def _nearest_heading(headings: list[str], index: int, *, step: int) -> str:
    i = index + step
    while 0 <= i < len(headings):
        if headings[i]:
            return headings[i]
        i += step
    return ""
```

- [ ] **Step 4: Run contextual tests**

Run:

```bash
pytest tests/test_contextual.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit Task 1**

Run:

```bash
git add src/trawl/contextual.py tests/test_contextual.py
git commit -m "feat(retrieval): add contextual prefix builder"
```

Expected: commit succeeds.

---

### Task 2: Retrieval Support For Context Texts

**Files:**
- Modify: `src/trawl/retrieval.py`
- Test: `tests/test_retrieval_contextual.py`

- [ ] **Step 1: Write failing retrieval tests**

Create `tests/test_retrieval_contextual.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_retrieval_contextual.py -v
```

Expected: FAIL with `TypeError: retrieve() got an unexpected keyword argument 'context_texts'`.

- [ ] **Step 3: Modify `RetrievalResult` and `retrieve()` signature**

In `src/trawl/retrieval.py`, update the `retrieve()` signature:

```python
def retrieve(
    query: str,
    chunks: list[Chunk],
    *,
    k: int = 5,
    base_url: str = DEFAULT_EMBEDDING_URL,
    model: str = DEFAULT_EMBEDDING_MODEL,
    extra_query_texts: list[str] | None = None,
    hybrid: bool = False,
    chunk_budget: int = 0,
    context_texts: list[str] | None = None,
) -> RetrievalResult:
```

- [ ] **Step 4: Add alignment validation**

Immediately after the existing empty-chunks guard in `retrieve()`, add:

```python
    if context_texts is not None and len(context_texts) != len(chunks):
        return RetrievalResult(
            scored=[],
            elapsed_ms=0,
            embed_calls=0,
            error=(
                f"context_texts length {len(context_texts)} "
                f"does not match chunks length {len(chunks)}"
            ),
            n_chunks_embedded=0,
        )
```

- [ ] **Step 5: Use provided contextual strings for chunk texts**

Replace the existing list comprehension that assigns `chunk_texts` in `retrieve()` with:

```python
    if context_texts is not None:
        chunk_texts = list(context_texts)
    else:
        chunk_texts = [
            (c.heading + "\n\n" + (c.embed_text or c.text))
            if c.heading
            else (c.embed_text or c.text)
            for c in chunks
        ]
```

Do not change the BM25 prefilter logic after this block; it should now operate on whichever `chunk_texts` were selected.

- [ ] **Step 6: Run retrieval tests**

Run:

```bash
pytest tests/test_retrieval_contextual.py tests/test_retrieval_hybrid.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit Task 2**

Run:

```bash
git add src/trawl/retrieval.py tests/test_retrieval_contextual.py
git commit -m "feat(retrieval): accept contextual ranking inputs"
```

Expected: commit succeeds.

---

### Task 3: Pipeline Wiring And Result Telemetry Fields

**Files:**
- Modify: `src/trawl/pipeline.py`
- Test: `tests/test_pipeline_contextual.py`

- [ ] **Step 1: Write failing pipeline tests**

Create `tests/test_pipeline_contextual.py`:

```python
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
    monkeypatch.setattr(pipeline.reranking, "rerank", lambda _q, scored, *, k, page_title="": (scored[:k], False))

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
        html = "<html><head><title>No Context</title></head><body><h1>A</h1><p>body body body body</p></body></html>"
        fetched = FetchResult(
            url=url,
            html=html,
            markdown="# A\n\nbody body body body",
            raw_html=html,
            fetcher="playwright",
            elapsed_ms=10,
        )
        extracted = pipeline.extraction.ExtractedContent(markdown=fetched.markdown, extractor="test")
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
    monkeypatch.setattr(pipeline.reranking, "rerank", lambda _q, scored, *, k, page_title="": (scored[:k], False))

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_pipeline_contextual.py -v
```

Expected: FAIL because `PipelineResult` has no contextual fields and pipeline does not pass `context_texts`.

- [ ] **Step 3: Import contextual module in pipeline**

In `src/trawl/pipeline.py`, update the import line:

```python
from . import chunking, contextual, enrichment, extraction, fetch_cache, hyde, reranking, retrieval, telemetry
```

- [ ] **Step 4: Add fields to `PipelineResult`**

Add these fields near existing retrieval diagnostics fields:

```python
    contextual_retrieval_used: bool = False
    context_prefix_chars_total: int = 0
    context_prefix_chars_avg: float = 0.0
```

- [ ] **Step 5: Add a helper for contextual batch construction**

In `src/trawl/pipeline.py`, near `_retrieval_diagnostics`, add:

```python
def _contextual_batch(chunks: list[chunking.Chunk], page_title: str):
    if not contextual.is_enabled():
        return None
    return contextual.build_contextual_texts(chunks, page_title=page_title)
```

- [ ] **Step 6: Wire contextual texts in `_build_profile_result()`**

In the `elif query:` branch of `_build_profile_result()`, before the call to `retrieval.retrieve`, add:

```python
        context_batch = _contextual_batch(chunks, page_title)
```

Then update the `retrieval.retrieve()` call in that branch to pass:

```python
            context_texts=context_batch.texts if context_batch else None,
```

In the successful `PipelineResult` return at the bottom of `_build_profile_result()`, add:

```python
        contextual_retrieval_used=bool(context_batch) if path == "profile_retrieval" else False,
        context_prefix_chars_total=(
            context_batch.prefix_chars_total if path == "profile_retrieval" and context_batch else 0
        ),
        context_prefix_chars_avg=(
            context_batch.prefix_chars_avg if path == "profile_retrieval" and context_batch else 0.0
        ),
```

In the retrieval error `PipelineResult` return inside `_build_profile_result()`, add:

```python
                contextual_retrieval_used=bool(context_batch),
                context_prefix_chars_total=context_batch.prefix_chars_total if context_batch else 0,
                context_prefix_chars_avg=context_batch.prefix_chars_avg if context_batch else 0.0,
```

- [ ] **Step 7: Wire contextual texts in `_run_full_pipeline()`**

In `_run_full_pipeline()`, before the call to `retrieval.retrieve`, add:

```python
    context_batch = _contextual_batch(chunks, page_title)
```

Update the `retrieval.retrieve()` call to pass:

```python
        context_texts=context_batch.texts if context_batch else None,
```

In the retrieval error `_error_result` call, add:

```python
            contextual_retrieval_used=bool(context_batch),
            context_prefix_chars_total=context_batch.prefix_chars_total if context_batch else 0,
            context_prefix_chars_avg=context_batch.prefix_chars_avg if context_batch else 0.0,
```

In the final `PipelineResult` return, add:

```python
        contextual_retrieval_used=bool(context_batch),
        context_prefix_chars_total=context_batch.prefix_chars_total if context_batch else 0,
        context_prefix_chars_avg=context_batch.prefix_chars_avg if context_batch else 0.0,
```

- [ ] **Step 8: Run pipeline contextual tests**

Run:

```bash
pytest tests/test_pipeline_contextual.py -v
```

Expected: PASS.

- [ ] **Step 9: Run cache/pipeline regression subset**

Run:

```bash
pytest tests/test_pipeline_cache.py tests/test_pipeline_contextual.py -v
```

Expected: PASS.

- [ ] **Step 10: Commit Task 3**

Run:

```bash
git add src/trawl/pipeline.py tests/test_pipeline_contextual.py
git commit -m "feat(pipeline): wire contextual retrieval inputs"
```

Expected: commit succeeds.

---

### Task 4: Telemetry Fields

**Files:**
- Modify: `src/trawl/telemetry.py`
- Test: `tests/test_telemetry.py`

- [ ] **Step 1: Add failing telemetry test**

Append to `tests/test_telemetry.py`:

```python
def test_build_event_includes_contextual_retrieval_stats():
    r = _sample_result()
    r.contextual_retrieval_used = True
    r.context_prefix_chars_total = 123
    r.context_prefix_chars_avg = 41.0

    event = telemetry._build_event(r)

    assert event["contextual_retrieval_used"] is True
    assert event["context_prefix_chars_total"] == 123
    assert event["context_prefix_chars_avg"] == 41.0
    assert "context_texts" not in event
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_telemetry.py::test_build_event_includes_contextual_retrieval_stats -v
```

Expected: FAIL with `KeyError: 'contextual_retrieval_used'`.

- [ ] **Step 3: Add contextual fields to telemetry event**

In `src/trawl/telemetry.py`, inside `_build_event()`, add these fields after `n_chunks_embedded`:

```python
        "contextual_retrieval_used": result.contextual_retrieval_used,
        "context_prefix_chars_total": result.context_prefix_chars_total,
        "context_prefix_chars_avg": result.context_prefix_chars_avg,
```

- [ ] **Step 4: Run telemetry tests**

Run:

```bash
pytest tests/test_telemetry.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit Task 4**

Run:

```bash
git add src/trawl/telemetry.py tests/test_telemetry.py
git commit -m "feat(telemetry): record contextual retrieval stats"
```

Expected: commit succeeds.

---

### Task 5: Documentation And Final Verification

**Files:**
- Modify: `README.md`
- Verify: existing tests

- [ ] **Step 1: Document environment variables**

In `README.md`, in the environment/configuration section that lists `TRAWL_*` variables, add:

```markdown
| `TRAWL_CONTEXTUAL_RETRIEVAL` | `0` | Set to `1` to prepend deterministic page/section context to dense and BM25 retrieval inputs. Output chunks are unchanged. |
| `TRAWL_CONTEXT_PREFIX_MAX_CHARS` | `320` | Maximum characters of contextual prefix per chunk before the chunk body is appended. |
```

- [ ] **Step 2: Run focused tests**

Run:

```bash
pytest tests/test_contextual.py tests/test_retrieval_contextual.py tests/test_pipeline_contextual.py tests/test_retrieval_hybrid.py tests/test_telemetry.py -v
```

Expected: PASS.

- [ ] **Step 3: Run lint/format checks**

Run:

```bash
ruff format src tests
ruff check src tests
```

Expected: both commands complete successfully. If `ruff format` changes files, inspect `git diff` and include those formatting changes in the final commit.

- [ ] **Step 4: Run pipeline parity when services are available**

Run:

```bash
python tests/test_pipeline.py
```

Expected: PASS count must not be lower with `TRAWL_CONTEXTUAL_RETRIEVAL=1` than with the feature unset. If local embedding or reranker services are unavailable, record the failure reason in the final handoff.

- [ ] **Step 5: Compare contextual mode manually when services are available**

Run:

```bash
TRAWL_CONTEXTUAL_RETRIEVAL=1 python tests/test_pipeline.py
```

Expected: no contextual-specific flipped-to-fail cases. Record pass count, p95 latency if printed, and any flipped cases in the final handoff.

- [ ] **Step 6: Commit Task 5**

Run:

```bash
git add README.md src tests
git commit -m "docs: document contextual retrieval flag"
```

Expected: commit succeeds if README or formatting changed. If there are no changes after verification, skip this commit and note that documentation was already current.

---

## Self-Review Checklist

- Spec coverage:
  - Deterministic prefix module: Task 1.
  - Dense/BM25 contextual inputs: Task 2.
  - Pipeline full/profile wiring: Task 3.
  - Telemetry fields without raw text: Task 4.
  - Docs and measurement gate commands: Task 5.
- Type consistency:
  - `ContextualText.text`, `ContextualText.prefix_chars`, `ContextualTextBatch.texts`, `ContextualTextBatch.prefix_chars_total`, and `ContextualTextBatch.prefix_chars_avg` are introduced in Task 1 and reused unchanged.
  - `retrieval.retrieve` with `context_texts: list[str] | None = None` is introduced in Task 2 and used in Task 3.
  - `PipelineResult.contextual_retrieval_used`, `context_prefix_chars_total`, and `context_prefix_chars_avg` are introduced in Task 3 and used in Task 4.
- Scope:
  - No LLM contextualization.
  - No reranker document format changes.
  - No default-on behavior.
  - No embedding cache changes.
