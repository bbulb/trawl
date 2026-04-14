"""Visit counter for the profile feature.

A single JSON file at $TRAWL_VISITS_FILE (default
~/.cache/trawl/visits.json) maps url_hash → visit_count. Used by
fetch_relevant's fallback path to populate the lazy suggest_profile
hint after N visits.

Concurrency: the MCP server serializes fetch calls via an asyncio
worker thread, so a single writer at a time is a safe assumption for
the MVP. Writes are atomic via .tmp + os.replace.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .profile import url_hash

DEFAULT_VISITS_FILE = (
    Path(
        os.environ.get(
            "TRAWL_VISITS_FILE",
            str(Path.home() / ".cache" / "trawl" / "visits.json"),
        )
    )
    .expanduser()
    .resolve()
)


def _load_all() -> dict[str, int]:
    if not DEFAULT_VISITS_FILE.exists():
        return {}
    try:
        return json.loads(DEFAULT_VISITS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # Corrupt or unreadable file: treat as empty. Don't crash trawl.
        return {}


def _save_all(counts: dict[str, int]) -> None:
    DEFAULT_VISITS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DEFAULT_VISITS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(counts, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, DEFAULT_VISITS_FILE)


def track_visit(url: str) -> int:
    """Increment the visit counter for `url` and return the new count."""
    counts = _load_all()
    h = url_hash(url)
    counts[h] = counts.get(h, 0) + 1
    _save_all(counts)
    return counts[h]


def get_visit_count(url: str) -> int:
    counts = _load_all()
    return counts.get(url_hash(url), 0)
