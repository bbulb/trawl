# Qwen3-Embedding-0.6B-GGUF drop-in A/B — design (2026-05-18)

Branch: `spike/qwen3-embedding-swap` (off `develop @ 973b023` post-
parity restore + Spike B + roadmap status + agent_patterns drift
refresh, 2026-05-19).

Pre-registered roadmap step #2 in
`docs/superpowers/plans/2026-05-18-trawl-improvement-roadmap.md`.

## Outcome (2026-05-19): REJECT

Measurement complete; 4 of 5 gates fail. BGE-M3 stays as default.
Full breakdown lives in `notes/qwen3-embedding-swap-outcome.md`
(gitignored). Gate table:

| Gate | Result | Notes |
|---|---|---|
| 1. parity 15/15 | **FAIL** 14/15 | `korean_wiki_person` regressed |
| 2. coding 24/24 | **FAIL** 23/24 | `claude_code_arxiv_abs_attention` regressed |
| 3. reader-comp Δ ≥ +1 | PASS Δ=+2 (4→6) | flipped `github_fastapi_readme`, `wiki_large_language_model` to pass |
| 4. Korean 3/3 | **FAIL** 2/3 | `korean_wiki_person`; project-differentiator gate |
| 5. retrieval p95 ≤ baseline ×1.2 | **FAIL** +89.9 % (1837 → 3489 ms) | side-by-side serve, same llama.cpp build |

Decision matrix entry "Korean regression (gate 4) → REJECT
immediately" applied. No env default change, GGUF left in
`~/models/qwen3-embedding/` for future retry.

## Hypothesis

Qwen3-Embedding-0.6B-GGUF served on `llama-server :8081` can replace
the current BGE-M3 dense embedding as the default retrieval backend
without parity regression, without coding-shard regression, with
no Korean-case regression, and with retrieval p95 within +20 % of
the BGE-M3 baseline (cache-disabled).

If the gate holds the swap can be made the default via a one-line
env-default change. If any gate misses the experiment ships as
"design doc + outcome note", keeping BGE-M3 as default.

## Why Qwen3-Embedding

- Same dense-embedding shape as BGE-M3 (1024-d vector, last-token
  pooling), so `_embed_batch` / `_embed_documents_with_cache` work
  unchanged.
- Newer training data (2025) and reported improvements on retrieval
  benchmarks at the 0.6 B-parameter tier.
- `gguf` quantisation matches the existing llama-server runtime —
  no new infrastructure component.
- Cache key already partitions on `model`, so the swap automatically
  invalidates BGE-M3 entries without manual eviction.

Model card: <https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF>

## External prerequisites (user — not in scope of any commit)

1. **Download** the GGUF (Q8_0 recommended for accuracy parity):

   ```bash
   mkdir -p ~/models/qwen3-embedding
   curl -L -o ~/models/qwen3-embedding/qwen3-embedding-0.6b-q8_0.gguf \
     https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF/resolve/main/qwen3-embedding-0.6b-q8_0.gguf
   ```

2. **Stop** the current bge-m3 server on `:8081` OR start Qwen3 on a
   distinct port and reconfigure trawl's `TRAWL_EMBED_URL`. Two
   patterns work:

   - **In-place swap** (simplest, what most gates assume):

     ```bash
     llama-server \
       --model ~/models/qwen3-embedding/qwen3-embedding-0.6b-q8_0.gguf \
       --embedding --pooling last \
       --port 8081 --ubatch-size 2048
     ```

   - **Side-by-side** (lets you flip env between baseline and
     experiment without restarting):

     ```bash
     # keep bge-m3 on 8081
     llama-server --model bge-m3.gguf --embedding --pooling last --port 8081 --ubatch-size 2048

     # add qwen3 on 8085 (or any free port)
     llama-server --model qwen3-embedding-0.6b-q8_0.gguf \
       --embedding --pooling last \
       --port 8085 --ubatch-size 2048
     ```

