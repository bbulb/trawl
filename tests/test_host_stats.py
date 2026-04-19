"""Tests for `src/trawl/host_stats.py` (C9).

Pure-function tests — no Playwright, no network. Covers record/
ceiling_ms round-trip, warm-up threshold, percentile bounds,
sanity filter, env disable, hostname parsing edge cases, rolling
window, and corrupt-file recovery.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trawl import host_stats

# ---------- fixtures


@pytest.fixture(autouse=True)
def isolated_stats(tmp_path: Path, monkeypatch):
    """Point stats at a temp file with the module on by default."""
    monkeypatch.setenv("TRAWL_HOST_STATS_PATH", str(tmp_path / "host_stats.json"))
    monkeypatch.setenv("TRAWL_HOST_STATS", "1")
    yield tmp_path


# ---------- cold start


def test_ceiling_returns_default_when_no_file():
    assert host_stats.ceiling_ms("https://example.com/", default=5000) == 5000


def test_ceiling_returns_default_under_warmup_threshold():
    url = "https://example.com/"
    # Record one observation — below MIN_OBSERVATIONS.
    host_stats.record(url, 1000)
    assert host_stats.ceiling_ms(url, default=5000) == 5000


def test_record_then_ceiling_round_trip():
    url = "https://a.example.com/"
    for ms in (800, 900, 1000, 850, 1100):
        host_stats.record(url, ms)
    # p95 of [800, 850, 900, 1000, 1100] → ~1080 × 1.5 = 1620, floored at 1500.
    result = host_stats.ceiling_ms(url, default=5000)
    assert result >= host_stats.MIN_CEILING_MS
    assert result <= host_stats.MAX_CEILING_MS
    # The adaptive ceiling should replace the default (not equal 5000).
    assert result != 5000


# ---------- bounds


def test_ceiling_clamped_to_floor():
    url = "https://fast.example.com/"
    # Very fast observations — p95 * 1.5 would be below the floor.
    for _ in range(10):
        host_stats.record(url, 200)
    assert host_stats.ceiling_ms(url, default=5000) == host_stats.MIN_CEILING_MS


def test_ceiling_clamped_to_cap():
    url = "https://slow.example.com/"
    # 50 observations near the cap, so p95 × 1.5 would blow past the cap.
    for _ in range(50):
        host_stats.record(url, 12_000)
    assert host_stats.ceiling_ms(url, default=5000) == host_stats.MAX_CEILING_MS


def test_ceiling_uses_recent_observations_only():
    """The rolling window discards the oldest samples once over WINDOW_SIZE."""
    url = "https://evolving.example.com/"
    # Flood with "slow" observations.
    for _ in range(host_stats.WINDOW_SIZE):
        host_stats.record(url, 8_000)
    slow_ceiling = host_stats.ceiling_ms(url, default=5000)
    # Then push enough "fast" observations to evict every slow one.
    for _ in range(host_stats.WINDOW_SIZE):
        host_stats.record(url, 500)
    fast_ceiling = host_stats.ceiling_ms(url, default=5000)
    assert fast_ceiling < slow_ceiling


# ---------- sanity filter


def test_negative_observation_not_recorded():
    url = "https://weird.example.com/"
    host_stats.record(url, -1)
    data = host_stats.snapshot()
    assert url not in data["hosts"]
    assert "weird.example.com" not in data["hosts"]


def test_absurd_observation_not_recorded():
    url = "https://absurd.example.com/"
    host_stats.record(url, host_stats.MAX_CEILING_MS * 3)
    assert host_stats.snapshot()["hosts"].get("absurd.example.com") is None


def test_non_numeric_observation_not_recorded():
    url = "https://strange.example.com/"
    host_stats.record(url, "not-a-number")  # type: ignore[arg-type]
    assert host_stats.snapshot()["hosts"].get("strange.example.com") is None


# ---------- url parsing


def test_hostname_lowercased_for_dedup():
    host_stats.record("https://Example.COM/", 800)
    host_stats.record("https://example.com/", 900)
    data = host_stats.snapshot()
    assert "example.com" in data["hosts"]
    assert "Example.COM" not in data["hosts"]
    assert len(data["hosts"]["example.com"]["samples_ms"]) == 2


def test_url_without_hostname_ignored():
    host_stats.record("not-a-url", 1000)
    host_stats.record("/relative/path", 1000)
    assert host_stats.snapshot()["hosts"] == {}


def test_ceiling_for_unparseable_url_returns_default():
    assert host_stats.ceiling_ms("not-a-url", default=5000) == 5000


# ---------- env disable


def test_ttl_zero_disables_record(monkeypatch):
    monkeypatch.setenv("TRAWL_HOST_STATS", "0")
    host_stats.record("https://example.com/", 1000)
    # No file should have been created.
    assert not host_stats._stats_path().exists()


def test_disabled_returns_default_for_ceiling(monkeypatch):
    # Record with it enabled.
    url = "https://example.com/"
    for _ in range(10):
        host_stats.record(url, 800)
    # Flip disable, confirm default is returned.
    monkeypatch.setenv("TRAWL_HOST_STATS", "0")
    assert host_stats.ceiling_ms(url, default=5000) == 5000


# ---------- resilience


def test_corrupt_json_returns_default():
    path = host_stats._stats_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{{{not json", encoding="utf-8")
    assert host_stats.ceiling_ms("https://example.com/", default=5000) == 5000
    # Calling record on top of corrupt data recovers.
    for _ in range(6):
        host_stats.record("https://example.com/", 700)
    adaptive = host_stats.ceiling_ms("https://example.com/", default=5000)
    assert adaptive != 5000


def test_wrong_schema_treated_as_empty():
    path = host_stats._stats_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": 999,
                "hosts": {"example.com": {"samples_ms": [100, 200], "updated_at": 0}},
            }
        ),
        encoding="utf-8",
    )
    # Schema mismatch → treated as empty. Caller gets the default.
    assert host_stats.ceiling_ms("https://example.com/", default=5000) == 5000


# ---------- clear


def test_clear_specific_host():
    host_stats.record("https://a.example/", 800)
    host_stats.record("https://b.example/", 800)
    host_stats.clear("a.example")
    snap = host_stats.snapshot()["hosts"]
    assert "a.example" not in snap
    assert "b.example" in snap


def test_clear_all():
    host_stats.record("https://a.example/", 800)
    host_stats.record("https://b.example/", 800)
    host_stats.clear()
    assert host_stats.snapshot()["hosts"] == {}


# ---------- percentile helper


def test_percentile_single_sample():
    assert host_stats._percentile([1234], 95) == 1234.0


def test_percentile_matches_sorted_rank():
    samples = list(range(1, 101))  # 1..100 inclusive
    assert abs(host_stats._percentile(samples, 95) - 95.05) < 0.1
    assert host_stats._percentile(samples, 50) == 50.5


def test_percentile_empty_returns_zero():
    assert host_stats._percentile([], 95) == 0.0
