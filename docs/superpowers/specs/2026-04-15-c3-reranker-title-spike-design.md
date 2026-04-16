# C3 spike — page title in reranker input

Status: spec (2026-04-15)
Scope: one-shot spike, accept/reject after measurement. No fine-tuning.

## Motivation

trawl's 2-stage retrieval (bge-m3 bi-encoder → bge-reranker-v2-m3
cross-encoder) is structurally identical to DeepQSE (arXiv:2210.08809).
DeepQSE's precision-stage input is `(title, query, sentence)` — the
page title provides a topic anchor that chunk text alone may lack.
trawl currently passes `heading + "\n\n" + text` to the reranker
(`src/trawl/reranking.py:46-51`) with no page-level title. This spike
tests whether adding the page title to the reranker input, without
any fine-tuning, shifts rerank quality on the 12-case parity matrix.

RESEARCH.md §C3 frames this as the cheap half of the DeepQSE-inspired
work; the expensive half (adapter fine-tune) is explicitly deferred.

## Goals

- Thread a page-level title from fetch → pipeline → reranker.
- Change reranker input format to include the title when available.
- A/B measure via the existing 12-case parity matrix.
- Decide accept/reject; update RESEARCH.md §C3 accordingly.

## Non-goals

- Adapter / LoRA fine-tune of bge-reranker-v2-m3 (deferred — see
  RESEARCH.md §C3 "검토 포인트").
- Per-fetcher specialised title extraction (PDF metadata, YouTube
  video title, GitHub repo description, etc.). A single generic
  fallback path is used.
- Rebuilding the MRR / recall@k A/B runner that was deleted with the
  `late-chunking` branch. Pass/fail + per-case rerank-score diff is
  sufficient for this decision.
- Changes to the bi-encoder retrieval stage.

## Design

### Title source

One title per page, extracted once per `fetch_relevant()` call.

Generic resolution order (applies to all fetchers):
1. HTML `<title>` via BeautifulSoup, stripped of surrounding
   whitespace. Used when the fetcher produced HTML.
2. First H1 in the extracted markdown (`^# ` line).
3. Empty string. Reranker formatter falls back to current format.

Title extraction lives in a new helper in `src/trawl/extraction.py`
(close to the existing BS fallback). The pipeline calls it once on
the HTML-or-markdown it already has; no new fetcher I/O.

PDF / YouTube / API fetchers (GitHub, Stack Exchange, Wikipedia,
passthrough) share the markdown-H1 fallback. Passthrough (raw
JSON/XML) returns empty title — acceptable, reranker falls back.

### Data flow

- `PipelineResult` gains `page_title: str = ""` for observability.
- `pipeline.fetch_relevant()` computes the title after extraction and
  before retrieval; passes it to `reranking.rerank()` as a new
  keyword argument `page_title: str = ""`.
- `Chunk` is **not** modified — title is page-level metadata, passed
  alongside the chunks, not duplicated per chunk.

### Reranker input format

In `reranking.rerank()`, build each document string as:

```
Title: {title}
Section: {heading}

{text}
```

Branching:
- title non-empty and heading non-empty → all four lines
- title non-empty, heading empty → `Title: {title}\n\n{text}`
- title empty, heading non-empty → `{heading}\n\n{text}` (current)
- both empty → `{text}` (current)

Rationale for explicit `Title:` / `Section:` labels: bge-reranker-v2-m3
is not DeepQSE-trained, so we cannot rely on positional priors for
field separation. Plain-text labels are the closest analogue the
model will have seen in pretraining data.

### Feature flag

Env var `TRAWL_RERANK_INCLUDE_TITLE`:
- `1` (default): new behaviour.
- `0`: legacy behaviour — reranker gets `heading + "\n\n" + text`
  exactly as today.

Read inside `reranking.rerank()` so the flag works without pipeline
wiring changes. Pipeline always extracts and passes the title; the
reranker decides whether to splice it in.

### Measurement protocol

1. Parity matrix baseline: `TRAWL_RERANK_INCLUDE_TITLE=0 python
   tests/test_pipeline.py` — must be 12/12 (sanity; unchanged from
   current behaviour).
2. Parity matrix with title: `TRAWL_RERANK_INCLUDE_TITLE=1 python
   tests/test_pipeline.py` — must be 12/12, no regressions.
3. Per-case score diff: run each case with `--verbose` under both
   flag values; capture top-k rerank scores. Summarise in
   `notes/c3-spike-results.md` (gitignored).

Acceptance criteria (all must hold to accept):
- No parity regression: 12/12 in both runs.
- At least one case shows a measurable positive shift — either
  (a) a currently-PASS case with notably higher top-1 rerank score
  on the ground-truth chunk, or (b) a near-miss case where the
  ground-truth chunk moves up in the top-k.
- No case shows a negative shift that would be a regression if
  ground truth were tightened.

If criteria fail, reject and update RESEARCH.md §C3 to
`rejected (2026-04-15)` with the measurement summary — mirroring the
C1 late-chunking closure.

## Risks and mitigations

- **Fetcher-specific titles missed**. YouTube transcripts don't have
  a markdown H1 for the video title; passthrough is raw. Mitigation:
  empty title is a valid state; reranker falls back gracefully.
  Follow-up work (out of scope) can add per-fetcher sources.
- **Title pollution**. Some pages have uninformative `<title>` tags
  ("Home", "Untitled"). Mitigation: measurement will surface this if
  it causes score regressions. Not pre-optimised.
- **Flag default leaks into benchmarks / profile eval**. Default `1`
  means `benchmarks/run_benchmark.py` and `benchmarks/profile_eval.py`
  will run with the new format after merge. Intentional — if it's
  good enough for parity it should be on everywhere — but flagged
  here so reviewers are aware.

## Test plan

- Parity matrix under both flag values (above).
- Unit test for the new `extract_title()` helper:
  HTML with `<title>`, HTML without, markdown-only with H1,
  markdown-only without H1, empty input.
- No changes to `tests/test_cases.yaml` ground truth.
- MCP smoke test (`tests/test_mcp_server.py`) — sanity only, since
  the change is internal to `reranking.py` + `extraction.py`.

## Rollout

- Land behind the flag defaulting to `1`.
- Document the flag in `.env.example`.
- If rejected, revert the code change but keep the decision record
  in RESEARCH.md §C3.

## References

- RESEARCH.md §C3 (2026-04-14).
- DeepQSE: arXiv:2210.08809.
- Current reranker call site: `src/trawl/reranking.py:30-77`.
- Current chunking / heading path: `src/trawl/chunking.py:26-38`.
