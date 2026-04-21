"""CJK per-document char cap validation.

Pre-registered follow-up to PR #43 (per-document char cap, shipped in
v0.4.3). PR #43's risk section flagged that pure CJK text tokenises
denser (~1-2 chars/token) than English code-heavy content (3.0-3.5
chars/token) used to bracket the default cap. A 1500-char CJK doc
could reach ~1500 tokens — well above the 512-token per-doc batch
limit on `bge-reranker-v2-m3`.

This script validates whether the current 1500 default survives a
Korean- / Japanese-heavy payload by:

1. ``--capture`` — runs `trawl.fetch_relevant()` once per CJK fixture
   (Korean Wikipedia 이순신 + Japanese Wikipedia 寿司) and intercepts
   the rerank POST inside `trawl.reranking.rerank()` to dump the
   exact ``{"model", "query", "documents"}`` payload. With the
   v0.4.3 default cap active, captured docs are already clamped to
   ≤ 1500 chars — precisely what we want to test.

2. default — replays each captured payload against `:8083` N times
   (default 200) and computes per-fixture failure rate. Extracts
   server-reported token counts from any 500 error bodies via the
   ``input \\((\\d+) tokens\\)`` pattern.

Pre-registered decision gates (design doc
``docs/superpowers/specs/2026-04-21-cjk-per-doc-cap-validation-design.md``):

    D-VALIDATE   — both fixtures < 0.5% failure.
                   Action: update PR #43 risk section to "validated".
    D-REPRODUCE  — any fixture >= 5% failure.
                   Action: file follow-up spike to lower default.
    D-INCONCLUSIVE — any fixture 0.5%-5% failure.
                   Action: expand N to 500 and re-run.

Invoke:
    mamba run -n trawl python benchmarks/cjk_per_doc_cap_validation.py --capture
    mamba run -n trawl python benchmarks/cjk_per_doc_cap_validation.py
    mamba run -n trawl python benchmarks/cjk_per_doc_cap_validation.py --n 500

Writes `benchmarks/results/cjk-per-doc-cap-validation/`:
    _captures/<fixture>.json   captured rerank payloads (--capture)
    <ts>/report.json           raw calls + aggregates + decision
    <ts>/report.md             human-readable report

Exit codes:
    0 — measurement or capture completed.
    2 — `:8083` unreachable (initial /health check failed).
    3 — capture phase could not produce a payload for a fixture.
"""

from __future__ import annotations

import argparse
import contextlib
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
REPO_ROOT = BENCH_DIR.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

RESULTS_ROOT = BENCH_DIR / "results" / "cjk-per-doc-cap-validation"
CAPTURE_DIR = RESULTS_ROOT / "_captures"

DEFAULT_URL = os.environ.get("TRAWL_RERANK_URL", "http://localhost:8083/v1")
DEFAULT_MODEL = os.environ.get("TRAWL_RERANK_MODEL", "bge-reranker-v2-m3")

CAPTURE_FIXTURES = {
    "ko_wiki_yi_sunsin": (
        "https://ko.wikipedia.org/wiki/%EC%9D%B4%EC%88%9C%EC%8B%A0",
        "이순신 직업 생년월일 주요 업적",
    ),
    "ja_wiki_sushi": (
        "https://ja.wikipedia.org/wiki/%E5%AF%BF%E5%8F%B8",
        "寿司の歴史と種類",
    ),
}

TOKEN_COUNT_RE = re.compile(r"input \((\d+) tokens\)")


@dataclass
class CallRecord:
    index: int
    fixture: str
    http_status: int | None
    elapsed_ms: int
    n_docs: int
    longest_doc_chars: int
    reported_token_count: int | None = None
    ok: bool = True
    error: str | None = None


# ---- Capture phase ------------------------------------------------------


