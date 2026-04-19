"""Pattern dataclass + YAML → object validator.

A pattern is either:
    * a single-operation pattern with top-level `url` + `query`, or
    * a multi-operation pattern with a `steps` list (each step is
      itself a small fetch_page / profile_page invocation).

The validator enforces:
    * required fields and enum membership
    * assertion DSL keys are in a fixed whitelist
    * id uniqueness is checked at loader-level (not here)

Strict-but-friendly errors: every PatternValidationError carries the
shard path and id (when available) so the harness can point at the
exact YAML location.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = 1

# Enums --------------------------------------------------------------

PRIMARY_AGENTS = {"claude_code", "openclaw", "hermes"}

CATEGORIES = {
    "single_fetch",
    "repeat_visits",
    "host_transfer",
    "passthrough",
    "compositional",
    "error_handling",
    "large_page",
    "code_heavy_query",
}

LIVE_MODES = {"required", "optional", "never"}

OPERATIONS = {"fetch_page", "profile_page"}

# Whitelist of assertion keys. Each key is checked against a per-key
# rule in `_check_assertion`.
ASSERTION_KEYS = {
    "chunks_contain_all",
    "chunks_contain_any",
    "chunks_contain_pattern",
    "n_chunks_returned",
    "profile_used",
    "path",
    "fetcher_used",
    "error_is_none",
    "error_contains",
    "suggest_profile",
    "content_type",
    "truncated",
    # C16 enrichment payload assertions (excerpts / outbound_links /
    # page_entities / chain_hints). Keep names aligned with the
    # PipelineResult field they inspect.
    "excerpts_min_count",
    "outbound_links_contain_any",
    "page_entities_contain_any",
    "chain_hints_has_key",
}

BUDGET_KEYS = {
    "total_ms_p95",
    "output_chars_max",
    "n_chunks_max",
}


# Errors -------------------------------------------------------------


class PatternValidationError(ValueError):
    """Raised when a pattern fails schema validation.

    The message includes the shard path + pattern id for navigation.
    """


# Dataclasses --------------------------------------------------------


@dataclass
class PatternStep:
    """One step inside a multi-operation pattern."""

    op: str  # ∈ OPERATIONS
    url: str | None = None
    query: str | None = None
    ref: int | None = None  # reuse url/query of step at this index
    assertions: dict[str, Any] = field(default_factory=dict)
    budgets: dict[str, Any] = field(default_factory=dict)


@dataclass
class Pattern:
    """A single agent usage pattern.

    Either (url, query) is set OR steps is non-empty — never both.
    """

    id: str
    primary_agent: list[str]
    category: str
    description: str
    live: str = "required"
    # Single-operation form
    url: str | None = None
    query: str | None = None
    assertions: dict[str, Any] = field(default_factory=dict)
    budgets: dict[str, Any] = field(default_factory=dict)
    # Multi-operation form
    steps: list[PatternStep] = field(default_factory=list)
    # Free-form metadata (not validated)
    meta: dict[str, Any] = field(default_factory=dict)
    # Provenance — set by loader, not by YAML
    shard: str = ""

    @property
    def is_multi_op(self) -> bool:
        return bool(self.steps)


# Parsing ------------------------------------------------------------


def parse_pattern(raw: dict[str, Any], *, shard: str = "") -> Pattern:
    """Convert one YAML mapping into a validated Pattern.

    Raises PatternValidationError with a useful message on any
    schema violation.
    """
    pid = str(raw.get("id", "")).strip()
    if not pid:
        raise PatternValidationError(f"[{shard}] pattern missing required field 'id'")

    def _err(msg: str) -> PatternValidationError:
        return PatternValidationError(f"[{shard}:{pid}] {msg}")

    primary_agent = raw.get("primary_agent")
    if isinstance(primary_agent, str):
        primary_agent = [primary_agent]
    if not isinstance(primary_agent, list) or not primary_agent:
        raise _err("'primary_agent' must be a non-empty list of agent names")
    bad_agents = [a for a in primary_agent if a not in PRIMARY_AGENTS]
    if bad_agents:
        raise _err(
            f"unknown primary_agent values {bad_agents!r}; allowed: {sorted(PRIMARY_AGENTS)}"
        )

    category = raw.get("category")
    if category not in CATEGORIES:
        raise _err(
            f"category {category!r} not in allowed set {sorted(CATEGORIES)}"
        )

    description = str(raw.get("description", "")).strip()
    if not description:
        raise _err("'description' is required and must be non-empty")

    live = raw.get("live", "required")
    if live not in LIVE_MODES:
        raise _err(f"live={live!r} not in {sorted(LIVE_MODES)}")

    has_steps = "steps" in raw and raw["steps"]
    has_single = "url" in raw or "query" in raw

    if has_steps and has_single:
        raise _err(
            "pattern must use EITHER top-level url/query OR steps[], not both"
        )
    if not has_steps and not has_single:
        raise _err("pattern must define either url+query or steps[]")

    pattern = Pattern(
        id=pid,
        primary_agent=primary_agent,
        category=category,
        description=description,
        live=live,
        meta=raw.get("meta", {}) or {},
        shard=shard,
    )

    if has_single:
        url = raw.get("url")
        query = raw.get("query")
        if not isinstance(url, str) or not url:
            raise _err("'url' is required for single-op patterns")
        # query may be empty for passthrough/profile-only patterns
        pattern.url = url
        pattern.query = query if isinstance(query, str) else None
        pattern.assertions = _validate_assertions(raw.get("assertions", {}) or {}, _err)
        pattern.budgets = _validate_budgets(raw.get("budgets", {}) or {}, _err)
    else:
        steps_raw = raw.get("steps") or []
        if not isinstance(steps_raw, list):
            raise _err("'steps' must be a list")
        pattern.steps = [
            _parse_step(s, idx, _err) for idx, s in enumerate(steps_raw)
        ]
        if not pattern.steps:
            raise _err("'steps' must contain at least one step")

    return pattern


def _parse_step(raw: Any, idx: int, _err) -> PatternStep:
    if not isinstance(raw, dict):
        raise _err(f"step {idx}: must be a mapping, got {type(raw).__name__}")

    op = raw.get("op")
    if op not in OPERATIONS:
        raise _err(f"step {idx}: op={op!r} not in {sorted(OPERATIONS)}")

    ref = raw.get("ref")
    if ref is not None:
        if not isinstance(ref, int) or ref < 0 or ref >= idx:
            raise _err(
                f"step {idx}: ref={ref!r} must be an integer in [0, {idx})"
            )
        if "url" in raw or "query" in raw:
            raise _err(
                f"step {idx}: when ref= is set, url/query must come from the referenced step"
            )

    url = raw.get("url") if ref is None else None
    query = raw.get("query") if ref is None else None
    if ref is None and op == "fetch_page" and not isinstance(url, str):
        raise _err(f"step {idx}: fetch_page requires url unless ref= is set")
    if ref is None and op == "profile_page" and not isinstance(url, str):
        raise _err(f"step {idx}: profile_page requires url unless ref= is set")

    assertions = _validate_assertions(raw.get("assertions", {}) or {}, _err)
    budgets = _validate_budgets(raw.get("budgets", {}) or {}, _err)

    return PatternStep(
        op=op,
        url=url if isinstance(url, str) else None,
        query=query if isinstance(query, str) else None,
        ref=ref,
        assertions=assertions,
        budgets=budgets,
    )


def _validate_assertions(raw: Any, _err) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise _err(f"assertions must be a mapping, got {type(raw).__name__}")
    bad_keys = set(raw) - ASSERTION_KEYS
    if bad_keys:
        raise _err(
            f"unknown assertion keys {sorted(bad_keys)}; allowed: {sorted(ASSERTION_KEYS)}"
        )
    for key, value in raw.items():
        _check_assertion_shape(key, value, _err)
    return dict(raw)


def _check_assertion_shape(key: str, value: Any, _err) -> None:
    """Per-key shape rules. Doesn't evaluate — only checks form."""
    if key in {"chunks_contain_all", "chunks_contain_any"}:
        if not isinstance(value, list) or not all(isinstance(s, str) for s in value):
            raise _err(f"assertion {key!r}: must be a list of strings")
        if not value:
            raise _err(f"assertion {key!r}: must be non-empty")
    elif key == "chunks_contain_pattern":
        if not isinstance(value, str):
            raise _err(f"assertion {key!r}: must be a regex string")
    elif key == "n_chunks_returned":
        if not isinstance(value, (int, str)):
            raise _err(
                f"assertion {key!r}: must be int or comparison string like '>= 3'"
            )
        if isinstance(value, str):
            _parse_comparison(value, _err, key=key)
    elif key in {"profile_used", "error_is_none", "suggest_profile", "truncated"}:
        if not isinstance(value, bool):
            raise _err(f"assertion {key!r}: must be bool")
    elif key in {"path", "fetcher_used", "content_type", "error_contains"}:
        if not isinstance(value, str):
            raise _err(f"assertion {key!r}: must be string")
    elif key == "excerpts_min_count":
        if not isinstance(value, (int, str)):
            raise _err(
                f"assertion {key!r}: must be int or comparison string like '>= 2'"
            )
        if isinstance(value, str):
            _parse_comparison(value, _err, key=key)
        elif value < 0:
            raise _err(f"assertion {key!r}: must be non-negative, got {value}")
    elif key in {"outbound_links_contain_any", "page_entities_contain_any"}:
        if not isinstance(value, list) or not all(isinstance(s, str) for s in value):
            raise _err(f"assertion {key!r}: must be a list of strings")
        if not value:
            raise _err(f"assertion {key!r}: must be non-empty")
    elif key == "chain_hints_has_key":
        if not isinstance(value, str) or not value:
            raise _err(f"assertion {key!r}: must be a non-empty string")


