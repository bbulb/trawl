# CJK per-doc cap validation — design (2026-04-21)

Branch: `spike/cjk-per-doc-cap-validation` (off `develop` post-0.4.3
back-merge at `3f2342a`).

Pre-registered follow-up to **PR #43** (per-document char cap, shipped
in v0.4.3). PR #43's design doc "Risk" section flagged a residual:
pure CJK text tokenises at ~1–2 chars/token, so a 1500-char CJK
document could reach 750–1500 tokens — above the 512-token per-doc
batch limit on `bge-reranker-v2-m3` that PR #43 was designed to
defend against. This spike validates whether the current 1500
default survives a Korean- / Japanese-heavy payload, and either:

- **Validates** the default (risk section updated, no code change), or
- **Reproduces** the 500 with CJK payload (separate follow-up spike
  lowers default to a CJK-safe value — likely 1000).

## Problem

PR #43's empirical bracket was on the MDN Fetch API page (English,
code-heavy, 3.0–3.5 chars/token). Captured MDN payload had a
1545-char doc tokenising to ~515 tokens → cap 1550 FAIL, cap 1500
PASS. The cap was sized for this regime.

CJK (Korean Hangul / Japanese kanji+kana / Chinese hanzi) tokenises
denser — ~1–2 chars/token for bge-m3 family tokenisers (SentencePiece
BPE with Unicode-aware merges, but CJK characters rarely merge into
multi-char tokens the way Latin sub-words do). A 1500-char Korean
paragraph could reach 1500 tokens — nearly 3× over the 512 batch
limit.

The visible effect, if reproduced: silent cosine fallback on Korean /
Japanese / Chinese pages with dense CJK chunks. assertion-based
tests still pass (because cosine is a reasonable fallback ordering)
but rerank-side quality degrades without signal.

## Scope

Single diagnostic measurement. **No source-code change in this PR.**
The outcome decides whether a follow-up spike is needed:

- **D-VALIDATE**: failure rate < 0.5% across ≥ 200 replays per
  fixture. PR #43 risk section updated with "validated against
  Korean / Japanese payload, no trigger at default 1500." Close.
- **D-REPRODUCE**: failure rate ≥ 5% on any CJK fixture. Open a
  separate follow-up spike (`spike/rerank-per-doc-char-cap-cjk`)
  with empirical bracket against a CJK payload; default lowered to
  the observed safe value (likely 1000).
- **D-INCONCLUSIVE**: 0.5% ≤ rate < 5%. Expand N to 500 and re-run.
  If still inconclusive, document as "rare edge, monitor in
  production telemetry via `rerank_capped` + log grep."

## Non-goals

- **No per-doc cap default change in this PR.** That belongs to the
  follow-up if D-REPRODUCE triggers.
- **No tokeniser change.** The BPE used by `:8083` is server-side.
- **No new env var.** `TRAWL_RERANK_MAX_PER_DOC_CHARS` already exists
  from PR #43; follow-up would just change its default.
- **No change to MDN fixture or the existing
  `reranker_mdn_sporadic_diag.py` runner.** This spike uses a new,
  smaller runner to avoid re-fetching the MDN page.
- **No change to `chunking.py` / `retrieval.py`.** Out of scope.

## Design

### New runner

`benchmarks/cjk_per_doc_cap_validation.py` — new single-purpose
runner. Responsibilities:

1. **Capture phase** (`--capture`): runs `fetch_relevant()` once per
   CJK fixture (Korean Wikipedia 이순신 + Japanese Wikipedia 寿司),
   intercepts the rerank POST exactly as
   `reranker_mdn_sporadic_diag.py::_intercept_rerank_post` does, and
   dumps the `{query, documents}` payload to
   `benchmarks/results/cjk-per-doc-cap-validation/_captures/<name>.json`.

2. **Replay phase** (default): loads captures, replays each against
   `:8083/v1/rerank` N times (default 200), records per-request
   `status_code`, `elapsed_ms`, and error body. Also records the
   per-doc char distribution of each captured payload so the report
   can correlate "longest doc char-length" vs "token count" (server
   echoes token count in 500 body).

3. **Aggregation**: per-fixture failure rate, status distribution,
   error-message token-count histogram (extracted via regex).

4. **Decision gate**: resolves D-VALIDATE / D-REPRODUCE /
   D-INCONCLUSIVE per the thresholds above; writes
   `report.md` + `report.json`.

The runner reuses the capture interceptor pattern from
`reranker_mdn_sporadic_diag.py`. Code duplication is acceptable for a
single-session diagnostic; extraction into a shared helper is a
later cleanup if the pattern recurs a third time.

### Fixtures

```python
CAPTURE_FIXTURES = {
    "ko_wiki_yi_sunsin": (
        "https://ko.wikipedia.org/wiki/%EC%9D%B4%EC%88%9C%EC%8B%A0",
        "이순신 직업 생년월일 주요 업적",
    ),
    "ja_wiki_sushi": (
        "https://ja.wikipedia.org/wiki/%E5%AF%BF%E5%8F%B8",
        "寿司の歴史と種類",
    ),
}
```

