# Contextual Retrieval Rollout Design

Date: 2026-05-02

## Goal

Introduce a safe rollout path for deterministic contextual retrieval after the initial measurement showed no flipped failures and one query-heavy flipped-to-pass case.

## Current Evidence

- Pipeline parity: baseline/contextual both `14/15`, failing `korean_wiki_person`.
- Agent `code_heavy_query`: `18/21 -> 19/21`.
- Flipped-to-pass: `coding/claude_code_python_asyncio_lookup`.
- Flipped-to-fail: none.
- Telemetry privacy: no raw context/chunk text.
- Latency caveat: previous baseline/contextual order was cache-confounded.

## Mode Semantics

`TRAWL_CONTEXTUAL_RETRIEVAL` accepts:

- `0`, `false`, `no`, unset: disabled.
- `1`, `true`, `yes`: enabled for every retrieval path that already supports contextual ranking inputs.
- `auto`: enabled only for likely-beneficial retrieval cases.

Initial `auto` policy:

- Enable for identifier/code-heavy queries.
- Enable for large pages where `len(chunks) >= 16`.
- Enable for structured/repeated chunks where any chunk has `record_group_id`.
- Disable for tiny pages where `len(chunks) <= 2`.
- Disable when `TRAWL_CONTEXT_PREFIX_MAX_CHARS=0`.

The policy is intentionally conservative and deterministic. It does not inspect raw page text beyond already-available chunk metadata and query string.

## Embedding Cache

Add a document embedding cache below `retrieval.retrieve()`.

The first implementation caches only document/chunk embeddings, not query embeddings, because query text and HyDE inputs vary per call while document inputs are stable across repeated page visits.

Cache key fields:

- schema version
- embedding model
- embedding endpoint base URL
- input text SHA-256
- contextual mode resolved for this retrieval: `off`, `on`, or `auto`
- contextual prefix max chars
- contextual prefix version

Environment:

- `TRAWL_EMBED_CACHE_TTL`: seconds, default `0` to keep cache disabled until measured.
- `TRAWL_EMBED_CACHE_PATH`: default `~/.cache/trawl/embeddings`.
- `TRAWL_EMBED_CACHE_MAX_MB`: default `512`.
- `TRAWL_CONTEXT_PREFIX_VERSION`: default `deterministic-v1`.

## Measurement Plan

Run cache-controlled comparisons before changing defaults:

1. Clear fetch cache or run both modes in paired warm-cache order.
2. Run baseline/contextual/auto with the same warmed fetch cache.
3. Use `--repeats 3` for agent-pattern subsets.
4. Report fetch, retrieval, rerank, and total p50/p95 separately.
5. Report embedding cache hit/miss counts when cache is enabled.

## Default Decision Gate

Default-on or `auto` is acceptable only if:

- Pipeline flipped-to-fail remains `0`.
- Agent-pattern net assertion delta remains `>= +1`, or the known asyncio retrieval failure remains fixed.
- Cache-controlled total p95 increase is `<= +20%`.
- Retrieval p95 increase is `<= +20%` when embedding cache is disabled.
- Telemetry still records no raw context/chunk text.
- Focused tests and Ruff pass.

## Rollout Decision

If the gate passes with `auto`, update docs to recommend `TRAWL_CONTEXTUAL_RETRIEVAL=auto`.
If the gate passes with full `on`, consider making `auto` the default and keeping `1` as forced-on.
If latency remains unclear, keep default off and enable embedding cache experiments first.
