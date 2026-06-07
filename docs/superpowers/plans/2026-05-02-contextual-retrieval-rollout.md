# Contextual Retrieval Rollout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move contextual retrieval from an opt-in measured feature toward a safe `auto` rollout, with cache-controlled latency evidence and contextual embedding caching.

**Architecture:** Keep the existing deterministic contextual prefix implementation as the ranking input builder. Add a small policy layer that resolves `TRAWL_CONTEXTUAL_RETRIEVAL` into `off`, `on`, or `auto`, then wire the pipeline through that policy. Add a focused embedding cache below `retrieval.retrieve()` so repeated page/query runs avoid re-embedding unchanged chunk inputs, with cache keys that include contextual mode and prefix version.

**Tech Stack:** Python 3.10+, dataclasses, pytest, monkeypatch, JSON-on-disk caches, existing `trawl.contextual`, `trawl.retrieval`, `trawl.pipeline`, and live parity runners.

---

## Starting State

Already implemented:

- `src/trawl/contextual.py` builds deterministic ranking-only prefix text.
- `retrieval.retrieve(..., context_texts=...)` uses contextual text for dense embedding, BM25 prefilter, and hybrid BM25 ranking.
- Full-page and profile retrieval paths pass contextual text when `TRAWL_CONTEXTUAL_RETRIEVAL=1`.
- Telemetry records contextual usage and prefix length stats without raw text.
- Measurement note: `docs/superpowers/handoffs/2026-05-02-contextual-retrieval-measurement.md`.

Not implemented:

- `TRAWL_CONTEXTUAL_RETRIEVAL=auto`.
- Default-on rollout.
- Cache-controlled latency measurement.
- Contextual embedding cache.
- Follow-up design spec for rollout and cache keying.

## File Structure

- Create `docs/superpowers/specs/2026-05-02-contextual-retrieval-rollout-design.md`: design spec for `auto`, measurement gate, and embedding cache keying.
- Modify `src/trawl/contextual.py`: add mode parsing and an `auto` policy helper while preserving `is_enabled()`.
- Modify `src/trawl/pipeline.py`: call the policy helper with query, chunks, page title, fetcher/path context.
- Create `tests/test_contextual_auto.py`: unit tests for mode parsing and auto policy.
- Create `src/trawl/embedding_cache.py`: disk cache for document embedding vectors keyed by input text hash, embedding model, contextual mode, and prefix version.
- Modify `src/trawl/retrieval.py`: optionally read/write document embeddings through `embedding_cache`; do not cache query embeddings in the first pass.
- Create `tests/test_embedding_cache.py`: unit tests for cache keying, schema mismatch, TTL, malformed entries, and disabled state.
- Create `tests/test_retrieval_embedding_cache.py`: retrieval-level tests proving document embedding calls are skipped on cache hits and invalidated by contextual mode/prefix changes.
- Modify `README.md` and `.env.example`: document `auto` mode and embedding cache environment variables.
- Create `docs/superpowers/handoffs/2026-05-02-contextual-retrieval-rollout-measurement.md`: final measurement note after cache-controlled live runs.

---

### Task 1: Write Rollout Design Spec

**Files:**
- Create: `docs/superpowers/specs/2026-05-02-contextual-retrieval-rollout-design.md`

- [ ] **Step 1: Create the design spec**

Create `docs/superpowers/specs/2026-05-02-contextual-retrieval-rollout-design.md` with this content:

```markdown
# Contextual Retrieval Rollout Design

Date: 2026-05-02

## Goal

Introduce a safe rollout path for deterministic contextual retrieval after the initial measurement showed no flipped failures and one query-heavy flipped-to-pass case.

## Current Evidence

- Pipeline parity: baseline/contextual both `14/15`, failing `korean_wiki_person`.
- Agent `code_heavy_query`: `18/21 -> 19/21`.
- Flipped-to-pass: `coding/claude_code_python_asyncio_lookup`.
- Flipped-to-fail: none.
- Telemetry privacy: no raw context/chunk text.
- Latency caveat: previous baseline/contextual order was cache-confounded.

## Mode Semantics

`TRAWL_CONTEXTUAL_RETRIEVAL` accepts:

- `0`, `false`, `no`, unset: disabled.
- `1`, `true`, `yes`: enabled for every retrieval path that already supports contextual ranking inputs.
- `auto`: enabled only for likely-beneficial retrieval cases.

Initial `auto` policy:

- Enable for identifier/code-heavy queries.
- Enable for large pages where `len(chunks) >= 16`.
- Enable for structured/repeated chunks where any chunk has `record_group_id`.
- Disable for tiny pages where `len(chunks) <= 2`.
- Disable when `TRAWL_CONTEXT_PREFIX_MAX_CHARS=0`.

The policy is intentionally conservative and deterministic. It does not inspect raw page text beyond already-available chunk metadata and query string.

## Embedding Cache

Add a document embedding cache below `retrieval.retrieve()`.

The first implementation caches only document/chunk embeddings, not query embeddings, because query text and HyDE inputs vary per call while document inputs are stable across repeated page visits.

Cache key fields:

- schema version
- embedding model
- embedding endpoint base URL
- input text SHA-256
- contextual mode resolved for this retrieval: `off`, `on`, or `auto`
- contextual prefix max chars
- contextual prefix version

Environment:

- `TRAWL_EMBED_CACHE_TTL`: seconds, default `0` to keep cache disabled until measured.
- `TRAWL_EMBED_CACHE_PATH`: default `~/.cache/trawl/embeddings`.
- `TRAWL_EMBED_CACHE_MAX_MB`: default `512`.
- `TRAWL_CONTEXT_PREFIX_VERSION`: default `deterministic-v1`.

## Measurement Plan

Run cache-controlled comparisons before changing defaults:

1. Clear fetch cache or run both modes in paired warm-cache order.
2. Run baseline/contextual/auto with the same warmed fetch cache.
3. Use `--repeats 3` for agent-pattern subsets.
4. Report fetch, retrieval, rerank, and total p50/p95 separately.
5. Report embedding cache hit/miss counts when cache is enabled.

## Default Decision Gate

Default-on or `auto` is acceptable only if:

- Pipeline flipped-to-fail remains `0`.
- Agent-pattern net assertion delta remains `>= +1`, or the known asyncio retrieval failure remains fixed.
- Cache-controlled total p95 increase is `<= +20%`.
- Retrieval p95 increase is `<= +20%` when embedding cache is disabled.
- Telemetry still records no raw context/chunk text.
- Focused tests and Ruff pass.

## Rollout Decision

If the gate passes with `auto`, update docs to recommend `TRAWL_CONTEXTUAL_RETRIEVAL=auto`.
If the gate passes with full `on`, consider making `auto` the default and keeping `1` as forced-on.
If latency remains unclear, keep default off and enable embedding cache experiments first.
```

