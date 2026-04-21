# Per-document char cap on rerank() payload — design (2026-04-21)

Branch: `spike/rerank-per-doc-char-cap` (off `develop` post-0.4.2 at
`08e7175`).

Pre-registered follow-up to **PR #41 D2 (H2)** — the MDN sporadic 500
diagnostic confirmed a per-document 512-token batch limit on
`bge-reranker-v2-m3` at `:8083`, distinct from the 8 192-token total
context limit addressed by PR #38's chunk-window cap.

## Problem

After PR #38 shipped a defensive cap on the *total* payload size,
the MDN reranker request still fast-rejects with HTTP 500 whenever a
single document exceeds ~512 tokens (~2 048 chars). Server response:

```
http 500: {"error":{"code":500,
  "message":"input (517 tokens) is too large to process. increase the
             physical batch size (current batch size: 512)",
  "type":"server_error"}}
```

Captured MDN payload (PR #41) had a longest doc of 2 056 chars
≈ 514 tokens. PR #38's `TRAWL_RERANK_MAX_CHARS=40000` (default) cap
does not bite here because the total sits at ~11 k chars — well
under 40 k. The per-doc average (40 000 / 30 ≈ 1 333) is also under,
but individual docs are not constrained.

The visible-to-user effect today: silent cosine fallback. The
reranker's exception handler in `rerank()` logs a `WARNING` and
returns the cosine ordering, which masks the rerank-side regression
from assertion-based tests but degrades top-k quality on pages with
oversize chunks.

## Scope

Add `TRAWL_RERANK_MAX_PER_DOC_CHARS` (default `1800`) per-document
char cap inside `_apply_caps`. Truncates any individual document
that exceeds the cap before the POST.

## Non-goals

- **No change to `chunking.py` or `retrieval.py`.** Upstream chunk
  sizes stay as they are — chunkers can produce arbitrarily long
  chunks for unbreakable content (long sentences, code blocks, table
  rows). The cap defends at the rerank boundary only.
- **No change to PR #38 caps.** `TRAWL_RERANK_MAX_DOCS=30` and
  `TRAWL_RERANK_MAX_CHARS=40000` defaults stay. The new per-doc cap
  is independent and complementary.
- **No retry / fallback rework.** Cosine fallback when the reranker
  is unreachable is unchanged; this PR removes one *cause* of the
  fallback, not the fallback itself.
- **No server-side change.** llama-server's `--ubatch-size 512` is
  out of scope (operator config).

## Design

### Field + env var

In `src/trawl/reranking.py`:

```python
DEFAULT_MAX_PER_DOC_CHARS = 1500
# Empirical bracket on the captured MDN Fetch_API payload (2026-04-21):
# cap=1500 PASS, cap=1550 FAIL (the 1545-char 2nd-longest doc tokenises
# to ~515 tokens — over the 512 batch limit). 1500 sits on the safe
# side at the observed code-heavy 3.0-3.5 chars/token ratio. Note: the
# initial design's 1800 default (matching MAX_EMBED_INPUT_CHARS) was
# calibrated against the wrong assumption of ~4 chars/token uniform;
# code-heavy content is denser. Pure CJK (~1-2 chars/token) is denser
# still and may need an even lower cap if it materialises.


def _max_per_doc_chars_env() -> int:
    """Per-document character cap. ``<= 0`` disables."""
    try:
        v = int(os.environ.get(
            "TRAWL_RERANK_MAX_PER_DOC_CHARS",
            str(DEFAULT_MAX_PER_DOC_CHARS),
        ))
    except ValueError:
        return DEFAULT_MAX_PER_DOC_CHARS
    return v
```

### `_apply_caps` change

Insert the per-doc clamp **between** the existing doc-count cap and
total-chars cap. Order rationale: doc-count first (drops least
relevant), then per-doc (defensive against single oversize doc),
then total-chars (proportional truncate if still over total budget,
which would now further trim the already-clamped docs).

```python
def _apply_caps(query, scored, documents):
    max_docs = _max_docs_env()
    max_per_doc = _max_per_doc_chars_env()
    max_chars = _max_chars_env()

    pre_docs = len(documents)
    pre_chars = len(query) + sum(len(d) for d in documents)

    docs = documents
    ranked = scored

    if max_docs > 0 and len(docs) > max_docs:
        docs = docs[:max_docs]
        ranked = ranked[:max_docs]

    # NEW: per-document cap. Defends against the per-doc 512-token
    # batch limit on bge-reranker-v2-m3 (PR #41 D2 outcome).
    if max_per_doc > 0 and docs:
        docs = [d[:max_per_doc] if len(d) > max_per_doc else d for d in docs]

    if max_chars > 0 and docs:
        total = len(query) + sum(len(d) for d in docs)
        if total > max_chars:
            budget = (max_chars - len(query)) // len(docs)
            budget = max(MIN_PER_DOC_CHARS, budget)
            docs = [d[:budget] for d in docs]

    post_chars = len(query) + sum(len(d) for d in docs)
    telemetry = {
        "pre_docs": pre_docs,
        "post_docs": len(docs),
        "pre_chars": pre_chars,
        "post_chars": post_chars,
    }
    ...
```

### Telemetry interaction with PR #40

PR #40 surfaces `PipelineResult.rerank_capped` from the predicate
`pre_docs != post_docs or pre_chars != post_chars`. Per-doc cap firing
changes `post_chars` (sum of doc lengths drops), so the existing
predicate detects it without modification. **No code change to
`rerank()` or `pipeline.py` required for the boolean field.**

The `WARNING` log line in `_apply_caps` stays unchanged in shape but
will now fire on per-doc cap activation too. Operators reading the
log can compute which cap fired by comparing `pre_docs == post_docs`
(per-doc or total-chars fired) vs `pre_docs != post_docs` (doc-count
also fired).

## Tests

1. **Unit `tests/test_reranking_cap.py`:**
   - `test_per_doc_cap_truncates_oversize`: a single doc longer than
     `MAX_PER_DOC_CHARS` is truncated; shorter docs unaffected;
     telemetry `pre_chars > post_chars`.
   - `test_per_doc_cap_off_by_zero`: `TRAWL_RERANK_MAX_PER_DOC_CHARS=0`
     disables (oversize doc passes through unchanged).
   - `test_per_doc_cap_invalid_env_falls_back`: malformed env value
     uses `DEFAULT_MAX_PER_DOC_CHARS`.
   - `test_per_doc_then_total_chars_stack`: setup where per-doc
     truncation alone would still exceed total cap, total cap
     proportionally truncates further.
   - `test_per_doc_cap_does_not_drop_docs`: doc count preserved when
     only per-doc fires.
   - `test_rerank_returns_capped_true_for_per_doc`: existing
     PR #40 boolean fires when only per-doc activates.

2. **`tests/test_pipeline.py` parity:** must stay 15/15. None of the
   existing 15 cases produce docs > 1800 chars (chunker target is
   450 chars; existing docs typically 450-770 chars), so the cap
   should be inert on parity workload.

3. **`tests/test_agent_patterns.py --shard coding`** (specifically
   `claude_code_mdn_fetch_api`): expected to flip from
   *cosine-fallback* (silent regression on rerank-quality, but
   assertion still passes thanks to shadow-DOM unwrap putting
   keywords into chunks at all) to *genuine reranker success* with
   the per-doc cap. Verify via:
   - `PipelineResult.rerank_used == True` on MDN page (was True
     already even on cosine fallback because `use_rerank=True` is
     passed in; the call just degrades silently — easier to verify
     by capturing logs).
   - Capture stderr for absence of "reranker unavailable, falling
     back to cosine" WARNING on MDN run.

4. **Diagnostic re-run** (manual sanity): re-run
   `benchmarks/reranker_mdn_sporadic_diag.py --capture` then
   default mode. Expected outcome:
   - `--capture` produces MDN payload where all docs ≤ 1800 chars.
   - Default mode shows MDN failure rate **0%** (was 100%); D-gate
     resolves to **D0** (overall < 0.5%).

## Pre-registered gate

| Check | Required | Action if fail |
|---|---|---|
| Unit tests pass | yes | fix |
| `python tests/test_pipeline.py` parity | 15/15 | revert; cap default may be too aggressive |
| `python tests/test_agent_patterns.py --shard coding` | ≤ 2 fails (same baseline) | investigate |
| Re-run diag — MDN failure rate | 0% (D0) | investigate; cap may not have fired (env/wiring) |

If parity drops or coding shard regresses by >0 from baseline (22/24),
revert and reconsider default. The cap default of 1800 mirrors
`MAX_EMBED_INPUT_CHARS=1800` already in `retrieval.py`, so it should
be a safe choice.

## Files touched

- `src/trawl/reranking.py` — new env helper + per-doc clamp in
  `_apply_caps`. Update module docstring "Defensive payload caps"
  comment to mention per-doc.
- `tests/test_reranking_cap.py` — 6 new test cases.
- `CLAUDE.md` — append to the existing
  `reranking.py DEFAULT_MAX_DOCS / DEFAULT_MAX_CHARS / MIN_PER_DOC_CHARS`
  row in "Things NOT to change", or add a new row for per-doc cap.
  Update the llama-server endpoint map's reranker section to mention
  the new env var.
- `CHANGELOG.md` — Unreleased "Added" entry.

`src/trawl/pipeline.py`, `src/trawl/telemetry.py`, and the diag
runner are NOT touched — PR #40's `rerank_capped` plumbing already
reflects per-doc cap fires via the existing `pre_chars != post_chars`
predicate.

## Risk

- **Low.** Single conditional in one helper. The cap is opt-out (zero
  sentinel) and the default (1800) leaves a 12% margin against the
  observed 514-token boundary at the empirical 4 chars/token ratio.
- **Char-vs-token ratio variance.** Pure CJK text tokenises at ~1-2
  chars/token, meaning a 1800-char CJK doc could reach 1800 tokens
  — well under the model's 8192-token context, well over the 512-
  token batch. **However**: the issue is the *batch* size (`--ubatch
  -size 512`), which is a server-side request-time setting, not a
  per-document limit. Re-reading the server message: "input (517
  tokens) is too large to process. increase the physical batch size
  (current batch size: 512)". This bounds the *single document being
  processed in one batch*, not the model context. So at 1800 chars
  Korean (= ~1800 tokens), the cap might not be enough. **Mitigation:**
  retest with a Korean-heavy URL during validation; if that fails,
  drop default to ~1500 chars or less. Document in the design's
  followup section if observed.

  **Validation (2026-04-21)**: `D-VALIDATE`. Separate spike
  `spike/cjk-per-doc-cap-validation` measured two CJK fixtures
  (Korean Wikipedia 이순신, Japanese Wikipedia 寿司) with cap at
  default 1500. 200 replays per fixture, 0 / 400 failures (both
  fixtures 0.0% failure rate). Longest chunks observed: Korean 311
  chars (≈ 207 tokens), Japanese 321 chars (≈ 214 tokens). The
  chunker's 450-char target combined with denser CJK sentence
  boundaries keeps CJK docs well under the cap's boundary regime
  — the 1500 cap is effectively inert on CJK prose. Design doc +
  runner: `docs/superpowers/specs/2026-04-21-cjk-per-doc-cap-validation-design.md`,
  `benchmarks/cjk_per_doc_cap_validation.py`. Caveats: two-fixture
  scope, Chinese not measured, chunker coupling noted in outcome.
  If `rerank_capped` telemetry (PR #40) ever spikes on CJK pages in
  production, revisit.

## Timing

Implementation 15 min, tests 20 min, validation runs 30 min,
PR + CHANGELOG 15 min. Total ~80 min.
