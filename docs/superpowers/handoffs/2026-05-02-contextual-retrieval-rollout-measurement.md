# Contextual Retrieval Rollout Measurement - 2026-05-02

## Environment

- Branch: `develop`
- HEAD: `aac03bc docs: document embedding cache settings`
- Reranking: enabled by default
- Fetch cache: `TRAWL_FETCH_CACHE_TTL=600`
- Embedding cache: disabled for baseline/auto/forced-on comparisons with `TRAWL_EMBED_CACHE_TTL=0`; enabled only for the smoke run with `TRAWL_EMBED_CACHE_TTL=600`

## Focused Verification

- `mamba run -n trawl pytest tests/test_contextual.py tests/test_contextual_auto.py tests/test_retrieval_contextual.py tests/test_retrieval_embedding_cache.py tests/test_pipeline_contextual.py tests/test_retrieval_hybrid.py tests/test_telemetry.py -q`
  - Result: `51 passed in 0.76s`
- `mamba run -n trawl ruff check src tests`
  - Result: `All checks passed!`

## Pipeline Results

| mode | pass | fail ids | latency p50/p95 | fetch p50/p95 | retrieval p50/p95 | rerank p50/p95 | result dir | notes |
|---|---:|---|---:|---:|---:|---:|---|---|
| baseline | 13/15 | `korean_wiki_person`, `hada_news` | 4312/7659ms | 1873/4773ms | 401/1932ms | 317/681ms | `tests/results/20260502-222925` | warmup result: `tests/results/20260502-222918` |
| auto | 13/15 | `korean_wiki_person`, `hada_news` | 1603/3508ms | 0/0ms | 384/2503ms | 342/663ms | `tests/results/20260502-223036` | no pipeline flipped-to-fail vs current baseline |
| forced-on | 13/15 | `korean_wiki_person`, `hada_news` | 1668/3360ms | 0/0ms | 433/2486ms | 329/665ms | `tests/results/20260502-223105` | no pipeline flipped-to-fail vs current baseline |

## Agent Pattern Results

| mode | pass | fail ids | total p50/p95 | result dir | notes |
|---|---:|---|---:|---|---|
| baseline | 17/21 | `claude_code_python_asyncio_lookup`, `claude_code_postgres_jsonb_indexes`, `hermes_youtube_3blue1brown_attention`, `claude_code_wiki_en_transformer_arch` | 7621/12165ms | `tests/results/agent_patterns_20260502-133449Z` | asyncio lookup failed |
| auto | 19/21 | `hermes_youtube_3blue1brown_attention`, `claude_code_wiki_en_transformer_arch` | 2153/4231ms | `tests/results/agent_patterns_20260502-133719Z` | asyncio lookup fixed; postgres budget failure cleared |
| forced-on | 19/21 | `hermes_youtube_3blue1brown_attention`, `claude_code_wiki_en_transformer_arch` | 2143/4318ms | `tests/results/agent_patterns_20260502-133948Z` | same pass/fail shape as auto |

## Flips

- flipped_to_pass: `coding/claude_code_python_asyncio_lookup`, `coding/claude_code_postgres_jsonb_indexes` for auto and forced-on vs current baseline.
- flipped_to_fail: none observed in pipeline or agent-pattern comparisons.

## Embedding Cache Smoke

- Settings: `TRAWL_FETCH_CACHE_TTL=600`, `TRAWL_EMBED_CACHE_TTL=600`, `TRAWL_EMBED_CACHE_PATH=/tmp/trawl-embed-cache-measure`, `TRAWL_CONTEXTUAL_RETRIEVAL=auto`.
- First `english_tech_docs` run: PASS, total 3597ms, fetch 2606ms, retrieval 277ms, rerank 272ms, result dir `tests/results/20260502-223957`.
- Second `english_tech_docs` run: PASS, total 346ms, fetch 0ms, retrieval 36ms, rerank 265ms, result dir `tests/results/20260502-224001`.
- Behavior was preserved and document retrieval work dropped on the repeated run.

## Decision

- default: `off`
- recommendation: keep runtime default off for now; document `TRAWL_CONTEXTUAL_RETRIEVAL=auto` as the preferred opt-in for code-heavy or large-page retrieval experiments.
- rationale: auto produced no observed flipped failures and improved agent-pattern pass count from `17/21` to `19/21`, including the known asyncio retrieval case. However, with embedding cache disabled, pipeline retrieval p95 increased from 1932ms to 2503ms for auto, a `+29.6%` change that misses the `<= +20%` default gate. Total p95 decreased in this paired run because baseline still paid fetch costs while auto/forced-on hit a warmer fetch cache, so total latency is not enough to justify a default flip by itself.

## Follow-Up

- Add embedding cache hit/miss telemetry before relying on cache-on live numbers.
- Re-run a stricter paired measurement that alternates baseline and auto after all fetches are warm.
- Reconsider default `auto` only if retrieval p95 stays within the `<= +20%` gate or cache hit telemetry proves repeated-query behavior covers the added document embedding cost.
