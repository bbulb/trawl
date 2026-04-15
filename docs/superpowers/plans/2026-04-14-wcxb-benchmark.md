# WCXB extraction benchmark — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `trawl.extraction.html_to_markdown` 의 품질을 외부 공개 벤치마크 WCXB(dev 1,497 pages)로 1회 측정하고, 동일 환경에서 Trafilatura baseline과 비교한 리포트를 생산한다.

**Architecture:** `benchmarks/wcxb/` 아래에 `fetch.py`(스냅샷 다운로드) / `run.py`(페이지별 extraction + F1 계산 + 집계 + 리포트) / `evaluate.py`(WCXB 공식 평가 로직 vendor)를 구성한다. 데이터와 결과는 모두 gitignore. 파이프라인 코드는 건드리지 않는다.

**Tech Stack:** Python 3.10+, `trafilatura`, `beautifulsoup4`, `lxml` (모두 이미 프로젝트 의존성), stdlib `argparse`·`json`·`gzip`·`hashlib`·`urllib.request`.

**Spec:** [`docs/superpowers/specs/2026-04-14-wcxb-benchmark-design.md`](../specs/2026-04-14-wcxb-benchmark-design.md)

**Env rule:** 모든 python/pytest 명령은 `mamba activate trawl` 환경 안에서 또는 `mamba run -n trawl <cmd>` 로 실행. 이 플랜의 모든 명령은 편의상 `mamba run -n trawl` 접두어를 생략하고 쓰지만, 실제 실행 시 붙여야 한다 (또는 먼저 `mamba activate trawl`).

---

## File structure

```
benchmarks/wcxb/
  __init__.py           빈 파일 (test import 편의용)
  fetch.py              WCXB dev 스냅샷 다운로드 + 해시 검증
  run.py                러너 CLI entrypoint
  evaluate.py           WCXB 공식 평가 로직 vendor
  aggregate.py          집계 + 리포트 렌더링 (run.py에서 import)
  ATTRIBUTION.md        CC-BY 4.0 출처 표기
  README.md             한 번 실행 안내
  data/                 gitignored (fetch.py가 채움)

tests/
  test_wcxb_evaluate.py   vendored evaluate.py 단위 테스트
  test_wcxb_runner.py     run-single-page + baseline 경로 단위 테스트
  test_wcxb_aggregate.py  집계·리포트 렌더링 단위 테스트
  test_wcxb_fetch.py      fetch.py 해시 검증 · idempotency 단위 테스트
  fixtures/wcxb/          소형 합성 페이지 + ground truth (3~5개) — TDD 입력
```

**Why split `run.py` / `aggregate.py` / `evaluate.py`:** `run.py`는 CLI·오케스트레이션만, `aggregate.py`는 순수 함수(입력 리스트 → 리포트), `evaluate.py`는 upstream vendor. 각각 단독 테스트 가능.

**Why fixtures under `tests/fixtures/wcxb/`:** 실제 WCXB 데이터는 gitignored이므로 테스트가 쓰지 못한다. 3~5개짜리 합성 HTML + JSON을 repo에 체크인해서 로직 단위 테스트에 사용.

---

## Task 0: Upstream 확인 및 vendor 준비 — **COMPLETED (by controller)**

확정된 값 (Task 1~10 모두 이 값을 전제로 작성됨):

- **Repo**: `Murrough-Foley/web-content-extraction-benchmark`
- **Pinned commit**: `c039d5ee9f5a3a984a0e167e63aacd04e76e78a9` (2026-04-04)
- **License**: CC-BY-4.0
- **Layout**:
  - `evaluate.py` (root) — 공식 평가 스크립트
  - `metadata.json` — dev 1,497 / test 511 카운트 + 파일별 메타
  - `dev/html/<id>.html.gz` + `dev/ground-truth/<id>.json` (4자리 zero-pad ID, 예: `0001`)
  - `test/...` (동일 구조, Phase 1 미사용)
- **evaluate.py API** (그대로 vendor; alias 불필요):
  - `tokenize(text) -> list[str]` = `re.findall(r'\w+', text.lower())` (대소문자 무시)
  - `word_f1(predicted, reference) -> (precision, recall, f1)` — 우리 테스트 시그니처와 정확히 일치
  - `snippet_check(text, snippets) -> float` — case-insensitive substring 비율
  - `get_page_type(data) -> str` — `data["_internal"]["page_type"]["primary"]` 파싱, `category` → `collection` 매핑 (dict/str 모두 지원). **Task 3/4/6에서 이 헬퍼 재사용.**
- **Ground truth JSON 스키마** (샘플 `0001.json`):
  ```json
  {
    "schema_version": "2.0",
    "url": "https://scratchculinary.com/...",
    "file_id": "0001",
    "_internal": {
      "page_type": {"primary": "article", "confidence": "verified",
                    "needs_review": false, "tags": [...]}
    },
    "ground_truth": {
      "title": "...", "author": "...", "publish_date": "...",
      "main_content": "...",
      "with": [...], "without": [...]
    }
  }
  ```
- **7개 page types**: `article, forum, product, collection, listing, documentation, service`.
  **우리 설계 초안의 "news"는 실제로 "article"** — 모든 fixture·테스트·문서에서 치환.
- **Manifest 생성**: GitHub contents API는 응답당 1000개 truncate. dev 디렉터리 완전 열거에는 git **trees** recursive API 사용 (Task 8 Step 5 분기 경로).

**All placeholders referenced in Task 1~10 are now concrete values (see above). Implementers must not re-fetch or re-investigate — use these values directly.**

### Corrections applied to Task 1~10 after Task 0 findings

Implementers: when a specific task step below differs from Task 0 reality, follow Task 0 reality.

- **Task 1 Step 4 (alias block)**: upstream `word_f1(predicted, reference) -> (precision, recall, f1)` exactly matches our test signature. **Do NOT add any alias.** Just `from .evaluate import word_f1` works. Skip the alias block entirely; delete the `raise NotImplementedError` block.
- **Task 1 Step 4 header**: substitute `<SHA from Task 0>` with `c039d5ee9f5a3a984a0e167e63aacd04e76e78a9`.
- **Task 1 Step 5 ATTRIBUTION.md**: same SHA substitution.
- **Task 2 (fixtures)**:
  - `news_sample` → **`article_sample`** (WCXB has no "news" type; closest is "article").
  - Fixture JSON schema must match upstream shape. Replace the flat `"page_type": "..."` + `"ground_truth": {...}` with the full schema including `file_id`, `url`, and the nested `_internal.page_type` block. Concrete schema:
    ```python
    "gt": {
        "schema_version": "2.0",
        "url": "https://example.test/article",
        "file_id": "article_sample",
        "_internal": {"page_type": {"primary": "article"}},
        "ground_truth": {
            "title": "Breaking: nothing happened",
            "main_content": "...",
            "with":  [...],
            "without": [...],
        },
    }
    ```
  - Same shape for `product_sample` (`"primary": "product"`) and `empty_sample` (`"primary": "article"`).
- **Task 3 tests**: every reference to `news_sample` becomes `article_sample`. Expected `result["page_type"] == "news"` becomes `"article"`. Rename the test functions accordingly (`test_evaluate_page_article_returns_expected_shape`, etc.).
- **Task 3 `_load_page` / `evaluate_page`**:
  - `page_type` extraction uses **`get_page_type` from the vendored `benchmarks.wcxb.evaluate`**, not a direct field access. Real GT JSONs nest it under `_internal.page_type.primary`. Fixture JSONs follow the same shape (post-correction above), so the helper works uniformly for both.
  - `_load_page` must accept **two directory layouts**:
    1. Flat (used by fixtures): `<data_dir>/<id>.html.gz` + `<data_dir>/<id>.json`
    2. Split (used by real WCXB dev): `<data_dir>/html/<id>.html.gz` + `<data_dir>/ground-truth/<id>.json`
     Probe in that order: try flat first, fall back to split. Raise `FileNotFoundError` only if neither exists.
