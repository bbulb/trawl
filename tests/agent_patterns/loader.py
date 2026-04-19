"""Load every agent_patterns/*.yaml shard, validate, dedupe IDs.

Public API:
    load_all_patterns() -> list[Pattern]
    load_shard(name: str) -> list[Pattern]   # name without .yaml suffix
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from .schema import Pattern, PatternValidationError, parse_pattern

logger = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent


def _shard_paths() -> list[Path]:
    return sorted(_HERE.glob("*.yaml"))


def load_all_patterns() -> list[Pattern]:
    """Load + validate every shard. Raise on duplicate IDs across shards."""
    seen: dict[str, str] = {}  # id -> shard
    patterns: list[Pattern] = []
    for shard_path in _shard_paths():
        shard_patterns = _load_one(shard_path)
        for p in shard_patterns:
            if p.id in seen:
                raise PatternValidationError(
                    f"duplicate pattern id {p.id!r}: defined in both "
                    f"{seen[p.id]!r} and {shard_path.name!r}"
                )
            seen[p.id] = shard_path.name
            patterns.append(p)
    logger.info("loaded %d patterns from %d shards", len(patterns), len(_shard_paths()))
    return patterns


def load_shard(name: str) -> list[Pattern]:
    """Load + validate a single shard by short name (e.g. 'coding')."""
    path = _HERE / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"no shard named {name!r} at {path}")
    return _load_one(path)


def _load_one(path: Path) -> list[Pattern]:
    """Parse one YAML file. Top-level must be {patterns: [...]}."""
    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict) or "patterns" not in raw:
        raise PatternValidationError(
            f"[{path.name}] top-level must be a mapping with a 'patterns' key"
        )
    items = raw["patterns"]
    if not isinstance(items, list):
        raise PatternValidationError(f"[{path.name}] 'patterns' must be a list")
    out: list[Pattern] = []
    seen_ids: set[str] = set()
    for entry in items:
        if not isinstance(entry, dict):
            raise PatternValidationError(
                f"[{path.name}] each pattern entry must be a mapping; got {type(entry).__name__}"
            )
        p = parse_pattern(entry, shard=path.stem)
        if p.id in seen_ids:
            raise PatternValidationError(f"[{path.name}] duplicate id {p.id!r} within shard")
                f"[{path.name}] each pattern entry must be a mapping; "
                f"got {type(entry).__name__}"
            )
        p = parse_pattern(entry, shard=path.stem)
        if p.id in seen_ids:
            raise PatternValidationError(
                f"[{path.name}] duplicate id {p.id!r} within shard"
            )
        seen_ids.add(p.id)
        out.append(p)
    return out