- [ ] **Step 2: Review the spec for internal consistency**

Run:

```bash
python - <<'PY'
from pathlib import Path

path = Path("docs/superpowers/specs/2026-05-02-contextual-retrieval-rollout-design.md")
terms = ["TB" + "D", "TO" + "DO", "implement " + "later", "fill in " + "details"]
text = path.read_text(encoding="utf-8")
for term in terms:
    if term in text:
        print(f"{path}: contains forbidden phrase: {term}")
PY
```

Expected: no output.

- [ ] **Step 3: Commit the design spec**

Run:

```bash
git add docs/superpowers/specs/2026-05-02-contextual-retrieval-rollout-design.md
git commit -m "docs: design contextual retrieval rollout"
```

Expected: commit succeeds.

---

### Task 2: Add Contextual Retrieval `auto` Policy

**Files:**
- Modify: `src/trawl/contextual.py`
- Modify: `src/trawl/pipeline.py`
- Create: `tests/test_contextual_auto.py`

- [ ] **Step 1: Write failing tests for mode parsing and auto policy**

Create `tests/test_contextual_auto.py`:

```python
"""Tests for contextual retrieval mode and auto policy."""

from __future__ import annotations

from trawl import contextual
from trawl.chunking import Chunk


def _chunk(text: str, *, index: int = 0, record_group_id: int | None = None) -> Chunk:
    return Chunk(
        text=text,
        embed_text=text,
        char_count=len(text),
        chunk_index=index,
        record_group_id=record_group_id,
        record_index=0 if record_group_id is not None else None,
    )


def test_mode_defaults_to_off(monkeypatch):
    monkeypatch.delenv("TRAWL_CONTEXTUAL_RETRIEVAL", raising=False)
    assert contextual.mode() == "off"


def test_mode_accepts_on_values(monkeypatch):
    for value in ("1", "true", "yes", "on"):
        monkeypatch.setenv("TRAWL_CONTEXTUAL_RETRIEVAL", value)
        assert contextual.mode() == "on"


def test_mode_accepts_auto(monkeypatch):
    monkeypatch.setenv("TRAWL_CONTEXTUAL_RETRIEVAL", "auto")
    assert contextual.mode() == "auto"


def test_mode_treats_unknown_as_off(monkeypatch):
    monkeypatch.setenv("TRAWL_CONTEXTUAL_RETRIEVAL", "surprise")
    assert contextual.mode() == "off"


def test_is_enabled_stays_backward_compatible(monkeypatch):
    monkeypatch.setenv("TRAWL_CONTEXTUAL_RETRIEVAL", "1")
    assert contextual.is_enabled() is True

    monkeypatch.setenv("TRAWL_CONTEXTUAL_RETRIEVAL", "auto")
    assert contextual.is_enabled() is False


def test_should_use_contextual_on_and_off(monkeypatch):
    chunks = [_chunk("alpha")]

    monkeypatch.setenv("TRAWL_CONTEXTUAL_RETRIEVAL", "1")
    assert contextual.should_use_contextual(query="alpha", chunks=chunks, page_title="") is True

    monkeypatch.setenv("TRAWL_CONTEXTUAL_RETRIEVAL", "0")
    assert contextual.should_use_contextual(query="alpha", chunks=chunks, page_title="") is False


def test_auto_disables_tiny_pages(monkeypatch):
    monkeypatch.setenv("TRAWL_CONTEXTUAL_RETRIEVAL", "auto")
    chunks = [_chunk("alpha"), _chunk("beta", index=1)]

    assert contextual.should_use_contextual(query="simple question", chunks=chunks, page_title="") is False


def test_auto_enables_identifier_queries(monkeypatch):
    monkeypatch.setenv("TRAWL_CONTEXTUAL_RETRIEVAL", "auto")
    chunks = [_chunk("x", index=i) for i in range(3)]

    assert contextual.should_use_contextual(
        query="how does asyncio.gather() handle exceptions",
        chunks=chunks,
        page_title="Python docs",
    ) is True


def test_auto_enables_large_pages(monkeypatch):
    monkeypatch.setenv("TRAWL_CONTEXTUAL_RETRIEVAL", "auto")
    chunks = [_chunk("x", index=i) for i in range(16)]

    assert contextual.should_use_contextual(query="concept query", chunks=chunks, page_title="Docs") is True


def test_auto_enables_repeated_records(monkeypatch):
    monkeypatch.setenv("TRAWL_CONTEXTUAL_RETRIEVAL", "auto")
    chunks = [_chunk("x", index=0), _chunk("y", index=1, record_group_id=2)]

    assert contextual.should_use_contextual(query="jobs", chunks=chunks, page_title="Listings") is True


def test_auto_disabled_when_prefix_cap_zero(monkeypatch):
    monkeypatch.setenv("TRAWL_CONTEXTUAL_RETRIEVAL", "auto")
    monkeypatch.setenv("TRAWL_CONTEXT_PREFIX_MAX_CHARS", "0")
    chunks = [_chunk("x", index=i) for i in range(16)]

    assert contextual.should_use_contextual(query="asyncio.gather()", chunks=chunks, page_title="Docs") is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_contextual_auto.py -q
```