- **Task 6 `_iter_page_ids`**:
  - Scan for JSONs in both `<data_dir>/*.json` (flat) and `<data_dir>/ground-truth/*.json` (split). Verify the paired `.html.gz` exists in the corresponding location.
  - `type_filter` comparison uses `get_page_type(meta)`, not `meta.get("page_type")`.
- **Task 8 fetch.py**:
  - `WCXB_COMMIT = "c039d5ee9f5a3a984a0e167e63aacd04e76e78a9"`.
  - `DEV_PATH = "dev"`.
  - Manifest key format: `"html/<id>.html.gz"` and `"ground-truth/<id>.json"` (relative to `dev/`). `_fetch_all` constructs URLs as `https://raw.githubusercontent.com/{repo}/{sha}/dev/{rel_path}` and saves to `<data_dir>/dev/{rel_path}`. Adjust the default `--data-dir` so the split layout lands correctly — default to `benchmarks/wcxb/data` and the fetch writes `data/dev/html/` + `data/dev/ground-truth/`; then Task 6 default CLI path `--data-dir benchmarks/wcxb/data/dev` works uniformly.
  - Manifest generation **must use the git trees API with `recursive=1`** (not the contents API, which truncates at 1000 entries). Inside the trees response, filter entries whose `path` starts with `dev/html/` or `dev/ground-truth/`. Hash each file by fetching its raw content. Expect ~2,994 entries.
- **Task 9 README sample command** and **Task 10 Step 3 sanity script**: no change needed.

---

## Task 1: Vendor WCXB evaluate.py + 단위 테스트

**Files:**
- Create: `benchmarks/wcxb/__init__.py`
- Create: `benchmarks/wcxb/evaluate.py`
- Create: `benchmarks/wcxb/ATTRIBUTION.md`
- Create: `tests/test_wcxb_evaluate.py`

- [ ] **Step 1: Write the failing test**

File: `tests/test_wcxb_evaluate.py`

```python
"""Unit tests for the vendored WCXB word-level F1 evaluator.

WCXB's official evaluate.py computes word-level F1 between predicted text
and ground-truth main_content. We verify by construction: identical strings
→ F1=1.0, fully disjoint → F1=0.0, and a hand-computable partial case.
"""

from benchmarks.wcxb.evaluate import word_f1


def test_identical_strings_f1_is_one():
    p, r, f = word_f1("the quick brown fox", "the quick brown fox")
    assert f == 1.0
    assert p == 1.0
    assert r == 1.0


def test_disjoint_strings_f1_is_zero():
    p, r, f = word_f1("alpha beta gamma", "one two three")
    assert f == 0.0


def test_partial_overlap_matches_hand_calculation():
    # prediction = 5 words, reference = 4 words, overlap = 3 words.
    # precision = 3/5 = 0.6, recall = 3/4 = 0.75
    # f1 = 2 * 0.6 * 0.75 / (0.6 + 0.75) = 0.6666...
    p, r, f = word_f1("a b c d e", "a b c x")
    assert abs(p - 0.6) < 1e-9
    assert abs(r - 0.75) < 1e-9
    assert abs(f - (2 * 0.6 * 0.75 / (0.6 + 0.75))) < 1e-9


def test_empty_prediction_f1_is_zero():
    _, _, f = word_f1("", "hello world")
    assert f == 0.0


def test_empty_reference_f1_is_zero():
    _, _, f = word_f1("hello world", "")
    assert f == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
pytest tests/test_wcxb_evaluate.py -v
```

Expected: `ModuleNotFoundError: No module named 'benchmarks.wcxb.evaluate'`.

- [ ] **Step 3: Create `benchmarks/wcxb/__init__.py` (empty)**

File: `benchmarks/wcxb/__init__.py`

```python
"""WCXB (Web Content Extraction Benchmark) extension benchmark for trawl.

Measures trawl.extraction.html_to_markdown quality on 1,497 WCXB dev pages
against a same-environment Trafilatura baseline.

See benchmarks/wcxb/README.md and docs/superpowers/specs/2026-04-14-wcxb-benchmark-design.md.
"""
```

- [ ] **Step 4: Vendor evaluate.py**

File: `benchmarks/wcxb/evaluate.py`

- Upstream의 `evaluate.py`를 다운로드:
  ```bash
  curl -sL -o benchmarks/wcxb/evaluate.py \
    "https://raw.githubusercontent.com/Murrough-Foley/web-content-extraction-benchmark/<SHA>/<path>/evaluate.py"
  ```
  (`<SHA>`, `<path>`은 Task 0에서 확정된 값)
- 파일 맨 위에 다음 헤더 주석을 **추가**한다 (원본 코드 위에):

```python
"""Vendored from Web Content Extraction Benchmark (WCXB).

Source: https://github.com/Murrough-Foley/web-content-extraction-benchmark
Commit: <SHA from Task 0>
License: CC-BY-4.0 (see benchmarks/wcxb/ATTRIBUTION.md)

Do NOT modify. If upstream changes, re-download and update commit hash.
"""
```

- Upstream 함수명이 우리 테스트의 `word_f1`과 다르면 파일 맨 아래에 **얇은 alias**를 추가한다 (원본은 건드리지 말 것):

```python
# Test-friendly alias — keep the signature (pred, ref) -> (precision, recall, f1).
# Upstream function name may differ; wrap it here without modifying the vendor code.
def word_f1(prediction: str, reference: str) -> tuple[float, float, float]:
    # Replace the body with the actual upstream call discovered in Task 0.
    # Example if upstream exposes `compute_f1(pred, ref) -> dict`:
    #     result = compute_f1(prediction, reference)
    #     return result["precision"], result["recall"], result["f1"]
    raise NotImplementedError("wire to upstream function from Task 0 Step 4")
```

Implementation note: 실제 upstream 함수가 이미 `(precision, recall, f1)` 튜플을 반환하면 alias는 한 줄. 다른 형태면 변환만 하는 얇은 래퍼.

- [ ] **Step 5: Create ATTRIBUTION.md**

File: `benchmarks/wcxb/ATTRIBUTION.md`

```markdown
# WCXB attribution

This directory vendors files from the **Web Content Extraction Benchmark
(WCXB)** and loads its dataset (downloaded at runtime by `fetch.py`).

- Upstream: <https://github.com/Murrough-Foley/web-content-extraction-benchmark>
- License: CC-BY-4.0
- Vendored commit: `<SHA from Task 0 Step 2>`
- Vendored file(s): `evaluate.py`

WCXB dataset and evaluation code © Murrough Foley, used under CC-BY-4.0.
Dataset files are downloaded by users on demand (see `fetch.py`) and are
not redistributed in this repository.
```

- [ ] **Step 6: Run tests to verify they pass**

Run:
```bash
pytest tests/test_wcxb_evaluate.py -v
```

Expected: All 5 tests pass. 만약 upstream 구현 차이로 소수점 비교가 실패하면, 테스트의 허용 오차(`1e-9`)가 아니라 **테스트 기댓값을 upstream 정의에 맞춰 조정**한다 (예: upstream이 tokenize 전 lowercase를 적용하는 경우). alias 본문이 아니라 테스트 기댓값을 바꾸는 것이 원칙.

- [ ] **Step 7: Commit**

```bash
git add benchmarks/wcxb/__init__.py \
        benchmarks/wcxb/evaluate.py \
        benchmarks/wcxb/ATTRIBUTION.md \
        tests/test_wcxb_evaluate.py
git commit -m "feat: vendor WCXB evaluate.py with attribution and unit tests"
```

---

## Task 2: 테스트 fixture — 합성 WCXB 페이지 3개

**Purpose:** 실제 WCXB 데이터는 gitignored이므로, 러너·집계·fetch 테스트용 최소 합성 fixture를 repo에 넣는다.

