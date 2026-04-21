# `PipelineResult.rerank_capped` telemetry — design (2026-04-21)

Branch: `feat/rerank-cap-telemetry` (off `develop` post-0.4.1 at
`a2c49a6`).

Pre-registered follow-up from the 0.4.1 release notes and the
2순위 section of `notes/next-session-2026-04-21-followups.md`.

## Scope

Expose whether the PR #38 chunk-window cap fired on each
`fetch_relevant()` call. Today the only signal is a single
`WARNING` log line in `reranking.py::_apply_caps`, and
`rerank()` already discards the returned telemetry dict
(`scored, documents, _ = _apply_caps(...)` at `src/trawl/reranking.py:171`).
Consumers who want to count cap firings across a run have no
offline signal.

## Non-goals

- Do not change cap behaviour. `_apply_caps` logic stays as is.
- Do not change defaults (`TRAWL_RERANK_MAX_DOCS=30`,
  `TRAWL_RERANK_MAX_CHARS=40000`).
- Do not add per-chunk telemetry — aggregate-only. A single
  boolean + optional pre/post counts.
- Do not back-port to 0.4.1. Ships as a minor feature on develop,
  to be included in the next release.

## Design

### Fields

Add to `src/trawl/pipeline.py::PipelineResult` (dataclass):

```python
# 0.4.2 — defensive chunk-window cap telemetry. True when rerank()'s
# pre-POST cap (TRAWL_RERANK_MAX_DOCS / TRAWL_RERANK_MAX_CHARS)
# dropped documents or truncated any doc. Stays False when the cap
# is disabled or when the payload was already under the limits.
rerank_capped: bool = False
```

Optional (include only if cheap): `rerank_cap_pre_chars` /
`rerank_cap_post_chars` ints — off by default. Keep the field set
narrow in the first PR; extra counts can be added later if a
specific consumer asks for them.

### Plumbing

`src/trawl/reranking.py::rerank()` returns `list[ScoredChunk]`
today. Two options:

1. **Expand return** — `rerank()` returns
   `tuple[list[ScoredChunk], bool]`. Caller in `pipeline.py`
   unpacks the second element into `PipelineResult.rerank_capped`.
   Touches one call site.
2. **Module-level sentinel** — store the last cap-fired flag on a
   module variable. Rejected: stateful, racy in multi-threaded use.

Go with option 1. Rename nothing else.

`_apply_caps` already returns a `telemetry` dict (see
`src/trawl/reranking.py:103-108`). `rerank()` needs to read
`telemetry["pre_docs"] != telemetry["post_docs"]` OR
`telemetry["pre_chars"] != telemetry["post_chars"]` and pass that
boolean up. Same predicate as the existing WARNING log line.

### Telemetry JSONL

Update `src/trawl/telemetry.py::_build_event` to include
`rerank_capped: result.rerank_capped` alongside `rerank_used`
(schema version stays at 1 — the event dict is additive and
consumers tolerate extra keys. If the user wants a strict schema
bump, that's a separate decision at release time).

## Tests

1. **`tests/test_reranking_cap.py`** — add assertion that
   `rerank()` returns `(scored, True)` when cap fires and
   `(scored, False)` when it does not. Reuse the existing fixtures
   that trigger each branch.
2. **`tests/test_pipeline.py` parity** — must stay 15/15.
   `PipelineResult.rerank_capped` defaults to `False` and the
   existing cases do not exceed the cap, so this is a
   non-regression check.
3. **`tests/test_telemetry.py`** (if present) or new unit:
   write `TRAWL_TELEMETRY=1` with a minimal `PipelineResult` and
   confirm the JSONL line includes `rerank_capped`.

Optional: `tests/test_agent_patterns.py --shard coding` smoke run
to confirm real-workload behaviour unchanged. Not gating — cap
does not fire on normal workload per PR #38 measurements.

## Pre-registered gate

| Check | Required |
|---|---|
| `pytest tests/test_reranking_cap.py tests/test_pipeline.py` | pass |
| `tests/test_telemetry.py` (or added unit covering the new key) | pass |
| `python tests/test_pipeline.py` parity | 15/15 |
| `python tests/test_agent_patterns.py --shard coding` | ≤ 2 fails (same as pre-change baseline — `man_curl_options` external flake, `arxiv_pdf_lora` unrelated) |

`rerank()` return signature change is not a breaking API change
for downstream consumers (library-internal only — external callers
of `fetch_relevant()` are unaffected; `reranking.rerank` is not
documented as a public entry point).

## Files touched

- `src/trawl/pipeline.py` — `PipelineResult` field + caller
  unpacks `rerank()` return.
- `src/trawl/reranking.py` — `rerank()` returns
  `tuple[list[ScoredChunk], bool]`.
- `src/trawl/telemetry.py` — `_build_event` includes
  `rerank_capped`.
- `tests/test_reranking_cap.py` — 1-2 assertions on the new return
  shape.
- `tests/test_telemetry.py` (or new) — 1 assertion on JSONL key.
- `CLAUDE.md` — "Things NOT to change" has a row for the cap
  defaults; add a note (or new row) that `rerank()` now returns
  `(scored, capped)` so future refactors do not silently drop the
  flag.
- `CHANGELOG.md` — Unreleased section entry.

No change in `src/trawl_mcp/`. No change in `benchmarks/`. The
`--via-trawl` mode of `reranker_stability_diag.py` already goes
through `rerank()`; it will need a `scored, _ = rerank(...)` update
to absorb the new tuple — include in the same PR.

## Risk

- Low. Single Boolean, single call site, single telemetry field.
  Highest-risk element is the `rerank()` tuple return breaking
  any test that still unpacks the old single-list shape. The
  existing `--via-trawl` diag runner is the only such caller.

## Timing

Implementation ~20 min, tests ~15 min, CLAUDE.md/CHANGELOG
~10 min, PR ~10 min. Total ~55 min.
