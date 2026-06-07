# Reader comparison benchmark — design (2026-04-27)

Source: `notes/improvement-roadmap-2026-04-27.md` R1.
Status: design approved, awaiting implementation plan.

## Goal

Add a repeatable query-based benchmark that compares `trawl` with external
URL-to-Markdown/readability systems and local extraction baselines on the same
URL/query/fact cases.

This benchmark answers a different question than WCXB:

- **WCXB** measures extraction F1 against human main-content text.
- **Reader comparison** measures whether a reader/retrieval system returns the
  facts needed for a realistic agent query, how many tokens it returns, how long
  it takes, and where failures happen.

The result should make later R2/R3/R4 work measurable instead of relying on
ad hoc inspection.

## Non-goals

- No CI integration in the first pass. Networked providers make CI brittle.
- No full crawler or multi-page traversal. Each case is one URL plus one query.
- No paid-provider hard dependency. Firecrawl and Crawl4AI must be optional and
  skipped cleanly when not configured.
- No WCXB changes. WCXB remains the extraction benchmark.
- No retrieval algorithm changes. This task adds measurement only.

## Current context

`benchmarks/run_benchmark.py` already compares `trawl` and Jina Reader for the
older `benchmarks/benchmark_cases.yaml` set. It mixes profile-generation
timing, prints human-oriented output, and stores a single JSON result.

`benchmarks/wcxb/` is a separate extraction benchmark with its own data cache,
runner, evaluator, and report format.

The R1 implementation should keep those boundaries:

- leave WCXB untouched,
- preserve the existing benchmark script for backward compatibility,
- add a new, more structured reader-comparison runner.

## Approach options

| Option | Description | Trade-off |
|---|---|---|
| Extend `run_benchmark.py` | Add all R1 fields and providers to the existing script | Fastest, but further entangles old Jina/profile assumptions with new benchmark semantics |
| Add a new `reader_comparison.py` runner | Reuse small helper ideas from the old script but define a clean case/result schema | Slight duplication, but clearer boundaries and easier future provider additions |
| Build a benchmark framework package | Shared provider interfaces, plugins, typed models, multiple commands | Too much structure before the first R1 measurement proves what is useful |

Chosen: **new runner, modest shared conventions**. It gives R1 a clean result
schema while avoiding a large framework.

## Files

```
benchmarks/
  reader_comparison.py          new CLI runner
  reader_comparison_cases.yaml  new case manifest
  README.md                     add reproduction command and output notes

benchmarks/results/
  reader-comparison/<timestamp>/
    results.jsonl               one provider/case result per line
    summary.csv                 compact tabular metrics
    report.md                   human-readable summary

tests/
  test_reader_comparison.py     offline unit tests for scoring, provider skip,
                                failure classification, and report fields
```

## Case schema

Each case is explicit about the expected useful facts and failure taxonomy.

```yaml
cases:
  - id: mdn_fetch_post
    category: docs
    url: https://developer.mozilla.org/en-US/docs/Web/API/Fetch_API/Using_Fetch
    query: how to make a POST request with the Fetch API
    expected_facts:
      - id: uses_fetch
        any_of: ["fetch(", "fetch ("]
      - id: method_post
        any_of: ["POST", "method"]
      - id: request_body
        any_of: ["body", "JSON.stringify"]
    failure_class:
      on_fetch_error: fetch
      on_empty_output: extraction
      on_missing_facts: retrieval
```

Fields:

- `id`, `category`, `url`, `query` are required.
- `expected_facts` is required and contains one or more fact groups.
- Each fact group supports `all_of`, `any_of`, or `pattern`.
- `failure_class` is required so every failed result can be classified without
  inventing categories after the run.

Initial cases should be a focused subset of existing parity/benchmark coverage:
technical docs, GitHub README, Wikipedia, StackOverflow, one news/front-page
case, and one PDF/manual-like case only if it is stable enough to avoid
date-sensitive assertions.

## Provider behavior

Providers produce normalized `ProviderResult` records:

```json
{
  "case_id": "mdn_fetch_post",
  "provider": "trawl",
  "status": "ok",
  "latency_ms": 1234,
  "tokens_returned": 812,
  "n_chunks_total": 42,
  "recall_at_k": 1.0,
  "mrr_at_k": 1.0,
  "answer_grounding_hit": true,
  "failure_phase": null,
  "missing_facts": [],
  "error": null
}
```

