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
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

DEFAULT_TTL_SECONDS = 300
DEFAULT_MAX_MB = 100
DEFAULT_CACHE_DIR = "~/.cache/trawl/fetches"
DEFAULT_REVALIDATE_TIMEOUT_SECONDS = 10.0

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
    extractor: str | None = None
    source_selector: str | None = None
    source_xpath: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    content_hash: str | None = None


@dataclass
class RevalidationResult:
    """Outcome of a conditional HTTP cache revalidation attempt."""

    status: str
    elapsed_ms: int
    etag: str | None = None
    last_modified: str | None = None
    error: str | None = None


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


def _revalidate_timeout_seconds() -> float:
    try:
        return float(
            os.environ.get(
                "TRAWL_FETCH_CACHE_REVALIDATE_TIMEOUT",
                str(DEFAULT_REVALIDATE_TIMEOUT_SECONDS),
            )
        )
    except ValueError:
        return DEFAULT_REVALIDATE_TIMEOUT_SECONDS


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


def content_hash(markdown: str) -> str:
    """Return the stable content hash stored with cache records."""
    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()


# ---------- Public API


def get(url: str, *, now: float | None = None) -> CachedFetch | None:
    """Return the cached record for ``url`` if fresh, else None.

    Stale, malformed, or schema-mismatched records are deleted as a
    side effect so the next caller doesn't repeat the check.
    """
    if not is_enabled():
        return None

    record, is_stale = _read(url, now=now)
    if record is None:
        return None
    if is_stale:
        _safe_unlink(_path_for(url))
        return None
    return record


def get_with_state(url: str, *, now: float | None = None) -> tuple[CachedFetch | None, bool]:
    """Return ``(record, is_stale)`` without deleting stale records.

    This is used by the pipeline to attempt HTTP revalidation before
    falling back to the existing stale-entry refetch behavior.
    """
    return _read(url, now=now)


def revalidate(entry: CachedFetch, *, now: float | None = None) -> RevalidationResult:
    """Conditionally revalidate ``entry`` using ETag/Last-Modified.

    A ``304`` refreshes the cached record's ``cached_at`` timestamp and
    preserves the existing markdown. A ``2xx`` non-304 means the origin
    changed; callers should perform a normal fetch/render and replace the
    cache with that pipeline output. Missing validators or HTTP errors
    return status strings rather than raising.
    """
    headers: dict[str, str] = {}
    if entry.etag:
        headers["If-None-Match"] = entry.etag
    if entry.last_modified:
        headers["If-Modified-Since"] = entry.last_modified
    if not headers:
        return RevalidationResult(status="missing_validators", elapsed_ms=0)

    t0 = time.monotonic()
    try:
        with httpx.Client(
            follow_redirects=True,
            timeout=_revalidate_timeout_seconds(),
            headers={"User-Agent": "trawl/0.1 (cache revalidation)"},
        ) as client:
            resp = client.get(entry.url, headers=headers)
    except httpx.HTTPError as e:
        return RevalidationResult(
            status="error",
            elapsed_ms=int((time.monotonic() - t0) * 1000),
            error=f"{type(e).__name__}: {e}",
        )

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    etag = _header(resp.headers, "etag") or entry.etag
    last_modified = _header(resp.headers, "last-modified") or entry.last_modified
    if resp.status_code == 304:
        refreshed = replace(
            entry,
            cached_at=time.time() if now is None else now,
            etag=etag,
            last_modified=last_modified,
            content_hash=entry.content_hash or content_hash(entry.markdown),
        )
        put(refreshed)
        return RevalidationResult(
            status="not_modified",
            elapsed_ms=elapsed_ms,
            etag=etag,
            last_modified=last_modified,
        )
    if 200 <= resp.status_code < 300:
        return RevalidationResult(
            status="modified",
            elapsed_ms=elapsed_ms,
            etag=etag,
            last_modified=last_modified,
        )
    return RevalidationResult(
        status="error",
        elapsed_ms=elapsed_ms,
        etag=etag,
        last_modified=last_modified,
        error=f"unexpected status {resp.status_code}",
    )


def _read(url: str, *, now: float | None = None) -> tuple[CachedFetch | None, bool]:
    if not is_enabled():
        return None, False

    path = _path_for(url)
    if not path.exists():
        return None, False

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("fetch_cache: dropping unreadable entry %s: %s", path, e)
        _safe_unlink(path)
        return None, False

    if raw.get("schema") != SCHEMA_VERSION:
        logger.debug("fetch_cache: schema mismatch at %s", path)
        _safe_unlink(path)
        return None, False

    now_ts = time.time() if now is None else now
    cached_at = float(raw.get("cached_at") or 0)
    ttl = _ttl_seconds()
    is_stale = cached_at + ttl < now_ts

    try:
        return CachedFetch(
            url=str(raw["url"]),
            markdown=str(raw["markdown"]),
            page_title=str(raw.get("page_title", "")),
            fetcher_used=str(raw["fetcher_used"]),
            content_type=raw.get("content_type"),
            cached_at=cached_at,
            fetch_elapsed_ms=int(raw.get("fetch_elapsed_ms", 0)),
            extractor=raw.get("extractor"),
            source_selector=raw.get("source_selector"),
            source_xpath=raw.get("source_xpath"),
            etag=raw.get("etag"),
            last_modified=raw.get("last_modified"),
            content_hash=raw.get("content_hash") or content_hash(str(raw["markdown"])),
        ), is_stale
    except (KeyError, TypeError, ValueError) as e:
        logger.debug("fetch_cache: record missing fields at %s: %s", path, e)
        _safe_unlink(path)
        return None, False


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

    if not entry.content_hash:
        entry = replace(entry, content_hash=content_hash(entry.markdown))
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


def _header(headers: object, name: str) -> str | None:
    try:
        value = headers.get(name)  # type: ignore[attr-defined]
    except AttributeError:
        return None
    if value is None:
        return None
    return str(value)


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