def _validate_budgets(raw: Any, _err) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise _err(f"budgets must be a mapping, got {type(raw).__name__}")
    bad_keys = set(raw) - BUDGET_KEYS
    if bad_keys:
        raise _err(
            f"unknown budget keys {sorted(bad_keys)}; allowed: {sorted(BUDGET_KEYS)}"
        )
    for key, value in raw.items():
        if not isinstance(value, (int, float)):
            raise _err(f"budget {key!r}: must be numeric")
        if value <= 0:
            raise _err(f"budget {key!r}: must be positive, got {value}")
    return dict(raw)


def _parse_comparison(spec: str, _err, *, key: str) -> tuple[str, int]:
    """Parse '>= 3' / '<= 12' / '== 5' / '< 8' / '> 2'.

    Returns (operator, threshold). The harness calls this again at
    eval time; here we only verify the form.
    """
    spec = spec.strip()
    for op in (">=", "<=", "==", "!=", ">", "<"):
        if spec.startswith(op):
            rest = spec[len(op):].strip()
            try:
                threshold = int(rest)
            except ValueError as e:
                raise _err(
                    f"{key!r}: comparison threshold not an integer: {spec!r}"
                ) from e
            return op, threshold
    raise _err(
        f"{key!r}: comparison must start with one of >= <= == != > <, got {spec!r}"
    )


def evaluate_comparison(spec: str | int, actual: int) -> bool:
    """Evaluate an n_chunks_returned-style comparison against an int.

    Used by the harness, not by the validator. Lives here so the
    parsing rules stay co-located with the schema.
    """
    if isinstance(spec, int):
        return actual == spec
    op, threshold = _parse_comparison(spec, _PassThroughError, key="comparison")
    return {
        ">=": actual >= threshold,
        "<=": actual <= threshold,
        "==": actual == threshold,
        "!=": actual != threshold,
        ">": actual > threshold,
        "<": actual < threshold,
    }[op]


def _PassThroughError(msg: str) -> ValueError:  # noqa: N802
    return ValueError(msg)
