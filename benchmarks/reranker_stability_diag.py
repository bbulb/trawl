"""Reranker `:8083` stability diagnostic.

Two modes:

- ``direct`` (default) — bypasses ``trawl.reranking.rerank()`` and hits
  ``bge-reranker-v2-m3`` at the configured URL directly. Reproduces
  the server-side input-validator failure mode (D2 in the original
  diagnostic, 2026-04-20).

- ``--via-trawl`` — routes requests through ``trawl.reranking.rerank()``
  so any client-side defensive caps (``TRAWL_RERANK_MAX_DOCS`` /
  ``TRAWL_RERANK_MAX_CHARS``) are exercised. Used to validate the
  chunk-window cap follow-up spike. A failure is inferred from the
  ``reranker unavailable, falling back to cosine`` log record.

Pre-registered decision gates (design doc
``docs/superpowers/specs/2026-04-20-reranker-stability-diag-design.md``
and its follow-up
``docs/superpowers/specs/2026-04-20-reranking-chunk-window-cap-design.md``):

    D1 — failure rate < 1% → no follow-up needed right now.
    D2 — payload size correlates with failure → chunk-window cap
         spike.
    D3 — failures cluster in time → retry / back-off / health-check
         recovery spike.
    D4 — scattered failures → simple retry with jitter spike.

Invoke:
    python benchmarks/reranker_stability_diag.py
    python benchmarks/reranker_stability_diag.py --burst-size 100
    python benchmarks/reranker_stability_diag.py --url http://localhost:8083/v1
    python benchmarks/reranker_stability_diag.py --via-trawl

Writes `benchmarks/results/reranker-stability-diag/<ts>/`:
    diag.json   per-request raw data + aggregates + decision hint
    diag.md     human-readable report

Exit code:
    0  — measurement completed.
    2  — server unreachable (initial /health check failed).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

BENCH_DIR = Path(__file__).resolve().parent
# Add repo src/ to sys.path so `trawl.reranking` can be imported in
# `--via-trawl` mode without requiring a prior `pip install -e .`.
REPO_ROOT = BENCH_DIR.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

RESULTS_ROOT = BENCH_DIR / "results" / "reranker-stability-diag"

DEFAULT_URL = os.environ.get("TRAWL_RERANK_URL", "http://localhost:8083/v1")
DEFAULT_MODEL = os.environ.get("TRAWL_RERANK_MODEL", "bge-reranker-v2-m3")

# Canary: a small known-good payload fired between bursts to measure
# server state drift independently of the burst's own workload.
CANARY_QUERY = "what is a reverse proxy"
CANARY_DOCS = [
    "Title: Nginx\nSection: Reverse proxy\n\nnginx can be configured as a reverse proxy.",
    "Title: HAProxy\nSection: Load balancing\n\nHAProxy distributes traffic across upstream servers.",
    "Title: Apache\nSection: mod_proxy\n\nApache's mod_proxy implements reverse proxy support.",
]


@dataclass
class RerankCall:
    index: int
    burst: str
    http_status: int | None
    elapsed_ms: int
    n_docs: int
    doc_char_total: int
    ok: bool
    error: str | None = None


def _build_docs(n: int, length: int) -> list[str]:
    """Generate `n` synthetic documents of roughly `length` characters
    each. Varied filler text so the reranker can't trivially score them
    identically.
    """
    base_words = [
        "retrieval", "query", "document", "ranker", "model", "score",
        "transformer", "embedding", "context", "attention", "softmax",
        "cross", "encoder", "tokenize", "truncate", "batch", "latency",
    ]
    docs = []
    for i in range(n):
        w = base_words[i % len(base_words)]
        # repeat filler to reach approximate target length
        reps = max(1, length // (len(w) + 1))
        body = " ".join([w] * reps)[:length]
        docs.append(f"Doc {i} section {w}\n\n{body}")
    return docs


def _call_rerank(
    client: httpx.Client,
    base_url: str,
    model: str,
    query: str,
    docs: list[str],
    index: int,
    burst: str,
) -> RerankCall:
    payload = {"model": model, "query": query, "documents": docs}
    doc_char_total = sum(len(d) for d in docs) + len(query)
    t0 = time.monotonic()
    try:
        r = client.post(f"{base_url}/rerank", json=payload)
        elapsed = int((time.monotonic() - t0) * 1000)
        if r.status_code == 200:
            return RerankCall(
                index=index, burst=burst, http_status=200, elapsed_ms=elapsed,
                n_docs=len(docs), doc_char_total=doc_char_total, ok=True,
            )
        body_preview = r.text[:200] if r.text else ""
        return RerankCall(
            index=index, burst=burst, http_status=r.status_code,
            elapsed_ms=elapsed, n_docs=len(docs),
            doc_char_total=doc_char_total, ok=False,
            error=f"http {r.status_code}: {body_preview}",
        )
    except httpx.HTTPError as e:
        elapsed = int((time.monotonic() - t0) * 1000)
        return RerankCall(
            index=index, burst=burst, http_status=None,
            elapsed_ms=elapsed, n_docs=len(docs),
            doc_char_total=doc_char_total, ok=False,
            error=f"{type(e).__name__}: {e}",
        )


# ---- --via-trawl path ---------------------------------------------------
#
# Build synthetic ScoredChunk objects, route through trawl.reranking.rerank()
# and infer per-request outcome from the `trawl.reranking` logger. A
# "reranker unavailable, falling back to cosine" WARNING is treated as a
# failure (the pipeline's client-side view of a server error). A "reranker
# input capped" WARNING is informational (the new cap logic firing).


class _ViaTrawlContext:
    """Holds the logging capture handler + last-call state shared across
    a diagnostic run.

    The caller attaches this to the ``trawl.reranking`` logger for the
    duration of the run and consults ``last_fallback`` / ``cap_fires``
    after each ``rerank()`` call.
    """

    def __init__(self) -> None:
        self.last_fallback_msg: str | None = None
        self.cap_fires = 0

        logger = self

        class _Handler(logging.Handler):
            def emit(self_handler, record: logging.LogRecord) -> None:
                msg = record.getMessage()
                if "falling back to cosine" in msg:
                    logger.last_fallback_msg = msg
                elif "reranker input capped" in msg:
                    logger.cap_fires += 1

        self.handler = _Handler(level=logging.WARNING)

    def install(self) -> None:
        logging.getLogger("trawl.reranking").addHandler(self.handler)
        logging.getLogger("trawl.reranking").setLevel(logging.WARNING)

    def uninstall(self) -> None:
        logging.getLogger("trawl.reranking").removeHandler(self.handler)

    def reset_last(self) -> None:
        self.last_fallback_msg = None


def _build_synthetic_scored(docs: list[str]):
    """Turn the synthetic ``docs`` used in direct mode into a list of
    ``ScoredChunk`` with just enough fields populated for ``rerank()`` to
    assemble its reranker-input strings (``embed_text`` drives the body,
    ``heading`` drives the section prefix)."""
    from trawl.chunking import Chunk
    from trawl.retrieval import ScoredChunk

    scored = []
    for i, body in enumerate(docs):
        # Leave heading empty so rerank()'s prefix logic emits the body
        # as-is -- keeps the outbound payload byte-equivalent to the
        # direct-mode call within a small margin.
        chunk = Chunk(
            text=body,
            heading_path=[],
            char_count=len(body),
            chunk_index=i,
            embed_text=body,
        )
        scored.append(ScoredChunk(chunk=chunk, score=1.0 - i / max(1, len(docs))))
    return scored


def _call_rerank_via_trawl(
    ctx: _ViaTrawlContext,
    base_url: str,
    model: str,
    query: str,
    docs: list[str],
    index: int,
    burst: str,
    *,
    k: int = 10,
) -> RerankCall:
    from trawl import reranking as trawl_reranking

    doc_char_total = sum(len(d) for d in docs) + len(query)
    ctx.reset_last()

    t0 = time.monotonic()
    try:
        trawl_reranking.rerank(
            query,
            _build_synthetic_scored(docs),
            k=k,
            page_title="",
            base_url=base_url,
            model=model,
        )
        elapsed = int((time.monotonic() - t0) * 1000)
    except Exception as e:  # rerank() is supposed to catch its own errors
        elapsed = int((time.monotonic() - t0) * 1000)
        return RerankCall(
            index=index, burst=burst, http_status=None,
            elapsed_ms=elapsed, n_docs=len(docs),
            doc_char_total=doc_char_total, ok=False,
            error=f"{type(e).__name__}: {e}",
        )

    if ctx.last_fallback_msg:
        return RerankCall(
            index=index, burst=burst, http_status=None,
            elapsed_ms=elapsed, n_docs=len(docs),
            doc_char_total=doc_char_total, ok=False,
            error=f"fallback: {ctx.last_fallback_msg}",
        )
    return RerankCall(
        index=index, burst=burst, http_status=200, elapsed_ms=elapsed,
        n_docs=len(docs), doc_char_total=doc_char_total, ok=True,
    )


def _check_health(url: str) -> tuple[bool, int | None]:
    health = url.rsplit("/v1", 1)[0] + "/health"
    try:
        r = httpx.get(health, timeout=3.0)
        return r.status_code == 200, r.status_code
    except Exception:
        return False, None


def _run_burst(
    client: httpx.Client, base_url: str, model: str,
    name: str, n_requests: int, n_docs: int, doc_len: int,
    verbose: bool,
    *,
    via_trawl_ctx: _ViaTrawlContext | None = None,
) -> list[RerankCall]:
    docs = _build_docs(n_docs, doc_len)
    query = f"locate the optimal configuration for {name}"
    calls: list[RerankCall] = []
    for i in range(n_requests):
        if via_trawl_ctx is not None:
            c = _call_rerank_via_trawl(
                via_trawl_ctx, base_url, model, query, docs, i, name,
            )
        else:
            c = _call_rerank(client, base_url, model, query, docs, i, name)
        calls.append(c)
        if verbose and (i == 0 or (i + 1) % 10 == 0 or not c.ok):
            tag = "OK " if c.ok else f"FAIL {c.http_status or '---'}"
            print(
                f"  [{name}] req {i + 1}/{n_requests} {tag} "
                f"{c.elapsed_ms}ms docs={c.n_docs} chars={c.doc_char_total}",
                file=sys.stderr,
            )
    return calls


def _aggregate(calls: list[RerankCall]) -> dict[str, Any]:
    if not calls:
        return {"total": 0, "failed": 0, "failure_rate": 0.0}
    failed = [c for c in calls if not c.ok]
    elapsed_ok = [c.elapsed_ms for c in calls if c.ok]
    failure_timing = [c.index for c in failed]
    status_dist: dict[str, int] = {}
    for c in calls:
        key = str(c.http_status) if c.http_status is not None else "exc"
        status_dist[key] = status_dist.get(key, 0) + 1
    return {
        "total": len(calls),
        "failed": len(failed),
        "failure_rate": len(failed) / len(calls),
        "status_distribution": status_dist,
        "elapsed_ms_median": statistics.median(elapsed_ok) if elapsed_ok else None,
        "elapsed_ms_p95": _pct(elapsed_ok, 0.95) if elapsed_ok else None,
        "failure_indices": failure_timing,
    }


def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    vs = sorted(values)
    idx = (len(vs) - 1) * p
    lo = int(idx)
    hi = min(lo + 1, len(vs) - 1)
    if lo == hi:
        return float(vs[lo])
    frac = idx - lo
    return float(vs[lo] * (1 - frac) + vs[hi] * frac)


def _decide_gate(overall_rate: float, per_burst: dict[str, Any]) -> tuple[str, str]:
    if overall_rate < 0.01:
        return (
            "D1",
            f"overall failure rate {overall_rate:.1%} < 1% — no follow-up spike needed right now",
        )
    # D2: payload correlation. Compare failure rate across bursts of
    # increasing payload.
    rates = []
    for name in ("small", "medium", "large"):
        b = per_burst.get(name)
        if b is None:
            continue
        rates.append((name, b.get("failure_rate", 0.0)))
    payload_correlated = False
    if len(rates) >= 2:
        # If max - min >= 0.10 AND the largest rate is the largest payload → likely correlated.
        max_name, max_rate = max(rates, key=lambda t: t[1])
        min_name, min_rate = min(rates, key=lambda t: t[1])
        if max_rate - min_rate >= 0.10 and max_name == "large":
            payload_correlated = True
    if payload_correlated:
        return (
            "D2",
            f"failure rate climbs with payload size ({', '.join(f'{n}={r:.0%}' for n, r in rates)}) — chunk-window cap spike",
        )
    # D3: temporal clustering — check if failures are contiguous within any burst.
    clustered = False
    for name, b in per_burst.items():
        indices = b.get("failure_indices") or []
        if len(indices) >= 3:
            # Contiguous if max gap between consecutive indices < 3
            gaps = [indices[i + 1] - indices[i] for i in range(len(indices) - 1)]
            if gaps and max(gaps) < 3:
                clustered = True
                break
    if clustered:
        return (
            "D3",
            "failures cluster in time (contiguous index blocks) — retry/back-off/health-check recovery spike",
        )
    # D4: scattered
    return (
        "D4",
        "failures scattered without clear payload or time correlation — simple retry with jitter spike",
    )


def _render_md(summary: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"# Reranker `:8083` stability diagnostic — {summary['generated_at']}")
    lines.append("")
    lines.append(f"**URL:** `{summary['url']}`")
    lines.append(f"**Model:** `{summary['model']}`")
    lines.append(f"**Mode:** `{summary.get('mode', 'direct')}`")
    lines.append(f"**Burst size:** {summary['burst_size']}")
    cap = summary.get("cap_telemetry")
    if cap is not None:
        lines.append(
            f"**Cap telemetry:** fires={cap['cap_fires']} "
            f"MAX_DOCS={cap['max_docs_env']} MAX_CHARS={cap['max_chars_env']}"
        )
    lines.append("")
    gate = summary["decision"]
    lines.append(f"## Decision hint: `{gate['code']}`")
    lines.append("")
    lines.append(f"{gate['text']}")
    lines.append("")
    lines.append("## Overall")
    lines.append("")
    o = summary["overall"]
    lines.append(f"- total requests: {o['total']}")
    lines.append(f"- failures: {o['failed']}")
    lines.append(f"- failure rate: {o['failure_rate']:.1%}")
    lines.append(f"- status distribution: {o['status_distribution']}")
    lines.append(f"- elapsed_ms median (ok): {o.get('elapsed_ms_median')}")
    lines.append(f"- elapsed_ms p95 (ok): {o.get('elapsed_ms_p95')}")
    lines.append("")
    lines.append("## Per burst")
    lines.append("")
    lines.append("| burst | total | failed | rate | median_ms | p95_ms | n_docs | doc_len |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for name in ("small", "medium", "large"):
        b = summary["per_burst"].get(name)
        if b is None:
            continue
        cfg = summary["burst_configs"].get(name, {})
        lines.append(
            f"| `{name}` | {b['total']} | {b['failed']} | {b['failure_rate']:.1%} | "
            f"{b.get('elapsed_ms_median')} | {b.get('elapsed_ms_p95')} | "
            f"{cfg.get('n_docs')} | {cfg.get('doc_len')} |"
        )
    lines.append("")
    lines.append("## Canary timeline")
    lines.append("")
    lines.append("| slot | ok | elapsed | status |")
    lines.append("|---|---|---:|---|")
    for i, c in enumerate(summary["canaries"]):
        ok = "PASS" if c.get("ok") else "FAIL"
        lines.append(f"| {i} | {ok} | {c.get('elapsed_ms')}ms | {c.get('http_status')} |")
    lines.append("")
    lines.append("## Health checks")
    lines.append("")
    lines.append("| checkpoint | ok | status |")
    lines.append("|---|---|---:|")
    for h in summary["health_checks"]:
        lines.append(f"| `{h['label']}` | {'PASS' if h['ok'] else 'FAIL'} | {h['status']} |")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--burst-size", type=int, default=50)
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument(
        "--via-trawl",
        action="store_true",
        help=(
            "Route each request through trawl.reranking.rerank() so the "
            "client-side caps (TRAWL_RERANK_MAX_DOCS / _MAX_CHARS) are "
            "exercised. Failures are inferred from the 'falling back to "
            "cosine' WARNING emitted by rerank()."
        ),
    )
    args = parser.parse_args(argv)

    ok, status = _check_health(args.url)
    if not ok:
        print(f"initial health check failed: status={status}", file=sys.stderr)
        return 2
    print(f"initial health: OK ({status})", file=sys.stderr)

    ts = time.strftime("%Y-%m-%dT%H-%M-%SZ", time.gmtime())
    out_dir = RESULTS_ROOT / ts
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"diag → {out_dir}", file=sys.stderr)

    burst_configs = {
        "small": {"n_docs": 5, "doc_len": 100},
        "medium": {"n_docs": 20, "doc_len": 500},
        "large": {"n_docs": 50, "doc_len": 2000},
    }

    all_calls: list[RerankCall] = []
    per_burst: dict[str, Any] = {}
    canaries: list[dict[str, Any]] = []
    health_checks: list[dict[str, Any]] = [{"label": "start", "ok": ok, "status": status}]

    via_trawl_ctx: _ViaTrawlContext | None = None
    if args.via_trawl:
        via_trawl_ctx = _ViaTrawlContext()
        via_trawl_ctx.install()
        print(
            f"[via-trawl] using trawl.reranking.rerank() "
            f"TRAWL_RERANK_MAX_DOCS={os.environ.get('TRAWL_RERANK_MAX_DOCS', '(default)')} "
            f"TRAWL_RERANK_MAX_CHARS={os.environ.get('TRAWL_RERANK_MAX_CHARS', '(default)')}",
            file=sys.stderr,
        )

    try:
        with httpx.Client(timeout=30.0) as client:
            # Warmup: 3 canary requests. Always run direct-mode canary so a
            # broken rerank() path does not mask server health.
            print("\n[warmup]", file=sys.stderr)
            for i in range(3):
                c = _call_rerank(client, args.url, args.model, CANARY_QUERY, CANARY_DOCS, i, "warmup")
                if args.verbose:
                    tag = "OK" if c.ok else "FAIL"
                    print(f"  warmup {i} {tag} {c.elapsed_ms}ms", file=sys.stderr)

            for name, cfg in burst_configs.items():
                print(f"\n[burst {name}] {args.burst_size} × {cfg['n_docs']} docs × {cfg['doc_len']} chars", file=sys.stderr)
                calls = _run_burst(
                    client, args.url, args.model, name,
                    args.burst_size, cfg["n_docs"], cfg["doc_len"],
                    args.verbose,
                    via_trawl_ctx=via_trawl_ctx,
                )
                all_calls.extend(calls)
                per_burst[name] = _aggregate(calls)

                # Canary after each burst - always direct-mode to probe
                # server state independently of the via_trawl path.
                cn = _call_rerank(client, args.url, args.model, CANARY_QUERY, CANARY_DOCS, 0, f"canary_{name}")
                canaries.append(asdict(cn))
                ok2, status2 = _check_health(args.url)
                health_checks.append({"label": f"after_{name}", "ok": ok2, "status": status2})
    finally:
        if via_trawl_ctx is not None:
            via_trawl_ctx.uninstall()

    overall = _aggregate(all_calls)
    decision_code, decision_text = _decide_gate(overall["failure_rate"], per_burst)

    summary = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "url": args.url,
        "model": args.model,
        "mode": "via_trawl" if args.via_trawl else "direct",
        "burst_size": args.burst_size,
        "burst_configs": burst_configs,
        "overall": overall,
        "per_burst": per_burst,
        "canaries": canaries,
        "health_checks": health_checks,
        "decision": {"code": decision_code, "text": decision_text},
        "raw_calls": [asdict(c) for c in all_calls],
    }
    if via_trawl_ctx is not None:
        summary["cap_telemetry"] = {
            "cap_fires": via_trawl_ctx.cap_fires,
            "max_docs_env": os.environ.get("TRAWL_RERANK_MAX_DOCS", "(default)"),
            "max_chars_env": os.environ.get("TRAWL_RERANK_MAX_CHARS", "(default)"),
        }
    (out_dir / "diag.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (out_dir / "diag.md").write_text(_render_md(summary), encoding="utf-8")

    print(f"\ndecision: {decision_code} — {decision_text}", file=sys.stderr)
    print(f"report: {out_dir}/diag.md", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
