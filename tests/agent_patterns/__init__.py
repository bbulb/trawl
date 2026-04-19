"""Agent usage pattern catalog.

Each YAML shard in this directory describes how trawl's primary
consumers (openclaw, hermes, Claude Code) call the pipeline. The
harness `tests/test_agent_patterns.py` loads every shard, validates
the schema, and runs each pattern against a live trawl deployment
(or against cached fixtures in dry-run mode).

See `docs/superpowers/specs/2026-04-19-agent-patterns-design.md` and
this directory's `README.md` for the catalog rules.
"""

from .loader import load_all_patterns, load_shard
from .schema import (
    SCHEMA_VERSION,
    Pattern,
    PatternStep,
    PatternValidationError,
    parse_pattern,
)

__all__ = [
    "SCHEMA_VERSION",
    "Pattern",
    "PatternStep",
    "PatternValidationError",
    "load_all_patterns",
    "load_shard",
    "parse_pattern",
]