**Files:**
- Create: `tests/fixtures/wcxb/news_sample.html.gz`
- Create: `tests/fixtures/wcxb/news_sample.json`
- Create: `tests/fixtures/wcxb/product_sample.html.gz`
- Create: `tests/fixtures/wcxb/product_sample.json`
- Create: `tests/fixtures/wcxb/empty_sample.html.gz`
- Create: `tests/fixtures/wcxb/empty_sample.json`

- [ ] **Step 1: Generate fixtures with a script (one-shot)**

임시 스크립트를 실행해 fixture를 만든다. 스크립트 자체는 repo에 넣지 않는다.

Run:
```bash
python - <<'PY'
import gzip, json, pathlib
root = pathlib.Path("tests/fixtures/wcxb")
root.mkdir(parents=True, exist_ok=True)

cases = {
    "news_sample": {
        "html": """<html><head><title>News</title></head><body>
            <nav>Home | About</nav>
            <article><h1>Breaking: nothing happened</h1>
            <p>The weather was normal today. Nothing to report.</p>
            <p>Experts confirm the story.</p></article>
            <footer>(c) 2026</footer></body></html>""",
        "gt": {
            "page_type": "news",
            "ground_truth": {
                "title": "Breaking: nothing happened",
                "main_content": "Breaking: nothing happened\nThe weather was normal today. Nothing to report.\nExperts confirm the story.",
                "with": ["weather was normal", "Experts confirm"],
                "without": ["Home | About", "(c) 2026"],
            },
        },
    },
    "product_sample": {
        "html": """<html><body>
            <div class="card"><h2>Starter</h2><p>$9/mo — includes basic features.</p></div>
            <div class="card"><h2>Pro</h2><p>$29/mo — includes advanced features.</p></div>
            </body></html>""",
        "gt": {
            "page_type": "product",
            "ground_truth": {
                "title": "Pricing",
                "main_content": "Starter\n$9/mo — includes basic features.\nPro\n$29/mo — includes advanced features.",
                "with": ["$9/mo", "$29/mo"],
                "without": [],
            },
        },
    },
    "empty_sample": {
        "html": "<html><body></body></html>",
        "gt": {
            "page_type": "news",
            "ground_truth": {
                "title": "",
                "main_content": "",
                "with": [],
                "without": [],
            },
        },
    },
}

for name, c in cases.items():
    (root / f"{name}.html.gz").write_bytes(gzip.compress(c["html"].encode("utf-8")))
    (root / f"{name}.json").write_text(json.dumps(c["gt"], indent=2))
print("done")
PY
```

Expected: `tests/fixtures/wcxb/` 안에 6개 파일 생성.

**Schema note:** `page_type` 과 `ground_truth` 의 실제 upstream 필드명이 Task 0에서 다른 것으로 확인되면, 위 스크립트의 key 이름을 upstream 실제 스키마로 치환 후 재실행한다.

- [ ] **Step 2: Commit**

```bash
git add tests/fixtures/wcxb/
git commit -m "test: add synthetic WCXB fixtures (news/product/empty)"
```

---

## Task 3: Single-page runner 함수 — trawl 경로

**Files:**
- Create: `benchmarks/wcxb/run.py`
- Create: `tests/test_wcxb_runner.py`

- [ ] **Step 1: Write the failing test**

File: `tests/test_wcxb_runner.py`

```python
"""Unit tests for the WCXB runner's single-page evaluation path.

The runner exposes `evaluate_page(data_dir, page_id)` which returns a dict
with keys matching the spec's raw.json schema. These tests use the
synthetic fixtures committed under tests/fixtures/wcxb/.
"""

from pathlib import Path

from benchmarks.wcxb.run import evaluate_page

FIXTURES = Path(__file__).parent / "fixtures" / "wcxb"


def test_evaluate_page_news_returns_expected_shape():
    result = evaluate_page(FIXTURES, "news_sample")

    assert result["id"] == "news_sample"
    assert result["page_type"] == "news"

    trawl = result["trawl"]
    assert isinstance(trawl["f1"], float)
    assert 0.0 <= trawl["f1"] <= 1.0
    assert isinstance(trawl["precision"], float)
    assert isinstance(trawl["recall"], float)
    assert trawl["time_ms"] >= 0
    assert isinstance(trawl["output_len"], int)
    assert trawl["error"] is None


def test_evaluate_page_news_trawl_f1_is_substantial():
    # Fixture is deliberately simple; expect trawl to recover most of the
    # main_content. We don't assert a tight bound — we just confirm the
    # pipeline is wired and not returning zero.
    result = evaluate_page(FIXTURES, "news_sample")
    assert result["trawl"]["f1"] > 0.5


def test_evaluate_page_empty_body_f1_is_zero_no_error():
    result = evaluate_page(FIXTURES, "empty_sample")
    assert result["trawl"]["error"] is None
    assert result["trawl"]["f1"] == 0.0


def test_evaluate_page_missing_files_raises():
    import pytest

    with pytest.raises(FileNotFoundError):
        evaluate_page(FIXTURES, "does_not_exist")
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
pytest tests/test_wcxb_runner.py -v
```

Expected: `ModuleNotFoundError: No module named 'benchmarks.wcxb.run'`.

- [ ] **Step 3: Implement `evaluate_page` (trawl path only)**

File: `benchmarks/wcxb/run.py`

```python
"""WCXB runner — loads each WCXB page, evaluates trawl and Trafilatura
baselines against the ground truth, writes raw.json + report.md.

Entrypoint: `python -m benchmarks.wcxb.run` (see argparse block at bottom).
"""

from __future__ import annotations

import gzip
import json
import time
from pathlib import Path

from trawl.extraction import html_to_markdown

from benchmarks.wcxb.evaluate import word_f1


def _load_page(data_dir: Path, page_id: str) -> tuple[str, dict]:
    html_path = data_dir / f"{page_id}.html.gz"
    json_path = data_dir / f"{page_id}.json"
    if not html_path.exists() or not json_path.exists():
        raise FileNotFoundError(f"WCXB page {page_id!r} not found under {data_dir}")
    html = gzip.decompress(html_path.read_bytes()).decode("utf-8", errors="replace")
    gt = json.loads(json_path.read_text())
    return html, gt


def _run_extractor(fn, html: str) -> tuple[str, int, str | None]:
    """Run an extractor, return (output, time_ms, error_or_none)."""
    t0 = time.perf_counter()
    try:
        out = fn(html) or ""
    except Exception as exc:
        return "", int((time.perf_counter() - t0) * 1000), f"{type(exc).__name__}: {exc}"
    return out, int((time.perf_counter() - t0) * 1000), None


def _score(output: str, ground_truth_text: str) -> dict:
    if not output:
        return {"f1": 0.0, "precision": 0.0, "recall": 0.0}
    p, r, f = word_f1(output, ground_truth_text)
    return {"f1": f, "precision": p, "recall": r}


def evaluate_page(data_dir: Path, page_id: str) -> dict:
    """Evaluate trawl on a single WCXB page.

    Returns the raw.json schema entry for this page with the trawl path
    filled in. Trafilatura baseline is added by `evaluate_page_with_baseline`
    (Task 4).
    """
    html, gt = _load_page(Path(data_dir), page_id)
    ground_truth_text = gt["ground_truth"]["main_content"]

    trawl_out, t_ms, err = _run_extractor(html_to_markdown, html)
    scores = _score(trawl_out, ground_truth_text)

    return {
        "id": page_id,
        "url": gt.get("url"),
        "page_type": gt.get("page_type"),
        "trawl": {
            **scores,
            "time_ms": t_ms,
            "output_len": len(trawl_out),
            "error": err,
        },
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
pytest tests/test_wcxb_runner.py -v
```

Expected: 4 tests pass.

- [ ] **Step 5: Commit**

```bash
git add benchmarks/wcxb/run.py tests/test_wcxb_runner.py
git commit -m "feat(wcxb): add single-page trawl evaluation function"
```

---

## Task 4: Single-page runner — Trafilatura baseline 경로

