# `TRAWL_EMBED_CACHE_TTL` default-on — design (2026-05-18)

Branch: `spike/embed-cache-default-on` (off `develop @ 069b600` post-
v0.4.4 + P0 stability foundations + P1 Goal 1).

Pre-registered roadmap step #3 in
`docs/superpowers/plans/2026-05-18-trawl-improvement-roadmap.md`.

## Problem

Document embedding cache landed default-off (`DEFAULT_TTL_SECONDS =
0`) so repeat queries against the same URL re-pay the bge-m3
embedding cost. The 2026-05-04 cache-controlled reader-comparison run
quantifies the gap:

| Mode | Cold p95 retrieval | Warm p95 retrieval | Δ |
|---|---:|---:|---:|
| dense | 1445 ms | 55.8 ms | **−96.1%** |
| hybrid | 1467 ms | 63.7 ms | −95.7% |
| contextual-auto | 1857 ms | 67.7 ms | −96.4% |
| contextual-forced | 1853 ms | 73.0 ms | −96.1% |

Source: `benchmarks/results/reader-comparison/retrieval-modes-cache-2026-05-04/`
(48 rows, 6 URLs × 2 phases × 4 modes, `--warm-repeat-embed-cache-ttl
86400`). The warm path is a near-zero-cost path that operators get
only by setting an env var.

The cache key already partitions on `model`, `base_url`, `text_sha256`,
`contextual_mode`, `prefix_max_chars`, `prefix_version`, and schema
version — stale content cannot survive a text edit, and contextual
on/off cannot collide. The hot path's only remaining cost is a single
`sha256` over chunk text plus a JSON file read.

## Scope

Flip `DEFAULT_TTL_SECONDS = 0` → `DEFAULT_TTL_SECONDS = 3600`
(1 hour) in `src/trawl/embedding_cache.py`. Everything else
unchanged:

- `is_enabled() = ttl > 0` predicate — unchanged.
- Disk usage bound `TRAWL_EMBED_CACHE_MAX_MB` (default 512 MB) +
  20%-headroom LRU trim — unchanged.
- Cache key fields and `SCHEMA_VERSION` — unchanged so existing
  caches still hit after the flip.
- `_ttl_seconds()`, `get()`, `put()` semantics — unchanged.
- Env override `TRAWL_EMBED_CACHE_TTL=0` continues to disable.

## Non-goals

- **No change to cache key fields or schema.** Existing on-disk
  records remain valid; key partitioning by contextual mode/version
  prevents cross-pollution.
- **No change to TTL value beyond the default.** 3600 s is short
  enough to expire stale embeddings within an hour for actively
  edited pages, long enough to amortize over a typical agent
  session. Operators wanting longer windows set the env var
  (`TRAWL_EMBED_CACHE_TTL=86400`, as in benchmarks).
- **No change to disk cap default.** 512 MB stays — operators with
  small disks can lower via env.
- **No host-specific TTL tiers.** Future-work candidate, outside
  this spike's measurement.

## Design

Single-line change:

```python
# src/trawl/embedding_cache.py
-DEFAULT_TTL_SECONDS = 0
+DEFAULT_TTL_SECONDS = 3600
```

Plus the corresponding test rename: the existing
`tests/test_embedding_cache.py::test_disabled_by_default` asserted
"no env → cache disabled" — under the new default that's no longer
true. The test is renamed to
`test_disabled_when_ttl_zero` and sets `TRAWL_EMBED_CACHE_TTL=0`
explicitly to keep the opt-out path covered. A new
`test_enabled_by_default` asserts the new default behaviour with no
env var set.

Rationale:

- `3600` s amortizes embedding cost over a 1-hour agent session.
  Longer windows (e.g. 86400 in the warm-repeat benchmark) are
  available via env.
- `0` continues to disable — same escape hatch as before.
- Defensive caps (`TRAWL_EMBED_CACHE_MAX_MB=512`, LRU trim with 20%
  headroom) prevent unbounded disk growth.
- Cache key already includes contextual mode + prefix version so the
  flip does not invalidate existing on-disk caches.

## Tests

1. **`tests/test_pipeline.py`** — must stay 15/15 with no env var
   set (validates default-on behaviour on the parity fixtures).
2. **`tests/test_embedding_cache.py`** — renamed `test_disabled_by_default`
   → `test_disabled_when_ttl_zero` + new `test_enabled_by_default`.
3. **Full `pytest`** — must remain green (`410+ passed`).
4. **Reader-comparison warm-repeat** — re-run the 6-URL warm-repeat
   measurement with the new default (no env var) and confirm the
   gates below.

## Pre-registered gate

| Check | Required | Action if fail |
|---|---|---|
| `python tests/test_pipeline.py` (no env var) | 15/15 | revert code change |
| `pytest` full suite | all pass | fix tests or revert |
| Cold retrieval p95 (default 3600 vs prior TTL=0 baseline) | within +10% | revert |
| Warm retrieval p95 reduction vs cold | ≥ 80% | revert — caching not effective |
| Disk usage after warm sweep | ≤ `TRAWL_EMBED_CACHE_MAX_MB` (512 MB) | revert |
| Cache key fields (model, base_url, text_sha256, contextual_mode, prefix_max_chars, prefix_version, schema) | unchanged | revert any field touch |

All gates measured before the commit. Fail-stop: any gate miss reverts
the one-line default change. No "close enough" — default-on should
match the chunk-budget-default-on (PR #46) discipline.

## Files touched

- `src/trawl/embedding_cache.py` — single-line default change
  (`0` → `3600`).
- `tests/test_embedding_cache.py` — rename + new default-on test.
- `CLAUDE.md` — embedding cache bullet (default state) updated.
- `README.md` — embedding cache settings section updated.
- `CHANGELOG.md` — `[Unreleased]` "Changed" entry.
- `docs/superpowers/specs/2026-05-18-embed-cache-default-on-design.md`
  — this file.
- `notes/embed-cache-default-on-outcome.md` — measurement record +
  decision (created post-measurement).

## Risk

- **Low.** Cache key partitioning by `text_sha256` + contextual mode
  prevents stale-content reuse. The disk cap + LRU trim is already
  shipped. The opt-out env var stays.
- **Counterfactual**: pages with content that changes within 1 hour
  but the URL stays the same (e.g. real-time dashboards) could
  receive stale embeddings. However: chunk text is hashed, so a
  content edit changes the cache key and forces a re-embed. Only
  the (rare) case where chunk text is preserved but ranking should
  shift would persist — and that case is implausible at the chunk
  granularity used (450 char default).
- **Disk usage**: 6 reader-comparison URLs at ~67 chunks/URL × 1024
  floats × 4 B ≈ ~1.5 MB. 15 parity URLs similarly. Both well
  below the 512 MB cap. Operators with constrained disks already
  have `TRAWL_EMBED_CACHE_MAX_MB` override.

## Timing

Code change + test rename ~10 min. Design doc ~15 min. Measurement
runs ~10 min (parity + 6-URL warm-repeat). PR + outcome note ~15 min.
Total ~50 min.

## Reference

- Roadmap step #3:
  `docs/superpowers/plans/2026-05-18-trawl-improvement-roadmap.md`
  (P1 follow-up after Goal 1).
- 2026-05-04 measurement source:
  `benchmarks/results/reader-comparison/retrieval-modes-cache-2026-05-04/`
- chunk-budget-default-on spike pattern (matches discipline):
  `docs/superpowers/specs/2026-04-22-chunk-budget-default-on-design.md`
- Measurement artefacts (this spike, gitignored):
  `benchmarks/results/embed-cache-default-on/<ts>/`