Expected: FAIL with `AttributeError` for `contextual.mode` or `contextual.should_use_contextual`.

- [ ] **Step 3: Implement mode and policy helpers**

Modify `src/trawl/contextual.py`:

```python
import re
```

Add near `DEFAULT_MAX_PREFIX_CHARS`:

```python
PREFIX_VERSION = "deterministic-v1"
AUTO_MIN_CHUNKS = 16
AUTO_TINY_PAGE_MAX_CHUNKS = 2
_IDENTIFIER_RE = re.compile(
    r"([A-Za-z_][A-Za-z0-9_]*[.:/][A-Za-z0-9_./:-]+|[A-Za-z_][A-Za-z0-9_]*\(\))"
)
_CODE_HINT_RE = re.compile(
    r"\b(api|class|cli|def|function|handler|method|module|parameter|signature|"
    r"traceback|import|async|await|exception|error|config|endpoint|sdk)\b",
    re.IGNORECASE,
)
```

Replace `is_enabled()` with:

```python
def mode() -> str:
    """Return contextual retrieval mode: off, on, or auto."""
    raw = os.environ.get("TRAWL_CONTEXTUAL_RETRIEVAL", "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return "on"
    if raw == "auto":
        return "auto"
    return "off"


def is_enabled() -> bool:
    """Return True only when contextual retrieval is forced on."""
    return mode() == "on"
```

Add before `build_contextual_text()`:

```python
def prefix_version() -> str:
    """Return the contextual prefix version used for cache keying."""
    return os.environ.get("TRAWL_CONTEXT_PREFIX_VERSION", PREFIX_VERSION).strip() or PREFIX_VERSION


def should_use_contextual(
    *,
    query: str,
    chunks: list[Chunk],
    page_title: str = "",
) -> bool:
    """Return whether contextual retrieval should be used for this request."""
    current = mode()
    if current == "off":
        return False
    if max_prefix_chars() <= 0:
        return False
    if current == "on":
        return True
    if not chunks:
        return False
    if len(chunks) <= AUTO_TINY_PAGE_MAX_CHUNKS:
        return False
    if _looks_identifier_query(query):
        return True
    if len(chunks) >= AUTO_MIN_CHUNKS:
        return True
    return any(c.record_group_id is not None for c in chunks)


def _looks_identifier_query(query: str) -> bool:
    if _IDENTIFIER_RE.search(query):
        return True
    if "`" in query:
        return True
    return bool(_CODE_HINT_RE.search(query) and re.search(r"[A-Za-z_][A-Za-z0-9_]*", query))
```

- [ ] **Step 4: Wire pipeline through policy helper**

Modify `src/trawl/pipeline.py` helper:

```python
def _contextual_batch(chunks: list[chunking.Chunk], page_title: str, query: str = ""):
    if not contextual.should_use_contextual(query=query, chunks=chunks, page_title=page_title):
        return None
    return contextual.build_contextual_texts(chunks, page_title=page_title)
```

Update call sites:

```python
context_batch = _contextual_batch(chunks, page_title, query or "")
```

For call sites where `query` is guaranteed to be `str`, use:

```python
context_batch = _contextual_batch(chunks, page_title, query)
```

- [ ] **Step 5: Run focused tests**

Run:

```bash
pytest tests/test_contextual.py tests/test_contextual_auto.py tests/test_pipeline_contextual.py tests/test_retrieval_contextual.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit auto policy**

Run:

```bash
git add src/trawl/contextual.py src/trawl/pipeline.py tests/test_contextual_auto.py
git commit -m "feat(contextual): add auto retrieval policy"
```

Expected: commit succeeds.

---

### Task 3: Add Contextual Rollout Documentation

**Files:**
- Modify: `README.md`
- Modify: `.env.example`

- [ ] **Step 1: Update README environment table**

Change the `TRAWL_CONTEXTUAL_RETRIEVAL` row in `README.md` to:

