# Contextual Retrieval Prefix Design

Date: 2026-05-02

## Goal

Improve retrieval quality by adding short deterministic context to each chunk before
embedding and lexical ranking. The returned chunk text and public payload shape stay
unchanged; context is only an internal ranking signal.

Primary success criterion is quality. Latency p95 may increase up to 20% if pass rate
or assertion coverage improves.

## Background

The current retrieval path embeds each chunk as `heading + "\n\n" + embed_text` when a
heading exists. This gives local section context but loses broader page context such as
page title, nearby sections, chunk position, and repeated-record metadata.

Related work on contextual retrieval shows that adding short chunk-specific context
before embedding and BM25 indexing can reduce retrieval failures. A full LLM-generated
contextualization step is not appropriate for the first implementation because it adds
cost, latency, nondeterminism, and caching requirements. This design starts with a
deterministic prefix built from metadata that trawl already has.

## Non-Goals

- Do not change `Chunk.text`, MCP response chunk text, `output_chars`, or token budget
  accounting.
- Do not call an LLM to generate context.
- Do not implement late chunking or a new embedding backend.
- Do not change reranker document construction in the first pass; reranker already
  injects title and section.
- Do not flip the feature on by default until measurement gates pass.

## Approach

Add a small contextualization module that builds ranking-only strings:

```text
Title: <page title>
Section: <heading path>
Position: chunk 4 of 37
Record: item 2 in repeated group 1
Nearby sections: Previous heading | Next heading

<chunk embed_text>
```

Each line is included only when the corresponding metadata is available. The prefix is
bounded by `TRAWL_CONTEXT_PREFIX_MAX_CHARS`, default `320`, before the chunk body is
appended. The body remains `chunk.embed_text or chunk.text`.

The feature is guarded by `TRAWL_CONTEXTUAL_RETRIEVAL`:

- `0` or unset: current behavior.
- `1`: dense embedding inputs and BM25 ranking inputs use contextual text.

## Architecture

### `src/trawl/contextual.py`

New pure-function module responsible for prefix construction.

Public helpers:

- `is_enabled() -> bool`
- `max_prefix_chars() -> int`
- `build_contextual_text(chunk, *, page_title, previous_heading, next_heading, total_chunks) -> ContextualText`
- `build_contextual_texts(chunks, *, page_title) -> ContextualTextBatch`

`ContextualText` contains:

- `text`: prefix plus body, used for retrieval.
- `prefix_chars`: length of the generated prefix.

`ContextualTextBatch` contains:

- `texts`: list of ranking input strings, aligned with chunks.
- `prefix_chars_total`
- `prefix_chars_avg`

### `src/trawl/retrieval.py`

Extend `retrieve()` with optional contextual inputs rather than making it import page
metadata itself:

```python
def retrieve(..., context_texts: list[str] | None = None, ...)
```

When `context_texts` is provided, it must be length-aligned with `chunks` and is used
for both:

- BM25 prefilter / hybrid sparse ranking.
- Dense document embedding.

When omitted, current `heading + embed_text` construction is unchanged.

### `src/trawl/pipeline.py`

Full-page and profile retrieval paths build contextual texts when
`TRAWL_CONTEXTUAL_RETRIEVAL=1`, then pass them into `retrieval.retrieve()`.

The pipeline already knows `page_title` and the ordered `chunks`, so it is the right
boundary for contextualization. Direct profile paths that return all chunks without
retrieval do not need contextual texts.

### `src/trawl/telemetry.py`

Add contextual retrieval fields to telemetry events:

- `contextual_retrieval_used`
- `context_prefix_chars_total`
- `context_prefix_chars_avg`

These fields are diagnostic only and must not include raw chunk text.

## Data Flow

Full retrieval path:

```text
fetch -> extract -> chunk -> optional contextual texts -> retrieve -> optional rerank -> payload
```

Profile retrieval path:

```text
profile subtree -> extract -> chunk -> optional contextual texts -> retrieve -> optional rerank -> payload
```

Direct paths:

```text
passthrough/profile_direct/error -> no contextual text construction
```

## Error Handling

- Malformed `TRAWL_CONTEXT_PREFIX_MAX_CHARS` falls back to `320`.
- Values below `0` are treated as `0`, which disables prefix text but still returns
  the body.
- If `context_texts` length differs from `chunks`, `retrieval.retrieve()` returns a
  `RetrievalResult` error rather than silently misaligning rankings.
- Prefix construction never raises for missing title, heading, record metadata, or
  char spans.

## Testing

Unit tests:

- Prefix includes title, section, position, nearby headings, and record metadata when
  present.
- Missing metadata is omitted cleanly.
- Prefix length is capped by `TRAWL_CONTEXT_PREFIX_MAX_CHARS`.
- Contextual text keeps the original body intact.
- `retrieve(context_texts=...)` uses provided strings for BM25/dense input.
- Misaligned `context_texts` returns a retrieval error.
- Telemetry emits contextual flags and prefix length stats without raw text.

Regression tests:

- `pytest tests/test_contextual.py tests/test_retrieval_contextual.py`
- `pytest tests/test_retrieval_hybrid.py tests/test_telemetry.py`
- `python tests/test_pipeline.py`

If local embedding/reranker services are available, run an agent-pattern or query-heavy
benchmark subset with the feature enabled and disabled.

## Measurement Gate

Adopt the feature only if:

- `tests/test_pipeline.py` has no contextual-specific flipped-to-fail cases.
- Query-heavy or agent-pattern benchmark shows net assertion delta `>= +1`, or a
  documented retrieval failure flips to pass with no failures.
- Latency p95 increase is `<= +20%`.
- Telemetry confirms average prefix size remains bounded and no raw text is recorded.

If gates do not pass, keep the code behind the flag and leave default off.

## Rollout

Initial rollout keeps `TRAWL_CONTEXTUAL_RETRIEVAL` default off. After measurement
passes, a follow-up decision can flip the default or use an `auto` mode for selected
query/page types.

## Open Follow-Ups

- Evaluate whether reranker should receive deterministic context after first-stage
  retrieval measurements are complete.
- Evaluate an embedding cache keyed by contextual mode and prefix version, because
  contextual texts change the document embedding inputs.
- Consider an LLM-generated contextualization experiment only after deterministic
  context has a measured baseline.
