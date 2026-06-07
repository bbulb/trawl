# P0 Stability Foundations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first stability slice from `docs/stability-speed-improvement-report-2026-05-04.md`: a `trawl doctor` health check and BM25 degraded retrieval when the embedding service is unavailable.

**Architecture:** Add a focused diagnostics module that checks local runtime prerequisites and model endpoints without changing the pipeline. Add a retrieval fallback that converts embedding HTTP failures into BM25-ranked results with warning metadata, so MCP callers can still receive evidence when the dense embedding server is down. Keep reranker behavior unchanged: if rerank is enabled and available, it may rerank the BM25 candidate window; otherwise the BM25 order is returned.

**Tech Stack:** Python 3.10+, `httpx`, Playwright, existing `trawl.bm25`, `pytest`, existing `PipelineResult`/MCP JSON serialization.

---

## New Session `/goal` Command

Use this in a fresh session:

```text
/goal Implement P0 stability foundations for trawl using docs/stability-speed-improvement-report-2026-05-04.md and docs/superpowers/plans/2026-05-04-p0-stability-foundations.md. Add a `python -m trawl.diagnostics` / `trawl-doctor` health check for Playwright, cache paths, embedding, reranker, and optional VLM configuration. Add BM25 degraded retrieval when the embedding endpoint is unavailable, expose warning metadata through PipelineResult/MCP/telemetry, update docs, and verify with targeted tests plus `mamba run -n trawl python -m pytest`.
```

## File Structure

- Create `src/trawl/diagnostics.py`: health-check dataclasses, check functions, text/JSON rendering, CLI entry point.
- Modify `pyproject.toml`: add `trawl-doctor = "trawl.diagnostics:_cli_entry"`.
- Modify `src/trawl/retrieval.py`: add BM25 fallback on embedding HTTP failure and a `warning` field on `RetrievalResult`.
- Modify `src/trawl/pipeline.py`: add `warnings` to `PipelineResult` and propagate retrieval fallback warnings on full/profile retrieval paths.
- Modify `src/trawl/telemetry.py`: include warning metadata in opt-in telemetry.
- Modify `README.md`: document `trawl-doctor`, degraded BM25 fallback, and embedding cache operational recommendation.
- Create `tests/test_diagnostics.py`: unit tests for health-check rendering and CLI behavior with mocked checks.
- Create `tests/test_retrieval_bm25_fallback.py`: unit tests for retrieval and pipeline fallback behavior.

## Task 1: Diagnostics Module

**Files:**
- Create: `src/trawl/diagnostics.py`
- Create: `tests/test_diagnostics.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Write diagnostics tests**

Create `tests/test_diagnostics.py`:

```python
from __future__ import annotations

import json

from trawl import diagnostics


def test_exit_code_fails_only_required_failures():
    rows = [
        diagnostics.CheckResult("python", "ok", "Python runtime available", required=True),
        diagnostics.CheckResult("reranker", "warn", "reranker unavailable", required=False),
    ]
    assert diagnostics.exit_code(rows) == 0

    rows.append(
        diagnostics.CheckResult("embedding", "fail", "embedding unavailable", required=True)
    )
    assert diagnostics.exit_code(rows) == 1


def test_render_text_includes_status_and_required_marker():
    rows = [
        diagnostics.CheckResult("embedding", "fail", "ConnectError: refused", required=True),
        diagnostics.CheckResult("reranker", "warn", "not configured", required=False),
    ]

    text = diagnostics.render_text(rows)

    assert "FAIL embedding" in text
    assert "WARN reranker" in text
    assert "required" in text
    assert "optional" in text


def test_main_json_output_uses_injected_checks(capsys):
    rows = [
        diagnostics.CheckResult(
            "python",
            "ok",
            "Python runtime available",
            required=True,
            detail={"version": "3.14.4"},
        )
    ]

    code = diagnostics.main(["--json"], checks=lambda include_network: rows)

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 0
    assert payload["ok"] is True
    assert payload["checks"][0]["name"] == "python"
    assert payload["checks"][0]["detail"]["version"] == "3.14.4"