**Files:**
- Modify: `benchmarks/wcxb/run.py`
- Modify: `tests/test_wcxb_runner.py`

- [ ] **Step 1: Add failing test for baseline path**

Append to `tests/test_wcxb_runner.py`:

```python
from benchmarks.wcxb.run import evaluate_page_with_baseline


def test_evaluate_page_with_baseline_has_both_extractors():
    result = evaluate_page_with_baseline(FIXTURES, "news_sample")
    assert "trawl" in result
    assert "trafilatura" in result

    traf = result["trafilatura"]
    assert isinstance(traf["f1"], float)
    assert traf["time_ms"] >= 0
    assert isinstance(traf["output_len"], int)
    assert traf["error"] is None


def test_evaluate_page_with_baseline_snippet_counts_present():
    result = evaluate_page_with_baseline(FIXTURES, "news_sample")
    w = result["with_snippets_hit"]
    wo = result["without_snippets_hit"]
    assert w["total"] == 2   # fixture has 2 with-snippets
    assert wo["total"] == 2  # fixture has 2 without-snippets
    assert 0 <= w["trawl"] <= w["total"]
    assert 0 <= w["trafilatura"] <= w["total"]
    assert 0 <= wo["trawl"] <= wo["total"]
    assert 0 <= wo["trafilatura"] <= wo["total"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
pytest tests/test_wcxb_runner.py -v
```

Expected: `ImportError: cannot import name 'evaluate_page_with_baseline'`.

- [ ] **Step 3: Extend `run.py` with baseline path**

Append to `benchmarks/wcxb/run.py` (above the `evaluate_page` definition or after — keep logical order):

```python
import trafilatura


# Same options as trawl.extraction._safe_trafilatura uses, minus precision/recall
# flags. This isolates the effect of trawl's 3-way + BS fallback vs plain
# Trafilatura markdown output on the same environment.
_TRAF_KWARGS = dict(
    output_format="markdown",
    include_links=True,
    include_images=False,
    include_tables=True,
    include_comments=False,
)


def _trafilatura_baseline(html: str) -> str:
    return trafilatura.extract(html, **_TRAF_KWARGS) or ""


def _count_snippets_hit(output: str, snippets: list[str]) -> int:
    return sum(1 for s in snippets if s and s in output)


def evaluate_page_with_baseline(data_dir: Path, page_id: str) -> dict:
    """Evaluate trawl + Trafilatura baseline on a single WCXB page.

    Returns the full raw.json schema entry per the design spec, including
    with/without snippet hit counts.
    """
    html, gt = _load_page(Path(data_dir), page_id)
    ground_truth_text = gt["ground_truth"]["main_content"]
    with_snips = gt["ground_truth"].get("with", []) or []
    without_snips = gt["ground_truth"].get("without", []) or []

    trawl_out, t_ms, t_err = _run_extractor(html_to_markdown, html)
    traf_out, b_ms, b_err = _run_extractor(_trafilatura_baseline, html)

    return {
        "id": page_id,
        "url": gt.get("url"),
        "page_type": gt.get("page_type"),
        "trawl": {
            **_score(trawl_out, ground_truth_text),
            "time_ms": t_ms,
            "output_len": len(trawl_out),
            "error": t_err,
        },
        "trafilatura": {
            **_score(traf_out, ground_truth_text),
            "time_ms": b_ms,
            "output_len": len(traf_out),
            "error": b_err,
        },
        "with_snippets_hit": {
            "trawl": _count_snippets_hit(trawl_out, with_snips),
            "trafilatura": _count_snippets_hit(traf_out, with_snips),
            "total": len(with_snips),
        },
        "without_snippets_hit": {
            "trawl": _count_snippets_hit(trawl_out, without_snips),
            "trafilatura": _count_snippets_hit(traf_out, without_snips),
            "total": len(without_snips),
        },
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
pytest tests/test_wcxb_runner.py -v
```

Expected: 6 tests pass.

- [ ] **Step 5: Commit**

```bash
git add benchmarks/wcxb/run.py tests/test_wcxb_runner.py
git commit -m "feat(wcxb): add Trafilatura baseline path and snippet hit counts"
```

---

## Task 5: 집계 + 리포트 렌더링

**Files:**
- Create: `benchmarks/wcxb/aggregate.py`
- Create: `tests/test_wcxb_aggregate.py`

- [ ] **Step 1: Write the failing test**

File: `tests/test_wcxb_aggregate.py`

```python
"""Unit tests for WCXB aggregation + report rendering.

These cover the pure-function layer: given a list of raw.json entries,
produce the summary stats and the rendered report.md string.
"""

from benchmarks.wcxb.aggregate import aggregate, render_report


def _mk(id_, ptype, trawl_f1, traf_f1, trawl_err=None, traf_err=None):
    return {
        "id": id_,
        "url": None,
        "page_type": ptype,
        "trawl": {"f1": trawl_f1, "precision": trawl_f1, "recall": trawl_f1,
                  "time_ms": 30, "output_len": 100, "error": trawl_err},
        "trafilatura": {"f1": traf_f1, "precision": traf_f1, "recall": traf_f1,
                        "time_ms": 20, "output_len": 90, "error": traf_err},
        "with_snippets_hit":    {"trawl": 0, "trafilatura": 0, "total": 0},
        "without_snippets_hit": {"trawl": 0, "trafilatura": 0, "total": 0},
    }


def test_aggregate_overall_excludes_errored_rows():
    entries = [
        _mk("a", "news", 0.9, 0.9),
        _mk("b", "news", 0.8, 0.7),
        _mk("c", "news", 0.0, 0.0, trawl_err="boom"),  # excluded
    ]
    agg = aggregate(entries)
    assert agg["overall"]["n_included"] == 2
    assert agg["overall"]["trawl"]["f1"] == 0.85
    assert agg["errors"]["trawl"] == 1
    assert agg["errors"]["trafilatura"] == 0


def test_aggregate_by_type_groups():
    entries = [
        _mk("a", "news", 0.9, 0.9),
        _mk("b", "product", 0.5, 0.4),
        _mk("c", "news", 0.8, 0.8),
    ]
    agg = aggregate(entries)
    by_type = {r["type"]: r for r in agg["by_type"]}
    assert by_type["news"]["n"] == 2
    assert abs(by_type["news"]["trawl_f1"] - 0.85) < 1e-9
    assert by_type["product"]["n"] == 1
    assert abs(by_type["product"]["delta"] - 0.1) < 1e-9  # 0.5 - 0.4


def test_aggregate_top_wins_and_losses_sorted_by_delta():
    entries = [
        _mk("win1",  "news", 0.9, 0.5),   # +0.4
        _mk("win2",  "news", 0.8, 0.5),   # +0.3
        _mk("loss1", "news", 0.5, 0.9),   # -0.4
        _mk("tie",   "news", 0.5, 0.5),   #  0.0
    ]
    agg = aggregate(entries, top_n=2)
    assert [w["id"] for w in agg["top_wins"]] == ["win1", "win2"]
    assert [l["id"] for l in agg["top_losses"]] == ["loss1"]


def test_render_report_contains_required_sections():
    entries = [_mk("a", "news", 0.9, 0.9)]
    agg = aggregate(entries)
    md = render_report(agg, corpus_label="dev", commit="abc123", n_pages=1)

    assert "# WCXB" in md
    assert "Corpus:" in md
    assert "Commit: abc123" in md
    assert "## Overall" in md
    assert "## By page type" in md
    assert "## Top" in md
    assert "## Errors" in md
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
pytest tests/test_wcxb_aggregate.py -v
```

Expected: `ModuleNotFoundError: No module named 'benchmarks.wcxb.aggregate'`.

- [ ] **Step 3: Implement `aggregate.py`**

File: `benchmarks/wcxb/aggregate.py`