### Required providers

- `trawl`: calls `fetch_relevant(url, query, use_rerank=True)` and evaluates
  returned chunks.
- `jina`: calls `https://r.jina.ai/<url>` and evaluates the full Markdown text.

### Optional providers

- `trafilatura`: local baseline if installed. It ignores query and evaluates
  full extracted text.
- `readability`: local baseline if a configured implementation is available.
- `firecrawl`: runs only when `FIRECRAWL_API_KEY` is set.
- `crawl4ai`: runs only when the package/runtime is importable.

Missing optional providers emit one `status: "skipped"` result per case with
`failure_phase: "not_configured"`. Skips count in provider availability but not
in quality aggregates.

## Metrics

The runner records the R1 fields:

- `tokens_returned`: rough token estimate from returned text.
- `latency_ms`: wall-clock provider call duration.
- `Recall@k`: for `trawl`, fraction of expected fact groups hit by the returned
  chunk set. For full-page providers, this is equivalent to recall over the
  returned document.
- `MRR@k`: first rank where any required fact group is found. Full-page
  providers use rank 1 when any fact is hit.
- `answer_grounding_hit`: true only when all expected fact groups are satisfied.
- `n_chunks_total`: `trawl` pipeline chunk count when available, otherwise null.
- `failure_phase`: one of `fetch`, `extraction`, `retrieval`, `rerank`,
  `not_configured`, or `provider_error`.

The first implementation can compute fact hits by string/pattern matching.
Semantic answer judging is explicitly out of scope.

## Data flow

```
reader_comparison_cases.yaml
   |
   v
reader_comparison.py
   |-- load cases and selected providers
   |-- execute provider/case pairs
   |-- normalize text/chunk outputs
   |-- score expected facts
   |-- classify failures
   v
benchmarks/results/reader-comparison/<timestamp>/
   |-- results.jsonl
   |-- summary.csv
   `-- report.md
```

## CLI

```bash
python benchmarks/reader_comparison.py
python benchmarks/reader_comparison.py --only mdn_fetch_post
python benchmarks/reader_comparison.py --provider trawl --provider jina
python benchmarks/reader_comparison.py --limit 3
python benchmarks/reader_comparison.py --output-dir benchmarks/results/reader-comparison/smoke
```

Default providers: `trawl`, `jina`, and installed local baselines. Paid or
service-backed providers require explicit configuration and may be skipped.

## Error handling

- Fetch/provider exception: record `status: "error"`, `failure_phase:
  "provider_error"` unless the case maps it more specifically.
- Empty output: record `status: "fail"`, `failure_phase: "extraction"`.
- Missing expected facts: record `status: "fail"`, `failure_phase: "retrieval"`.
- Optional provider unavailable: record `status: "skipped"`, `failure_phase:
  "not_configured"`.
- The process exits 0 if at least one required provider ran for all selected
  cases and outputs were written. It exits 1 for malformed cases or if all
  required providers fail before scoring.

## Tests

Offline unit tests cover:

- case loading rejects missing `expected_facts` or `failure_class`;
- scoring handles `all_of`, `any_of`, and regex `pattern`;
- `Recall@k`, `MRR@k`, and `answer_grounding_hit` are computed from ranked
  chunks;
- skipped optional providers are recorded without entering quality aggregates;
- JSONL/CSV/report writers include the required R1 fields.

Networked provider execution remains manual benchmark behavior, not a unit-test
requirement.

## Gate

R1 is complete when:

- every new benchmark case has expected facts and failure classification;
- the runner writes JSONL, CSV, and Markdown outputs;
- README documents reproduction commands and optional provider environment
  variables;
- offline tests pass;
- existing WCXB tests and core pipeline tests are not regressed by benchmark
  support code.

## Follow-ups

- Add provider-specific adapters for Firecrawl and Crawl4AI once credentials and
  runtime choices are settled.
- Feed R2/R3/R4 experiment outputs into this result schema rather than creating
  new one-off reports.
- Consider promoting shared scoring helpers if a second benchmark starts using
  the same case/fact schema.
