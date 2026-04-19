# C5 Hierarchical Section Fetch — Spike Conclusion

**Date:** 2026-04-20
**Branch:** `spike/c5-premise-measurement` (off
`feat/c6-hybrid-retrieval` at `faa0b43`)
**Design:**
[2026-04-20-c5-hierarchical-fetch-design.md](2026-04-20-c5-hierarchical-fetch-design.md)
**Measurement:**
[benchmarks/results/c5-premise/2026-04-19T22-34-21Z/](../../../benchmarks/results/c5-premise/2026-04-19T22-34-21Z/)
**Status:** Complete — **verdict: defer C5 as scoped; file a narrower
follow-up on longform retrieval cost**

## TL;DR

- **C5 as scoped in `notes/RESEARCH.md` (NestBrowse-style hierarchical
  section fetch for 1M-token-scale pages) is not justified by the
  current workload.** The max page in 29 runs across parity +
  benchmark + stress is **269,012 chars (≈ 67k tokens)**, and
  `page_chars.p95 = 157k` and `n_chunks_total.p95 = 277` are both
  below the pre-registered adopt thresholds.
- **The only threshold crossed is `retrieval_ms.p95 = 5057 ms` vs 1000
  ms.** Root cause is embedding cost scaling linearly with
  `n_chunks_total`, not page size per se. The cases that drive it are
  Wikipedia longform and arXiv PDFs — hosts with dedicated fetchers
  that already produce a reasonable subtree.
- **The pre-registered `adopt_narrow` branch therefore triggers, but
  the adopt work is *not* C5.** It is a separate, smaller feature:
  longform retrieval-cost mitigation via chunk budget, embedding
  throughput, or host-specific auto-subtree in the existing fetchers.

## Measurement summary

Dataset: 12 parity cases + 12 benchmark cases + 3 stress-tail
Wikipedia / Python-docs pages. 29/30 succeeded (Reuters 403, excluded
from percentiles). Single pass, single-process, no profile generation
(only 2 URLs in the cache had pre-existing profiles).

### Overall percentiles

| metric | n | p50 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|
| page_chars | 29 | 18,890 | **157,205** | 246,567 | 269,012 |
| n_chunks_total | 29 | 55 | **277** | 486 | 563 |
| fetch_ms | 29 | 2,096 | 4,606 | 4,832 | 4,871 |
| chunk_ms | 29 | 1 | 4 | 5 | 6 |
| retrieval_ms | 29 | 1,002 | **5,057** | 5,850 | 6,145 |
| rerank_ms | 29 | 412 | 578 | 917 | 1,035 |
| total_ms | 29 | 4,774 | 8,663 | 9,328 | 9,492 |

### Pre-registered thresholds (from design doc)

| threshold | measured | crossed? |
|---|---:|---:|
| `page_chars.p95 >= 200,000` | 157,205 | **no** |
| `n_chunks_total.p95 >= 500` | 277 | **no** |
| `retrieval_ms.p95 >= 1,000` | 5,057 | **yes** |

Per the rule: one threshold crossed → `adopt_narrow`.

### What drives the crossed threshold

Cases with `retrieval_ms > 2000 ms`:

| case | chunks | page_chars | retrieval_ms |
|---|---:|---:|---:|
| wiki_history_of_the_internet | 563 | 188,850 | 6,145 |
| arxiv_pdf | 261 | 109,737 | 5,091 |
| wiki_llm | 288 | 109,483 | 5,006 |
| korean_wiki_person | 190 | 61,245 | 3,793 |
| wiki_list_countries | 168 | 64,199 | 2,993 |
| japanese_wiki | 152 | 49,165 | 2,471 |
| stackoverflow_question | 128 | 44,031 | 2,178 |
| korean_news_ranking | 83 | 22,029 | 2,101 |

Fit is roughly linear: `retrieval_ms ≈ 10–12 ms × n_chunks_total`.
This matches what you would expect from batched bge-m3 cosine — the
embedding server is the bottleneck, not the client.

