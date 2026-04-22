# `TRAWL_CHUNK_BUDGET` default-on — design (2026-04-22)

Branch: `spike/chunk-budget-default-on` (off `develop @ 317868c` post-
PR #45).

Pre-registered completion of future item #4 in the longform-retrieval-
cost design doc (`docs/superpowers/specs/2026-04-20-longform-retrieval-cost-design.md`),
triggered by the `curl.se` manpage latency regression on `claude_code_man_curl_options`.

## Problem

`claude_code_man_curl_options` pattern (`curl.se/docs/manpage.html`,
budget 14000 ms) has been regressing since 2026-04-20:

| time | repeats | total_p95 | status |
|---|---:|---:|---|
| 2026-04-20 baseline (000e985) | 1 | — | PASS |
| 2026-04-20 flip | 1 | 17314 | FAIL (budget, 24% over) |
| 2026-04-20 re-check | 3 | 14872 | FAIL (budget, 6% over) |
| 2026-04-21 re-measure | 5 | 25149 | FAIL (budget, 80% over) |

Re-measurement on 2026-04-21 shows retrieval-dominated latency with
the breakdown `fetch_ms=0` (cache hit), `chunk_ms=9`, **`retrieval_ms=15706`**
on the slow observation — the page has **275 KB / 760 chunks**, and
bge-m3 throughput (~50 chunks/s) makes retrieval the bottleneck. The
curl.se manpage is a legitimately long document with no single-page
alternative; it is not an outlier, it is simply beyond what the
default (no prefilter) path handles within the budget.

With `TRAWL_CHUNK_BUDGET=100` enabled (opt-in feature, shipped in
v0.3.x longform follow-up), p95 drops from **25149 → 3065 ms**
(88% reduction). The assertion is unchanged (`chunks=12, path=
'full_page_retrieval'`). This validates the prefilter's design on a
case beyond the original 4 measurement fixtures.

## Scope

Flip `TRAWL_CHUNK_BUDGET` default from `"0"` (disabled) to `"100"`
(enabled, pool cap = 100 chunks) inside `_read_chunk_budget()` in
`src/trawl/pipeline.py`. Everything else unchanged:
- BM25 tokenizer (C6's `src/trawl/bm25.py`) — unchanged.
- Prefilter behaviour when pool ≤ budget — no-op, unchanged.
- `PipelineResult.n_chunks_embedded` — unchanged.
- Env-based opt-out — `TRAWL_CHUNK_BUDGET=0` disables, exactly as
  default-on hybrid-retrieval (C6 style).

## Non-goals

- **No change to BM25 tokenizer / ranking / k threshold.**
- **No change to chunker.** The 450-char max_chars and sentence
  splitter stay as-is — the prefilter operates on post-chunking
  output.
- **No CI env var removal.** Existing `TRAWL_CHUNK_BUDGET=100`
  explicitly set in any CI config now mirrors default; leaving it
  in is fine and documents intent.
- **No budget tuning.** 100 is kept as the default per prior
  measurement. A host-specific default (PDF=250, wiki=150, etc.) is
  a separate followup (was listed as #1 in the original design doc's
  future-work section).

## Design

Single-line change:

```python
# src/trawl/pipeline.py
def _read_chunk_budget() -> int:
    """Read `TRAWL_CHUNK_BUDGET` at call time; treat malformed input as disabled."""
-   raw = os.environ.get("TRAWL_CHUNK_BUDGET", "0")
+   raw = os.environ.get("TRAWL_CHUNK_BUDGET", "100")
    try:
        return max(0, int(raw))
    except ValueError:
        return 0
```

Rationale:
- `0` disables (operator opt-out), keeping the same escape hatch as
  before.
- `100` is the default validated against 4 longform fixtures
  (wiki_history, arxiv_pdf, wiki_llm, korean_wiki_person) + this
  spike's 3 measurements (parity 15/15, agent_patterns coding 23/24
  with the 1 fail pre-existing and unrelated, curl.se flipped to
  PASS).

## Tests

1. **`tests/test_pipeline.py`** — must stay 15/15 with no env var
   set (validates default-on behaviour on the parity fixtures).
2. **`tests/test_agent_patterns.py --shard coding`** — must not
   regress vs prior measurement (23/24 — the 1 pre-existing failure
   `arxiv_pdf_lora` has `fetcher_used: expected 'pdf', got None`
   which is a PDF fetcher issue, not chunk budget).

No unit test added for the env-var default; behaviour is observable
only through integration runs, and the parity matrix is the
authoritative gate per CLAUDE.md's "Things NOT to change" rules.

## Pre-registered gate

| Check | Required | Action if fail |
|---|---|---|
| `python tests/test_pipeline.py` (no env var) | 15/15 | revert code change |
| `python tests/test_agent_patterns.py --shard coding` | ≥ 23/24 (same as baseline) | revert |
| `claude_code_man_curl_options` | PASS | revert — signal didn't hold |

All three gates measured before the commit. Fail-stop: any
regression reverts the single-line change. No "close enough" —
default-on should be strict parity + the curl.se flip.

## Files touched

- `src/trawl/pipeline.py` — single-line default change (`"0"` → `"100"`),
  docstring updated to reflect new default.
- `CLAUDE.md` — longform retrieval cost bullet + endpoint map
  "Chunk budget prefilter" entry updated from "default off, opt-in"
  to "default on, opt-out via TRAWL_CHUNK_BUDGET=0".
- `README.md` — "(opt-in)" removed from features bullet.
- `CHANGELOG.md` — `[Unreleased]` "Changed" entry.
- `docs/superpowers/specs/2026-04-22-chunk-budget-default-on-design.md`
  — this file.
- `notes/curl-options-latency-2026-04-27.md` — appended closure note.

## Risk

- **Low.** The feature has been shipping default-off since 2026-04-20
  with positive longform telemetry and zero regressions on the
  original 4 cases + parity. This spike re-validates against parity
  + agent_patterns coding shard. Any page producing < 100 chunks is
  unaffected (prefilter no-op). Pages producing > 100 chunks see
  BM25 drop the lowest-scoring chunks before embedding — rank-1
  identity is preserved on the 4 original + 15 parity + ~23 coding
  agent patterns = ~42 distinct URLs.
- **Counterfactual**: if future host-specific defaults (PDF = 250,
  wiki = 150) ship, the global default becomes redundant for those
  hosts. Flipping the global to 100 now does not block the per-host
  defaults — the latter can later override on a per-fetcher basis.

## Timing

Code change + doc updates ~20 min. Validation runs ~10 min (parity
+ coding shard). PR + CHANGELOG ~15 min. Total ~45 min.

## Reference

- Original longform design doc:
  `docs/superpowers/specs/2026-04-20-longform-retrieval-cost-design.md`
  (future-work item #4 "default-on 전환").
- curl.se regression note: `notes/curl-options-latency-2026-04-27.md`
  (pre-spike), will be appended with closure.
- Measurement artefacts (gitignored):
  `tests/results/agent_patterns_20260421-221302Z/` — coding shard
  with TRAWL_CHUNK_BUDGET=100 env.
  `tests/results/20260422-070956/` — parity with TRAWL_CHUNK_BUDGET=100.