```markdown
| `TRAWL_CONTEXTUAL_RETRIEVAL` | `0` | `0` disables contextual retrieval, `1` forces deterministic page/section context for dense and BM25 retrieval inputs, and `auto` enables it for identifier/code-heavy queries, large pages, and repeated-record pages. Output chunks are unchanged. |
```

Add a row after `TRAWL_CONTEXT_PREFIX_MAX_CHARS`:

```markdown
| `TRAWL_CONTEXT_PREFIX_VERSION` | `deterministic-v1` | Prefix version string used for contextual embedding cache invalidation. |
```

- [ ] **Step 2: Update `.env.example`**

Replace the contextual retrieval block with:

```bash
# ---- Contextual retrieval (optional; off by default) ----
# 0 disables, 1 forces on, auto enables for identifier/code-heavy queries,
# large pages, and repeated-record pages. Returned chunks are unchanged.
# TRAWL_CONTEXTUAL_RETRIEVAL=0
# TRAWL_CONTEXT_PREFIX_MAX_CHARS=320
# TRAWL_CONTEXT_PREFIX_VERSION=deterministic-v1
```

- [ ] **Step 3: Verify docs mention auto and prefix version**

Run:

```bash
rg -n "TRAWL_CONTEXTUAL_RETRIEVAL|TRAWL_CONTEXT_PREFIX_VERSION" README.md .env.example
```

Expected: output includes `auto` in README and `TRAWL_CONTEXT_PREFIX_VERSION` in both files.

- [ ] **Step 4: Commit docs**

Run:

```bash
git add README.md .env.example
git commit -m "docs: document contextual retrieval auto mode"
```

Expected: commit succeeds.

---

### Task 4: Add Embedding Cache Module

**Files:**
- Create: `src/trawl/embedding_cache.py`
- Create: `tests/test_embedding_cache.py`

- [ ] **Step 1: Write failing embedding cache tests**

Create `tests/test_embedding_cache.py`:

```python
"""Unit tests for document embedding cache."""

from __future__ import annotations

import json
import time

from trawl import embedding_cache


def test_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("TRAWL_EMBED_CACHE_TTL", raising=False)
    monkeypatch.setenv("TRAWL_EMBED_CACHE_PATH", str(tmp_path))

    key = embedding_cache.CacheKey(
        model="bge-m3",
        base_url="http://localhost:8081/v1",
        text="hello",
        contextual_mode="off",
        prefix_max_chars=320,
        prefix_version="deterministic-v1",
    )

    embedding_cache.put(key, [1.0, 0.0])
    assert embedding_cache.get(key) is None


def test_put_get_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setenv("TRAWL_EMBED_CACHE_TTL", "60")
    monkeypatch.setenv("TRAWL_EMBED_CACHE_PATH", str(tmp_path))

    key = embedding_cache.CacheKey(
        model="bge-m3",
        base_url="http://localhost:8081/v1",
        text="hello",
        contextual_mode="on",
        prefix_max_chars=320,
        prefix_version="deterministic-v1",
    )

    embedding_cache.put(key, [1.0, 2.0, 3.0], now=1000.0)
    assert embedding_cache.get(key, now=1001.0) == [1.0, 2.0, 3.0]


def test_key_changes_with_contextual_mode():
    base = dict(
        model="bge-m3",
        base_url="http://localhost:8081/v1",
        text="same text",
        prefix_max_chars=320,
        prefix_version="deterministic-v1",
    )

    off = embedding_cache.key_for(embedding_cache.CacheKey(contextual_mode="off", **base))
    on = embedding_cache.key_for(embedding_cache.CacheKey(contextual_mode="on", **base))

    assert off != on


def test_key_changes_with_prefix_version():
    base = dict(
        model="bge-m3",
        base_url="http://localhost:8081/v1",
        text="same text",
        contextual_mode="auto",
        prefix_max_chars=320,
    )

    v1 = embedding_cache.key_for(embedding_cache.CacheKey(prefix_version="deterministic-v1", **base))
    v2 = embedding_cache.key_for(embedding_cache.CacheKey(prefix_version="deterministic-v2", **base))

    assert v1 != v2


def test_expired_entry_is_removed(monkeypatch, tmp_path):
    monkeypatch.setenv("TRAWL_EMBED_CACHE_TTL", "10")
    monkeypatch.setenv("TRAWL_EMBED_CACHE_PATH", str(tmp_path))
    key = embedding_cache.CacheKey(
        model="bge-m3",
        base_url="http://localhost:8081/v1",
        text="hello",
        contextual_mode="off",
        prefix_max_chars=320,
        prefix_version="deterministic-v1",
    )

    embedding_cache.put(key, [1.0], now=1000.0)
    assert embedding_cache.get(key, now=1011.0) is None
    assert not list(tmp_path.glob("*.json"))


def test_malformed_entry_is_removed(monkeypatch, tmp_path):
    monkeypatch.setenv("TRAWL_EMBED_CACHE_TTL", "60")
    monkeypatch.setenv("TRAWL_EMBED_CACHE_PATH", str(tmp_path))
    path = tmp_path / "bad.json"
    path.write_text("{not-json", encoding="utf-8")

    key = embedding_cache.CacheKey(
        model="bge-m3",
        base_url="http://localhost:8081/v1",
        text="bad",
        contextual_mode="off",
        prefix_max_chars=320,
        prefix_version="deterministic-v1",
    )
    target = embedding_cache.path_for_key(embedding_cache.key_for(key))
    path.rename(target)

    assert embedding_cache.get(key) is None
    assert not target.exists()


def test_schema_mismatch_is_removed(monkeypatch, tmp_path):
    monkeypatch.setenv("TRAWL_EMBED_CACHE_TTL", "60")
    monkeypatch.setenv("TRAWL_EMBED_CACHE_PATH", str(tmp_path))
    key = embedding_cache.CacheKey(
        model="bge-m3",
        base_url="http://localhost:8081/v1",
        text="hello",
        contextual_mode="off",
        prefix_max_chars=320,
        prefix_version="deterministic-v1",
    )
    target = embedding_cache.path_for_key(embedding_cache.key_for(key))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"schema": -1, "cached_at": time.time(), "embedding": [1.0]}))

    assert embedding_cache.get(key) is None
    assert not target.exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_embedding_cache.py -q
```

