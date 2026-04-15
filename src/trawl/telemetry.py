"""Opt-in JSONL telemetry for fetch_relevant() calls.

Activated only when TRAWL_TELEMETRY=1. All failures are swallowed so
telemetry can never break a user fetch. See
docs/superpowers/specs/2026-04-15-c4-telemetry-design.md.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .pipeline import PipelineResult

logger = logging.getLogger(__name__)


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


def _write_event(result: "PipelineResult") -> None:
    raise NotImplementedError
