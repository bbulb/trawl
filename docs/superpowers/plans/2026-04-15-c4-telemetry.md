# C4 Telemetry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add opt-in JSONL telemetry for `fetch_relevant()` calls so C4 decisions can be informed by weeks of real usage data.

**Architecture:** Single new module `src/trawl/telemetry.py` exposing `record(result: PipelineResult) -> None`. Activated only when `TRAWL_TELEMETRY=1`. Writes one JSON line per call to `~/.trawl/telemetry.jsonl` (override via `TRAWL_TELEMETRY_PATH`). Size-based single-generation rotation at `TRAWL_TELEMETRY_MAX_BYTES` (default 64 MB). All failures swallowed with warning. `pipeline.fetch_relevant()` wraps its impl so telemetry fires at a single point regardless of which internal return path was taken.

**Tech Stack:** Python stdlib only (`json`, `hashlib`, `os`, `logging`, `pathlib`, `urllib.parse`, `datetime`). pytest for tests.

**Spec:** `docs/superpowers/specs/2026-04-15-c4-telemetry-design.md`

---

## File Structure

- Create `src/trawl/telemetry.py` — module, public `record()` only.
- Create `tests/test_telemetry.py` — unit tests, no external servers.
- Modify `src/trawl/pipeline.py:503-631` — rename `fetch_relevant` body to `_fetch_relevant_impl`, add thin wrapper that calls impl then `telemetry.record(result)`.
- Modify `.env.example` — three new env vars + one-line comments.
- Modify `ARCHITECTURE.md` — short "Telemetry (optional)" section.
- Modify `notes/RESEARCH.md` §C4 — one-line memo about the new collector.

---

## Task 1: Bootstrap telemetry module with no-op behavior

**Files:**
- Create: `src/trawl/telemetry.py`
- Test: `tests/test_telemetry.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_telemetry.py`:

