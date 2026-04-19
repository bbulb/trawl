"""Per-host adaptive content-ready ceiling (C9).

Tracks the last ``WINDOW_SIZE`` observed Playwright fetch durations
per hostname and returns an adaptive ceiling for subsequent fetches
to that host. New hosts fall back to the caller-provided default
until ``MIN_OBSERVATIONS`` samples have accumulated, so fresh
installs behave identically to the pre-C9 baseline.

Backed by a single JSON file at ``TRAWL_HOST_STATS_PATH`` (default
``~/.cache/trawl/host_stats.json``). Writes are atomic via
``tempfile.NamedTemporaryFile`` + ``os.replace``. Last-writer-wins
when multiple processes race — observation loss is tolerable
compared with the complexity of a file lock.

Env vars
--------
- ``TRAWL_HOST_STATS``       — set to ``0`` to disable (both record and
  ceiling_ms become no-ops). Default ``1``.
- ``TRAWL_HOST_STATS_PATH``  — override the storage path.

Internals
---------
All the tuning constants (window size, warm-up threshold, ceiling
bounds, multiplier) live as module-level constants; do not expose
them via env vars until real usage data justifies the knob. See
docs/superpowers/specs/2026-04-20-c9-per-host-ceiling-design.md.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

DEFAULT_PATH = "~/.cache/trawl/host_stats.json"

# Rolling-window geometry. Empirically sized to trade off "react to
# regime change" against "ignore single-fetch spikes". Bumping either
# value requires re-running the parity / workflows matrix because the
# per-host ceiling feeds directly into playwright content-ready wait.
WINDOW_SIZE = 50
MIN_OBSERVATIONS = 5

# Ceiling bounds. Floor keeps a safety margin for the content-ready
# detector (stableTicks=4 × polling=150ms + buffer). Cap prevents
# runaway — see the "feedback loop risk" section of the C9 spec.
MIN_CEILING_MS = 1500
MAX_CEILING_MS = 15_000

# Multiplier on the observed p95. 1.5 gives ~50% headroom above the
# worst-of-the-recent-20% observations so an occasional slow page
# still lands within the ceiling.
CEILING_MULTIPLIER = 1.5

# Observations above this are dropped as clearly anomalous (network
# hiccup, OS scheduling stall). 2x the runtime cap is conservative.
_OBSERVATION_SANITY_CAP_MS = MAX_CEILING_MS * 2


# ---------- Env + path helpers


def is_enabled() -> bool:
    return os.environ.get("TRAWL_HOST_STATS", "1") != "0"


def _stats_path() -> Path:
    raw = os.environ.get("TRAWL_HOST_STATS_PATH", DEFAULT_PATH)
    return Path(raw).expanduser()


def _hostname(url: str) -> str | None:
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    if not host:
        return None
    return host.lower()


# ---------- File I/O


def _load() -> dict:
    """Return the parsed stats file or an empty skeleton on any failure."""
    path = _stats_path()
    if not path.exists():
        return {"schema": SCHEMA_VERSION, "hosts": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("host_stats: unreadable stats file %s: %s", path, e)
        return {"schema": SCHEMA_VERSION, "hosts": {}}
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA_VERSION:
        logger.debug("host_stats: schema mismatch at %s", path)
        return {"schema": SCHEMA_VERSION, "hosts": {}}
    hosts = raw.get("hosts")
    if not isinstance(hosts, dict):
        return {"schema": SCHEMA_VERSION, "hosts": {}}
    return {"schema": SCHEMA_VERSION, "hosts": hosts}


def _save(data: dict) -> None:
    path = _stats_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning("host_stats: cannot create %s: %s", path.parent, e)
        return
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
            encoding="utf-8",
        ) as tf:
            json.dump(data, tf, ensure_ascii=False)
            tmp_path = Path(tf.name)
        os.replace(tmp_path, path)
    except OSError as e:
        logger.warning("host_stats: write failed: %s", e)


# ---------- Public API


def record(url: str, fetch_ms: int) -> None:
    """Append one observation to the host's rolling window.

    Silently skipped when disabled, when the URL has no usable hostname,
    or when ``fetch_ms`` is outside the sanity band.
    """
    if not is_enabled():
        return
    if not isinstance(fetch_ms, (int, float)) or fetch_ms < 0:
        return
    if fetch_ms > _OBSERVATION_SANITY_CAP_MS:
        return

    host = _hostname(url)
    if host is None:
        return

    data = _load()
    hosts = data["hosts"]
    entry = hosts.get(host) or {"samples_ms": [], "updated_at": 0.0}
    samples = list(entry.get("samples_ms") or [])
    samples.append(int(fetch_ms))
    if len(samples) > WINDOW_SIZE:
        samples = samples[-WINDOW_SIZE:]
    hosts[host] = {"samples_ms": samples, "updated_at": time.time()}
    _save(data)


def ceiling_ms(url: str, *, default: int) -> int:
    """Return the adaptive ceiling for ``url``, or ``default``.

    Returns ``default`` when disabled, when the host is unknown, when
    fewer than ``MIN_OBSERVATIONS`` samples are recorded, or when the
    stats file is unreadable. Otherwise returns ``p95 * multiplier``
    clamped to ``[MIN_CEILING_MS, MAX_CEILING_MS]``.
    """
    if not is_enabled():
        return default

    host = _hostname(url)
    if host is None:
        return default

    data = _load()
    entry = data["hosts"].get(host)
    if not entry:
        return default
    samples = entry.get("samples_ms") or []
    if len(samples) < MIN_OBSERVATIONS:
        return default
    p95 = _percentile(samples, 95)
    adaptive = int(p95 * CEILING_MULTIPLIER)
    return max(MIN_CEILING_MS, min(MAX_CEILING_MS, adaptive))


def clear(host: str | None = None) -> None:
    """Wipe stats for one host or all hosts. For tests + debugging."""
    data = _load()
    if host is None:
        data["hosts"] = {}
    else:
        data["hosts"].pop(host.lower(), None)
    _save(data)


def snapshot() -> dict:
    """Return a shallow copy of the full stats dict (for diagnostics)."""
    return _load()


# ---------- Internal


def _percentile(samples: list[int], pct: int) -> float:
    """Linear-interpolation percentile on a small list.

    ``statistics.quantiles`` needs n >= 2; for a single sample we
    return it as-is. `pct` is in [0, 100].
    """
    if not samples:
        return 0.0
    if len(samples) == 1:
        return float(samples[0])
    ordered = sorted(samples)
    rank = (pct / 100.0) * (len(ordered) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return float(ordered[lo])
    frac = rank - lo
    return ordered[lo] * (1.0 - frac) + ordered[hi] * frac
