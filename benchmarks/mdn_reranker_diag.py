"""MDN reranker diagnostic — where does the keyword-bearing chunk rank?

One-shot diagnostic for the `claude_code_mdn_fetch_api` pattern. Runs
the same URL + query three times (raw / reranked / reranked+HyDE) with
k=30 so we can see the rank distribution of chunks that contain the
assertion keywords (`JSON.stringify`, `Content-Type`, `method:`).

The HyDE spike (PR #32) concluded that the MDN failure is reranker-
mediated. This script measures that directly:

    - Mode `raw`      — `use_rerank=False` → hybrid retrieval (dense +
                         BM25) with RRF fusion, reported in fused
                         dense-cosine order. No reranker.
    - Mode `reranked` — `use_rerank=True`  → same retrieval + the
                         `bge-reranker-v2-m3` pass that reorders the
                         top-k by relevance_score.
    - Mode `with_hyde` — `use_hyde=True, use_rerank=True` → HyDE
                         augmentation on the dense side + reranker.

For each mode it writes a Markdown table with rank / heading / 60-char
preview / score / keyword flags, plus a per-keyword "where does it
appear in the ranking" summary and a pre-registered `decision_hint`
(D1–D4 per the design doc).

Invoke:
    python benchmarks/mdn_reranker_diag.py
    python benchmarks/mdn_reranker_diag.py --k 50
    python benchmarks/mdn_reranker_diag.py --url <override>

Writes `benchmarks/results/mdn-reranker-diag/<ts>/`:
    diag.md    human-readable report
    diag.json  programmatic access

Exit code:
    0  — measurement completed
    2  — infra failure (any of :8081/:8082/:8083 unreachable or first
         fetch errors)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

BENCH_DIR = Path(__file__).resolve().parent
REPO_ROOT = BENCH_DIR.parent
RESULTS_ROOT = BENCH_DIR / "results" / "mdn-reranker-diag"

DEFAULT_URL = "https://developer.mozilla.org/en-US/docs/Web/API/Fetch_API/Using_Fetch"
DEFAULT_QUERY = "send a POST request with a JSON body using fetch"
KEYWORDS = ["JSON.stringify", "Content-Type", "method:"]


def _chunk_sig(chunk: dict) -> str:
    body = (chunk.get("text") or "")[:80].strip()
    heading = chunk.get("heading") or ""
    return hashlib.sha1(f"{heading}||{body}".encode()).hexdigest()[:12]


def _preview(text: str, n: int = 60) -> str:
    line = " ".join((text or "").split())
    return line[:n] + ("…" if len(line) > n else "")


def _keyword_flags(chunk: dict) -> dict[str, bool]:
    body = ((chunk.get("heading") or "") + "\n" + (chunk.get("text") or ""))
    return {kw: (kw in body) for kw in KEYWORDS}


def _contains_any(flags: dict[str, bool]) -> bool:
    return any(flags.values())


def _run_mode(mode: str, url: str, query: str, k: int, verbose: bool) -> dict[str, Any]:
    """Run fetch_relevant with the mode's flag set, return a structured
    summary including a per-chunk table.
    """
    from trawl import fetch_relevant, to_dict

    use_hyde = mode == "with_hyde"
    use_rerank = mode != "raw"

    t0 = time.monotonic()
    # Ensure hybrid retrieval is on for all modes so BM25 signal feeds the
    # fused candidate list. (The default is already hybrid off in
    # production — we force it on here because the HyDE spike baseline
    # used hybrid_on.)
    os.environ["TRAWL_HYBRID_RETRIEVAL"] = "1"
    os.environ["TRAWL_HYBRID_RRF_K"] = "60"
    os.environ.pop("TRAWL_BM25_EXTRAS", None)  # reverted after PR #32

    result = fetch_relevant(url, query, k=k, use_hyde=use_hyde, use_rerank=use_rerank)
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    data = to_dict(result)
    chunks = data.get("chunks") or []

    rows = []
    for idx, c in enumerate(chunks):
        flags = _keyword_flags(c)
        rows.append({
            "rank": idx + 1,
            "sig": _chunk_sig(c),
            "heading": (c.get("heading") or "").strip()[:48],
            "preview": _preview(c.get("text") or "", 60),
            "score": c.get("score"),
            "keyword_flags": flags,
            "any_keyword": _contains_any(flags),
        })

    keyword_ranks = {kw: [r["rank"] for r in rows if r["keyword_flags"].get(kw)] for kw in KEYWORDS}
    any_keyword_ranks = [r["rank"] for r in rows if r["any_keyword"]]

    if verbose:
        print(f"[{mode}] elapsed={elapsed_ms}ms, chunks={len(chunks)}, "
              f"keyword-bearing ranks={any_keyword_ranks[:10]}", file=sys.stderr)

    return {
        "mode": mode,
        "use_hyde": use_hyde,
        "use_rerank": use_rerank,
        "elapsed_ms": elapsed_ms,
        "error": data.get("error"),
        "hyde_text": data.get("hyde_text") or "",
        "page_title": data.get("page_title") or "",
        "fetcher_used": data.get("fetcher_used"),
        "rerank_ms": data.get("rerank_ms"),
        "n_chunks_total": data.get("n_chunks_total"),
        "n_chunks_returned": len(chunks),
        "rows": rows,
        "keyword_ranks": keyword_ranks,
        "any_keyword_ranks": any_keyword_ranks,
    }


def _decision_hint(modes: dict[str, Any]) -> tuple[str, str]:
    """Pick D1/D2/D3/D4 per the design doc.

    Returns (code, human_readable).
    """
    raw = modes["raw"]
    rer = modes["reranked"]
    hyd = modes["with_hyde"]

    raw_in_5 = any(r <= 5 for r in raw["any_keyword_ranks"])
    rer_in_5 = any(r <= 5 for r in rer["any_keyword_ranks"])
    hyd_in_5 = any(r <= 5 for r in hyd["any_keyword_ranks"])

    if raw_in_5 and rer_in_5:
        return "D3", "keyword chunk is in both raw top-5 and reranked top-5 — assertion/top-k join worth re-checking"
    if raw_in_5 and not rer_in_5:
        return "D2", "reranker demotes the keyword chunk out of top-5 — category-conditional rerank bypass candidate"
    if (not raw_in_5) and (not rer_in_5) and hyd_in_5:
        return "D4", "HyDE lifts keyword chunk into top-5 under rerank — HyDE default-on for code queries + possibly wider k"
    return "D1", "keyword chunk is outside raw top-5 — retrieval-side problem, not reranker"


def _render_md(run: dict[str, Any], limit: int = 30) -> str:
    rows = run["rows"][:limit]
    lines: list[str] = []
    lines.append(f"### `{run['mode']}` — {run['n_chunks_returned']} chunks returned, {run['elapsed_ms']} ms")
    lines.append("")
    if run["mode"] == "with_hyde" and run["hyde_text"]:
        lines.append("**HyDE output:**")
        lines.append("")
        lines.append("```")
        lines.append(run["hyde_text"])
        lines.append("```")
        lines.append("")
    lines.append("| rank | heading | preview | score | JSON.stringify | Content-Type | method: |")
    lines.append("|---:|---|---|---:|:---:|:---:|:---:|")
    for r in rows:
        flags = r["keyword_flags"]
        score = r["score"]
        score_s = f"{score:.3f}" if isinstance(score, (int, float)) else "-"
        js = "✔" if flags.get("JSON.stringify") else ""
        ct = "✔" if flags.get("Content-Type") else ""
        me = "✔" if flags.get("method:") else ""
        heading = r["heading"].replace("|", "\\|")
        preview = r["preview"].replace("|", "\\|")
        lines.append(f"| {r['rank']} | {heading} | {preview} | {score_s} | {js} | {ct} | {me} |")
    lines.append("")
    lines.append(f"**Keyword-bearing ranks** (any): {run['any_keyword_ranks']}")
    lines.append("")
    per_kw = ", ".join(f"`{kw}`→{run['keyword_ranks'][kw]}" for kw in KEYWORDS)
    lines.append(f"**Per keyword**: {per_kw}")
    lines.append("")
    return "\n".join(lines)


def _precheck_servers() -> bool:
    import httpx

    endpoints = [
        ("embed", os.environ.get("TRAWL_EMBED_URL", "http://localhost:8081/v1")),
        ("hyde", os.environ.get("TRAWL_HYDE_URL", "http://localhost:8082/v1")),
        ("rerank", os.environ.get("TRAWL_RERANK_URL", "http://localhost:8083/v1")),
    ]
    for name, base in endpoints:
        health = base.rsplit("/v1", 1)[0] + "/health"
        try:
            httpx.get(health, timeout=3.0).raise_for_status()
        except Exception as e:  # noqa: BLE001
            print(f"{name} server health check failed ({health}): {e}", file=sys.stderr)
            return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--query", default=DEFAULT_QUERY)
    parser.add_argument("--k", type=int, default=30, help="retrieval k for diagnostic (default 30)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    if not _precheck_servers():
        return 2

    ts = time.strftime("%Y-%m-%dT%H-%M-%SZ", time.gmtime())
    out_dir = RESULTS_ROOT / ts
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"diag → {out_dir}", file=sys.stderr)

    modes: dict[str, Any] = {}
    for mode in ("raw", "reranked", "with_hyde"):
        print(f"\n[mode {mode}]", file=sys.stderr)
        modes[mode] = _run_mode(mode, args.url, args.query, args.k, args.verbose)

    # First-fetch sanity: if any mode errored AND we can't recover via cache,
    # bail with exit code 2.
    if modes["raw"].get("error"):
        print(f"raw mode failed: {modes['raw']['error']}", file=sys.stderr)
        return 2

    decision, decision_text = _decision_hint(modes)

    generated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    summary = {
        "generated_at": generated_at,
        "url": args.url,
        "query": args.query,
        "k": args.k,
        "keywords": KEYWORDS,
        "decision_hint": decision,
        "decision_text": decision_text,
        "modes": modes,
    }
    (out_dir / "diag.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # Markdown report
    lines: list[str] = []
    lines.append(f"# MDN reranker diagnostic — {generated_at}")
    lines.append("")
    lines.append(f"**URL:** {args.url}")
    lines.append(f"**Query:** `{args.query}`")
    lines.append(f"**k:** {args.k}")
    lines.append(f"**Keywords:** {KEYWORDS}")
    lines.append("")
    lines.append("## Decision hint")
    lines.append("")
    lines.append(f"- **`{decision}`** — {decision_text}")
    lines.append("")
    lines.append("## Keyword rank summary")
    lines.append("")
    lines.append("| mode | any-keyword ranks | JSON.stringify | Content-Type | method: | n_chunks_total |")
    lines.append("|---|---|---|---|---|---:|")
    for mode in ("raw", "reranked", "with_hyde"):
        m = modes[mode]
        lines.append(
            f"| `{mode}` | {m['any_keyword_ranks']} | "
            f"{m['keyword_ranks']['JSON.stringify']} | "
            f"{m['keyword_ranks']['Content-Type']} | "
            f"{m['keyword_ranks']['method:']} | "
            f"{m['n_chunks_total']} |"
        )
    lines.append("")
    lines.append("## Per-mode details")
    lines.append("")
    for mode in ("raw", "reranked", "with_hyde"):
        lines.append(_render_md(modes[mode]))

    (out_dir / "diag.md").write_text("\n".join(lines), encoding="utf-8")

    print(f"\ndecision: {decision} — {decision_text}", file=sys.stderr)
    print(f"report: {out_dir}/diag.md", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