3. **Smoke test** the endpoint before any measurement run:

   ```bash
   curl -s http://localhost:8081/embeddings \
     -H 'Content-Type: application/json' \
     -d '{"input":"hello","model":"qwen3-embedding"}' | head -c 200
   ```

   Confirm a vector comes back. Note the `model` string the server
   reports — that's the value to pass via `TRAWL_EMBED_MODEL`.

## Env override matrix (no code change)

| Variable | Baseline (BGE-M3) | Experiment (Qwen3) |
|---|---|---|
| `TRAWL_EMBED_URL` | unset (default `http://localhost:8081/v1`) | depends on serving pattern above |
| `TRAWL_EMBED_MODEL` | unset (default `bge-m3`) | model id the server reports, e.g. `qwen3-embedding` |
| `TRAWL_EMBED_CACHE_TTL` | `0` during the run (so cold metrics aren't cache-contaminated) | `0` during the run |
| `TRAWL_EMBED_CACHE_PATH` | per-run isolated tmp dir | per-run isolated tmp dir |
| `TRAWL_RERANK_URL` | unchanged (`:8083`, optional) | unchanged |

Setting `TRAWL_EMBED_CACHE_TTL=0` for both runs is **required** so the
warm-repeat measurement from Spike B does not skew the comparison.
The cache key includes `model`, so even with TTL > 0 a Qwen3 query
would miss the BGE-M3 entry — but the cleanest A/B keeps cache out
of the picture entirely.

## Pre-registered gates

All five required. Any miss = REJECT, ship design + outcome note,
revert env.

| Gate | Required | Source |
|---|---|---|
| 1. `python tests/test_pipeline.py` | 15/15 | full parity matrix |
| 2. `python tests/test_agent_patterns.py --shard coding` | ≥ 24/24 (current baseline) | coding usage shard |
| 3. reader-comparison net assertion delta | ≥ +1 vs BGE-M3 baseline; flipped-to-fail = 0 | `benchmarks/reader_comparison.py` |
| 4. Korean cases (`pricing_page_ko`, `korean_wiki_person`, `korean_news_ranking`) | regression = 0 | subset of (1) |
| 5. retrieval p95 with cache off | within BGE-M3 baseline +20 % | per-mode aggregate over reader-comparison |

A "regression" on gates (1)/(2)/(4) means any case that was passing
under BGE-M3 starts failing under Qwen3. Korean is called out
separately because BGE-M3's multilingual advantage is the strongest
known reason Qwen3 might lose ground.

## Measurement plan

`benchmarks/qwen3_embedding_swap.py` (added by the prep PR) runs
both runs back-to-back when invoked with `--baseline` or
`--experiment`. Each run produces a JSON summary plus the raw
artefacts under `benchmarks/results/qwen3-embedding-swap/<ts>/`.

```bash
# 1. Cold baseline — point at the BGE-M3 server
TRAWL_EMBED_MODEL=bge-m3 \
  mamba run -n trawl python benchmarks/qwen3_embedding_swap.py \
    --label baseline --out benchmarks/results/qwen3-embedding-swap/$(date -u +%Y%m%d-%H%M%SZ)-baseline

# 2. Swap the server (see "External prerequisites" above).

# 3. Experiment run — point at Qwen3
TRAWL_EMBED_MODEL=qwen3-embedding \
  mamba run -n trawl python benchmarks/qwen3_embedding_swap.py \
    --label experiment --out benchmarks/results/qwen3-embedding-swap/$(date -u +%Y%m%d-%H%M%SZ)-experiment

# 4. Compare
mamba run -n trawl python benchmarks/qwen3_embedding_swap.py --compare \
    benchmarks/results/qwen3-embedding-swap/*-baseline \
    benchmarks/results/qwen3-embedding-swap/*-experiment
```

The script wraps the three existing harnesses (parity matrix,
coding agent-patterns shard, reader-comparison) plus a small
retrieval-only p95 sample so all five gates can be evaluated from
the same artefact pair without manual stitching.

## Decision matrix

| Outcome | Action |
|---|---|
| All five gates pass | Single-line env default change (`DEFAULT_EMBEDDING_MODEL` in `src/trawl/retrieval.py`) + CLAUDE.md / CHANGELOG / README update. Cache TTL stays default-on (3600 s); the model field in the cache key partitions cleanly. |
| Korean regression (gate 4) | **REJECT immediately** — Korean multilingual coverage is a project differentiator. Outcome note documents which Korean case(s) regressed and why. |
| Coding regression (gate 2) | REJECT. Note the failing patterns; consider whether they're identifier-heavy English (Qwen3 might still win an English-only subset). |
| Reader-comparison net = 0 / negative (gate 3) | REJECT. Net delta needs to clear ≥ +1 — equal-quality swap is not a reason to change defaults. |
| Retrieval p95 > BGE-M3 +20 % (gate 5) | REJECT. Latency is a project constraint. |
| Parity regression (gate 1) | REJECT. 15/15 is the parity floor. |

## Files touched (planned)

When measurement starts on the spike branch:

- `src/trawl/retrieval.py` — single-line default model change (only if all gates pass).
- `CLAUDE.md` — Current status + Critical rules llama-server endpoint map (only on adoption).
- `README.md` — env table model default (only on adoption).
- `CHANGELOG.md` — `[Unreleased]` Changed entry (only on adoption).
- `docs/superpowers/specs/2026-05-18-qwen3-embedding-swap-design.md` — this file (already committed by prep PR).
- `benchmarks/qwen3_embedding_swap.py` — helper script (already committed by prep PR).
- `notes/qwen3-embedding-swap-outcome.md` — measurement record + decision (always, gitignored).

## Risk

- **Korean coverage** — BGE-M3 was trained heavily on multilingual
  data with strong Korean retrieval; Qwen3-Embedding is newer but
  Korean parity is not certain. Gate 4 is the primary stopgap.
- **Pooling semantics** — Qwen3-Embedding documentation specifies
  last-token pooling (`--pooling last`). Pooling mismatch produces
  garbage vectors silently. The smoke-test step exists for this.
- **Cache key partitioning** — already validated by Spike B's design.
  Model swap creates a fresh cache namespace, no manual cleanup
  needed. Old BGE-M3 entries remain on disk until LRU trim.
- **Reranker compatibility** — `:8083` reranker is downstream of
  retrieval and takes plain text, not embeddings. Reranker is
  untouched by this spike.
- **Disk usage** — TTL=0 during measurement means cache is bypassed.
  After adoption with TTL=3600 default, fresh Qwen3 entries grow
  the cache the same way BGE-M3 entries do. Already capped by
  `TRAWL_EMBED_CACHE_MAX_MB=512` (LRU trim with 20 % headroom).

## Timing

- External prereq (download + serve): user-side, ~10-20 min depending
  on bandwidth and existing llama-server config.
- Design doc: this file, committed in prep PR.
- Helper script: `benchmarks/qwen3_embedding_swap.py`, committed in
  prep PR.
- Measurement runs (baseline + experiment): ~10-15 min each on the
  current local llama-server (parity ~3 min, coding shard ~3 min,
  reader-comparison ~3 min).
- Decision + adoption commit: ~15 min if all gates pass.
- Outcome note: ~10 min.

Total spike session length: ~1 hour after prereq.

## Reference

- Roadmap: `docs/superpowers/plans/2026-05-18-trawl-improvement-roadmap.md` § 2
- Spike pattern (matches discipline): `docs/superpowers/specs/2026-04-22-chunk-budget-default-on-design.md` (PR #46), `docs/superpowers/specs/2026-05-18-embed-cache-default-on-design.md` (PR #48)
- Model card: <https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF>
- BGE-M3 baseline retrieval p95 (cache off):
  `benchmarks/results/reader-comparison/retrieval-modes-cache-2026-05-04/dense/` cold p95 ≈ 1967 ms across 12 rows.