```python
"""WCXB result aggregation + report rendering.

Pure functions: (list of raw entries) -> summary dict -> markdown string.
No IO. Kept separate from run.py to make the reporting layer independently
testable and reusable by future Phase 2 tooling.
"""

from __future__ import annotations

from statistics import mean, median
from typing import Iterable


def _ok(entry: dict, key: str) -> bool:
    return entry[key]["error"] is None


def _mean_or_zero(values: list[float]) -> float:
    return mean(values) if values else 0.0


def aggregate(entries: Iterable[dict], top_n: int = 10) -> dict:
    """Compute overall + per-type F1 summaries and top wins/losses.

    Rows with an error on *either* extractor are excluded from the averaged
    comparison (both columns need to be valid for Δ to be meaningful) but
    errors are counted separately.
    """
    entries = list(entries)

    both_ok = [e for e in entries if _ok(e, "trawl") and _ok(e, "trafilatura")]

    def _agg_block(rows: list[dict], key: str) -> dict:
        if not rows:
            return {"f1": 0.0, "precision": 0.0, "recall": 0.0, "median_time_ms": 0}
        return {
            "f1": _mean_or_zero([r[key]["f1"] for r in rows]),
            "precision": _mean_or_zero([r[key]["precision"] for r in rows]),
            "recall": _mean_or_zero([r[key]["recall"] for r in rows]),
            "median_time_ms": int(median([r[key]["time_ms"] for r in rows])),
        }

    overall = {
        "n_included": len(both_ok),
        "trawl": _agg_block(both_ok, "trawl"),
        "trafilatura": _agg_block(both_ok, "trafilatura"),
    }
    overall["delta_f1"] = overall["trawl"]["f1"] - overall["trafilatura"]["f1"]

    by_type: dict[str, list[dict]] = {}
    for e in both_ok:
        by_type.setdefault(e["page_type"] or "unknown", []).append(e)

    by_type_rows = []
    for ptype, rows in sorted(by_type.items()):
        t_f1 = _mean_or_zero([r["trawl"]["f1"] for r in rows])
        b_f1 = _mean_or_zero([r["trafilatura"]["f1"] for r in rows])
        by_type_rows.append({
            "type": ptype,
            "n": len(rows),
            "trawl_f1": t_f1,
            "trafilatura_f1": b_f1,
            "delta": t_f1 - b_f1,
        })

    ranked = sorted(both_ok, key=lambda e: e["trawl"]["f1"] - e["trafilatura"]["f1"], reverse=True)
    top_wins = [{"id": e["id"], "delta": e["trawl"]["f1"] - e["trafilatura"]["f1"]}
                for e in ranked[:top_n] if (e["trawl"]["f1"] - e["trafilatura"]["f1"]) > 0]
    top_losses = [{"id": e["id"], "delta": e["trawl"]["f1"] - e["trafilatura"]["f1"]}
                  for e in list(reversed(ranked))[:top_n] if (e["trawl"]["f1"] - e["trafilatura"]["f1"]) < 0]

    errors = {
        "trawl": sum(1 for e in entries if e["trawl"]["error"] is not None),
        "trafilatura": sum(1 for e in entries if e["trafilatura"]["error"] is not None),
        "trawl_ids": [e["id"] for e in entries if e["trawl"]["error"] is not None],
        "trafilatura_ids": [e["id"] for e in entries if e["trafilatura"]["error"] is not None],
    }

    return {
        "overall": overall,
        "by_type": by_type_rows,
        "top_wins": top_wins,
        "top_losses": top_losses,
        "errors": errors,
    }


def render_report(agg: dict, *, corpus_label: str, commit: str, n_pages: int,
                  timestamp: str = "") -> str:
    o = agg["overall"]
    lines = [f"# WCXB extraction benchmark — {timestamp}".rstrip(),
             "",
             f"Corpus: WCXB {corpus_label} split, {n_pages} pages.",
             f"Commit: {commit}",
             "",
             "## Overall",
             "",
             "| Extractor   | F1    | Precision | Recall | Median time |",
             "|-------------|-------|-----------|--------|-------------|",
             f"| trawl       | {o['trawl']['f1']:.3f} | {o['trawl']['precision']:.3f}     | {o['trawl']['recall']:.3f}  | {o['trawl']['median_time_ms']:>3d} ms      |",
             f"| trafilatura | {o['trafilatura']['f1']:.3f} | {o['trafilatura']['precision']:.3f}     | {o['trafilatura']['recall']:.3f}  | {o['trafilatura']['median_time_ms']:>3d} ms      |",
             "",
             f"Δ F1 (trawl − trafilatura) = {o['delta_f1']:+.3f}",
             "",
             "## By page type",
             "",
             "| Type        |  N  | trawl F1 | traf F1 | Δ      |",
             "|-------------|-----|----------|---------|--------|"]
    for r in agg["by_type"]:
        lines.append(f"| {r['type']:<11} | {r['n']:>3d} | {r['trawl_f1']:.3f}    | {r['trafilatura_f1']:.3f}   | {r['delta']:+.3f} |")

    lines += ["",
              f"## Top {len(agg['top_wins'])} trawl wins (Δ F1)",
              ""]
    for w in agg["top_wins"]:
        lines.append(f"- {w['id']}: {w['delta']:+.3f}")

    lines += ["",
              f"## Top {len(agg['top_losses'])} trawl losses (Δ F1)",
              ""]
    for l in agg["top_losses"]:
        lines.append(f"- {l['id']}: {l['delta']:+.3f}")

    err = agg["errors"]
    lines += ["",
              "## Errors",
              f"trawl: {err['trawl']} ({', '.join(err['trawl_ids']) or 'none'})",
              f"trafilatura: {err['trafilatura']} ({', '.join(err['trafilatura_ids']) or 'none'})",
              ""]
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
pytest tests/test_wcxb_aggregate.py -v
```

Expected: 4 tests pass.

- [ ] **Step 5: Commit**

```bash
git add benchmarks/wcxb/aggregate.py tests/test_wcxb_aggregate.py
git commit -m "feat(wcxb): add aggregation and markdown report rendering"
```

---

## Task 6: CLI + 결과 write-out

**Files:**
- Modify: `benchmarks/wcxb/run.py`
- Modify: `tests/test_wcxb_runner.py`

- [ ] **Step 1: Add failing test for end-to-end runner on fixtures**

Append to `tests/test_wcxb_runner.py`:

```python
import json as _json
from benchmarks.wcxb.run import run_all


def test_run_all_writes_raw_and_report(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    exit_code = run_all(
        data_dir=FIXTURES,
        out_dir=out_dir,
        limit=None,
        type_filter=None,
        no_baseline=False,
    )
    assert exit_code == 0

    raw = _json.loads((out_dir / "raw.json").read_text())
    assert isinstance(raw, list)
    # fixture has 3 samples — all should be processed
    assert {e["id"] for e in raw} == {"news_sample", "product_sample", "empty_sample"}

    report = (out_dir / "report.md").read_text()
    assert "# WCXB" in report
    assert "## Overall" in report


def test_run_all_respects_limit(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    run_all(data_dir=FIXTURES, out_dir=out_dir, limit=1, type_filter=None, no_baseline=False)
    raw = _json.loads((out_dir / "raw.json").read_text())
    assert len(raw) == 1


def test_run_all_type_filter(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    run_all(data_dir=FIXTURES, out_dir=out_dir, limit=None, type_filter="product", no_baseline=False)
    raw = _json.loads((out_dir / "raw.json").read_text())
    assert {e["id"] for e in raw} == {"product_sample"}


def test_run_all_no_baseline_sets_trafilatura_null(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    run_all(data_dir=FIXTURES, out_dir=out_dir, limit=None, type_filter=None, no_baseline=True)
    raw = _json.loads((out_dir / "raw.json").read_text())
    for e in raw:
        assert e["trafilatura"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
pytest tests/test_wcxb_runner.py -v
```

Expected: `ImportError: cannot import name 'run_all'`.

- [ ] **Step 3: Implement `run_all` + argparse entry**

Append to `benchmarks/wcxb/run.py`:

