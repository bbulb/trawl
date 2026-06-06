"""A/B: query-aware RRF weights vs equal-weight RRF (S3 validation).

Query-aware weighted fusion shipped in b81d57b (2026-04-27) and went
live with hybrid default-on (#58), but the isolated A/B that the S3
roadmap gate calls for was never run. This script measures whether the
weighting earns its place versus plain equal-weight RRF.

Method: for each reader_comparison case, fetch end-to-end twice with
identical settings, flipping ONLY `_fusion_weights` between the shipped
query-aware values and all-1.0 (equal weight) via monkeypatch — no
production toggle. Embeddings come from the shared cache, so the two
runs differ only in fusion ordering. Metrics per case: top-1 chunk
identity and the rank at which each expected fact first appears.

Run: mamba run -n trawl python benchmarks/query_aware_fusion_ab.py
Requires the bge-m3 endpoint (:8081); reranker (:8083) optional.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

from trawl import fetch_relevant, retrieval

CASES = Path(__file__).resolve().parent / "reader_comparison_cases.yaml"


def _equal_weights(query_type, ranker_names):
    return {name: 1.0 for name in ranker_names}


def _first_fact_rank(chunks: list[dict], fact: dict) -> int | None:
    """0-based rank of the first chunk containing any of the fact's
    any_of substrings, or None if absent from the returned chunks."""
    needles = fact.get("any_of") or []
    for rank, c in enumerate(chunks):
        text = c.get("text") or ""
        if any(n in text for n in needles):
            return rank
    return None


_USE_RERANK = "--no-rerank" not in sys.argv


def _measure(url: str, query: str):
    r = fetch_relevant(url, query, use_rerank=_USE_RERANK)
    chunks = r.chunks or []
    top1 = chunks[0].get("text", "")[:40] if chunks else None
    return r, chunks, top1


def main() -> int:
    cases = yaml.safe_load(CASES.read_text(encoding="utf-8"))
    if isinstance(cases, dict):
        cases = cases.get("cases", cases.get("patterns", []))

    orig = retrieval._fusion_weights
    rows = []
    for case in cases:
        url, query = case["url"], case["query"]
        qtype = retrieval._classify_query(query)
        facts = case.get("expected_facts", [])

        # Weighted (shipped default)
        retrieval._fusion_weights = orig
        rw, cw, t1w = _measure(url, query)
        # Equal weight
        retrieval._fusion_weights = _equal_weights
        re_, ce, t1e = _measure(url, query)
        retrieval._fusion_weights = orig

        ranks_w = {f["id"]: _first_fact_rank(cw, f) for f in facts}
        ranks_e = {f["id"]: _first_fact_rank(ce, f) for f in facts}
        found_w = sum(1 for v in ranks_w.values() if v is not None)
        found_e = sum(1 for v in ranks_e.values() if v is not None)
        rows.append(
            {
                "id": case["id"],
                "qtype": qtype,
                "top1_same": t1w == t1e,
                "found_w": found_w,
                "found_e": found_e,
                "n_facts": len(facts),
                "ranks_w": ranks_w,
                "ranks_e": ranks_e,
                "err_w": rw.error,
                "err_e": re_.error,
            }
        )

    print(f"{'case':28} {'qtype':10} {'top1':6} {'facts(w/e)':12} first-fact ranks (w|e)")
    print("-" * 90)
    net = 0
    top1_changes = 0
    for r in rows:
        if not r["top1_same"]:
            top1_changes += 1
        net += r["found_w"] - r["found_e"]
        rk = " ".join(f"{fid}:{r['ranks_w'][fid]}|{r['ranks_e'][fid]}" for fid in r["ranks_w"])
        print(
            f"{r['id']:28} {r['qtype']:10} "
            f"{'same' if r['top1_same'] else 'DIFF':6} "
            f"{str(r['found_w']) + '/' + str(r['found_e']):12} {rk}"
        )
    print("-" * 90)
    print(
        f"net facts-found delta (weighted - equal): {net:+d} | "
        f"top1 changed: {top1_changes}/{len(rows)}"
    )
    ident = [r for r in rows if r["qtype"] == "identifier"]
    net_id = sum(r["found_w"] - r["found_e"] for r in ident)
    id_changes = sum(1 for r in ident if not r["top1_same"])
    print(
        f"identifier-only ({len(ident)} cases): net facts delta {net_id:+d} | "
        f"top1 changed {id_changes}/{len(ident)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