```python
"""Unit tests for src/trawl/telemetry.py. No external servers."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from trawl import telemetry
from trawl.pipeline import PipelineResult


def _sample_result(url: str = "https://example.com/a", query: str = "what") -> PipelineResult:
    return PipelineResult(
        url=url,
        query=query,
        fetcher_used="playwright",
        fetch_ms=100,
        chunk_ms=10,
        retrieval_ms=20,
        total_ms=140,
        page_chars=1234,
        n_chunks_total=7,
        structured_path=False,
        hyde_used=False,
        hyde_text="",
        chunks=[],
        path="full_page_retrieval",
    )


def test_record_noop_when_disabled(tmp_path: Path, monkeypatch):
    target = tmp_path / "t.jsonl"
    monkeypatch.delenv("TRAWL_TELEMETRY", raising=False)
    monkeypatch.setenv("TRAWL_TELEMETRY_PATH", str(target))

    telemetry.record(_sample_result())

    assert not target.exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `mamba run -n trawl pytest tests/test_telemetry.py::test_record_noop_when_disabled -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'trawl.telemetry'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/trawl/telemetry.py`:

```python
"""Opt-in JSONL telemetry for fetch_relevant() calls.

Activated only when TRAWL_TELEMETRY=1. All failures are swallowed so
telemetry can never break a user fetch. See
docs/superpowers/specs/2026-04-15-c4-telemetry-design.md.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .pipeline import PipelineResult

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    return os.environ.get("TRAWL_TELEMETRY", "").strip() in {"1", "true", "yes"}


def record(result: PipelineResult) -> None:
    """Append a telemetry event for one fetch_relevant() call.

    No-op unless TRAWL_TELEMETRY=1. Failures are logged at WARNING and
    swallowed.
    """
    if not _enabled():
        return
    try:
        _write_event(result)
    except Exception as e:  # noqa: BLE001
        logger.warning("telemetry record failed: %s", e)


def _write_event(result: PipelineResult) -> None:
    raise NotImplementedError
```

- [ ] **Step 4: Run test to verify it passes**

Run: `mamba run -n trawl pytest tests/test_telemetry.py::test_record_noop_when_disabled -v`
Expected: PASS (no-op path hit, `_write_event` never called).

- [ ] **Step 5: Commit**

```bash
git add src/trawl/telemetry.py tests/test_telemetry.py
git commit -m "feat(telemetry): opt-in module scaffold with no-op default"
```

---

## Task 2: Build the event dict

**Files:**
- Modify: `src/trawl/telemetry.py`
- Test: `tests/test_telemetry.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_telemetry.py`:

```python
def test_build_event_fields():
    r = _sample_result(url="https://www.example.com/a/b?x=1", query="hello")
    event = telemetry._build_event(r)

    assert event["schema"] == 1
    assert event["host"] == "www.example.com"
    assert event["url"] == "https://www.example.com/a/b?x=1"
    # sha1("hello") = aaf4c61ddcc5e8a2dabede0f3b482cd9aea9434d → first 16
    assert event["query_sha1"] == "aaf4c61ddcc5e8a2"
    assert event["fetcher_used"] == "playwright"
    assert event["path"] == "full_page_retrieval"
    assert event["profile_used"] is False
    assert event["profile_hash"] is None
    assert event["rerank_used"] is False
    assert event["hyde_used"] is False
    assert event["fetch_ms"] == 100
    assert event["total_ms"] == 140
    assert event["n_chunks_total"] == 7
    assert event["error"] is None
    assert "ts" in event and event["ts"].endswith("Z")
    # Must NOT contain raw query, chunks, or hyde_text
    assert "query" not in event
    assert "chunks" not in event
    assert "hyde_text" not in event
```

- [ ] **Step 2: Run test to verify it fails**

Run: `mamba run -n trawl pytest tests/test_telemetry.py::test_build_event_fields -v`
Expected: FAIL with `AttributeError: module 'trawl.telemetry' has no attribute '_build_event'`.

- [ ] **Step 3: Write minimal implementation**

Add to `src/trawl/telemetry.py` (above `_write_event`):

```python
import hashlib
from datetime import datetime, timezone
from urllib.parse import urlsplit


SCHEMA_VERSION = 1


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _query_sha1(query: str) -> str:
    return hashlib.sha1(query.encode("utf-8")).hexdigest()[:16]


def _build_event(result: PipelineResult) -> dict:
    return {
        "ts": _utc_now_iso(),
        "schema": SCHEMA_VERSION,
        "host": urlsplit(result.url).netloc,
        "url": result.url,
        "query_sha1": _query_sha1(result.query),
        "fetcher_used": result.fetcher_used,
        "path": result.path,
        "profile_used": result.profile_used,
        "profile_hash": result.profile_hash,
        "suggest_profile": result.suggest_profile,
        "suggest_profile_reason": result.suggest_profile_reason,
        "content_type": result.content_type,
        "structured_path": result.structured_path,
        "rerank_used": result.rerank_used,
        "hyde_used": result.hyde_used,
        "fetch_ms": result.fetch_ms,
        "chunk_ms": result.chunk_ms,
        "retrieval_ms": result.retrieval_ms,
        "rerank_ms": result.rerank_ms,
        "total_ms": result.total_ms,
        "page_chars": result.page_chars,
        "n_chunks_total": result.n_chunks_total,
        "error": result.error,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `mamba run -n trawl pytest tests/test_telemetry.py::test_build_event_fields -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/trawl/telemetry.py tests/test_telemetry.py
git commit -m "feat(telemetry): build event dict from PipelineResult"
```

---

## Task 3: Write events to JSONL file

**Files:**
- Modify: `src/trawl/telemetry.py`
- Test: `tests/test_telemetry.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_telemetry.py`:

```python
def test_record_appends_jsonl(tmp_path: Path, monkeypatch):
    target = tmp_path / "t.jsonl"
    monkeypatch.setenv("TRAWL_TELEMETRY", "1")
    monkeypatch.setenv("TRAWL_TELEMETRY_PATH", str(target))

    telemetry.record(_sample_result(url="https://a.example.com/x", query="q1"))
    telemetry.record(_sample_result(url="https://b.example.com/y", query="q2"))
    telemetry.record(_sample_result(url="https://c.example.com/z", query="q3"))

    assert target.exists()
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    for line in lines:
        event = json.loads(line)
        assert event["schema"] == 1
        assert event["host"].endswith("example.com")


def test_record_creates_directory(tmp_path: Path, monkeypatch):
    target = tmp_path / "nested" / "dir" / "t.jsonl"
    monkeypatch.setenv("TRAWL_TELEMETRY", "1")
    monkeypatch.setenv("TRAWL_TELEMETRY_PATH", str(target))

    telemetry.record(_sample_result())

    assert target.exists()
    assert target.parent.is_dir()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `mamba run -n trawl pytest tests/test_telemetry.py::test_record_appends_jsonl tests/test_telemetry.py::test_record_creates_directory -v`
Expected: FAIL with `NotImplementedError` inside `_write_event`.

- [ ] **Step 3: Write minimal implementation**

Replace `_write_event` in `src/trawl/telemetry.py` and add path helper:

```python
import json
from pathlib import Path


DEFAULT_PATH = "~/.trawl/telemetry.jsonl"


def _target_path() -> Path:
    raw = os.environ.get("TRAWL_TELEMETRY_PATH") or DEFAULT_PATH
    return Path(raw).expanduser()


def _write_event(result: PipelineResult) -> None:
    path = _target_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    event = _build_event(result)
    line = json.dumps(event, ensure_ascii=False) + "\n"
    newly_created = not path.exists()
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
    if newly_created:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `mamba run -n trawl pytest tests/test_telemetry.py -v`
Expected: all four tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/trawl/telemetry.py tests/test_telemetry.py
git commit -m "feat(telemetry): append events to JSONL with 0600/0700 perms"
```

---

## Task 4: Size-based rotation

**Files:**
- Modify: `src/trawl/telemetry.py`
- Test: `tests/test_telemetry.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_telemetry.py`:

```python
def test_rotation_when_exceeds_max_bytes(tmp_path: Path, monkeypatch):
    target = tmp_path / "t.jsonl"
    monkeypatch.setenv("TRAWL_TELEMETRY", "1")
    monkeypatch.setenv("TRAWL_TELEMETRY_PATH", str(target))
    # One event is ~500 bytes; 300 bytes forces rotation after the first write.
    monkeypatch.setenv("TRAWL_TELEMETRY_MAX_BYTES", "300")

    telemetry.record(_sample_result(query="first"))
    telemetry.record(_sample_result(query="second"))
    telemetry.record(_sample_result(query="third"))

    rotated = target.with_suffix(target.suffix + ".1")
    assert rotated.exists(), "rotated .1 file should be created"
    assert target.exists(), "new current file should be created"

    # Current file must contain only the most recent event(s).
    current_lines = target.read_text(encoding="utf-8").splitlines()
    assert len(current_lines) >= 1
    last_event = json.loads(current_lines[-1])
    # sha1("third")[:16]
    assert last_event["query_sha1"] == hashlib_sha1_prefix("third")


def hashlib_sha1_prefix(s: str) -> str:
    import hashlib as _h
    return _h.sha1(s.encode("utf-8")).hexdigest()[:16]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `mamba run -n trawl pytest tests/test_telemetry.py::test_rotation_when_exceeds_max_bytes -v`
Expected: FAIL — no rotation logic yet, `.1` file not created.

- [ ] **Step 3: Write minimal implementation**

Modify `_write_event` in `src/trawl/telemetry.py` to check size and rotate before the append:

```python
DEFAULT_MAX_BYTES = 64 * 1024 * 1024  # 64 MB


def _max_bytes() -> int:
    raw = os.environ.get("TRAWL_TELEMETRY_MAX_BYTES")
    if not raw:
        return DEFAULT_MAX_BYTES
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_MAX_BYTES


def _maybe_rotate(path: Path) -> None:
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return
    if size < _max_bytes():
        return
    rotated = path.with_suffix(path.suffix + ".1")
    try:
        if rotated.exists():
            rotated.unlink()
        path.rename(rotated)
    except OSError:
        # Another process may have rotated concurrently. Next append
        # will land in whichever file is current.
        pass


def _write_event(result: PipelineResult) -> None:
    path = _target_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    _maybe_rotate(path)
    event = _build_event(result)
    line = json.dumps(event, ensure_ascii=False) + "\n"
    newly_created = not path.exists()
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
    if newly_created:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `mamba run -n trawl pytest tests/test_telemetry.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/trawl/telemetry.py tests/test_telemetry.py
git commit -m "feat(telemetry): single-generation size-based rotation"
```

---

## Task 5: Failure is silent

**Files:**
- Modify: `tests/test_telemetry.py` (test-only)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_telemetry.py`:

```python
def test_record_swallows_io_errors(tmp_path: Path, monkeypatch, caplog):
    # Point at a path whose parent cannot be created.
    target = tmp_path / "ro" / "t.jsonl"
    tmp_path.chmod(0o500)  # make tmp_path read+execute only
    try:
        monkeypatch.setenv("TRAWL_TELEMETRY", "1")
        monkeypatch.setenv("TRAWL_TELEMETRY_PATH", str(target))

        with caplog.at_level("WARNING", logger="trawl.telemetry"):
            telemetry.record(_sample_result())  # must not raise

        assert any("telemetry record failed" in r.message for r in caplog.records)
    finally:
        tmp_path.chmod(0o700)  # restore so pytest can clean up
```

- [ ] **Step 2: Run test to verify it passes immediately**

Run: `mamba run -n trawl pytest tests/test_telemetry.py::test_record_swallows_io_errors -v`
Expected: PASS — the outer `try/except` in `record()` from Task 1 already handles this. This test locks in that behavior.

If it FAILS (e.g., running as root bypasses the permission check), skip the test with `pytest.skip` when `os.geteuid() == 0`. Add at top of the test:

```python
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("cannot test permission denial as root")
```

- [ ] **Step 3: Commit**

```bash
git add tests/test_telemetry.py
git commit -m "test(telemetry): lock in silent-failure contract"
```

---

## Task 6: Wire telemetry into pipeline.fetch_relevant

**Files:**
- Modify: `src/trawl/pipeline.py:503-631`
- Test: `tests/test_telemetry.py`

- [ ] **Step 1: Write the failing integration test**

Append to `tests/test_telemetry.py`:

```python
def test_fetch_relevant_records_telemetry(tmp_path: Path, monkeypatch):
    """fetch_relevant() must call telemetry.record exactly once, regardless
    of which internal return path was taken (error path is fine)."""
    target = tmp_path / "t.jsonl"
    monkeypatch.setenv("TRAWL_TELEMETRY", "1")
    monkeypatch.setenv("TRAWL_TELEMETRY_PATH", str(target))

    from trawl import pipeline

    # query=None with no profile triggers the fast error-return path —
    # does not require any network or embedding server.
    result = pipeline.fetch_relevant("https://never.example.com/x", "")
    assert result.error is not None  # sanity

    assert target.exists()
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["url"] == "https://never.example.com/x"
    assert event["error"] is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `mamba run -n trawl pytest tests/test_telemetry.py::test_fetch_relevant_records_telemetry -v`
Expected: FAIL — `target` does not exist because `fetch_relevant` does not yet call `telemetry.record`.

- [ ] **Step 3: Modify `src/trawl/pipeline.py`**

Near the top of `src/trawl/pipeline.py`, add import (alongside existing `from . import chunking, extraction, ...`):

```python
from . import chunking, extraction, hyde, reranking, retrieval, telemetry
```

At line 503, rename `def fetch_relevant(` to `def _fetch_relevant_impl(` (leave the body and all inner `return` statements untouched). Then add a new public wrapper directly above the renamed function:

```python
def fetch_relevant(
    url: str,
    query: str,
    *,
    k: int | None = None,
    use_hyde: bool = False,
    use_rerank: bool = True,
) -> PipelineResult:
    """Public entry point. See _fetch_relevant_impl for logic.

    Records one telemetry event per call when TRAWL_TELEMETRY=1.
    Telemetry failures never propagate.
    """
    result = _fetch_relevant_impl(
        url,
        query,
        k=k,
        use_hyde=use_hyde,
        use_rerank=use_rerank,
    )
    telemetry.record(result)
    return result
```

Note: copy the exact parameter list from the current `fetch_relevant` signature. If it differs from `(url, query, *, k, use_hyde, use_rerank)`, match what is actually there.

- [ ] **Step 4: Run the new test to verify it passes**

Run: `mamba run -n trawl pytest tests/test_telemetry.py::test_fetch_relevant_records_telemetry -v`
Expected: PASS.

- [ ] **Step 5: Run full telemetry test suite**

Run: `mamba run -n trawl pytest tests/test_telemetry.py -v`
Expected: all tests PASS.

- [ ] **Step 6: Run parity matrix (MANDATORY per CLAUDE.md)**

Ensure `TRAWL_TELEMETRY` is NOT set, then run:

```bash
unset TRAWL_TELEMETRY
mamba run -n trawl python tests/test_pipeline.py
```

Expected: 12/12 cases pass. If any case regresses, stop and investigate — do not commit.

- [ ] **Step 7: Commit**

```bash
git add src/trawl/pipeline.py tests/test_telemetry.py
git commit -m "feat(pipeline): record telemetry on fetch_relevant completion"
```

---

## Task 7: Documentation

**Files:**
- Modify: `.env.example`
- Modify: `ARCHITECTURE.md`
- Modify: `notes/RESEARCH.md`

- [ ] **Step 1: Update `.env.example`**

Append at the end:

```bash
# Telemetry (opt-in; used to inform C4 decision)
# Set TRAWL_TELEMETRY=1 to append one JSON line per fetch_relevant() call.
# TRAWL_TELEMETRY=1
# TRAWL_TELEMETRY_PATH=~/.trawl/telemetry.jsonl
# TRAWL_TELEMETRY_MAX_BYTES=67108864
```

- [ ] **Step 2: Add Telemetry section to `ARCHITECTURE.md`**

Add a new section near the end (before any "Known limitations" section if present, otherwise at the end):

```markdown
## Telemetry (optional)

Opt-in JSONL collector for `fetch_relevant()` calls. Off by default.
Activated with `TRAWL_TELEMETRY=1`; writes to `~/.trawl/telemetry.jsonl`
(override with `TRAWL_TELEMETRY_PATH`). Single-generation size rotation
at `TRAWL_TELEMETRY_MAX_BYTES` (default 64 MB) — older data moves to
`telemetry.jsonl.1`.

Each line captures host, URL (plaintext), query SHA-1 prefix (query
plaintext is never stored), fetcher path, profile hit/miss, rerank and
HyDE flags, and latency/size breakdown. Full schema: see
`docs/superpowers/specs/2026-04-15-c4-telemetry-design.md`.

Purpose: feed the C4 (`notes/RESEARCH.md`) decision on whether
index-based extraction as a profile fallback has a problem to solve.
```

- [ ] **Step 3: Update `notes/RESEARCH.md` §C4**

Find the line in `notes/RESEARCH.md` section C4 that starts with "**결정 후 다음 단계.** 먼저 profile cache hit/miss 통계로 실제 miss rate가". Insert before it:

```markdown
**선결 데이터 수집 상태.** 2026-04-15 telemetry collector merged
(`src/trawl/telemetry.py`). `TRAWL_TELEMETRY=1`로 활성화. 최소 수 주
수집 후 재검토.

```

- [ ] **Step 4: Verify parity matrix one more time**

```bash
unset TRAWL_TELEMETRY
mamba run -n trawl python tests/test_pipeline.py
```

Expected: 12/12.

- [ ] **Step 5: Commit**

```bash
git add .env.example ARCHITECTURE.md notes/RESEARCH.md
git commit -m "docs: document TRAWL_TELEMETRY opt-in collector"
```

---

## Self-Review Checklist

Run once all tasks are complete:

- [ ] `mamba run -n trawl pytest tests/test_telemetry.py -v` — all green.
- [ ] `mamba run -n trawl python tests/test_pipeline.py` — 12/12.
- [ ] `git log --oneline` shows 6 focused commits (one per task + docs).
- [ ] `~/.trawl/telemetry.jsonl` does NOT exist on a fresh checkout without the env var.
- [ ] `TRAWL_TELEMETRY=1 mamba run -n trawl python -c "from trawl import fetch_relevant; fetch_relevant('https://example.com/', 'test')"` produces a single JSON line with the expected fields.