```python
import argparse
import json as _json
import subprocess
import sys
from datetime import datetime

from benchmarks.wcxb.aggregate import aggregate, render_report


def _iter_page_ids(data_dir: Path, type_filter: str | None) -> list[str]:
    ids = []
    for jpath in sorted(data_dir.glob("*.json")):
        if not (data_dir / f"{jpath.stem}.html.gz").exists():
            continue
        if type_filter:
            try:
                meta = _json.loads(jpath.read_text())
            except Exception:
                continue
            if meta.get("page_type") != type_filter:
                continue
        ids.append(jpath.stem)
    return ids


def _git_short_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True
        ).strip()
    except Exception:
        return "unknown"


def run_all(*, data_dir: Path, out_dir: Path, limit: int | None,
            type_filter: str | None, no_baseline: bool) -> int:
    data_dir = Path(data_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    ids = _iter_page_ids(data_dir, type_filter)
    if limit:
        ids = ids[:limit]

    results = []
    for i, page_id in enumerate(ids, start=1):
        try:
            if no_baseline:
                entry = evaluate_page(data_dir, page_id)
                entry["trafilatura"] = None
                entry.setdefault("with_snippets_hit", None)
                entry.setdefault("without_snippets_hit", None)
            else:
                entry = evaluate_page_with_baseline(data_dir, page_id)
        except FileNotFoundError:
            continue
        results.append(entry)

        if i % 100 == 0 or i == len(ids):
            rows_ok = [r for r in results if r["trawl"]["error"] is None
                       and (no_baseline or r["trafilatura"]["error"] is None)]
            if rows_ok:
                t_avg = sum(r["trawl"]["f1"] for r in rows_ok) / len(rows_ok)
                if no_baseline:
                    print(f"[{i}/{len(ids)}] trawl avg F1={t_avg:.3f}", file=sys.stderr)
                else:
                    b_avg = sum(r["trafilatura"]["f1"] for r in rows_ok) / len(rows_ok)
                    print(f"[{i}/{len(ids)}] trawl avg F1={t_avg:.3f}, traf avg F1={b_avg:.3f}",
                          file=sys.stderr)

    (out_dir / "raw.json").write_text(_json.dumps(results, indent=2, ensure_ascii=False))

    if not no_baseline:
        agg = aggregate(results)
        report = render_report(
            agg,
            corpus_label="dev" if data_dir.name == "dev" else data_dir.name,
            commit=_git_short_sha(),
            n_pages=len(results),
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M"),
        )
        (out_dir / "report.md").write_text(report)
    else:
        (out_dir / "report.md").write_text(
            "# WCXB extraction benchmark — trawl-only run (--no-baseline)\n\n"
            "See raw.json for per-page results.\n"
        )

    # Exit non-zero if overall trawl error rate exceeds 5%
    n_err = sum(1 for r in results if r["trawl"]["error"] is not None)
    if results and n_err / len(results) >= 0.05:
        print(f"ERROR: trawl error rate {n_err}/{len(results)} ≥ 5%", file=sys.stderr)
        return 1
    return 0


def _main() -> int:
    p = argparse.ArgumentParser(description="Run the WCXB extraction benchmark.")
    p.add_argument("--data-dir", default="benchmarks/wcxb/data/dev", type=Path)
    p.add_argument("--out-dir", default=None, type=Path,
                   help="Output directory (default: benchmarks/results/wcxb_<timestamp>)")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--type", dest="type_filter", default=None,
                   help="Restrict to a single page_type (e.g. news, product, forum)")
    p.add_argument("--no-baseline", action="store_true",
                   help="Skip Trafilatura baseline (trawl only)")
    args = p.parse_args()

    if args.out_dir is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        args.out_dir = Path("benchmarks/results") / f"wcxb_{ts}"

    return run_all(
        data_dir=args.data_dir,
        out_dir=args.out_dir,
        limit=args.limit,
        type_filter=args.type_filter,
        no_baseline=args.no_baseline,
    )


if __name__ == "__main__":
    sys.exit(_main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
pytest tests/test_wcxb_runner.py -v
```

Expected: 10 tests pass (6 prior + 4 new).

- [ ] **Step 5: Commit**

```bash
git add benchmarks/wcxb/run.py tests/test_wcxb_runner.py
git commit -m "feat(wcxb): add run_all orchestrator, argparse CLI, progress logging"
```

---

## Task 7: Sanity check 경로 — Trafilatura default mode

**Purpose:** Spec §Success criteria 요구사항. 마크다운 옵션 없이 default-mode Trafilatura로 한 번 더 돌려 WCXB dev 전체 공개 F1 **0.791** 과 **±0.025 이내** 재현되는지 확인. 결과는 `raw.json`의 별도 필드에만 기록하고 메인 표엔 섞지 않는다. (초기 설계 draft에 박혔던 "0.958"은 article-only 구버전 수치였으므로 쓰지 않는다.)

**Files:**
- Modify: `benchmarks/wcxb/run.py`
- Modify: `tests/test_wcxb_runner.py`

- [ ] **Step 1: Add failing test**

Append to `tests/test_wcxb_runner.py`:

```python
def test_evaluate_page_with_baseline_includes_sanity_field():
    result = evaluate_page_with_baseline(FIXTURES, "news_sample")
    assert "sanity_traf_default" in result
    sd = result["sanity_traf_default"]
    assert "f1" in sd
    assert isinstance(sd["f1"], float)
    assert sd["error"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
pytest tests/test_wcxb_runner.py::test_evaluate_page_with_baseline_includes_sanity_field -v
```

Expected: `KeyError: 'sanity_traf_default'`.

- [ ] **Step 3: Extend `evaluate_page_with_baseline`**

In `benchmarks/wcxb/run.py`, just above the `return { ... }` in `evaluate_page_with_baseline`, add:

```python
    # Sanity check: Trafilatura in default mode (no markdown flags), matching
    # how WCXB upstream measured the published dev-set F1=0.791. Used once
    # after a full run to verify the vendored evaluate.py reproduces the
    # public number. (Note: 0.958 that appeared in some earlier notes is the
    # article-only sub-score, not the 7-type dev total — do not conflate.)
    sanity_out, s_ms, s_err = _run_extractor(
        lambda h: trafilatura.extract(h) or "", html
    )
    sanity = {
        **_score(sanity_out, ground_truth_text),
        "time_ms": s_ms,
        "error": s_err,
    }
```

And add `"sanity_traf_default": sanity,` to the returned dict.

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
pytest tests/test_wcxb_runner.py -v
```

Expected: all 11 tests pass.

- [ ] **Step 5: Commit**

```bash
git add benchmarks/wcxb/run.py tests/test_wcxb_runner.py
git commit -m "feat(wcxb): add Trafilatura default-mode sanity field"
```

---

## Task 8: fetch.py — 스냅샷 다운로드

**Files:**
- Create: `benchmarks/wcxb/fetch.py`
- Create: `tests/test_wcxb_fetch.py`

**Note:** 실제 네트워크 호출은 테스트에서 피한다. `fetch.py`는 `download_one(url, dest, expected_sha256)` 같은 작은 함수로 분해해, 해시 검증과 idempotency 를 로컬 fixture(또는 `file://` URL)로 테스트 가능하게 만든다.

- [ ] **Step 1: Write the failing test**

File: `tests/test_wcxb_fetch.py`