Expected: FAIL during import because `trawl.embedding_cache` does not exist.

- [ ] **Step 3: Implement `src/trawl/embedding_cache.py`**

Create `src/trawl/embedding_cache.py`:

```python
"""On-disk cache for document embedding vectors."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
DEFAULT_TTL_SECONDS = 0
DEFAULT_MAX_MB = 512
DEFAULT_CACHE_DIR = "~/.cache/trawl/embeddings"
TRIM_HEADROOM_FRACTION = 0.20


@dataclass(frozen=True)
class CacheKey:
    model: str
    base_url: str
    text: str
    contextual_mode: str
    prefix_max_chars: int
    prefix_version: str


def _ttl_seconds() -> int:
    try:
        return int(os.environ.get("TRAWL_EMBED_CACHE_TTL", DEFAULT_TTL_SECONDS))
    except ValueError:
        return DEFAULT_TTL_SECONDS


def _max_bytes() -> int:
    try:
        mb = int(os.environ.get("TRAWL_EMBED_CACHE_MAX_MB", DEFAULT_MAX_MB))
    except ValueError:
        mb = DEFAULT_MAX_MB
    return max(mb, 1) * 1024 * 1024


def _cache_dir() -> Path:
    return Path(os.environ.get("TRAWL_EMBED_CACHE_PATH", DEFAULT_CACHE_DIR)).expanduser()


def is_enabled() -> bool:
    return _ttl_seconds() > 0


def key_for(key: CacheKey) -> str:
    payload = {
        "schema": SCHEMA_VERSION,
        "model": key.model,
        "base_url": key.base_url,
        "text_sha256": hashlib.sha256(key.text.encode("utf-8")).hexdigest(),
        "contextual_mode": key.contextual_mode,
        "prefix_max_chars": key.prefix_max_chars,
        "prefix_version": key.prefix_version,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def path_for_key(cache_key: str) -> Path:
    return _cache_dir() / f"{cache_key}.json"


def get(key: CacheKey, *, now: float | None = None) -> list[float] | None:
    if not is_enabled():
        return None

    path = path_for_key(key_for(key))
    if not path.exists():
        return None

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _safe_unlink(path)
        return None

    if raw.get("schema") != SCHEMA_VERSION:
        _safe_unlink(path)
        return None

    now_ts = time.time() if now is None else now
    cached_at = float(raw.get("cached_at") or 0)
    if cached_at + _ttl_seconds() < now_ts:
        _safe_unlink(path)
        return None

    embedding = raw.get("embedding")
    if not isinstance(embedding, list):
        _safe_unlink(path)
        return None
    try:
        return [float(x) for x in embedding]
    except (TypeError, ValueError):
        _safe_unlink(path)
        return None


def put(key: CacheKey, embedding: list[float], *, now: float | None = None) -> None:
    if not is_enabled():
        return

    cache_dir = _cache_dir()
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning("embedding_cache: cannot create %s: %s", cache_dir, e)
        return

    payload = {
        "schema": SCHEMA_VERSION,
        "cached_at": time.time() if now is None else now,
        "key": asdict(key) | {"text": "<sha256>"},
        "embedding": embedding,
    }
    target = path_for_key(key_for(key))
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".tmp",
            dir=cache_dir,
            delete=False,
            encoding="utf-8",
        ) as tf:
            json.dump(payload, tf, ensure_ascii=False)
            tmp_path = Path(tf.name)
        os.replace(tmp_path, target)
    except OSError as e:
        logger.warning("embedding_cache: write failed: %s", e)
        return

    _trim_if_over_cap()


def clear() -> None:
    cache_dir = _cache_dir()
    if not cache_dir.exists():
        return
    for path in cache_dir.glob("*.json"):
        _safe_unlink(path)


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        logger.debug("embedding_cache: unlink %s failed: %s", path, e)


def _trim_if_over_cap() -> None:
    cache_dir = _cache_dir()
    if not cache_dir.exists():
        return
    try:
        files = [(path, path.stat().st_size, path.stat().st_mtime) for path in cache_dir.glob("*.json")]
    except OSError as e:
        logger.debug("embedding_cache: stat walk failed: %s", e)
        return

    total = sum(size for _path, size, _mtime in files)
    cap = _max_bytes()
    if total <= cap:
        return

    target = int(cap * (1.0 - TRIM_HEADROOM_FRACTION))
    for path, size, _mtime in sorted(files, key=lambda row: row[2]):
        _safe_unlink(path)
        total -= size
        if total <= target:
            break
```

