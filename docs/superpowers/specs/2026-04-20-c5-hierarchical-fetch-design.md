# C5 Hierarchical Section Fetch — Spike Design

**Date:** 2026-04-20
**Branch:** `spike/c5-premise-measurement` (off
`feat/c6-hybrid-retrieval` at `faa0b43`)
**Type:** Premise-verification spike (no `src/trawl/` changes)
**Status:** Draft

## Goal

Verify whether the current pipeline hits the scale problem that C5
(NestBrowse-style section-level lazy fetch) is designed to solve.
Collect `page_chars`, `n_chunks_total`, and per-stage timing across the
parity matrix, benchmark matrix, and a handful of known long-form
stress URLs. Decide adopt / defer / narrow on pre-registered
thresholds.

Expected outcome: either a numbers-backed "defer" conclusion (profile
LCA + records chunking already covers the envelope we actually fetch),
or a narrow adopt plan scoped to the specific hosts / categories where
page size is genuinely pathological.

## Why

`notes/RESEARCH.md` §C5 explicitly gates the feature on this premise:

> "현재 파이프라인에서 '1M 토큰 급'이 실제로 문제인지 로그/벤치로 먼저
> 확인. 문제 없으면 defer."

Two unknowns block a C5 plan:

1. **Is there a real size problem to solve?** Without telemetry
   history (`~/.cache/trawl/telemetry.jsonl` is empty), we do not know
   the `page_chars` / `n_chunks_total` distribution of pages trawl
   actually handles. A feature aimed at "1M-token pages" is premature
   if the p99 page is 80k chars.
2. **If there is, does the profile system already cover it?**
   `src/trawl/profiles/mapper.py:313–392` performs DOM LCA subtree
   selection — which is structurally the same idea as C5's "section
   subset", just at fetch time rather than post-chunk time. Any C5
   proposal must demonstrate it catches cases the profile fast path
   misses (profile-less pages, pages where the LCA subtree is still
   large).

The spike answers both with numbers, not arguments.

## Scope

**In scope:**

- New script `benchmarks/c5_page_size_measure.py` that:
  - Runs the 12 parity cases (`tests/test_cases.yaml`) and the 12
    benchmark cases (`benchmarks/benchmark_cases.yaml`) through
    `fetch_relevant()` with `TRAWL_TELEMETRY=1` and an isolated
    `TRAWL_TELEMETRY_PATH` so it does not pollute the user's cache.
  - Appends a short "stress" list of known long pages (Wikipedia
    mega-pages, full-module docs) — 3-5 URLs — to probe the tail
    explicitly.
  - Aggregates the resulting `telemetry.jsonl` into a summary JSON
    with p50/p95/p99 of `page_chars`, `n_chunks_total`,
    `retrieval_ms`, `chunk_ms`, and per-category / per-host
    breakdowns. Saves to `tests/results/c5-premise/<ts>/`.
- Design + conclusion docs (`docs/superpowers/specs/2026-04-20-c5-*`).

**Out of scope:**

- Implementation of hierarchical fetch itself. That comes only if the
  spike says adopt.
- Any change to `src/trawl/`, including adding new telemetry fields.
  Existing `page_chars` / `n_chunks_total` are sufficient.
- Running the full `tests/agent_patterns/` catalog live — most
  patterns are schema-dry-run by design, and live execution would
  dominate spike cost without changing the distribution conclusion.
- Synthetic worst-case pages. The point is to characterise the
  workload trawl *actually* gets, not the worst the web can produce.

## Data sources

| Source | Cases | Why |
|---|---|---|
| `tests/test_cases.yaml` | 12 | Canonical parity matrix — realistic Korean + English sites, mix of structured/prose |
| `benchmarks/benchmark_cases.yaml` | 12 | Longer-form docs + Wikipedia mega-pages, selected to mirror agent demand |
| Stress tail (new, embedded in script) | 3-5 | Known long Wikipedia (`List of ...`), full-page MDN reference, long-form GitHub README — deliberately stresses the tail to see if anything truly exceeds the profile envelope |

Each URL is fetched twice: once **with** the profile path (`use_profile=True`, default) and once **without**
(`use_profile=False`) if a profile exists. This gives a direct
"profile-extracted subtree vs full page" size comparison — the core of
the C5-vs-profile overlap question.

For sites with no cached profile (the default for the benchmark
cases), only the single profile-less run executes. That is still the
number that matters: if profile-less `page_chars` p95 is small,
profile-less chunk/retrieval is already cheap, and C5 has nothing to
optimise.

## Success Criteria

Pre-registered so the spike ends in a clear decision rather than a
judgment call.

| Outcome | Signal | Decision |
|---|---|---|
| **Defer** | p95 `page_chars` < 200k chars AND p95 `n_chunks_total` < 500 AND p95 `retrieval_ms` < 1000 ms | Conclusion doc documenting numbers, mark C5 status `deferred` in `notes/RESEARCH.md` |
| **Adopt — narrow** | One of the above thresholds crossed on a specific host or category, others fine | Scope C5 plan to that host/category only — likely a per-host chunk budget + a section-heading prefilter, not a general subtree API |
| **Adopt — broad** | ≥ 2 of the 3 thresholds crossed across multiple categories | Full C5 plan: section indexing at chunk time, two-stage retrieval (index → selected sections), new profile-less subtree heuristic |

Secondary signal, not in the pre-registered decision but worth
recording:

- **Profile reduction ratio** — for URLs with a cached profile, compute
  `page_chars(profile=True) / page_chars(profile=False)`. If the ratio
  is ≤ 0.3 across the board, profile already provides most of the
  "select a subset" win and C5's marginal gain is small on covered
  hosts.

## Measurement protocol

1. Start a clean `~/.cache/trawl/telemetry.jsonl` destination by
   pointing `TRAWL_TELEMETRY_PATH` at
   `tests/results/c5-premise/<ts>/telemetry.jsonl`.
2. Warm the embedding server with one dummy request (avoid first-call
   skew).
3. Run parity + benchmark + stress in that order, single-process, no
   concurrency (keeps `fetch_ms` clean).
4. For each URL with a profile, do a second pass with `use_profile=False`.
5. Aggregate with a small pandas / stdlib summariser embedded in the
   script; emit `summary.json` and a `report.md`.
6. Commit only `report.md` and the aggregated `summary.json`
   (telemetry raw JSONL is gitignored via `tests/results/`).

## Risks

- **LLM / embedding server unavailable.** The measurement needs
  `:8081` (embeddings). If `:8083` reranker is down, skip rerank for
  the spike. Log which servers were reachable in `summary.json`.
- **Network flakes.** The benchmark set hits the public web;
  Cloudflare-protected pages (Reuters) may 403. Record failures in
  `summary.json` and exclude from percentile computation.
- **Profile cache state.** The user's profile cache state leaks into
  results. Mitigation: spike script logs the list of URLs with
  profiles at start and does the paired no-profile run so the profile
  effect is measurable, not confounding.

## Exit conditions

- Defer decision → commit design + conclusion + script + summary, open
  PR titled `spike(c5): page-size premise measurement — defer`,
  mark RESEARCH.md C5 status.
- Adopt decision → commit the same, plus a stub `C5 plan` doc
  enumerating which hosts / categories drove the adopt, leave the
  actual plan-writing for a follow-up PR.
