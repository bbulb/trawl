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
        files = [
            (path, path.stat().st_size, path.stat().st_mtime)
            for path in cache_dir.glob("*.json")
        ]
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
