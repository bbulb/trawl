# Contextual Retrieval Measurement - 2026-05-02

## Environment

- Branch: `develop`
- HEAD: `7699166 docs: add contextual retrieval handoff`
- Reference environment: `mamba run -n trawl ...`
- Reranking: enabled by default in both parity and agent-pattern runs
- Result directories:
  - Pipeline baseline: `tests/results/20260502-175930`
  - Pipeline contextual: `tests/results/20260502-180048`
  - Agent-pattern baseline: `tests/results/agent_patterns_20260502-090310Z`
  - Agent-pattern contextual: `tests/results/agent_patterns_20260502-090403Z`

## Commands

```bash
git status --short
mamba run -n trawl pytest tests/test_contextual.py \
  tests/test_retrieval_contextual.py \
  tests/test_pipeline_contextual.py \
  tests/test_retrieval_hybrid.py \
  tests/test_telemetry.py -q
mamba run -n trawl ruff check src tests

unset TRAWL_CONTEXTUAL_RETRIEVAL
mamba run -n trawl python tests/test_pipeline.py

TRAWL_CONTEXTUAL_RETRIEVAL=1 \
  mamba run -n trawl python tests/test_pipeline.py

unset TRAWL_CONTEXTUAL_RETRIEVAL
mamba run -n trawl python tests/test_agent_patterns.py --category code_heavy_query

TRAWL_CONTEXTUAL_RETRIEVAL=1 \
  mamba run -n trawl python tests/test_agent_patterns.py --category code_heavy_query

rm -f /tmp/trawl-contextual-telemetry.jsonl
TRAWL_TELEMETRY=1 \
TRAWL_TELEMETRY_PATH=/tmp/trawl-contextual-telemetry.jsonl \
TRAWL_CONTEXTUAL_RETRIEVAL=1 \
  mamba run -n trawl python tests/test_pipeline.py --only very_short_page
tail -n 3 /tmp/trawl-contextual-telemetry.jsonl
```

## Results

| runner | mode | pass | fail ids | latency p50/p95 | notes |
|---|---:|---:|---|---:|---|
| pipeline | baseline | 14/15 | `korean_wiki_person` | 4572ms / 15287ms | cold live fetches observed |
| pipeline | contextual | 14/15 | `korean_wiki_person` | 1459ms / 3457ms | warm cache effects observed |
| agent `code_heavy_query` | baseline | 18/21 | `coding/claude_code_python_asyncio_lookup`, `multimedia/hermes_youtube_3blue1brown_attention`, `wiki_reference/claude_code_wiki_en_transformer_arch` | 5459ms / 7782ms | `repeats=1` |
| agent `code_heavy_query` | contextual | 19/21 | `multimedia/hermes_youtube_3blue1brown_attention`, `wiki_reference/claude_code_wiki_en_transformer_arch` | 2146ms / 4197ms | `repeats=1` |

Latency caveat: baseline ran before contextual in both live comparisons. The contextual runs show many cache hits and much lower fetch time, so the p95 decrease is not strong evidence that contextual retrieval itself improves latency. It is enough to show no observed latency regression in this run, but a cache-controlled repeat is still needed before flipping defaults.

## Flips

- Pipeline flipped to pass: none
- Pipeline flipped to fail: none
- Agent-pattern flipped to pass:
  - `coding/claude_code_python_asyncio_lookup`
- Agent-pattern flipped to fail: none

The positive agent-pattern flip is a documented retrieval failure: baseline missed all of `asyncio.gather`, `TaskGroup`, and `create_task`; contextual retrieved relevant `asyncio.gather` / `TaskGroup` chunks and passed.

## Prefix Stats

Pipeline contextual run:

- `context_prefix_chars_total` sum: `61969`
- cases with non-zero prefix total: `15/15`
- average `context_prefix_chars_avg` across cases: `92.46`
- min/max `context_prefix_chars_avg`: `22.0` / `280.0`

Agent-pattern contextual run:

- `context_prefix_chars_total` sum: `319568`
- patterns with non-zero prefix total: `21/21`
- average `context_prefix_chars_avg` across patterns: `176.22`
- min/max `context_prefix_chars_avg`: `23.85` / `320.0`

## Telemetry Check

Telemetry sample path: `/tmp/trawl-contextual-telemetry.jsonl`

The sampled `very_short_page` telemetry record included:

- `contextual_retrieval_used: true`
- `context_prefix_chars_total: 68`
- `context_prefix_chars_avg: 68.0`

No raw chunk text, contextual input text, page text, or context prefix text was present in the telemetry record. Existing metadata fields such as URL, host, timing, counts, ranker names, and hashes were present.

## Gate

| Metric | Gate | Result |
|---|---|---|
| parity flipped_to_fail | `0` | pass: `0` |
| query-heavy / agent-pattern net assertion delta | `>= +1`, or one documented retrieval failure flips to pass | pass: `+1`, `coding/claude_code_python_asyncio_lookup` |
| latency p95 increase | `<= +20%` | pass in observed run, but cache-confounded |
| telemetry privacy | no raw context/chunk text | pass |
| focused tests | pass | pass: `37 passed in 0.74s` |
| ruff check | pass | pass |

## Decision

Decision: adopt as a candidate for an `auto` or default-on design spec, but do not flip the runtime default in this measurement note.

Rationale: contextual retrieval produced no flipped failures, fixed one query-heavy retrieval assertion, and preserved telemetry privacy. The only weak part of the gate is latency evidence: the observed p95 moved in the right direction, but the live run order made it cache-confounded. That should shape the next spec and rollout plan rather than block the candidate entirely.

## Follow-Up

1. Write a design spec for `TRAWL_CONTEXTUAL_RETRIEVAL=auto` or default-on rollout.
2. Include a cache-controlled latency plan in that spec:
   - randomized run order, or cold/warm pairs for both modes
   - `--repeats` greater than `1` for agent patterns
   - separate fetch, retrieval, rerank, and total p95 reporting
3. Include contextual embedding cache keying in the design:
   - URL
   - markdown hash
   - embedding model
   - chunker version
   - contextual mode
   - prefix max chars / prefix version
