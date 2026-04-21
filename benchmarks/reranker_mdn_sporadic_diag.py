"""Reranker `:8083` MDN sporadic 500 diagnostic.

Pre-registered follow-up to PR #38 (chunk-window cap). PR #36's D2
gate covered the *large* payload failure mode (40 k chars cap). This
diagnostic targets the *small* payload sporadic 500 still seen on
`claude_code_mdn_fetch_api` runs even after the cap landed.

Two phases:

1. ``--capture`` — runs `trawl.fetch_relevant()` once per fixture
   (MDN Fetch_API page + React `useEffect` page as a foreign canary)
   and intercepts the rerank POST inside `trawl.reranking.rerank()` to
   dump the exact `{"model", "query", "documents"}` payload to JSON.
   No `:8083` traffic is generated during capture (the interceptor
   returns a stubbed empty result so `fetch_relevant()` does not
   actually rerank). Run once; the dumps are reused across measurement
   runs so MDN is hit at most once per session.

2. default — replays the captured payloads against `:8083` through
   six pre-registered sweeps. Aggregates failure rate per sweep and
   per variant, then resolves a D0-D5 decision hint per the design
   doc's gate table.

Pre-registered decision gates (design doc
``docs/superpowers/specs/2026-04-21-reranker-mdn-sporadic-diag-design.md``):

    D0 — overall fail rate < 0.5%  → no follow-up needed right now.
    D1 — canary ≈ MDN failure rate → shared-tenant / slot collision.
    D2 — same payload >10% AND MDN-only → payload-specific tokenizer
                                          edge.
    D3 — gap-specific spike        → keep-alive drift.
    D4 — stripped variant <2x base → HTML entity / Unicode issue.
    D5 — scattered, no correlate   → non-deterministic edge.

Invoke:
    python benchmarks/reranker_mdn_sporadic_diag.py --capture
    python benchmarks/reranker_mdn_sporadic_diag.py
    python benchmarks/reranker_mdn_sporadic_diag.py --burst-sweep-size 30

Writes `benchmarks/results/reranker-mdn-sporadic-diag/`:
    _captures/<variant>.json   captured rerank payloads (--capture)
    <ts>/diag.json             per-request raw + aggregates + decision
    <ts>/diag.md               human-readable report

Exit code:
    0 — measurement (or capture) completed.
    2 — `:8083` unreachable (initial /health check failed).
    3 — capture phase could not produce a payload for one of the
        fixtures (MDN/React fetch failed, embedding server down, ...).
"""

from __future__ import annotations

import argparse
import contextlib
import html
import json
import os
import re
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

BENCH_DIR = Path(__file__).resolve().parent
# Repo src/ on sys.path so `from trawl import fetch_relevant` works
# without requiring a prior `pip install -e .`.
REPO_ROOT = BENCH_DIR.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

RESULTS_ROOT = BENCH_DIR / "results" / "reranker-mdn-sporadic-diag"
CAPTURE_DIR = RESULTS_ROOT / "_captures"

DEFAULT_URL = os.environ.get("TRAWL_RERANK_URL", "http://localhost:8083/v1")
DEFAULT_MODEL = os.environ.get("TRAWL_RERANK_MODEL", "bge-reranker-v2-m3")

# Capture fixtures: variant_name -> (url, query). MDN is the
# bottleneck; React useEffect docs is a foreign canary unrelated to
# MDN's shadow-DOM unwrap path.
CAPTURE_FIXTURES = {
    "mdn": (
        "https://developer.mozilla.org/en-US/docs/Web/API/Fetch_API/Using_Fetch",
        "send a POST request with a JSON body using fetch",
    ),
    "canary_react": (
        "https://react.dev/reference/react/useEffect",
        "useEffect cleanup function example",
    ),
}


@dataclass
class CallRecord:
    index: int
    sweep: str
    variant: str
    http_status: int | None
    elapsed_ms: int
    n_docs: int
    doc_char_total: int
    ok: bool
    ts_offset_ms: int = 0
    error: str | None = None


# ---- Capture phase ------------------------------------------------------