def test_main_no_network_passes_flag_to_checks(capsys):
    seen = {}

    def fake_checks(include_network: bool):
        seen["include_network"] = include_network
        return [
            diagnostics.CheckResult("python", "ok", "Python runtime available", required=True)
        ]

    code = diagnostics.main(["--no-network"], checks=fake_checks)

    captured = capsys.readouterr()
    assert code == 0
    assert seen == {"include_network": False}
    assert "OK python" in captured.out
```

- [ ] **Step 2: Run diagnostics tests to verify failure**

Run:

```bash
mamba run -n trawl python -m pytest tests/test_diagnostics.py -q
```

Expected: FAIL with `ImportError` or `AttributeError` because `trawl.diagnostics` does not exist yet.

- [ ] **Step 3: Implement diagnostics module**

Create `src/trawl/diagnostics.py`:

```python
"""Runtime health checks for trawl deployments."""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

import httpx

from . import __version__, embedding_cache, fetch_cache, reranking, retrieval, telemetry

Status = Literal["ok", "warn", "fail"]


@dataclass
class CheckResult:
    name: str
    status: Status
    message: str
    required: bool
    detail: dict = field(default_factory=dict)


def run_checks(*, include_network: bool = True) -> list[CheckResult]:
    """Run local and endpoint health checks."""
    rows = [
        check_python(),
        check_playwright_browser(),
        check_writable_path("fetch_cache", fetch_cache._cache_dir(), required=True),
        check_writable_path("embedding_cache", embedding_cache._cache_dir(), required=False),
        check_writable_path("telemetry", telemetry._target_path().parent, required=False),
        check_vlm_configured(),
    ]
    if include_network:
        rows.append(check_embedding_endpoint())
        rows.append(check_reranker_endpoint())
    else:
        rows.append(CheckResult("embedding", "warn", "network checks skipped", required=True))
        rows.append(CheckResult("reranker", "warn", "network checks skipped", required=False))
    return rows


def check_python() -> CheckResult:
    version = platform.python_version()
    required_ok = sys.version_info >= (3, 10)
    return CheckResult(
        "python",
        "ok" if required_ok else "fail",
        "Python runtime available" if required_ok else "Python 3.10+ required",
        required=True,
        detail={"version": version, "trawl_version": __version__},
    )


def check_playwright_browser() -> CheckResult:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            executable = Path(pw.chromium.executable_path)
        if executable.exists():
            return CheckResult(
                "playwright",
                "ok",
                "Chromium browser executable found",
                required=True,
                detail={"executable": str(executable)},
            )
        return CheckResult(
            "playwright",
            "fail",
            "Chromium executable missing; run `playwright install chromium`",
            required=True,
            detail={"executable": str(executable)},
        )
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            "playwright",
            "fail",
            f"{type(e).__name__}: {e}",
            required=True,
        )


def check_writable_path(name: str, path: Path, *, required: bool) -> CheckResult:
    target_dir = path if path.suffix == "" else path.parent
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target_dir, prefix=".trawl-doctor-", delete=True):
            pass
        return CheckResult(
            name,
            "ok",
            "path is writable",
            required=required,
            detail={"path": str(target_dir)},
        )
    except OSError as e:
        return CheckResult(
            name,
            "fail" if required else "warn",
            f"{type(e).__name__}: {e}",
            required=required,
            detail={"path": str(target_dir)},
        )


