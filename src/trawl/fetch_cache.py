"""On-disk per-URL fetch cache (C8).

Caches the pre-chunk output of a successful HTML/PDF fetch so repeated
visits to the same URL skip Playwright/Trafilatura. The cache key is a
sha256 of the URL; the value is a small JSON record with the markdown,
page title, fetcher name, and metadata needed to resume the pipeline
at the chunking step.

Scope
-----
- Caches Playwright+Trafilatura, PDF, and API fetcher output (wikipedia,
  github, stackexchange, youtube).
- Skips profile fast/transfer paths, passthrough JSON/RSS/XML, and
  error results — each has its own failure mode we don't want to cache.
- No HTTP ETag/Last-Modified revalidation — TTL only. Follow-up design
  point documented in the C8 spec.

Env vars
--------
- ``TRAWL_FETCH_CACHE_TTL``      — seconds, default 300. Set to ``0`` to
  disable entirely (``put`` becomes a no-op; ``get`` always returns None).
- ``TRAWL_FETCH_CACHE_PATH``     — directory, default ``~/.cache/trawl/fetches``.
- ``TRAWL_FETCH_CACHE_MAX_MB``   — soft size cap, default 100. Exceeding
  this triggers an LRU trim (mtime-based, 20% headroom reclaimed).

Concurrency
-----------
Writes are atomic via ``tempfile.NamedTemporaryFile`` + ``os.replace``.
Concurrent writers for the same URL produce last-writer-wins with
effectively identical contents; concurrent readers tolerate a half-
written file (JSON decode error → treated as cache miss and deleted).
"""

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

DEFAULT_TTL_SECONDS = 300
DEFAULT_MAX_MB = 100
DEFAULT_CACHE_DIR = "~/.cache/trawl/fetches"

# Reclaim 20% of the cap so a single trim doesn't thrash. Value picked
# to balance "rarely trim" against "don't evict more than necessary".
TRIM_HEADROOM_FRACTION = 0.20


# ---------- Record


@dataclass
class CachedFetch:
    """One fetch record. JSON-serialisable; all fields required."""

    url: str
    markdown: str
    page_title: str
    fetcher_used: str
    content_type: str | None
    cached_at: float
    fetch_elapsed_ms: int


# ---------- Env helpers


def _ttl_seconds() -> int:
    try:
        return int(os.environ.get("TRAWL_FETCH_CACHE_TTL", DEFAULT_TTL_SECONDS))
    except ValueError:
        return DEFAULT_TTL_SECONDS


def _max_bytes() -> int:
    try:
        mb = int(os.environ.get("TRAWL_FETCH_CACHE_MAX_MB", DEFAULT_MAX_MB))
    except ValueError:
        mb = DEFAULT_MAX_MB
    return max(mb, 1) * 1024 * 1024


def _cache_dir() -> Path:
    raw = os.environ.get("TRAWL_FETCH_CACHE_PATH", DEFAULT_CACHE_DIR)
    return Path(raw).expanduser()


def is_enabled() -> bool:
    return _ttl_seconds() > 0


# ---------- Key / path


def _key_for(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _path_for(url: str) -> Path:
    return _cache_dir() / f"{_key_for(url)}.json"


# ---------- Public API


def get(url: str, *, now: float | None = None) -> CachedFetch | None:
    """Return the cached record for ``url`` if fresh, else None.

    Stale, malformed, or schema-mismatched records are deleted as a
    side effect so the next caller doesn't repeat the check.
    """
    if not is_enabled():
        return None

    path = _path_for(url)
    if not path.exists():
        return None

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("fetch_cache: dropping unreadable entry %s: %s", path, e)
        _safe_unlink(path)
        return None

    if raw.get("schema") != SCHEMA_VERSION:
        logger.debug("fetch_cache: schema mismatch at %s", path)
        _safe_unlink(path)
        return None

    now_ts = time.time() if now is None else now
    cached_at = float(raw.get("cached_at") or 0)
    ttl = _ttl_seconds()
    if cached_at + ttl < now_ts:
        _safe_unlink(path)
        return None

    try:
        return CachedFetch(
            url=str(raw["url"]),
            markdown=str(raw["markdown"]),
            page_title=str(raw.get("page_title", "")),
            fetcher_used=str(raw["fetcher_used"]),
            content_type=raw.get("content_type"),
            cached_at=cached_at,
            fetch_elapsed_ms=int(raw.get("fetch_elapsed_ms", 0)),
        )
    except (KeyError, TypeError, ValueError) as e:
        logger.debug("fetch_cache: record missing fields at %s: %s", path, e)
        _safe_unlink(path)
        return None


def put(entry: CachedFetch) -> None:
    """Write ``entry`` atomically; no-op when disabled."""
    if not is_enabled():
        return

    cache_dir = _cache_dir()
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning("fetch_cache: cannot create %s: %s", cache_dir, e)
        return

    payload = asdict(entry)
    payload["schema"] = SCHEMA_VERSION

    target = _path_for(entry.url)
    try:
        # NamedTemporaryFile in the same dir so os.replace is atomic.
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
        logger.warning("fetch_cache: write failed for %s: %s", entry.url, e)
        return

    _trim_if_over_cap()


def clear(url: str | None = None) -> None:
    """Delete one URL's entry (or all entries when ``url`` is None)."""
    cache_dir = _cache_dir()
    if not cache_dir.exists():
        return
    if url is not None:
        _safe_unlink(_path_for(url))
        return
    for path in cache_dir.glob("*.json"):
        _safe_unlink(path)


# ---------- Internal


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        logger.debug("fetch_cache: unlink %s failed: %s", path, e)


def _trim_if_over_cap() -> None:
    """Soft LRU. Only scans when the cap would be exceeded."""
    cache_dir = _cache_dir()
    if not cache_dir.exists():
        return

    cap = _max_bytes()
    try:
        entries: list[tuple[float, int, Path]] = []
        total = 0
        for path in cache_dir.glob("*.json"):
            try:
                st = path.stat()
            except OSError:
                continue
            total += st.st_size
            entries.append((st.st_mtime, st.st_size, path))
        if total <= cap:
            return
    except OSError as e:
        logger.debug("fetch_cache: stat walk failed: %s", e)
        return

    reclaim_to = int(cap * (1.0 - TRIM_HEADROOM_FRACTION))
    entries.sort(key=lambda t: t[0])  # oldest mtime first
    for _mtime, size, path in entries:
        if total <= reclaim_to:
            break
        _safe_unlink(path)
        total -= size
