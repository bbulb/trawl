"""Opt-in JSONL telemetry for fetch_relevant() calls.

Activated only when TRAWL_TELEMETRY=1. All failures are swallowed so
telemetry can never break a user fetch. See
docs/superpowers/specs/2026-04-15-c4-telemetry-design.md.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from .pipeline import PipelineResult

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _query_sha1(query: str) -> str:
    return hashlib.sha1(query.encode("utf-8")).hexdigest()[:16]


def _build_event(result: "PipelineResult") -> dict:
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


def _enabled() -> bool:
    return os.environ.get("TRAWL_TELEMETRY", "").strip() in {"1", "true", "yes"}


def record(result: "PipelineResult") -> None:
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


DEFAULT_PATH = "~/.cache/trawl/telemetry.jsonl"
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


def _target_path() -> Path:
    raw = os.environ.get("TRAWL_TELEMETRY_PATH") or DEFAULT_PATH
    return Path(raw).expanduser()


def _write_event(result: "PipelineResult") -> None:
    path = _target_path()
    # Parent directory may be shared with other trawl caches
    # (profiles, visits) — don't touch its permissions.
    path.parent.mkdir(parents=True, exist_ok=True)
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
