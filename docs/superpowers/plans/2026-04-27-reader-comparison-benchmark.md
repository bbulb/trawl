# Reader Comparison Benchmark Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an R1 reader-comparison benchmark that records query-based fact coverage, latency, token count, chunk count, and failure phase for trawl and reader baselines.

**Architecture:** Add one focused benchmark runner, one YAML case manifest, one README section, and offline unit tests. The runner owns case loading, scoring, provider normalization, optional-provider skip records, and report writing; production `src/trawl/` code is not changed.

**Tech Stack:** Python 3.10+, pytest, PyYAML, httpx, existing `trawl.fetch_relevant` and `trawl.to_dict`.

---

### Task 1: Scoring and Case Validation

**Files:**
- Create: `tests/test_reader_comparison.py`
- Create: `benchmarks/reader_comparison.py`

- [ ] **Step 1: Write failing tests for fact scoring and case validation**

Add tests that import `benchmarks.reader_comparison` and assert:

```python
def test_score_ranked_texts_computes_recall_mrr_and_missing_facts():
    facts = [
        {"id": "fetch_call", "any_of": ["fetch(", "fetch ("]},
        {"id": "post_method", "all_of": ["method", "POST"]},
        {"id": "json_body", "pattern": r"JSON\.stringify"},
    ]
    ranked = [
        "Use fetch(url, { method: 'GET' })",
        "Use method: 'POST' with JSON.stringify(payload)",
    ]

    score = rc.score_ranked_texts(ranked, facts)

    assert score["recall_at_k"] == 1.0
    assert score["mrr_at_k"] == 1.0
    assert score["answer_grounding_hit"] is True
    assert score["missing_facts"] == []
```

Also assert malformed cases raise `ValueError` when `expected_facts` or `failure_class` is missing.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_reader_comparison.py -q`

Expected: import failure because `benchmarks.reader_comparison` does not exist.

- [ ] **Step 3: Implement minimal case validation and scoring**

Create `benchmarks/reader_comparison.py` with:

```python
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

REQUIRED_CASE_FIELDS = {"id", "category", "url", "query", "expected_facts", "failure_class"}

def load_cases(path: Path) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text()) or {}
    cases = data.get("cases")
    if not isinstance(cases, list):
        raise ValueError("case file must contain a cases list")
    for case in cases:
        validate_case(case)
    return cases

def validate_case(case: dict[str, Any]) -> None:
    missing = sorted(REQUIRED_CASE_FIELDS - set(case))
    if missing:
        raise ValueError(f"case {case.get('id', '<unknown>')} missing fields: {', '.join(missing)}")
    if not case["expected_facts"]:
        raise ValueError(f"case {case['id']} must define expected_facts")
    for fact in case["expected_facts"]:
        if "id" not in fact:
            raise ValueError(f"case {case['id']} has fact without id")
        checks = [name for name in ("all_of", "any_of", "pattern") if name in fact]
        if len(checks) != 1:
            raise ValueError(f"fact {fact['id']} must define exactly one matcher")

def fact_matches(text: str, fact: dict[str, Any]) -> bool:
    if "all_of" in fact:
        return all(value in text for value in fact["all_of"])
    if "any_of" in fact:
        return any(value in text for value in fact["any_of"])
    return re.search(fact["pattern"], text) is not None