def check_embedding_endpoint() -> CheckResult:
    base_url = retrieval.DEFAULT_EMBEDDING_URL
    model = retrieval.DEFAULT_EMBEDDING_MODEL
    try:
        response = httpx.post(
            f"{base_url}/embeddings",
            json={"model": model, "input": ["trawl doctor smoke"]},
            timeout=5.0,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data.get("data"), list) or not data["data"]:
            raise ValueError("missing data[] in embedding response")
        return CheckResult(
            "embedding",
            "ok",
            "embedding endpoint returned a vector",
            required=True,
            detail={"url": base_url, "model": model},
        )
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            "embedding",
            "fail",
            f"{type(e).__name__}: {e}",
            required=True,
            detail={"url": base_url, "model": model},
        )


def check_reranker_endpoint() -> CheckResult:
    base_url = reranking.DEFAULT_RERANKER_URL
    model = reranking.DEFAULT_RERANKER_MODEL
    try:
        response = httpx.post(
            f"{base_url}/rerank",
            json={"model": model, "query": "smoke", "documents": ["smoke document"]},
            timeout=5.0,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data.get("results"), list):
            raise ValueError("missing results[] in rerank response")
        return CheckResult(
            "reranker",
            "ok",
            "reranker endpoint returned scores",
            required=False,
            detail={"url": base_url, "model": model},
        )
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            "reranker",
            "warn",
            f"{type(e).__name__}: {e}",
            required=False,
            detail={"url": base_url, "model": model},
        )


def check_vlm_configured() -> CheckResult:
    url = os.environ.get("TRAWL_VLM_URL", "").strip()
    if url:
        return CheckResult(
            "vlm",
            "ok",
            "TRAWL_VLM_URL configured; profile_page can be exposed by MCP",
            required=False,
            detail={"url": url},
        )
    return CheckResult(
        "vlm",
        "warn",
        "TRAWL_VLM_URL unset; profile_page remains disabled",
        required=False,
    )


def exit_code(rows: list[CheckResult]) -> int:
    return 1 if any(row.required and row.status == "fail" for row in rows) else 0


def render_text(rows: list[CheckResult]) -> str:
    lines = ["trawl doctor"]
    for row in rows:
        label = row.status.upper()
        importance = "required" if row.required else "optional"
        lines.append(f"{label:<4} {row.name:<16} [{importance}] {row.message}")
    return "\n".join(lines) + "\n"