```python
import hashlib
from pathlib import Path

from benchmarks.wcxb.fetch import download_one, verify_sha256, HashMismatch


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_download_one_idempotent_with_matching_hash(tmp_path):
    src = tmp_path / "src.txt"
    src.write_bytes(b"hello wcxb")
    expected = _sha256(src)

    dest = tmp_path / "dest.txt"
    assert download_one(src.as_uri(), dest, expected) is True   # downloaded
    assert download_one(src.as_uri(), dest, expected) is False  # already there, skipped


def test_download_one_raises_on_hash_mismatch(tmp_path):
    src = tmp_path / "src.txt"
    src.write_bytes(b"hello")
    dest = tmp_path / "dest.txt"

    import pytest
    with pytest.raises(HashMismatch):
        download_one(src.as_uri(), dest, "0" * 64)


def test_verify_sha256_true_for_match(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"abc")
    assert verify_sha256(p, _sha256(p)) is True


def test_verify_sha256_false_for_mismatch(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"abc")
    assert verify_sha256(p, "0" * 64) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
pytest tests/test_wcxb_fetch.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement `fetch.py`**

File: `benchmarks/wcxb/fetch.py`

```python
"""Download the WCXB dev split snapshot locally (idempotent).

The manifest file (`benchmarks/wcxb/manifest.json`, generated by this
script on first run or committed by a maintainer) lists every
`<id>.html.gz` and `<id>.json` file with its SHA-256. Subsequent runs
re-verify hashes and skip already-downloaded files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path


WCXB_REPO = "Murrough-Foley/web-content-extraction-benchmark"
WCXB_COMMIT = "<SHA from Task 0 Step 2>"
DEV_PATH = "<dev path from Task 0>"  # e.g. "data/dev"


class HashMismatch(RuntimeError):
    pass


def verify_sha256(path: Path, expected: str) -> bool:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest() == expected


def download_one(url: str, dest: Path, expected_sha256: str) -> bool:
    """Download url to dest. Skip if dest exists and hash matches.

    Returns True if a download occurred, False if skipped. Raises
    HashMismatch if the downloaded bytes don't match the expected hash.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and verify_sha256(dest, expected_sha256):
        return False
    with urllib.request.urlopen(url) as resp, dest.open("wb") as f:
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
    if not verify_sha256(dest, expected_sha256):
        dest.unlink(missing_ok=True)
        raise HashMismatch(f"{url} -> {dest}: hash mismatch")
    return True


def _load_manifest(manifest_path: Path) -> dict:
    if not manifest_path.exists():
        raise SystemExit(
            f"Manifest not found at {manifest_path}. Regenerate with --refresh-manifest "
            f"or check out a commit that includes it."
        )
    return json.loads(manifest_path.read_text())


def _fetch_all(manifest: dict, data_dir: Path) -> tuple[int, int]:
    downloaded = 0
    skipped = 0
    base = f"https://raw.githubusercontent.com/{WCXB_REPO}/{WCXB_COMMIT}/{DEV_PATH}"
    for rel_path, sha in manifest.items():
        url = f"{base}/{rel_path}"
        dest = data_dir / rel_path
        did = download_one(url, dest, sha)
        if did:
            downloaded += 1
        else:
            skipped += 1
    return downloaded, skipped


