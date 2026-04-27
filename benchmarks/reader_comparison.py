"""Query-based reader comparison benchmark."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

REQUIRED_CASE_FIELDS = {"id", "category", "url", "query", "expected_facts", "failure_class"}


def load_cases(path: Path) -> list[dict[str, Any]]:
    """Load and validate reader-comparison benchmark cases."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cases = data.get("cases")
    if not isinstance(cases, list):
        raise ValueError("case file must contain a cases list")
    for case in cases:
        validate_case(case)
    return cases


def validate_case(case: dict[str, Any]) -> None:
    """Validate one reader-comparison benchmark case."""
    missing = sorted(REQUIRED_CASE_FIELDS - set(case))
    if missing:
        raise ValueError(f"case {case.get('id', '<unknown>')} missing fields: {', '.join(missing)}")
    if not case["expected_facts"]:
        raise ValueError(f"case {case['id']} must define expected_facts")
    for fact in case["expected_facts"]:
        if "id" not in fact:
            raise ValueError(f"case {case['id']} has fact without id")
        checks = [name for name in ("all_of", "any_of", "pattern") if name in fact]
        if len(checks) != 1:
            raise ValueError(f"fact {fact['id']} must define exactly one matcher")


def fact_matches(text: str, fact: dict[str, Any]) -> bool:
    """Return whether text satisfies a fact matcher."""
    if "all_of" in fact:
        return all(value in text for value in fact["all_of"])
    if "any_of" in fact:
        return any(value in text for value in fact["any_of"])
    return re.search(fact["pattern"], text) is not None


def score_ranked_texts(ranked_texts: list[str], facts: list[dict[str, Any]]) -> dict[str, Any]:
    """Score expected fact coverage over ranked chunks or documents."""
    found: dict[str, int] = {}
    for rank, _text in enumerate(ranked_texts, start=1):
        cumulative = "\n\n".join(ranked_texts[:rank])
        for fact in facts:
            if fact["id"] not in found and fact_matches(cumulative, fact):
                found[fact["id"]] = rank
    missing = [fact["id"] for fact in facts if fact["id"] not in found]
    recall = len(found) / len(facts) if facts else 0.0
    first_rank = min(found.values()) if found else None
    return {
        "recall_at_k": recall,
        "mrr_at_k": (1.0 / first_rank) if first_rank else 0.0,
        "answer_grounding_hit": not missing,
        "missing_facts": missing,
    }