def render_json(rows: list[CheckResult]) -> str:
    payload = {
        "ok": exit_code(rows) == 0,
        "checks": [asdict(row) for row in rows],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check trawl runtime health.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    parser.add_argument(
        "--no-network",
        action="store_true",
        help="Skip embedding and reranker endpoint requests.",
    )
    return parser.parse_args(argv)


def main(
    argv: list[str] | None = None,
    *,
    checks: Callable[[bool], list[CheckResult]] = lambda include_network: run_checks(
        include_network=include_network
    ),
) -> int:
    args = parse_args(argv)
    rows = checks(not args.no_network)
    if args.json:
        print(render_json(rows), end="")
    else:
        print(render_text(rows), end="")
    return exit_code(rows)


def _cli_entry() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    _cli_entry()
```

- [ ] **Step 4: Add console script**

Modify `pyproject.toml` under `[project.scripts]`:

```toml
[project.scripts]
trawl-mcp = "trawl_mcp.server:_cli_entry"
trawl-doctor = "trawl.diagnostics:_cli_entry"
```

- [ ] **Step 5: Run diagnostics tests**

Run:

```bash
mamba run -n trawl python -m pytest tests/test_diagnostics.py -q
```

Expected: PASS.

- [ ] **Step 6: Smoke the no-network CLI**

Run:

```bash
mamba run -n trawl python -m trawl.diagnostics --no-network
```

Expected: text output beginning with `trawl doctor`; exit code `0` unless local Playwright/cache prerequisites are broken.

- [ ] **Step 7: Commit diagnostics slice**

```bash
git add pyproject.toml src/trawl/diagnostics.py tests/test_diagnostics.py
git commit -m "feat: add runtime doctor"
```

## Task 2: BM25 Fallback in Retrieval

**Files:**
- Modify: `src/trawl/retrieval.py`
- Create: `tests/test_retrieval_bm25_fallback.py`

- [ ] **Step 1: Write retrieval fallback tests**

Create `tests/test_retrieval_bm25_fallback.py`:

```python
from __future__ import annotations

import httpx

from trawl import pipeline, retrieval
from trawl.chunking import Chunk
from trawl.fetchers.playwright import FetchResult


def _chunk(text: str, heading: str = "") -> Chunk:
    return Chunk(
        text=text,
        heading_path=[heading] if heading else [],
        char_count=len(text),
        chunk_index=0,
        embed_text=text,
    )


def test_retrieve_uses_bm25_fallback_when_embedding_fails(monkeypatch):
    chunks = [
        _chunk("general discussion of task scheduling"),
        _chunk("asyncio.gather awaits tasks concurrently"),
        _chunk("unrelated installation notes"),
    ]

    def fail_embed(_client, _base_url, _model, _texts):
        raise httpx.ConnectError("embedding down")

    monkeypatch.setattr(retrieval, "_embed_batch", fail_embed)

    result = retrieval.retrieve("asyncio.gather tasks", chunks, k=2)

    assert result.error is None
    assert result.warning
    assert "embedding unavailable" in result.warning
    assert result.retrieval_mode == "bm25_fallback"
    assert result.fusion_weights == {"bm25": 1.0}
    assert "asyncio.gather" in result.scored[0].chunk.text
    assert result.n_chunks_embedded == 0


def test_pipeline_returns_chunks_with_warning_when_embedding_fails(monkeypatch):
    monkeypatch.setenv("TRAWL_FETCH_CACHE_TTL", "0")

    fetched = FetchResult(
        url="https://example.test/asyncio",
        html="<html><title>Asyncio</title><body>asyncio.gather awaits tasks</body></html>",
        markdown="asyncio.gather awaits tasks concurrently\n\nother unrelated text",
        raw_html="",
        fetcher="test",
        elapsed_ms=1,
    )

    def fake_fetch_html(_url, query=None):
        return (
            fetched,
            pipeline.extraction.ExtractedContent(
                markdown=fetched.markdown,
                extractor="test",
            ),
            "test",
        )

    def fail_embed(_client, _base_url, _model, _texts):
        raise httpx.ConnectError("embedding down")

    monkeypatch.setattr(pipeline, "_fetch_html", fake_fetch_html)
    monkeypatch.setattr(retrieval, "_embed_batch", fail_embed)

    result = pipeline.fetch_relevant(
        "https://example.test/asyncio",
        "asyncio.gather tasks",
        use_rerank=False,
    )
    payload = pipeline.to_dict(result)

    assert payload["error"] is None
    assert payload["warnings"]
    assert "embedding unavailable" in payload["warnings"][0]
    assert payload["retrieval_diagnostics"]["mode"] == "bm25_fallback"
    assert payload["chunks"]
    assert "asyncio.gather" in payload["chunks"][0]["text"]
```

- [ ] **Step 2: Run fallback tests to verify failure**

Run:

```bash
mamba run -n trawl python -m pytest tests/test_retrieval_bm25_fallback.py -q
```

Expected: FAIL because `RetrievalResult.warning` and `PipelineResult.warnings` do not exist yet, and retrieval still returns an error on embedding HTTP failure.

- [ ] **Step 3: Add fallback fields and helper in `retrieval.py`**

Modify `src/trawl/retrieval.py`:

```python
@dataclass
class RetrievalResult:
    scored: list[ScoredChunk]
    elapsed_ms: int
    embed_calls: int
    error: str | None = None
    warning: str | None = None
    n_chunks_embedded: int = 0
    retrieval_mode: str = "dense"
    query_type: str = "concept"
    fusion_weights: dict[str, float] | None = None
    rank_diagnostics: list[dict] | None = None
    sparse_rank_error: str | None = None
```

Add this helper near `_ranking_from_scores`:

```python
def _bm25_fallback_result(
    query: str,
    chunks: list[Chunk],
    chunk_texts: list[str],
    *,
    k: int,
    t0: float,
    embed_calls: int,
    error: str,
) -> RetrievalResult:
    ranked = bm25_rank(query, chunk_texts)
    scored = [ScoredChunk(chunk=chunks[i], score=0.0) for i in ranked[:k]]
    query_type = _classify_query(query)
    diagnostics = [
        {
            "pool_index": i,
            "chunk_index": chunks[i].chunk_index,
            "ranks": {"bm25": rank},
            "contributions": {"bm25": round(1.0 / (DEFAULT_FALLBACK_RRF_K + rank), 6)},
            "fusion_score": round(1.0 / (DEFAULT_FALLBACK_RRF_K + rank), 6),
        }
        for rank, i in enumerate(ranked[:k])
    ]
    return RetrievalResult(
        scored=scored,
        elapsed_ms=int((time.monotonic() - t0) * 1000),
        embed_calls=embed_calls,
        error=None,
        warning=f"embedding unavailable; using BM25 fallback: {error}",
        n_chunks_embedded=0,
        retrieval_mode="bm25_fallback",
        query_type=query_type,
        fusion_weights={"bm25": 1.0},
        rank_diagnostics=diagnostics,
    )
```

Add the constant near `HTTP_TIMEOUT_S`:

```python
DEFAULT_FALLBACK_RRF_K = int(os.environ.get("TRAWL_BM25_FALLBACK_RRF_K", "60"))
```

- [ ] **Step 4: Use fallback inside the embedding exception path**

Replace the `except httpx.HTTPError as e:` block in `retrieve()` with:

```python
    except httpx.HTTPError as e:
        return _bm25_fallback_result(
            query,
            chunks,
            chunk_texts,
            k=k,
            t0=t0,
            embed_calls=embed_calls,
            error=f"{type(e).__name__}: {e}",
        )
```

- [ ] **Step 5: Run retrieval fallback test**

Run:

```bash
mamba run -n trawl python -m pytest tests/test_retrieval_bm25_fallback.py::test_retrieve_uses_bm25_fallback_when_embedding_fails -q
```

Expected: PASS for the retrieval-level fallback test.

- [ ] **Step 6: Commit retrieval fallback core**

```bash
git add src/trawl/retrieval.py tests/test_retrieval_bm25_fallback.py
git commit -m "feat: fall back to bm25 when embeddings fail"
```

## Task 3: Pipeline, MCP, and Telemetry Warning Propagation

**Files:**
- Modify: `src/trawl/pipeline.py`
- Modify: `src/trawl/telemetry.py`
- Test: `tests/test_retrieval_bm25_fallback.py`

- [ ] **Step 1: Add `warnings` to `PipelineResult`**

In `src/trawl/pipeline.py`, add this field after `error`:

```python
    warnings: list[str] = field(default_factory=list)
```

Update `_error_result()` default fields:

```python
        "warnings": [],
```

- [ ] **Step 2: Propagate retrieval warnings in full pipeline**

In `_run_full_pipeline()`, before the final `PipelineResult(...)`, add:

```python
    warnings = [retrieved.warning] if retrieved.warning else []
```

Then include this keyword in the final `PipelineResult(...)`:

```python
        warnings=warnings,
```

- [ ] **Step 3: Propagate retrieval warnings in profile retrieval**

In `_build_profile_result()`, after rerank/final scored selection in the `profile_retrieval` branch, preserve:

```python
        retrieval_warning = retrieved.warning
```

Initialize before the branch:

```python
    retrieval_warning = None
```

Include this keyword in the final `PipelineResult(...)`:

```python
        warnings=[retrieval_warning] if retrieval_warning else [],
```

- [ ] **Step 4: Add telemetry warning field**

In `src/trawl/telemetry.py`, add this key in `_build_event()`:

```python
        "warnings": list(result.warnings),
```

- [ ] **Step 5: Run pipeline fallback test**

Run:

```bash
mamba run -n trawl python -m pytest tests/test_retrieval_bm25_fallback.py -q
```

Expected: PASS.

- [ ] **Step 6: Run focused regression tests**

Run:

```bash
mamba run -n trawl python -m pytest tests/test_retrieval_hybrid.py tests/test_pipeline.py tests/test_telemetry.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit propagation**

```bash
git add src/trawl/pipeline.py src/trawl/telemetry.py tests/test_retrieval_bm25_fallback.py
git commit -m "feat: expose degraded retrieval warnings"
```

## Task 4: Documentation and Final Verification

**Files:**
- Modify: `README.md`
- Modify: `docs/stability-speed-improvement-report-2026-05-04.md` if implementation outcomes differ from the original recommendations.

- [ ] **Step 1: Document diagnostics command**

Add this subsection near README configuration/usage sections:

````markdown
### Runtime health check

Use `trawl-doctor` to check the local runtime before wiring trawl into an MCP client:

```bash
trawl-doctor
# or
python -m trawl.diagnostics --json
```

The doctor checks Python, Playwright Chromium, cache-path writability, the embedding endpoint, the optional reranker endpoint, and optional VLM profile configuration. Embedding is required for dense retrieval; reranker and VLM are optional.
````

- [ ] **Step 2: Document BM25 degraded fallback**

Add this paragraph near the retrieval/reranking feature list:

```markdown
If the embedding endpoint is unavailable, trawl falls back to BM25 lexical ranking and returns a warning in the result payload instead of failing the entire fetch. This degraded mode is meant for operational continuity; quality is best with the bge-m3 embedding service running.
```

- [ ] **Step 3: Document embedding cache operational recommendation**

Add this paragraph near cache/environment configuration:

````markdown
For repeated queries over the same pages, consider enabling the document embedding cache:

```bash
export TRAWL_EMBED_CACHE_TTL=86400
```

The cache key includes model, endpoint, contextual-retrieval mode/version, and a hash of the text, so content changes naturally miss the cache.
````

- [ ] **Step 4: Run README/report text scan**

Run:

```bash
rg -n "TO""DO|FIX""ME|TB""D" README.md docs/stability-speed-improvement-report-2026-05-04.md docs/superpowers/plans/2026-05-04-p0-stability-foundations.md
```

Expected: no matches.

- [ ] **Step 5: Run full verification**

Run:

```bash
mamba run -n trawl python -m pytest
```

Expected: `363 passed` or the updated total with all tests passing.

- [ ] **Step 6: Run no-network doctor smoke**

Run:

```bash
mamba run -n trawl python -m trawl.diagnostics --no-network
```

Expected: output begins with `trawl doctor`; exit code `0` unless a required local prerequisite is genuinely missing.

- [ ] **Step 7: Optional endpoint doctor smoke**

Run only when local embedding/reranker services are expected to be up:

```bash
mamba run -n trawl python -m trawl.diagnostics --json
```

Expected: JSON payload. `ok` is `true` when required checks pass. Reranker may warn without failing the command.

- [ ] **Step 8: Commit docs and final verification notes**

```bash
git add README.md docs/stability-speed-improvement-report-2026-05-04.md docs/superpowers/plans/2026-05-04-p0-stability-foundations.md
git commit -m "docs: plan p0 stability foundations"
```

## Self-Review Checklist

- [ ] The implementation adds one user-facing diagnostic command and does not change MCP tool schemas except additive `warnings`.
- [ ] Embedding HTTP failures return chunks when BM25 can rank non-empty chunks.
- [ ] Reranker fallback behavior remains unchanged.
- [ ] Unit tests mock network/model failure; CI does not require local embedding or reranker services.
- [ ] Full test suite passes before declaring the goal complete.