Per-category view confirms the pattern: `wiki` p95 = 4,807 ms and
`stress_wiki_longform` = 6,145 ms dominate, while every non-wiki
category sits at or below 2,200 ms p95.

## Why this does not justify C5

The C5 proposal in `notes/RESEARCH.md` §C5 frames the problem as
"관련 섹션만 끌어오고 나머지는 skip" — i.e., fetch-time selection of a
subtree. Three reasons this is a poor fit for the measured pain:

1. **Not a page-size problem.** The workload envelope tops out at
   269k chars / 563 chunks. A "hierarchical section fetch" aimed at
   1M-token pages is solving a problem that is not in the data.
2. **Duplicates the profile fast path.** `profiles/mapper.py` already
   does DOM-LCA subtree selection when a profile exists. The slow
   cases above (Wikipedia, arXiv) all have dedicated fetchers
   (`fetchers/wikipedia.py`, `fetchers/pdf.py`) that already own the
   responsibility of producing the subtree. Adding a second subtree
   mechanism at chunk time would be two systems disagreeing.
3. **Retrieval cost is downstream of chunk count, not subtree size.**
   Even if we halve page_chars by selecting a subtree, if the same
   number of chunks survive (because each is ≤ 450 chars by design),
   retrieval cost is unchanged. The pain is the embedding stage, not
   the chunker.

## What the `adopt_narrow` follow-up *should* be

Not C5. A separate, smaller issue tracked explicitly as "longform
retrieval cost". Three concrete options, in increasing scope:

1. **Chunk budget with heading-based prefilter** (smallest). Cap the
   pool sent to bge-m3 at N (~150). When the raw chunk pool exceeds
   N, drop chunks whose heading-path sim to the query (using a cheap
   lexical or BM25 score — C6 now provides one) is bottom-quartile.
   Expected win: wiki longform `retrieval_ms` from 5s → 1.5s with no
   rank-1 regression (needs verification).
2. **Embedding throughput** (small-medium). Increase `EMBEDDING_BATCH`
   from 64 to 128 or 256, conditional on llama-server
   `--ubatch-size`. Tests show the embed server is the bottleneck;
   fewer round trips means less latency even at the same total
   compute. Gated by `CLAUDE.md` "Things NOT to change" — needs a
   parity matrix run before / after.
3. **Auto-subtree for host fetchers** (medium). In
   `fetchers/wikipedia.py` / `fetchers/pdf.py`, produce a subtree
   selection (e.g., `.mw-parser-output` minus `.navbox`, `.reflist`)
   by default without a VLM profile. Makes profile coverage for these
   hosts a fetcher-level default, not a user-level opt-in. Reduces
   both page_chars and n_chunks_total at the source.

Of these, (1) has the best effort/impact ratio: reuses the BM25
scorer that already landed in C6, fits inside
`retrieval.py:adaptive_k` style of tuning, and does not touch
fetchers. It should be spec'd separately, not under the C5 banner.

## Decision

- **C5 status:** `deferred`. Re-open only if future telemetry shows
  `page_chars.p95 ≥ 500k` or `n_chunks_total.p95 ≥ 800` across
  realistic workloads — neither condition holds today.
- **Follow-up work item filed:** "longform retrieval cost" (working
  title) — to be scoped as its own spike/plan starting from option
  (1) above. Not bundled into this PR.
- **Update `notes/RESEARCH.md` §C5** to `status: deferred
  (2026-04-20)` with a one-line pointer to this conclusion doc.

## Files changed by this spike

- `docs/superpowers/specs/2026-04-20-c5-hierarchical-fetch-design.md`
- `docs/superpowers/specs/2026-04-20-c5-hierarchical-fetch-conclusion.md`
- `benchmarks/c5_page_size_measure.py`
- `benchmarks/results/c5-premise/<ts>/report.md` + `summary.json`
  (summary files only; raw `telemetry.jsonl` is gitignored via
  `benchmarks/results/`)
- `notes/RESEARCH.md` — status update for C5

No `src/trawl/` or `src/trawl_mcp/` changes. Parity matrix was not
re-run because nothing pipeline-adjacent changed.