def _main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--data-dir", default=Path("benchmarks/wcxb/data/dev"), type=Path)
    p.add_argument("--manifest", default=Path("benchmarks/wcxb/manifest.json"), type=Path)
    p.add_argument("--refresh-manifest", action="store_true",
                   help="Re-enumerate upstream dev directory and regenerate manifest.json.")
    args = p.parse_args()

    if args.refresh_manifest:
        # Uses the GitHub REST API to list the dev directory and fetch each
        # file's sha256. This is a maintainer operation, run once per pinned
        # commit. Implementation left to the maintainer who knows the exact
        # upstream structure from Task 0.
        raise SystemExit("--refresh-manifest not implemented in Phase 1. "
                         "Generate manifest.json manually from upstream and commit it.")

    manifest = _load_manifest(args.manifest)
    downloaded, skipped = _fetch_all(manifest, args.data_dir)
    print(f"Fetched {downloaded}, skipped {skipped}, total {len(manifest)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
```

**Maintainer note in-file:** `--refresh-manifest`는 Phase 1 밖이다. 초회 manifest는 maintainer가 Task 0 결과를 바탕으로 수동 작성 후 커밋한다 (다음 step).

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
pytest tests/test_wcxb_fetch.py -v
```

Expected: 4 tests pass.

- [ ] **Step 5: Generate manifest.json (manual, one-time)**

다음 스크립트로 upstream `DEV_PATH/` 하위 모든 `.html.gz` 및 `.json` 파일의 SHA-256을 수집해 `benchmarks/wcxb/manifest.json`을 생성한다. Task 0에서 얻은 정확한 upstream 경로로 치환:

Run:
```bash
python - <<'PY'
import json, urllib.request, hashlib, sys
# Substitute with actual values from Task 0:
repo = "Murrough-Foley/web-content-extraction-benchmark"
commit = "<SHA>"
dev_path = "<dev path>"

# List dir via GitHub API (handles truncation for large dirs):
api = f"https://api.github.com/repos/{repo}/contents/{dev_path}?ref={commit}"
items = json.load(urllib.request.urlopen(api))
assert isinstance(items, list) and items, "unexpected GitHub API response"

manifest = {}
for it in items:
    if it["type"] != "file":
        continue
    name = it["name"]
    raw_url = it["download_url"]
    data = urllib.request.urlopen(raw_url).read()
    manifest[name] = hashlib.sha256(data).hexdigest()

import pathlib
pathlib.Path("benchmarks/wcxb/manifest.json").write_text(
    json.dumps(manifest, indent=2, sort_keys=True)
)
print(f"manifest.json written with {len(manifest)} entries")
PY
```

Expected: `benchmarks/wcxb/manifest.json` contains 1,497 HTML.gz + 1,497 JSON = ~2,994 entries (actual count verified in Task 0 Step 1).

**If the directory has > 1000 files**, the GitHub contents API may truncate. In that case, use the git trees API with `recursive=true` instead — substitute the `api = ...` URL with:
```
https://api.github.com/repos/{repo}/git/trees/{commit}?recursive=1
```
and filter to entries whose `path` starts with `dev_path`.

- [ ] **Step 6: Verify fetch.py works against real upstream (smoke only, 5 files)**

```bash
# Create a trimmed manifest with 5 entries for smoke:
python -c "
import json, pathlib
m = json.loads(pathlib.Path('benchmarks/wcxb/manifest.json').read_text())
items = dict(list(m.items())[:5])
pathlib.Path('/tmp/wcxb_smoke_manifest.json').write_text(json.dumps(items))
"
python benchmarks/wcxb/fetch.py --manifest /tmp/wcxb_smoke_manifest.json
# Second run should skip all:
python benchmarks/wcxb/fetch.py --manifest /tmp/wcxb_smoke_manifest.json
```

Expected first run: `Fetched 5, skipped 0, total 5`.
Expected second run: `Fetched 0, skipped 5, total 5`.
No HashMismatch.

- [ ] **Step 7: Commit**

```bash
git add benchmarks/wcxb/fetch.py \
        benchmarks/wcxb/manifest.json \
        tests/test_wcxb_fetch.py
git commit -m "feat(wcxb): add fetch.py with pinned manifest for dev split"
```

---

## Task 9: README + CLAUDE.md + .gitignore

**Files:**
- Create: `benchmarks/wcxb/README.md`
- Modify: `.gitignore`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Create `benchmarks/wcxb/README.md`**

File: `benchmarks/wcxb/README.md`

```markdown
# WCXB extraction benchmark

One-shot external benchmark of `trawl.extraction.html_to_markdown` vs a
same-environment Trafilatura baseline on the WCXB dev split (1,497 pages,
7 page types, 1,613 domains, CC-BY-4.0).

See [`../../docs/superpowers/specs/2026-04-14-wcxb-benchmark-design.md`](../../docs/superpowers/specs/2026-04-14-wcxb-benchmark-design.md) for the full design.

## Run

```bash
# 1. Download the snapshot (~one-time, uses pinned manifest.json)
mamba run -n trawl python benchmarks/wcxb/fetch.py

# 2. Run the benchmark
mamba run -n trawl python benchmarks/wcxb/run.py
```

Results land under `benchmarks/results/wcxb_<timestamp>/`:
- `raw.json` — per-page F1, precision, recall, time, output length, errors
- `report.md` — overall + per-type summary, top wins/losses, error counts

Useful flags:
- `--limit 50` smoke-test subset
- `--type forum` restrict to one page type
- `--no-baseline` trawl only (faster, no comparison)

## Layout

- `fetch.py` — downloads files listed in `manifest.json` to `data/dev/`
  (gitignored) and verifies SHA-256.
- `run.py` — orchestrator + argparse CLI.
- `aggregate.py` — pure-function aggregation + report rendering.
- `evaluate.py` — vendored WCXB word-F1 evaluator (CC-BY-4.0, see `ATTRIBUTION.md`).
- `manifest.json` — pinned per-file SHA-256 for the snapshot. Regenerate
  with `--refresh-manifest` (Phase 2).
```

- [ ] **Step 2: Update `.gitignore`**

Check current contents, then append under the existing `# benchmark outputs` block:

```
# WCXB benchmark data (downloaded, not redistributed)
benchmarks/wcxb/data/
```

(`benchmarks/results/` is already globbed. The `wcxb_*` subdirs are covered.)

- [ ] **Step 3: Update `CLAUDE.md` Quick Reference**

In `CLAUDE.md`, find the fenced code block in the "Quick Reference" section (starts with `# First time (creates the env + installs deps)`), and add at the bottom of that block, right before the closing fence:

```bash
# WCXB external extraction benchmark (one-shot)
python benchmarks/wcxb/fetch.py && python benchmarks/wcxb/run.py
```

- [ ] **Step 4: Update `CLAUDE.md` Code layout**

In `CLAUDE.md` under the `## Code layout` section, inside the `benchmarks/` block, add entries for `wcxb/`:

```
benchmarks/
  benchmark_cases.yaml           12 cases for trawl vs Jina comparison
  run_benchmark.py               trawl (base/profile/cached) vs Jina runner
  profile_eval_cases.yaml        36 cases for VLM profile eval
  profile_eval.py                profile generation quality evaluator
  wcxb/                          external WCXB extraction benchmark (Phase 1)
    fetch.py                       snapshot download + hash verify
    run.py                         runner (trawl + Trafilatura baseline)
    aggregate.py                   summary + report rendering
    evaluate.py                    vendored WCXB word-F1 evaluator
    manifest.json                  pinned SHA-256 manifest of dev split
  results/                       gitignored benchmark outputs
```

- [ ] **Step 5: Verify nothing is broken**

Run:
```bash
pytest tests/test_wcxb_evaluate.py tests/test_wcxb_runner.py tests/test_wcxb_aggregate.py tests/test_wcxb_fetch.py -v
```

Expected: all tests still pass (15 total so far).

Also sanity-check that pre-existing pipeline tests still pass:
```bash
pytest tests/test_pipeline.py -v
```

Expected: 12/12 parity matrix cases still pass.

- [ ] **Step 6: Commit**

```bash
git add benchmarks/wcxb/README.md .gitignore CLAUDE.md
git commit -m "docs(wcxb): add README, gitignore data dir, register in CLAUDE.md"
```

---

## Task 10: 전체 실행 + README 2행 요약 반영

**Purpose:** 실제 1,497 페이지에 대해 벤치마크를 돌리고, 나온 숫자를 프로젝트 README에 반영한다.

**Files:**
- Modify: `README.md` (숫자 반영)
- Generated: `benchmarks/results/wcxb_<ts>/raw.json`, `report.md` (gitignored)

- [ ] **Step 1: 실제 스냅샷 다운로드**

Run:
```bash
mamba run -n trawl python benchmarks/wcxb/fetch.py
```

Expected: `Fetched ~2994, skipped 0, total ~2994`. 재실행 시 모두 skip. HashMismatch 에러 없음.

- [ ] **Step 2: 전체 벤치마크 실행**

Run:
```bash
mamba run -n trawl python benchmarks/wcxb/run.py 2>&1 | tee /tmp/wcxb_run.log
```

Expected:
- 진행 로그가 100페이지마다 stderr에 출력.
- 에러율 < 5%.
- `benchmarks/results/wcxb_<timestamp>/{raw.json, report.md}` 생성.
- Exit code 0.

- [ ] **Step 3: Sanity check — 공식 F1 재현 검증**

`raw.json`에서 `sanity_traf_default.f1`의 평균이 WCXB dev 전체 공개값 **0.791** 의 **±0.025 이내**인지 확인:

```bash
python - <<'PY'
import json, pathlib, glob
latest = max(glob.glob("benchmarks/results/wcxb_*/"), key=lambda p: p)
entries = json.loads((pathlib.Path(latest) / "raw.json").read_text())
ok = [e["sanity_traf_default"]["f1"] for e in entries if e["sanity_traf_default"]["error"] is None]
mean = sum(ok) / len(ok)
print(f"sanity traf-default mean F1 = {mean:.4f} (expected ~0.791)")
assert 0.766 <= mean <= 0.816, f"sanity check failed: {mean}"
print("sanity: PASS")
PY
```

Expected: mean F1 within [0.766, 0.816], `sanity: PASS`. (Reference: 2026-04-14 실제 실행 측정치 0.773.)

**If this fails**: do NOT proceed to Step 4. Debug the vendored `evaluate.py` or the Trafilatura default-mode call. Common causes: different tokenization vs upstream (e.g. case-folding), different default Trafilatura options between library versions. Record findings in a commit message on a fix branch.

**Historical note:** 초기 설계 draft는 "0.958 ±0.02"를 기준으로 박았는데, 0.958은 WCXB가 공개한 **article-only** 서브 스코어였다. 우리가 돌리는 dev 전체(7 types)는 **0.791**이 upstream이 README에 명시한 숫자. 이 구분을 놓치지 말 것.

- [ ] **Step 4: `report.md` 확인 후 README에 반영**

열어서 확인:
```bash
cat benchmarks/results/wcxb_*/report.md | head -40
```

`README.md`의 "Evaluation" 섹션 (또는 가장 가까운 품질 지표 섹션)에 다음 스니펫을 추가:

```markdown
### External: WCXB dev (1,497 pages)

Beyond the internal 12-case parity matrix, trawl's extraction stage is
cross-validated against the [WCXB](https://github.com/Murrough-Foley/web-content-extraction-benchmark)
public benchmark (CC-BY-4.0, 1,497 dev pages across 7 page types).

| Extractor                         |   F1   |
|-----------------------------------|--------|
| trawl (`html_to_markdown`)        |  0.XXX |
| Trafilatura (same environment)    |  0.YYY |

Per-page-type breakdown and error counts: see
[`benchmarks/wcxb/README.md`](benchmarks/wcxb/README.md) and run the benchmark
locally to regenerate.
```

`0.XXX`와 `0.YYY`를 `report.md`의 실제 값으로 치환.

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "docs: report external WCXB dev F1 in README"
```

`benchmarks/results/wcxb_*/`는 gitignored이므로 커밋되지 않는다.

---

## Plan self-review notes

- **Spec coverage**: §Architecture(Task 1,3,4,5,6,8), §Runner interface(Task 6), §Error handling(Task 6 run_all, Task 1 score), §Report formats(Task 5,6), §Repository integration(Task 9), §README integration(Task 10), §Success criteria(Task 10 Step 2,3). Non-goals 명시적으로 Task 밖 유지.
- **Placeholder scan**: `<SHA>`, `<dev path>`, `<evaluate.py 경로>`는 Task 0에서 실제값으로 확정된다는 조건. placeholder가 Task 0 결과로 치환되는 구조라 의도적. Task 1/8 에서 이를 명시.
- **Type consistency**: `evaluate_page`, `evaluate_page_with_baseline`, `run_all`, `aggregate`, `render_report`, `word_f1`, `download_one`, `verify_sha256` — task 간 시그니처·반환 shape 일관.
- **File paths**: 모두 절대/상대 명시.

---

## Execution choice

Plan complete and saved to `docs/superpowers/plans/2026-04-14-wcxb-benchmark.md`.