@contextlib.contextmanager
def _intercept_rerank_post():
    """Swap `trawl.reranking.httpx` so `rerank()` captures the POST
    payload without touching `:8083`.
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
        docs = meta["payload"]["documents"]
        n_docs = len(docs)
        longest = max((len(d) for d in docs), default=0)
        char_total = sum(len(d) for d in docs) + len(meta["payload"]["query"])
        print(
            f"  -> {out_path}  (n_docs={n_docs} longest_doc_chars={longest} "
            f"char_total={char_total})",
            file=sys.stderr,
        )
    return 0


# ---- Replay phase -------------------------------------------------------


def _call(
    client: httpx.Client,
    base_url: str,
    payload: dict[str, Any],
    fixture: str,
    index: int,
    longest_doc_chars: int,
) -> CallRecord:
    n_docs = len(payload["documents"])
    t0 = time.monotonic()
    try:
        r = client.post(f"{base_url}/rerank", json=payload)
        elapsed = int((time.monotonic() - t0) * 1000)
        if r.status_code == 200:
            return CallRecord(
                index=index, fixture=fixture, http_status=200,
                elapsed_ms=elapsed, n_docs=n_docs,
                longest_doc_chars=longest_doc_chars, ok=True,
            )
        body_preview = (r.text or "")[:300]
        tok_match = TOKEN_COUNT_RE.search(body_preview)
        reported = int(tok_match.group(1)) if tok_match else None
        return CallRecord(
            index=index, fixture=fixture, http_status=r.status_code,
            elapsed_ms=elapsed, n_docs=n_docs,
            longest_doc_chars=longest_doc_chars,
            reported_token_count=reported, ok=False,
            error=f"http {r.status_code}: {body_preview}",
        )
    except httpx.HTTPError as e:
        elapsed = int((time.monotonic() - t0) * 1000)
        return CallRecord(
            index=index, fixture=fixture, http_status=None,
            elapsed_ms=elapsed, n_docs=n_docs,
            longest_doc_chars=longest_doc_chars, ok=False,
            error=f"{type(e).__name__}: {e}",
        )


def _run_fixture(
    client: httpx.Client,
    base_url: str,
    fixture: str,
    payload: dict[str, Any],
    n: int,
    gap_ms: int,
    verbose: bool,
) -> list[CallRecord]:
    longest = max((len(d) for d in payload["documents"]), default=0)
    calls: list[CallRecord] = []
    for i in range(n):
        if i > 0 and gap_ms > 0:
            time.sleep(gap_ms / 1000.0)
        c = _call(client, base_url, payload, fixture, i, longest)
        calls.append(c)
        if verbose and (i == 0 or (i + 1) % 25 == 0 or not c.ok):
            tag = "OK " if c.ok else f"FAIL {c.http_status or '---'}"
            tok = f" tok={c.reported_token_count}" if c.reported_token_count else ""
            print(
                f"  [{fixture}] {i + 1}/{n} {tag} {c.elapsed_ms}ms{tok}",
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
    observed_token_counts = sorted(
        c.reported_token_count for c in failed if c.reported_token_count is not None
    )
    return {
        "total": len(calls),
        "failed": len(failed),
        "failure_rate": len(failed) / len(calls),
        "status_distribution": status_dist,
        "elapsed_ms_median": statistics.median(elapsed_ok) if elapsed_ok else None,
        "elapsed_ms_p95": _pct(elapsed_ok, 0.95) if elapsed_ok else None,
        "longest_doc_chars": calls[0].longest_doc_chars,
        "n_docs_in_payload": calls[0].n_docs,
        "observed_token_counts": observed_token_counts,
    }


def _decide_gate(per_fixture: dict[str, dict[str, Any]]) -> tuple[str, str]:
    rates = {name: agg["failure_rate"] for name, agg in per_fixture.items()}
    if not rates:
        return ("D-INCONCLUSIVE", "no fixtures measured")

    worst_name, worst_rate = max(rates.items(), key=lambda t: t[1])
    best_rate = min(rates.values())

    if worst_rate >= 0.05:
        return (
            "D-REPRODUCE",
            f"fixture `{worst_name}` failure rate {worst_rate:.1%} >= 5% — "
            f"CJK payload reproduces the per-doc 512-token batch limit "
            f"failure even at cap=1500. File follow-up spike to lower "
            f"default (target candidate: 1000 chars). Observed token "
            f"counts: {per_fixture[worst_name].get('observed_token_counts')}.",
        )

    if worst_rate < 0.005:
        return (
            "D-VALIDATE",
            f"both fixtures < 0.5% failure (worst `{worst_name}` "
            f"{worst_rate:.2%}, best {best_rate:.2%}) — PR #43's cap=1500 "
            f"default survives Korean + Japanese payloads. Update risk "
            f"section to 'validated against CJK, no trigger.'",
        )

    return (
        "D-INCONCLUSIVE",
        f"worst fixture `{worst_name}` at {worst_rate:.1%} (between 0.5% "
        f"and 5%). Expand N to 500 and re-run; if still inconclusive, "
        f"document as 'rare edge, monitor via rerank_capped.'",
    )


# ---- Output -------------------------------------------------------------


def _render_md(summary: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"# CJK per-doc cap validation — {summary['generated_at']}")
    lines.append("")
    lines.append(f"**Rerank URL:** `{summary['url']}`")
    lines.append(f"**Model:** `{summary['model']}`")
    lines.append(f"**N per fixture:** {summary['params']['n']}")
    lines.append(f"**Cap active:** `TRAWL_RERANK_MAX_PER_DOC_CHARS="
                 f"{summary['env']['TRAWL_RERANK_MAX_PER_DOC_CHARS']}`")
    lines.append("")
    lines.append("## Captures")
    lines.append("")
    lines.append("| fixture | url | query | n_docs | longest_doc_chars |")
    lines.append("|---|---|---|---:|---:|")
    for name, meta in summary["captures"].items():
        lines.append(
            f"| `{name}` | `{meta['url']}` | `{meta['query']}` | "
            f"{meta['n_docs']} | {meta['longest_doc_chars']} |"
        )
    lines.append("")
    gate = summary["decision"]
    lines.append(f"## Decision: `{gate['code']}`")
    lines.append("")
    lines.append(gate["text"])
    lines.append("")
    lines.append("## Per fixture")
    lines.append("")
    lines.append(
        "| fixture | total | failed | rate | median_ms | p95_ms | status_dist | tok_counts |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---|---|")
    for name, v in summary["per_fixture"].items():
        lines.append(
            f"| `{name}` | {v['total']} | {v['failed']} | "
            f"{v['failure_rate']:.1%} | {v.get('elapsed_ms_median')} | "
            f"{v.get('elapsed_ms_p95')} | {v.get('status_distribution')} | "
            f"{v.get('observed_token_counts')} |"
        )
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
        "--capture", action="store_true",
        help="Capture rerank payloads for the fixtures and exit.",
    )
    parser.add_argument(
        "--n", type=int, default=200,
        help="Number of replays per fixture (default 200).",
    )
    parser.add_argument(
        "--gap-ms", type=int, default=50,
        help="Inter-request gap in milliseconds (default 50).",
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

    captures: dict[str, dict[str, Any]] = {}
    for name in CAPTURE_FIXTURES:
        path = CAPTURE_DIR / f"{name}.json"
        if not path.exists():
            print(
                f"missing capture for {name} at {path}. Run --capture first.",
                file=sys.stderr,
            )
            return 3
        captures[name] = json.loads(path.read_text(encoding="utf-8"))

    ts = time.strftime("%Y-%m-%dT%H-%M-%SZ", time.gmtime())
    out_dir = RESULTS_ROOT / ts
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"diag -> {out_dir}", file=sys.stderr)

    all_calls: list[CallRecord] = []
    per_fixture: dict[str, dict[str, Any]] = {}
    capture_meta: dict[str, dict[str, Any]] = {}

    with httpx.Client(timeout=30.0) as client:
        for name, cap in captures.items():
            payload = cap["payload"]
            docs = payload["documents"]
            longest = max((len(d) for d in docs), default=0)
            capture_meta[name] = {
                "url": cap["url"],
                "query": cap["query"],
                "n_docs": len(docs),
                "longest_doc_chars": longest,
            }
            print(
                f"\n[fixture {name}] n_docs={len(docs)} longest={longest}",
                file=sys.stderr,
            )
            calls = _run_fixture(
                client, args.url, name, payload, args.n, args.gap_ms,
                verbose=args.verbose,
            )
            all_calls.extend(calls)
            agg = _aggregate(calls)
            per_fixture[name] = agg
            print(
                f"  total={agg['total']} failed={agg['failed']} "
                f"rate={agg['failure_rate']:.1%} "
                f"status_dist={agg['status_distribution']} "
                f"tok={agg['observed_token_counts']}",
                file=sys.stderr,
            )

    code, text = _decide_gate(per_fixture)

    summary = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "url": args.url,
        "model": args.model,
        "env": {
            "TRAWL_RERANK_MAX_PER_DOC_CHARS": os.environ.get(
                "TRAWL_RERANK_MAX_PER_DOC_CHARS", "1500 (default)"
            ),
        },
        "params": {"n": args.n, "gap_ms": args.gap_ms},
        "captures": capture_meta,
        "per_fixture": per_fixture,
        "decision": {"code": code, "text": text},
        "raw_calls": [asdict(c) for c in all_calls],
    }

    (out_dir / "report.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (out_dir / "report.md").write_text(_render_md(summary), encoding="utf-8")

    print(f"\ndecision: {code} — {text}", file=sys.stderr)
    print(f"report: {out_dir}/report.md", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