- [ ] **Step 4: Run embedding cache tests**

Run:

```bash
pytest tests/test_embedding_cache.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit embedding cache module**

Run:

```bash
git add src/trawl/embedding_cache.py tests/test_embedding_cache.py
git commit -m "feat(retrieval): add document embedding cache"
```

Expected: commit succeeds.

---

### Task 5: Wire Embedding Cache Into Retrieval

**Files:**
- Modify: `src/trawl/retrieval.py`
- Test: `tests/test_retrieval_embedding_cache.py`

- [ ] **Step 1: Write failing retrieval cache tests**

Create `tests/test_retrieval_embedding_cache.py`:

```python
"""Retrieval tests for document embedding cache integration."""

from __future__ import annotations

from trawl import retrieval
from trawl.chunking import Chunk


def _chunk(text: str) -> Chunk:
    return Chunk(text=text, embed_text=text, char_count=len(text))


def test_retrieve_reuses_cached_document_embedding(monkeypatch, tmp_path):
    monkeypatch.setenv("TRAWL_EMBED_CACHE_TTL", "60")
    monkeypatch.setenv("TRAWL_EMBED_CACHE_PATH", str(tmp_path))
    monkeypatch.setenv("TRAWL_CONTEXTUAL_RETRIEVAL", "0")
    chunks = [_chunk("alpha body")]
    calls: list[list[str]] = []

    def _fake_embed(_client, _base_url, _model, texts):
        calls.append(list(texts))
        if texts == ["alpha query"]:
            return [[1.0, 0.0]]
        return [[1.0, 0.0]]

    monkeypatch.setattr(retrieval, "_embed_batch", _fake_embed)

    first = retrieval.retrieve("alpha query", chunks, k=1)
    second = retrieval.retrieve("alpha query", chunks, k=1)

    assert first.error is None
    assert second.error is None
    assert calls == [["alpha query"], ["alpha body"], ["alpha query"]]
    assert second.scored[0].chunk is chunks[0]


def test_contextual_mode_invalidates_document_embedding_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("TRAWL_EMBED_CACHE_TTL", "60")
    monkeypatch.setenv("TRAWL_EMBED_CACHE_PATH", str(tmp_path))
    chunks = [_chunk("alpha body")]
    calls: list[list[str]] = []

    def _fake_embed(_client, _base_url, _model, texts):
        calls.append(list(texts))
        if texts == ["alpha query"]:
            return [[1.0, 0.0]]
        return [[1.0, 0.0]]

    monkeypatch.setattr(retrieval, "_embed_batch", _fake_embed)

    monkeypatch.setenv("TRAWL_CONTEXTUAL_RETRIEVAL", "0")
    retrieval.retrieve("alpha query", chunks, k=1)

    monkeypatch.setenv("TRAWL_CONTEXTUAL_RETRIEVAL", "1")
    retrieval.retrieve("alpha query", chunks, k=1, context_texts=["Title: T\n\nalpha body"])

    assert calls == [
        ["alpha query"],
        ["alpha body"],
        ["alpha query"],
        ["Title: T\n\nalpha body"],
    ]


def test_embedding_cache_disabled_keeps_current_embedding_calls(monkeypatch, tmp_path):
    monkeypatch.setenv("TRAWL_EMBED_CACHE_TTL", "0")
    monkeypatch.setenv("TRAWL_EMBED_CACHE_PATH", str(tmp_path))
    chunks = [_chunk("alpha body")]
    calls: list[list[str]] = []

    def _fake_embed(_client, _base_url, _model, texts):
        calls.append(list(texts))
        if texts == ["alpha query"]:
            return [[1.0, 0.0]]
        return [[1.0, 0.0]]

    monkeypatch.setattr(retrieval, "_embed_batch", _fake_embed)

    retrieval.retrieve("alpha query", chunks, k=1)
    retrieval.retrieve("alpha query", chunks, k=1)

    assert calls == [["alpha query"], ["alpha body"], ["alpha query"], ["alpha body"]]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_retrieval_embedding_cache.py -q
```

Expected: FAIL because `retrieval.retrieve()` does not use `embedding_cache`.

- [ ] **Step 3: Import cache modules in retrieval**

Modify top of `src/trawl/retrieval.py`:

```python
from . import contextual, embedding_cache
```

- [ ] **Step 4: Add cached document embedding helper**

Add below `_embed_batch()`:

```python
def _embed_documents_with_cache(
    client: httpx.Client,
    base_url: str,
    model: str,
    texts: list[str],
    *,
    contextual_mode: str,
) -> tuple[list[list[float]], int]:
    embeddings: list[list[float] | None] = []
    misses: list[tuple[int, str, embedding_cache.CacheKey]] = []
    prefix_max = contextual.max_prefix_chars()
    prefix_version = contextual.prefix_version()

    for index, text in enumerate(texts):
        key = embedding_cache.CacheKey(
            model=model,
            base_url=base_url,
            text=text,
            contextual_mode=contextual_mode,
            prefix_max_chars=prefix_max,
            prefix_version=prefix_version,
        )
        cached = embedding_cache.get(key)
        if cached is None:
            embeddings.append(None)
            misses.append((index, text, key))
        else:
            embeddings.append(cached)

    embed_calls = 0
    for start in range(0, len(misses), EMBEDDING_BATCH):
        batch = misses[start : start + EMBEDDING_BATCH]
        if not batch:
            continue
        batch_embeddings = _embed_batch(client, base_url, model, [text for _index, text, _key in batch])
        embed_calls += 1
        for (index, _text, key), embedding in zip(batch, batch_embeddings, strict=True):
            embeddings[index] = embedding
            embedding_cache.put(key, embedding)

    return [embedding for embedding in embeddings if embedding is not None], embed_calls