Both are long, content-dense Wikipedia pages already present in
`tests/test_cases.yaml` (parity cases `korean_wiki_person` and
`japanese_wiki`), so no new external dependency.

### Metadata collected per request

For each replay call:

- `index`, `fixture`, `status_code`, `elapsed_ms`, `error_body_preview`.
- From the 500 error body (when present):
  `token_count = int(re.search(r"input \((\d+) tokens\)", body))` —
  records the tokenizer count of the offending document. This tells
  us directly whether the longest 1500-char CJK doc exceeds 512
  tokens.

Per-fixture aggregate:
- `n_docs`, `longest_doc_chars`, `second_longest_doc_chars`.
- `failure_rate`, `status_distribution`.
- `observed_token_counts` — sorted list of token counts reported by
  failed requests.

### Important subtlety — default cap already fires

With `TRAWL_RERANK_MAX_PER_DOC_CHARS=1500` as default (v0.4.3),
captured payloads will **already be clamped** to ≤ 1500 chars per
doc. Perfect for this validation: if the 1500 clamp is sufficient
for CJK, failure rate should be 0%. If it's insufficient, failures
at 1500 chars directly falsify PR #43's CJK assumption.

If we want to bracket the CJK-safe value (D-REPRODUCE path), the
follow-up spike would capture with `TRAWL_RERANK_MAX_PER_DOC_CHARS=0`
(no cap) and then test decreasing cap values. This PR does not
perform that bracket; it only checks whether 1500 is safe.

### Environmental preconditions

- `:8081` bge-m3 embedding server reachable (otherwise capture fails).
- `:8083` bge-reranker-v2-m3 server reachable.
- `mamba activate trawl` (editable install).

The runner exits non-zero on missing servers with a clear message.

## Tests

No unit tests added — this is a diagnostic script, not a library
change. The runner itself is validated by successful `--capture` +
replay execution. `test_pipeline.py` and `test_agent_patterns.py`
do not need re-running (no code change).

## Pre-registered gate

| Fixture | Metric | D-VALIDATE | D-REPRODUCE | D-INCONCLUSIVE |
|---|---|---|---|---|
| ko_wiki_yi_sunsin (N=200 replays) | failure_rate | < 0.5% | ≥ 5% | 0.5%–5% |
| ja_wiki_sushi (N=200 replays)    | failure_rate | < 0.5% | ≥ 5% | 0.5%–5% |
| any fixture | captured `longest_doc_chars` | any | any | any (informational) |
| any fixture | token_count reported in any 500 | N/A (no 500) | > 512 (corroborates hypothesis) | N/A |

**Decision rule**:
- Both fixtures VALIDATE → overall D-VALIDATE. Update PR #43 risk
  section to "validated against Korean + Japanese, no trigger."
- Any fixture REPRODUCE → overall D-REPRODUCE. File follow-up spike
  recommending default 1000 (or lower if token counts require).
- Any fixture INCONCLUSIVE (and no REPRODUCE) → expand to N=500 and
  re-run once; if still inconclusive, document as "rare edge."

## Files touched

- `benchmarks/cjk_per_doc_cap_validation.py` — new runner (~220
  lines, capture + replay + decision, mirrors
  `reranker_mdn_sporadic_diag.py` structure).
- `docs/superpowers/specs/2026-04-21-cjk-per-doc-cap-validation-design.md`
  — this file.
- `notes/cjk-per-doc-cap-validation-outcome.md` — outcome note
  (gitignored, written post-measurement).
- **If D-VALIDATE**:
  `docs/superpowers/specs/2026-04-21-rerank-per-doc-char-cap-design.md`
  "Risk" section appended with validation result.
- **If D-REPRODUCE**: no changes in this PR; follow-up spike owns
  the default change.

## Risk

- **Cache hits during capture**: `TRAWL_FETCH_CACHE_TTL` (default
  300s) is enabled in the test env; two capture runs within 5
  minutes will hit cache. Acceptable — the captured payload is
  deterministic, cache hit does not affect reranker input. First
  run populates cache anyway.
- **Embedding server required**: if `:8081` is down, capture fails
  cleanly (exit 3). Measurement cannot proceed without it.
- **Reranker server load**: 2 × 200 replays = 400 requests, expected
  < 3 minutes total. Does not stress the server meaningfully.
- **Page drift**: Wikipedia pages can change between runs. Acceptable
  — we care about the token density of typical CJK content, not
  exact reproducibility across sessions. If D-REPRODUCE triggers,
  the follow-up spike captures its own snapshot.

## Timing

Design doc: 20 min (this file). Runner: 30 min. Capture + replay:
~5 min (2 captures × 1 fetch each, then 400 replays). Report +
outcome note: 15 min. PR: 15 min. Total ~85 min.

## Decision tree post-measurement

```
D-VALIDATE  → update PR #43 risk section, close this branch, merge PR
              (title: "spike(reranker): CJK per-doc cap validation — D-VALIDATE")
D-REPRODUCE → file new issue + note, open spike/rerank-per-doc-char-cap-cjk
              (merge the diag PR as a diagnostic landing pad; the default
              change goes in the follow-up)
D-INCONCLUSIVE → re-run with N=500 once; if still inconclusive, document
              as "rare edge, monitor via rerank_capped" and close
```