@contextlib.contextmanager
def _intercept_rerank_post():
    """Swap `trawl.reranking.httpx` for a fake module whose `Client`
    captures the POST payload and returns a stubbed empty result.

    Yields the ``captured`` dict; after the context exits, it holds
    ``{"url": ..., "json": ...}`` if `rerank()` ran during the block.
    Reverting the swap on exit keeps other modules using real httpx.
    """
    captured: dict[str, Any] = {}

    class _MockResponse:
        status_code = 200

        def raise_for_status(self) -> None: pass

        def json(self) -> dict[str, Any]:
            return {"results": []}

    class _CapturingClient:
        def __init__(self, *_a, **_kw): pass

        def __enter__(self): return self

        def __exit__(self, *_a): pass

        def post(self, _url: str, *, json: dict[str, Any] | None = None, **_kw):
            captured["url"] = _url
            captured["json"] = json
            return _MockResponse()

    import httpx as _httpx_real
    import trawl.reranking as rr

    class _FakeHttpx:
        Client = _CapturingClient
        HTTPError = _httpx_real.HTTPError

    saved = rr.httpx
    rr.httpx = _FakeHttpx
    try:
        yield captured
    finally:
        rr.httpx = saved


def _capture_payload(url: str, query: str, output_path: Path) -> None:
    """Run fetch_relevant once with the rerank POST intercepted; dump
    payload to ``output_path``. Raises if rerank() never ran during the
    fetch (e.g. fetch failed before retrieval, or page produced 0
    chunks)."""
    with _intercept_rerank_post() as captured:
        from trawl import fetch_relevant
        result = fetch_relevant(url, query)

    if not captured:
        raise RuntimeError(
            f"rerank not invoked for {url} — fetch_relevant returned "
            f"path={result.path}, error={result.error!r}, "
            f"n_chunks_total={result.n_chunks_total}"
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(
            {
                "url": url,
                "query": query,
                "rerank_url": captured["url"],
                "payload": captured["json"],
                "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _capture_main(_args: argparse.Namespace) -> int:
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    for name, (url, query) in CAPTURE_FIXTURES.items():
        out_path = CAPTURE_DIR / f"{name}.json"
        print(f"[capture] {name}: {url}", file=sys.stderr)
        try:
            _capture_payload(url, query, out_path)
        except Exception as e:
            print(f"  FAIL: {e}", file=sys.stderr)
            return 3
        meta = json.loads(out_path.read_text())
        n_docs = len(meta["payload"]["documents"])
        chars = sum(len(d) for d in meta["payload"]["documents"]) + len(
            meta["payload"]["query"]
        )
        print(
            f"  -> {out_path}  (n_docs={n_docs} char_total={chars})",
            file=sys.stderr,
        )
    return 0


# ---- Strip variant ------------------------------------------------------


def _strip_documents(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of payload with HTML-unescape + ASCII-only filter
    + whitespace normalisation applied to each document.

    Used to test H4 (HTML entity / Unicode is what the server tokenizer
    chokes on).
    """
    new_docs: list[str] = []
    for d in payload["documents"]:
        d2 = html.unescape(d)
        d2 = re.sub(r"[^\x00-\x7f]+", " ", d2)
        d2 = re.sub(r"\s+", " ", d2)
        new_docs.append(d2)
    return {**payload, "documents": new_docs}


# ---- Sweep execution ----------------------------------------------------


def _call(
    client: httpx.Client,
    base_url: str,
    payload: dict[str, Any],
    sweep: str,
    variant: str,
    index: int,
    *,
    t_session_start: float,
) -> CallRecord:
    n_docs = len(payload["documents"])
    char_total = sum(len(d) for d in payload["documents"]) + len(payload.get("query", ""))
    t0 = time.monotonic()
    ts_offset_ms = int((t0 - t_session_start) * 1000)
    try:
        r = client.post(f"{base_url}/rerank", json=payload)
        elapsed = int((time.monotonic() - t0) * 1000)
        if r.status_code == 200:
            return CallRecord(
                index=index, sweep=sweep, variant=variant,
                http_status=200, elapsed_ms=elapsed,
                n_docs=n_docs, doc_char_total=char_total,
                ok=True, ts_offset_ms=ts_offset_ms,
            )
        body_preview = (r.text or "")[:200]
        return CallRecord(
            index=index, sweep=sweep, variant=variant,
            http_status=r.status_code, elapsed_ms=elapsed,
            n_docs=n_docs, doc_char_total=char_total,
            ok=False, ts_offset_ms=ts_offset_ms,
            error=f"http {r.status_code}: {body_preview}",
        )
    except httpx.HTTPError as e:
        elapsed = int((time.monotonic() - t0) * 1000)
        return CallRecord(
            index=index, sweep=sweep, variant=variant,
            http_status=None, elapsed_ms=elapsed,
            n_docs=n_docs, doc_char_total=char_total,
            ok=False, ts_offset_ms=ts_offset_ms,
            error=f"{type(e).__name__}: {e}",
        )


def _run_sweep(
    client: httpx.Client,
    base_url: str,
    payload: dict[str, Any],
    n: int,
    gap_ms: int,
    sweep: str,
    variant: str,
    *,
    canary_payload: dict[str, Any] | None = None,
    canary_variant: str = "",
    canary_every: int = 0,
    verbose: bool,
    t_session_start: float,
) -> list[CallRecord]:
    calls: list[CallRecord] = []
    for i in range(n):
        if i > 0 and gap_ms > 0:
            time.sleep(gap_ms / 1000.0)
        # Foreign canary insertion: every `canary_every` requests
        # (skipping i=0) we slot a single canary call to compare.
        if (
            canary_payload is not None
            and canary_every > 0
            and i > 0
            and i % canary_every == 0
        ):
            cc = _call(
                client, base_url, canary_payload, sweep, canary_variant,
                index=-i, t_session_start=t_session_start,
            )
            calls.append(cc)
            if verbose:
                tag = "OK" if cc.ok else f"FAIL {cc.http_status or '---'}"
                print(
                    f"  [{sweep}] canary@{i}: {tag} {cc.elapsed_ms}ms",
                    file=sys.stderr,
                )
        c = _call(
            client, base_url, payload, sweep, variant,
            index=i, t_session_start=t_session_start,
        )
        calls.append(c)
        if verbose and (i == 0 or (i + 1) % 25 == 0 or not c.ok):
            tag = "OK " if c.ok else f"FAIL {c.http_status or '---'}"
            print(
                f"  [{sweep}] {i + 1}/{n} {tag} {c.elapsed_ms}ms",
                file=sys.stderr,
            )
    return calls


# ---- Aggregation --------------------------------------------------------


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


def _aggregate(calls: list[CallRecord]) -> dict[str, Any]:
    if not calls:
        return {"total": 0, "failed": 0, "failure_rate": 0.0}
    failed = [c for c in calls if not c.ok]
    elapsed_ok = [c.elapsed_ms for c in calls if c.ok]
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
        "failure_indices": [c.index for c in failed],
    }


def _decide_gate(
    per_sweep: dict[str, dict[str, Any]],
    per_variant: dict[str, dict[str, Any]],
) -> tuple[str, str]:
    """Pre-registered gate resolution. See design doc table."""
    total = sum(s["total"] for s in per_sweep.values())
    failed = sum(s["failed"] for s in per_sweep.values())
    overall_rate = failed / max(total, 1)

    if overall_rate < 0.005:
        return (
            "D0",
            f"overall failure rate {overall_rate:.2%} < 0.5% — diagnostic "
            f"point appears stable; reopen if MDN sporadic 500s recur.",
        )

    mdn_rate = per_variant.get("MDN", {}).get("failure_rate", 0.0)
    canary_rate = per_variant.get("canary_react", {}).get("failure_rate", 0.0)
    strip_rate = per_variant.get("MDN_stripped", {}).get("failure_rate")

    # D2: same payload >10% AND MDN-only.
    if mdn_rate > 0.10 and canary_rate < max(mdn_rate / 2, 0.05):
        return (
            "D2",
            f"MDN payload failure {mdn_rate:.0%} >> canary {canary_rate:.0%} — "
            f"payload-specific server tokenizer edge (H2). Next: bisect "
            f"document strings to locate the trigger substring.",
        )

    # D4: stripped < half of raw MDN AND raw MDN had measurable failures.
    if strip_rate is not None and mdn_rate > 0.02 and strip_rate < mdn_rate / 2:
        return (
            "D4",
            f"stripped variant {strip_rate:.0%} << MDN raw {mdn_rate:.0%} — "
            f"HTML entity / Unicode in payload triggers the server "
            f"tokenizer (H4). Next: revisit shadow-DOM unwrap escape or "
            f"add `html.unescape` in `_build_documents`.",
        )

    # D3: gap-sweep variation.
    gap_rates = {
        name: per_sweep[name]["failure_rate"]
        for name in ("gap_0", "gap_500", "gap_5000")
        if name in per_sweep
    }
    if gap_rates:
        max_name, max_rate = max(gap_rates.items(), key=lambda t: t[1])
        min_rate = min(gap_rates.values())
        if max_rate - min_rate >= 0.10 and max_rate >= 0.05:
            return (
                "D3",
                f"failure rate varies sharply across gap sweeps "
                f"({', '.join(f'{n}={r:.0%}' for n, r in gap_rates.items())}; "
                f"worst={max_name}) — keep-alive / TCP drift (H3). Next: "
                f"add per-call connection recycling or short-lived client "
                f"pool in `reranking.py`.",
            )

    # D1: canary ≈ MDN.
    if (
        canary_rate >= 0.02
        and abs(canary_rate - mdn_rate) <= max(mdn_rate * 0.30, 0.02)
    ):
        return (
            "D1",
            f"canary {canary_rate:.0%} ≈ MDN {mdn_rate:.0%} — failures span "
            f"variants, suggesting shared-tenant / slot collision on "
            f":8083 (H1). Next: investigate other consumers, consider "
            f"`TRAWL_RERANK_SLOT` pinning if confirmed.",
        )

    # D5: catch-all.
    return (
        "D5",
        f"failure rate {overall_rate:.1%} not concentrated in any "
        f"sweep/variant — possibly non-deterministic edge (H5). Next: "
        f"consider retry+jitter in `reranking.py` (separate spike).",
    )


# ---- Output -------------------------------------------------------------


def _render_md(summary: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(
        f"# Reranker MDN sporadic 500 diagnostic — {summary['generated_at']}"
    )
    lines.append("")
    lines.append(f"**URL:** `{summary['url']}`")
    lines.append(f"**Model:** `{summary['model']}`")
    lines.append("")
    cap = summary["captures"]
    lines.append("## Captures")
    lines.append("")
    lines.append("| variant | url | query |")
    lines.append("|---|---|---|")
    for name, meta in cap.items():
        lines.append(f"| `{name}` | `{meta['url']}` | `{meta['query']}` |")
    lines.append("")
    gate = summary["decision"]
    lines.append(f"## Decision: `{gate['code']}`")
    lines.append("")
    lines.append(gate["text"])
    lines.append("")
    lines.append("## Per sweep")
    lines.append("")
    lines.append(
        "| sweep | total | failed | rate | median_ms | p95_ms | status_dist |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---|")
    for name in ("repetition", "gap_0", "gap_500", "gap_5000", "strip", "canary"):
        s = summary["per_sweep"].get(name)
        if s is None:
            continue
        lines.append(
            f"| `{name}` | {s['total']} | {s['failed']} | "
            f"{s['failure_rate']:.1%} | {s.get('elapsed_ms_median')} | "
            f"{s.get('elapsed_ms_p95')} | {s.get('status_distribution')} |"
        )
    lines.append("")
    lines.append("## Per variant")
    lines.append("")
    lines.append("| variant | total | failed | rate |")
    lines.append("|---|---:|---:|---:|")
    for name, v in summary["per_variant"].items():
        lines.append(
            f"| `{name}` | {v['total']} | {v['failed']} | "
            f"{v['failure_rate']:.1%} |"
        )
    lines.append("")
    lines.append("## Health checks")
    lines.append("")
    lines.append("| checkpoint | ok | status |")
    lines.append("|---|---|---:|")
    for h in summary["health_checks"]:
        ok = "PASS" if h["ok"] else "FAIL"
        lines.append(f"| `{h['label']}` | {ok} | {h['status']} |")
    lines.append("")
    return "\n".join(lines)


def _check_health(url: str) -> tuple[bool, int | None]:
    health = url.rsplit("/v1", 1)[0] + "/health"
    try:
        r = httpx.get(health, timeout=3.0)
        return r.status_code == 200, r.status_code
    except Exception:
        return False, None


# ---- Main ---------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--capture",
        action="store_true",
        help="Capture rerank payloads for the fixtures and exit.",
    )
    parser.add_argument(
        "--burst-sweep-size", type=int, default=50,
        help="N for gap_0 / gap_500 / gap_5000 / strip / canary sweeps "
             "(default 50).",
    )
    parser.add_argument(
        "--repetition-size", type=int, default=200,
        help="N for the repetition baseline sweep (default 200).",
    )
    parser.add_argument(
        "--canary-every", type=int, default=20,
        help="Insert one MDN canary every N requests in the canary "
             "sweep (default 20).",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    if args.capture:
        return _capture_main(args)

    ok, status = _check_health(args.url)
    if not ok:
        print(f"initial health check failed: status={status}", file=sys.stderr)
        return 2
    print(f"initial health: OK ({status})", file=sys.stderr)

    mdn_path = CAPTURE_DIR / "mdn.json"
    canary_path = CAPTURE_DIR / "canary_react.json"
    if not mdn_path.exists() or not canary_path.exists():
        print(
            f"missing captures at {CAPTURE_DIR}. Run with --capture first.",
            file=sys.stderr,
        )
        return 3

    mdn_capture = json.loads(mdn_path.read_text(encoding="utf-8"))
    canary_capture = json.loads(canary_path.read_text(encoding="utf-8"))
    mdn_payload = mdn_capture["payload"]
    canary_payload = canary_capture["payload"]
    stripped_payload = _strip_documents(mdn_payload)

    print(
        f"MDN payload: n_docs={len(mdn_payload['documents'])} "
        f"char_total={sum(len(d) for d in mdn_payload['documents']) + len(mdn_payload['query'])}",
        file=sys.stderr,
    )
    print(
        f"canary payload: n_docs={len(canary_payload['documents'])} "
        f"char_total={sum(len(d) for d in canary_payload['documents']) + len(canary_payload['query'])}",
        file=sys.stderr,
    )

    ts = time.strftime("%Y-%m-%dT%H-%M-%SZ", time.gmtime())
    out_dir = RESULTS_ROOT / ts
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"diag -> {out_dir}", file=sys.stderr)

    health_checks: list[dict[str, Any]] = [
        {"label": "start", "ok": ok, "status": status}
    ]

    sweep_specs: list[tuple[str, dict[str, Any], str, int, int]] = [
        ("repetition", mdn_payload, "MDN", args.repetition_size, 100),
        ("gap_0", mdn_payload, "MDN", args.burst_sweep_size, 0),
        ("gap_500", mdn_payload, "MDN", args.burst_sweep_size, 500),
        ("gap_5000", mdn_payload, "MDN", args.burst_sweep_size, 5000),
        ("strip", stripped_payload, "MDN_stripped", args.burst_sweep_size, 100),
        ("canary", canary_payload, "canary_react", args.burst_sweep_size, 100),
    ]

    all_calls: list[CallRecord] = []
    per_sweep: dict[str, dict[str, Any]] = {}
    t_session_start = time.monotonic()

    # Single httpx.Client across all sweeps so connection-pool / keep-
    # alive state is shared. This makes gap-sweep variation meaningful;
    # `trawl.reranking.rerank()` itself uses a fresh client per call,
    # so an absence of gap-driven failures here cannot prove the
    # production code is immune (it just rules out keep-alive drift
    # under sustained reuse).
    with httpx.Client(timeout=30.0) as client:
        for name, payload, variant, n, gap in sweep_specs:
            print(
                f"\n[sweep {name}] n={n} gap={gap}ms variant={variant}",
                file=sys.stderr,
            )
            extra = {}
            if name == "canary":
                extra = dict(
                    canary_payload=mdn_payload,
                    canary_variant="MDN",
                    canary_every=args.canary_every,
                )
            calls = _run_sweep(
                client, args.url, payload, n, gap, name, variant,
                verbose=args.verbose,
                t_session_start=t_session_start,
                **extra,
            )
            all_calls.extend(calls)
            per_sweep[name] = _aggregate(calls)
            agg = per_sweep[name]
            print(
                f"  total={agg['total']} failed={agg['failed']} "
                f"rate={agg['failure_rate']:.1%} "
                f"status_dist={agg['status_distribution']}",
                file=sys.stderr,
            )
            ok2, status2 = _check_health(args.url)
            health_checks.append(
                {"label": f"after_{name}", "ok": ok2, "status": status2}
            )

    by_variant: dict[str, list[CallRecord]] = {}
    for c in all_calls:
        by_variant.setdefault(c.variant, []).append(c)
    per_variant = {name: _aggregate(calls) for name, calls in by_variant.items()}

    code, text = _decide_gate(per_sweep, per_variant)

    summary = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "url": args.url,
        "model": args.model,
        "captures": {
            "mdn": {"url": mdn_capture["url"], "query": mdn_capture["query"]},
            "canary_react": {
                "url": canary_capture["url"],
                "query": canary_capture["query"],
            },
        },
        "params": {
            "burst_sweep_size": args.burst_sweep_size,
            "repetition_size": args.repetition_size,
            "canary_every": args.canary_every,
        },
        "per_sweep": per_sweep,
        "per_variant": per_variant,
        "health_checks": health_checks,
        "decision": {"code": code, "text": text},
        "raw_calls": [asdict(c) for c in all_calls],
    }

    (out_dir / "diag.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (out_dir / "diag.md").write_text(_render_md(summary), encoding="utf-8")

    print(f"\ndecision: {code} — {text}", file=sys.stderr)
    print(f"report: {out_dir}/diag.md", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