```

- [ ] **Step 5: Use helper in `retrieve()`**

Replace the document embedding loop:

```python
chunk_embs: list[list[float]] = []
for start in range(0, len(chunk_texts), EMBEDDING_BATCH):
    batch = chunk_texts[start : start + EMBEDDING_BATCH]
    chunk_embs.extend(_embed_batch(client, base_url, model, batch))
    embed_calls += 1
```

with:

```python
contextual_mode = contextual.mode() if context_texts is not None else "off"
chunk_embs, doc_embed_calls = _embed_documents_with_cache(
    client,
    base_url,
    model,
    chunk_texts,
    contextual_mode=contextual_mode,
)
embed_calls += doc_embed_calls
```

- [ ] **Step 6: Run retrieval cache tests**

Run:

```bash
pytest tests/test_retrieval_embedding_cache.py tests/test_retrieval_contextual.py tests/test_retrieval_hybrid.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit retrieval cache wiring**

Run:

```bash
git add src/trawl/retrieval.py tests/test_retrieval_embedding_cache.py
git commit -m "feat(retrieval): cache document embeddings"
```

Expected: commit succeeds.

---

### Task 6: Document Embedding Cache Configuration

**Files:**
- Modify: `README.md`
- Modify: `.env.example`

- [ ] **Step 1: Add README environment rows**

Add after embedding endpoint rows in `README.md`:

```markdown
| `TRAWL_EMBED_CACHE_TTL` | `0` | Document embedding cache TTL in seconds. `0` disables the cache. |
| `TRAWL_EMBED_CACHE_PATH` | `~/.cache/trawl/embeddings` | Directory for cached document embedding vectors. |
| `TRAWL_EMBED_CACHE_MAX_MB` | `512` | Soft size cap for the embedding cache; old entries are trimmed by mtime. |
```

- [ ] **Step 2: Add `.env.example` block**

Add after the embedding endpoint settings:

```bash
# ---- Embedding cache (optional; off by default) ----
# Caches document/chunk embeddings, not query embeddings. Cache keys include
# model, endpoint, input text hash, contextual mode, and prefix version.
# TRAWL_EMBED_CACHE_TTL=0
# TRAWL_EMBED_CACHE_PATH=~/.cache/trawl/embeddings
# TRAWL_EMBED_CACHE_MAX_MB=512
```

- [ ] **Step 3: Verify docs**

Run:

```bash
rg -n "TRAWL_EMBED_CACHE" README.md .env.example
```

Expected: both files contain all three variables.

- [ ] **Step 4: Commit docs**

Run:

```bash
git add README.md .env.example
git commit -m "docs: document embedding cache settings"
```

Expected: commit succeeds.

---

### Task 7: Run Cache-Controlled Measurement

**Files:**
- Create: `docs/superpowers/handoffs/2026-05-02-contextual-retrieval-rollout-measurement.md`

- [ ] **Step 1: Run focused verification**

Run:

```bash
mamba run -n trawl pytest tests/test_contextual.py \
  tests/test_contextual_auto.py \
  tests/test_retrieval_contextual.py \
  tests/test_retrieval_embedding_cache.py \
  tests/test_pipeline_contextual.py \
  tests/test_retrieval_hybrid.py \
  tests/test_telemetry.py -q
mamba run -n trawl ruff check src tests
```

Expected: focused tests pass and Ruff reports `All checks passed!`.

- [ ] **Step 2: Run warm-cache paired pipeline measurements**

Run:

```bash
export TRAWL_FETCH_CACHE_TTL=600
export TRAWL_EMBED_CACHE_TTL=0

unset TRAWL_CONTEXTUAL_RETRIEVAL
mamba run -n trawl python tests/test_pipeline.py --only very_short_page

unset TRAWL_CONTEXTUAL_RETRIEVAL
mamba run -n trawl python tests/test_pipeline.py

TRAWL_CONTEXTUAL_RETRIEVAL=auto \
  mamba run -n trawl python tests/test_pipeline.py

TRAWL_CONTEXTUAL_RETRIEVAL=1 \
  mamba run -n trawl python tests/test_pipeline.py
```

Expected: collect pass count, failed IDs, p50/p95, and result directories for baseline, auto, and forced-on.

- [ ] **Step 3: Run agent-pattern measurements with repeats**

Run:

```bash
export TRAWL_FETCH_CACHE_TTL=600
export TRAWL_EMBED_CACHE_TTL=0

unset TRAWL_CONTEXTUAL_RETRIEVAL
mamba run -n trawl python tests/test_agent_patterns.py --category code_heavy_query --repeats 3

TRAWL_CONTEXTUAL_RETRIEVAL=auto \
  mamba run -n trawl python tests/test_agent_patterns.py --category code_heavy_query --repeats 3

TRAWL_CONTEXTUAL_RETRIEVAL=1 \
  mamba run -n trawl python tests/test_agent_patterns.py --category code_heavy_query --repeats 3
```

