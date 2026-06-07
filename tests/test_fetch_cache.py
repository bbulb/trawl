"""Tests for `src/trawl/fetch_cache.py` (C8).

Pure-function tests — no Playwright, no network, no embedding server.
Exercises put/get round-trip, TTL expiry, disable-via-env, LRU trim,
schema/corrupt-file handling, and clear().
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from trawl import fetch_cache

# ---------- fixtures


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path: Path, monkeypatch):
    """Point the cache at a temp dir with defaults for each test."""
    monkeypatch.setenv("TRAWL_FETCH_CACHE_PATH", str(tmp_path))
    monkeypatch.setenv("TRAWL_FETCH_CACHE_TTL", "300")
    monkeypatch.setenv("TRAWL_FETCH_CACHE_MAX_MB", "100")
    yield tmp_path


def _entry(
    url: str = "https://example.com/a",
    markdown: str = "# Hello\n\nworld",
    page_title: str = "Hello — example",
    fetcher_used: str = "playwright+trafilatura",
    content_type: str | None = "text/html",
    cached_at: float | None = None,
    fetch_elapsed_ms: int = 1234,
    etag: str | None = None,
    last_modified: str | None = None,
) -> fetch_cache.CachedFetch:
    return fetch_cache.CachedFetch(
        url=url,
        markdown=markdown,
        page_title=page_title,
        fetcher_used=fetcher_used,
        content_type=content_type,
        cached_at=cached_at if cached_at is not None else time.time(),
        fetch_elapsed_ms=fetch_elapsed_ms,
        etag=etag,
        last_modified=last_modified,
    )


# ---------- put/get round-trip


def test_put_then_get_returns_equal_record():
    entry = _entry(etag='"abc"', last_modified="Mon, 04 May 2026 00:00:00 GMT")
    fetch_cache.put(entry)
    got = fetch_cache.get(entry.url)
    assert got is not None
    assert got.url == entry.url
    assert got.markdown == entry.markdown
    assert got.page_title == entry.page_title
    assert got.fetcher_used == entry.fetcher_used
    assert got.content_type == entry.content_type
    assert got.fetch_elapsed_ms == entry.fetch_elapsed_ms
    assert got.etag == '"abc"'
    assert got.last_modified == "Mon, 04 May 2026 00:00:00 GMT"
    assert got.content_hash == fetch_cache.content_hash(entry.markdown)


def test_get_missing_url_returns_none():
    assert fetch_cache.get("https://not-cached.example.com/") is None


def test_put_creates_cache_dir_when_missing(tmp_path, monkeypatch):
    nested = tmp_path / "deep" / "cache"
    monkeypatch.setenv("TRAWL_FETCH_CACHE_PATH", str(nested))
    fetch_cache.put(_entry())
    assert nested.exists()
    assert any(nested.glob("*.json"))


def test_distinct_urls_do_not_collide():
    a = _entry(url="https://example.com/a", markdown="A")
    b = _entry(url="https://example.com/b", markdown="B")
    fetch_cache.put(a)
    fetch_cache.put(b)
    assert fetch_cache.get(a.url).markdown == "A"
    assert fetch_cache.get(b.url).markdown == "B"


def test_put_overwrites_prior_entry_for_same_url():
    url = "https://example.com/overwrite"
    fetch_cache.put(_entry(url=url, markdown="first"))
    fetch_cache.put(_entry(url=url, markdown="second"))
    assert fetch_cache.get(url).markdown == "second"


def test_none_content_type_roundtrips():
    entry = _entry(content_type=None)
    fetch_cache.put(entry)
    got = fetch_cache.get(entry.url)
    assert got.content_type is None


def test_legacy_record_without_revalidation_fields_still_reads():
    url = "https://example.com/legacy"
    path = fetch_cache._path_for(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": fetch_cache.SCHEMA_VERSION,
                "url": url,
                "markdown": "# legacy",
                "page_title": "legacy",
                "fetcher_used": "playwright+trafilatura",
                "content_type": "text/html",
                "cached_at": time.time(),
                "fetch_elapsed_ms": 10,
            }
        ),
        encoding="utf-8",
    )

    got = fetch_cache.get(url)

    assert got is not None
    assert got.etag is None
    assert got.last_modified is None
    assert got.content_hash == fetch_cache.content_hash("# legacy")


# ---------- TTL expiry


def test_stale_entry_returns_none(monkeypatch):
    monkeypatch.setenv("TRAWL_FETCH_CACHE_TTL", "60")
    entry = _entry(cached_at=time.time() - 3600)
    fetch_cache.put(entry)
    assert fetch_cache.get(entry.url) is None


def test_stale_entry_is_deleted_on_get():
    entry = _entry(cached_at=time.time() - 1_000_000)
    fetch_cache.put(entry)
    fetch_cache.get(entry.url)
    # File should have been unlinked.
    path = fetch_cache._path_for(entry.url)
    assert not path.exists()


def test_get_with_explicit_now_controls_expiry():
    entry = _entry(cached_at=1000.0)
    fetch_cache.put(entry)
    # 200s after cached_at is inside default 300s TTL.
    assert fetch_cache.get(entry.url, now=1200.0) is not None
    # 400s after cached_at is past the TTL.
    fetch_cache.put(entry)  # re-put since previous get may have deleted
    assert fetch_cache.get(entry.url, now=1400.0) is None


def test_get_with_state_returns_stale_entry_without_deleting():
    entry = _entry(cached_at=1000.0, etag='"old"')
    fetch_cache.put(entry)

    got, is_stale = fetch_cache.get_with_state(entry.url, now=1400.0)

    assert got is not None
    assert is_stale is True
    assert got.etag == '"old"'
    assert fetch_cache._path_for(entry.url).exists()


def test_revalidate_not_modified_refreshes_cached_entry(monkeypatch):
    entry = _entry(
        cached_at=1000.0,
        etag='"old"',
        last_modified="Mon, 04 May 2026 00:00:00 GMT",
    )
    fetch_cache.put(entry)
    seen_headers: dict[str, str] = {}

    class FakeResponse:
        status_code = 304
        headers = {"etag": '"new"', "last-modified": "Mon, 04 May 2026 01:00:00 GMT"}

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, url, *, headers):
            assert url == entry.url
            seen_headers.update(headers)
            return FakeResponse()

    monkeypatch.setattr(fetch_cache.httpx, "Client", FakeClient)

    result = fetch_cache.revalidate(entry, now=2000.0)

    assert result.status == "not_modified"
    assert seen_headers == {
        "If-None-Match": '"old"',
        "If-Modified-Since": "Mon, 04 May 2026 00:00:00 GMT",
    }
    refreshed = fetch_cache.get(entry.url, now=2001.0)
    assert refreshed is not None
    assert refreshed.cached_at == 2000.0
    assert refreshed.etag == '"new"'
    assert refreshed.last_modified == "Mon, 04 May 2026 01:00:00 GMT"


def test_revalidate_modified_reports_200_without_reusing_stale(monkeypatch):
    entry = _entry(cached_at=1000.0, etag='"old"')

    class FakeResponse:
        status_code = 200
        headers = {"etag": '"replacement"'}

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, _url, *, headers):
            assert headers == {"If-None-Match": '"old"'}
            return FakeResponse()

    monkeypatch.setattr(fetch_cache.httpx, "Client", FakeClient)

    result = fetch_cache.revalidate(entry, now=2000.0)

    assert result.status == "modified"
    assert result.etag == '"replacement"'


def test_revalidate_without_validators_reports_missing_validators():
    entry = _entry(cached_at=1000.0)

    result = fetch_cache.revalidate(entry, now=2000.0)

    assert result.status == "missing_validators"


# ---------- disable via env


def test_ttl_zero_disables_put(monkeypatch):
    monkeypatch.setenv("TRAWL_FETCH_CACHE_TTL", "0")
    fetch_cache.put(_entry())
    assert not fetch_cache.is_enabled()
    # With the cache dir possibly still empty, no files are created.
    assert list(fetch_cache._cache_dir().glob("*.json")) == []


def test_ttl_zero_disables_get(monkeypatch):
    # Put with TTL>0, then disable, then get.
    fetch_cache.put(_entry())
    monkeypatch.setenv("TRAWL_FETCH_CACHE_TTL", "0")
    assert fetch_cache.get("https://example.com/a") is None


# ---------- corrupt / schema mismatch


def test_malformed_json_is_skipped_and_deleted():
    url = "https://example.com/bad"
    path = fetch_cache._path_for(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not valid json {{", encoding="utf-8")
    assert fetch_cache.get(url) is None
    assert not path.exists()


def test_wrong_schema_version_is_skipped():
    url = "https://example.com/schema"
    path = fetch_cache._path_for(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": 9999,
                "url": url,
                "markdown": "x",
                "page_title": "",
                "fetcher_used": "whatever",
                "content_type": None,
                "cached_at": time.time(),
                "fetch_elapsed_ms": 0,
            }
        ),
        encoding="utf-8",
    )
    assert fetch_cache.get(url) is None
    assert not path.exists()


def test_missing_required_fields_returns_none():
    url = "https://example.com/partial"
    path = fetch_cache._path_for(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": fetch_cache.SCHEMA_VERSION,
                # no url / markdown / fetcher_used
                "cached_at": time.time(),
            }
        ),
        encoding="utf-8",
    )
    assert fetch_cache.get(url) is None


# ---------- LRU trim


def test_trim_evicts_oldest_when_over_cap(monkeypatch):
    # Cap at 1 MB so a handful of larger entries trigger trim.
    monkeypatch.setenv("TRAWL_FETCH_CACHE_MAX_MB", "1")

    # Four ~400 KB entries — total ≈ 1.6 MB, over the 1 MB cap.
    big_md = "x" * (400 * 1024)
    times = [time.time() - 40, time.time() - 30, time.time() - 20, time.time() - 10]
    entries = [
        _entry(url=f"https://example.com/{i}", markdown=big_md, cached_at=times[i])
        for i in range(4)
    ]
    for e in entries:
        fetch_cache.put(e)
        # put() triggers trim on the last one. Set mtime manually so
        # oldest-first eviction is deterministic.
        fpath = fetch_cache._path_for(e.url)
        if fpath.exists():
            import os as _os

            _os.utime(fpath, (e.cached_at, e.cached_at))

    # Force one more put to re-run trim with the manually-set mtimes.
    fetch_cache.put(_entry(url="https://example.com/trigger", markdown=big_md))
    import os as _os

    fpath = fetch_cache._path_for("https://example.com/trigger")
    _os.utime(fpath, (time.time(), time.time()))

    # Oldest entry (i=0) should be evicted.
    assert fetch_cache.get("https://example.com/0") is None


def test_trim_respects_headroom(monkeypatch):
    monkeypatch.setenv("TRAWL_FETCH_CACHE_MAX_MB", "1")
    # Fill exactly to cap — should not evict anything.
    small_md = "x" * (100 * 1024)  # 100 KB, 5 entries → 500 KB total
    for i in range(5):
        fetch_cache.put(_entry(url=f"https://example.com/ok/{i}", markdown=small_md))
    # All five should remain.
    for i in range(5):
        assert fetch_cache.get(f"https://example.com/ok/{i}") is not None


# ---------- clear()


def test_clear_specific_url():
    fetch_cache.put(_entry(url="https://a.example/"))
    fetch_cache.put(_entry(url="https://b.example/"))
    fetch_cache.clear("https://a.example/")
    assert fetch_cache.get("https://a.example/") is None
    assert fetch_cache.get("https://b.example/") is not None


def test_clear_all():
    fetch_cache.put(_entry(url="https://a.example/"))
    fetch_cache.put(_entry(url="https://b.example/"))
    fetch_cache.clear()
    assert fetch_cache.get("https://a.example/") is None
    assert fetch_cache.get("https://b.example/") is None


def test_clear_when_dir_missing_is_noop(tmp_path, monkeypatch):
    # Point at a non-existent path.
    ghost = tmp_path / "does-not-exist"
    monkeypatch.setenv("TRAWL_FETCH_CACHE_PATH", str(ghost))
    fetch_cache.clear()  # must not raise
    fetch_cache.clear("https://anything.example/")


# ---------- env var handling


def test_invalid_ttl_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("TRAWL_FETCH_CACHE_TTL", "not-a-number")
    # Still enabled because fallback is the default (300).
    assert fetch_cache.is_enabled()
    fetch_cache.put(_entry())
    assert fetch_cache.get("https://example.com/a") is not None


def test_invalid_max_mb_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("TRAWL_FETCH_CACHE_MAX_MB", "not-a-number")
    # put should still succeed (fallback to 100 MB).
    fetch_cache.put(_entry())
    assert fetch_cache.get("https://example.com/a") is not None