def score_ranked_texts(ranked_texts: list[str], facts: list[dict[str, Any]]) -> dict[str, Any]:
    found: dict[str, int] = {}
    for rank, text in enumerate(ranked_texts, start=1):
        cumulative = "\n\n".join(ranked_texts[:rank])
        for fact in facts:
            if fact["id"] not in found and fact_matches(cumulative, fact):
                found[fact["id"]] = rank
    missing = [fact["id"] for fact in facts if fact["id"] not in found]
    recall = len(found) / len(facts) if facts else 0.0
    first_rank = min(found.values()) if found else None
    return {
        "recall_at_k": recall,
        "mrr_at_k": (1.0 / first_rank) if first_rank else 0.0,
        "answer_grounding_hit": not missing,
        "missing_facts": missing,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_reader_comparison.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add benchmarks/reader_comparison.py tests/test_reader_comparison.py
git commit -m "test: add reader comparison scoring"
```

### Task 2: Provider Normalization and Skip Records

**Files:**
- Modify: `benchmarks/reader_comparison.py`
- Modify: `tests/test_reader_comparison.py`

- [ ] **Step 1: Write failing tests for normalized results**

Add tests asserting:

```python
def test_build_result_classifies_missing_facts_as_retrieval_failure():
    case = {
        "id": "mdn_fetch_post",
        "category": "docs",
        "url": "https://example.test",
        "query": "fetch post",
        "expected_facts": [{"id": "post", "any_of": ["POST"]}],
        "failure_class": {"on_empty_output": "extraction", "on_missing_facts": "retrieval"},
    }

    result = rc.build_scored_result(
        case=case,
        provider="example",
        status="ok",
        latency_ms=12,
        ranked_texts=["GET example"],
        n_chunks_total=None,
        error=None,
    )

    assert result["status"] == "fail"
    assert result["failure_phase"] == "retrieval"
    assert result["tokens_returned"] > 0
```

Also test `build_skip_result(case, "firecrawl", "FIRECRAWL_API_KEY not set")`.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_reader_comparison.py -q`

Expected: FAIL because result builders do not exist.

- [ ] **Step 3: Implement result builders**

Add token estimation, `build_scored_result`, and `build_skip_result`. Use `status: "fail"` for empty output or missing facts; use `failure_class.on_empty_output` and `failure_class.on_missing_facts` when present.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_reader_comparison.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add benchmarks/reader_comparison.py tests/test_reader_comparison.py
git commit -m "feat: normalize reader comparison results"
```

### Task 3: Writers and Report Aggregation

**Files:**
- Modify: `benchmarks/reader_comparison.py`
- Modify: `tests/test_reader_comparison.py`

- [ ] **Step 1: Write failing tests for JSONL, CSV, and Markdown outputs**

Use `tmp_path` to call `write_outputs(output_dir, results)` with one ok result and one skipped result. Assert `results.jsonl`, `summary.csv`, and `report.md` exist and contain `tokens_returned`, `latency_ms`, `Recall@k`, `MRR@k`, `answer_grounding_hit`, `failure_phase`.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_reader_comparison.py -q`

Expected: FAIL because `write_outputs` does not exist.

- [ ] **Step 3: Implement output writers**

Write JSONL using `json.dumps(..., ensure_ascii=False)`, CSV using `csv.DictWriter`, and a compact Markdown report with provider counts, pass rate excluding skipped rows, and average latency/tokens for non-skipped rows.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_reader_comparison.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add benchmarks/reader_comparison.py tests/test_reader_comparison.py
git commit -m "feat: write reader comparison reports"
```

### Task 4: Providers, CLI, Cases, and README

**Files:**
- Modify: `benchmarks/reader_comparison.py`
- Create: `benchmarks/reader_comparison_cases.yaml`
- Create or modify: `benchmarks/README.md`
- Modify: `tests/test_reader_comparison.py`

- [ ] **Step 1: Write failing CLI/provider tests**

Add offline tests for provider selection:

```python
def test_select_cases_filters_only_and_limit():
    cases = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    assert rc.select_cases(cases, only="b", limit=None) == [{"id": "b"}]
    assert rc.select_cases(cases, only=None, limit=2) == [{"id": "a"}, {"id": "b"}]
```

Add a test that the default case manifest loads and every case has `expected_facts` and `failure_class`.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_reader_comparison.py -q`

Expected: FAIL because CLI helper and manifest are missing.

- [ ] **Step 3: Implement providers and CLI**

Add:

- `run_trawl_provider(case)` using `fetch_relevant` and `to_dict`;
- `run_jina_provider(case)` using `httpx`;
- `run_trafilatura_provider(case)` that returns skip when import fails;
- explicit skip stubs for Firecrawl and Crawl4AI when not configured;
- `main(argv=None)` with `--cases`, `--only`, `--limit`, repeatable `--provider`, and `--output-dir`.

Keep network calls outside unit tests.

- [ ] **Step 4: Add initial manifest and README**

Create 5-6 stable cases from existing benchmark coverage: MDN Fetch API, Python asyncio, React useState, FastAPI GitHub README, Wikipedia LLM, StackOverflow venv. Avoid volatile front-page facts in the initial manifest.

Document:

```bash
python benchmarks/reader_comparison.py
python benchmarks/reader_comparison.py --provider trawl --provider jina --limit 2
```

Also document optional provider env/runtime requirements.

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_reader_comparison.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```bash
git add benchmarks/reader_comparison.py benchmarks/reader_comparison_cases.yaml benchmarks/README.md tests/test_reader_comparison.py
git commit -m "feat: add reader comparison benchmark"
```

### Task 5: Verification

**Files:**
- No new files.

- [ ] **Step 1: Run focused tests**

Run: `pytest tests/test_reader_comparison.py tests/test_wcxb_runner.py tests/test_pipeline.py -q`

Expected: PASS or only documented pre-existing external-service failures.

- [ ] **Step 2: Run lint/format checks**

Run:

```bash
ruff format benchmarks/reader_comparison.py tests/test_reader_comparison.py
ruff check benchmarks/reader_comparison.py tests/test_reader_comparison.py
```

Expected: PASS.

- [ ] **Step 3: Run smoke benchmark without paid providers**

Run:

```bash
python benchmarks/reader_comparison.py --provider trafilatura --limit 1 --output-dir benchmarks/results/reader-comparison/smoke
```

Expected: outputs are written; trafilatura either runs or records skipped/not configured.

- [ ] **Step 4: Final status**

Run: `git status --short`

Expected: only intentional changes are present; pre-existing untracked `AGENTS.md` may remain.