Expected: collect pass count, failed IDs, p50/p95, and whether `coding/claude_code_python_asyncio_lookup` remains fixed.

- [ ] **Step 4: Run embedding-cache-on smoke measurement**

Run:

```bash
export TRAWL_FETCH_CACHE_TTL=600
export TRAWL_EMBED_CACHE_TTL=600
rm -rf /tmp/trawl-embed-cache-measure
export TRAWL_EMBED_CACHE_PATH=/tmp/trawl-embed-cache-measure

TRAWL_CONTEXTUAL_RETRIEVAL=auto \
  mamba run -n trawl python tests/test_pipeline.py --only english_tech_docs

TRAWL_CONTEXTUAL_RETRIEVAL=auto \
  mamba run -n trawl python tests/test_pipeline.py --only english_tech_docs
```

Expected: second run should have lower retrieval time or fewer document embedding calls if telemetry is expanded later; at minimum it should preserve pass/fail behavior.

- [ ] **Step 5: Create measurement note**

Create `docs/superpowers/handoffs/2026-05-02-contextual-retrieval-rollout-measurement.md`:

```markdown
# Contextual Retrieval Rollout Measurement - 2026-05-02

## Environment

- Branch:
- HEAD:
- Reranking:
- Fetch cache:
- Embedding cache:

## Focused Verification

## Pipeline Results

| mode | pass | fail ids | latency p50/p95 | result dir | notes |
|---|---:|---|---:|---|---|
| baseline | | | | | |
| auto | | | | | |
| forced-on | | | | | |

## Agent Pattern Results

| mode | pass | fail ids | latency p50/p95 | result dir | notes |
|---|---:|---|---:|---|---|
| baseline | | | | | |
| auto | | | | | |
| forced-on | | | | | |

## Flips

- flipped_to_pass:
- flipped_to_fail:

## Embedding Cache Smoke

## Decision

- default:
- recommendation:
- rationale:

## Follow-Up
```

Populate each field from Steps 1-4 with observed values before committing.

- [ ] **Step 6: Commit measurement note**

Run:

```bash
git add docs/superpowers/handoffs/2026-05-02-contextual-retrieval-rollout-measurement.md
git commit -m "docs: measure contextual retrieval rollout"
```

Expected: commit succeeds.

---

### Task 8: Default Recommendation Change

**Files:**
- Modify: `README.md`
- Modify: `.env.example`
- Modify: `src/trawl/contextual.py`
- Test: `tests/test_contextual_auto.py`

- [ ] **Step 1: Decide from the measurement note**

Open:

```bash
sed -n '1,220p' docs/superpowers/handoffs/2026-05-02-contextual-retrieval-rollout-measurement.md
```

Proceed only if the documented decision says `default: auto`.

- [ ] **Step 2: Update default mode test**

Modify `tests/test_contextual_auto.py`:

```python
def test_mode_defaults_to_auto(monkeypatch):
    monkeypatch.delenv("TRAWL_CONTEXTUAL_RETRIEVAL", raising=False)
    assert contextual.mode() == "auto"
```

Remove or replace `test_mode_defaults_to_off`.

- [ ] **Step 3: Run the test and verify it fails**

Run:

```bash
pytest tests/test_contextual_auto.py::test_mode_defaults_to_auto -q
```

Expected: FAIL because unset mode still returns `off`.

- [ ] **Step 4: Change default mode**

Modify `src/trawl/contextual.py`:

```python
def mode() -> str:
    """Return contextual retrieval mode: off, on, or auto."""
    raw = os.environ.get("TRAWL_CONTEXTUAL_RETRIEVAL", "auto").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return "on"
    if raw == "auto":
        return "auto"
    return "off"
```

- [ ] **Step 5: Update docs for default auto**

Change README default value for `TRAWL_CONTEXTUAL_RETRIEVAL` from `0` to `auto`.

Change `.env.example` contextual block:

```bash
# TRAWL_CONTEXTUAL_RETRIEVAL=auto
```

- [ ] **Step 6: Run final verification**

Run:

```bash
pytest tests/test_contextual.py tests/test_contextual_auto.py tests/test_pipeline_contextual.py tests/test_retrieval_contextual.py tests/test_retrieval_embedding_cache.py tests/test_retrieval_hybrid.py tests/test_telemetry.py -q
ruff check src tests
```

Expected: all tests pass and Ruff reports no issues.

- [ ] **Step 7: Commit default recommendation**

Run:

```bash
git add src/trawl/contextual.py tests/test_contextual_auto.py README.md .env.example
git commit -m "feat(contextual): default retrieval policy to auto"
```

Expected: commit succeeds.

---

## Self-Review Checklist

- Spec coverage: Tasks 1 and 7 cover rollout design and measurement; Tasks 2-3 cover `auto`; Tasks 4-6 cover contextual embedding cache; Task 8 covers default change only after measurement.
- Placeholder scan: before executing, run the same forbidden-phrase Python scan from Task 1 Step 2 against this plan file.
- Type consistency: `contextual.mode()`, `contextual.should_use_contextual()`, `contextual.prefix_version()`, `embedding_cache.CacheKey`, `embedding_cache.get()`, and `embedding_cache.put()` are introduced before use.
