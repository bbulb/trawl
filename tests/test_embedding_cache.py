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
