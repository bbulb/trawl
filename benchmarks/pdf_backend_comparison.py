"""Compare optional PDF/document extraction backends on fact and table recall."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

from trawl.fetchers import pdf_backends

BENCH_DIR = Path(__file__).resolve().parent
DEFAULT_CASES_FILE = BENCH_DIR / "pdf_backend_cases.yaml"
DEFAULT_RESULTS_ROOT = BENCH_DIR / "results" / "pdf-backends"
HTTP_TIMEOUT = 120.0
DEFAULT_BACKENDS = ["pymupdf", "markitdown", "unstructured", "docling", "mineru"]
REQUIRED_CASE_FIELDS = {"id", "category", "source", "query", "expected_facts", "failure_class"}
RESULT_FIELDS = [
    "case_id",
    "category",
    "backend",
    "status",
    "latency_ms",
    "chars_returned",
    "n_tables",
    "recall_at_k",
    "answer_grounding_hit",
    "table_hit",
    "failure_phase",
    "missing_facts",
    "missing_tables",
    "error",
]


def load_cases(path: Path) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cases = data.get("cases")
    if not isinstance(cases, list):
        raise ValueError("case file must contain a cases list")
    for case in cases:
        validate_case(case)
    return cases


def validate_case(case: dict[str, Any]) -> None:
    missing = sorted(REQUIRED_CASE_FIELDS - set(case))
    if missing:
        raise ValueError(f"case {case.get('id', '<unknown>')} missing fields: {', '.join(missing)}")
    if not case["expected_facts"]:
        raise ValueError(f"case {case['id']} must define expected_facts")
    for group_name in ("expected_facts", "expected_tables"):
        for fact in case.get(group_name, []) or []:
            checks = [name for name in ("all_of", "any_of", "pattern") if name in fact]
            if "id" not in fact or len(checks) != 1:
                raise ValueError(f"{group_name} entry in {case['id']} needs id and one matcher")


def select_cases(
    cases: list[dict[str, Any]], *, only: str | None, limit: int | None
) -> list[dict[str, Any]]:
    selected = [case for case in cases if only is None or case["id"] == only]
    if only is not None and not selected:
        raise ValueError(f"unknown case id: {only}")
    if limit is not None:
        selected = selected[:limit]
    return selected


def load_source_bytes(source: str) -> bytes:
    if source.startswith(("http://", "https://")):
        with httpx.Client(follow_redirects=True, timeout=HTTP_TIMEOUT) as client:
            response = client.get(source, headers={"User-Agent": "trawl/0.1"})
            response.raise_for_status()
            return response.content
    return Path(source).expanduser().read_bytes()


def build_backend_result(
    case: dict[str, Any], extraction: pdf_backends.PdfExtraction
) -> dict[str, Any]:
    fact_score = score_groups(extraction.markdown, case["expected_facts"])
    table_text = tables_to_text(extraction.tables)
    table_score = score_groups(table_text, case.get("expected_tables", []) or [])
    error = extraction.error
    status = "ok"
    failure_phase = None

    if error:
        status = "error"
        failure_phase = case.get("failure_class", {}).get("on_fetch_error", "provider_error")
    elif not extraction.markdown.strip():
        status = "fail"
        failure_phase = case.get("failure_class", {}).get("on_empty_output", "extraction")
    elif not fact_score["answer_grounding_hit"] or not table_score["answer_grounding_hit"]:
        status = "fail"
        failure_phase = case.get("failure_class", {}).get("on_missing_facts", "extraction")

    return {
        "case_id": case["id"],
        "category": case["category"],
        "backend": extraction.backend,
        "status": status,
        "latency_ms": extraction.elapsed_ms,
        "chars_returned": len(extraction.markdown),
        "n_tables": len(extraction.tables),
        "recall_at_k": fact_score["recall_at_k"],
        "answer_grounding_hit": fact_score["answer_grounding_hit"],
        "table_hit": table_score["answer_grounding_hit"],
        "failure_phase": failure_phase,
        "missing_facts": fact_score["missing"],
        "missing_tables": table_score["missing"],
        "error": error,
    }


def score_groups(text: str, groups: list[dict[str, Any]]) -> dict[str, Any]:
    if not groups:
        return {"recall_at_k": 1.0, "answer_grounding_hit": True, "missing": []}
    missing = [group["id"] for group in groups if not group_matches(text, group)]
    found = len(groups) - len(missing)
    return {
        "recall_at_k": found / len(groups),
        "answer_grounding_hit": not missing,
        "missing": missing,
    }


def group_matches(text: str, group: dict[str, Any]) -> bool:
    if "all_of" in group:
        return all(value in text for value in group["all_of"])
    if "any_of" in group:
        return any(value in text for value in group["any_of"])
    return re.search(group["pattern"], text) is not None


def tables_to_text(tables: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for table in tables:
        for row in table.get("rows", []) or []:
            lines.append("\t".join(str(cell) for cell in row))
    return "\n".join(lines)


def run_case(case: dict[str, Any], backends: list[str]) -> list[dict[str, Any]]:
    try:
        content = load_source_bytes(case["source"])
    except Exception as exc:  # pragma: no cover - network/filesystem behavior
        return [
            {
                "case_id": case["id"],
                "category": case["category"],
                "backend": backend,
                "status": "error",
                "latency_ms": 0,
                "chars_returned": 0,
                "n_tables": 0,
                "recall_at_k": 0.0,
                "answer_grounding_hit": False,
                "table_hit": False,
                "failure_phase": case.get("failure_class", {}).get("on_fetch_error", "fetch"),
                "missing_facts": [fact["id"] for fact in case["expected_facts"]],
                "missing_tables": [fact["id"] for fact in case.get("expected_tables", []) or []],
                "error": f"{type(exc).__name__}: {exc}",
            }
            for backend in backends
        ]

    return [
        build_backend_result(case, pdf_backends.extract_pdf_bytes(content, backend=backend))
        for backend in backends
    ]


def write_outputs(output_dir: Path, results: list[dict[str, Any]]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "results.jsonl").open("w", encoding="utf-8") as f:
        for result in results:
            f.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
    with (output_dir / "summary.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=RESULT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for result in results:
            row = dict(result)
            row["missing_facts"] = ",".join(row.get("missing_facts") or [])
            row["missing_tables"] = ",".join(row.get("missing_tables") or [])
            writer.writerow(row)
    (output_dir / "report.md").write_text(render_report(results), encoding="utf-8")


def render_report(results: list[dict[str, Any]]) -> str:
    lines = [
        "# PDF backend comparison",
        "",
        f"Rows: {len(results)}",
        "",
        "| Backend | Rows | Pass rate | Avg latency ms | Avg chars | Avg tables |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for backend in sorted({result["backend"] for result in results}):
        rows = [result for result in results if result["backend"] == backend]
        ok = [result for result in rows if result["status"] == "ok"]
        lines.append(
            f"| {backend} | {len(rows)} | {len(ok) / len(rows):.2f} | "
            f"{_average([row['latency_ms'] for row in rows]):.1f} | "
            f"{_average([row['chars_returned'] for row in rows]):.1f} | "
            f"{_average([row['n_tables'] for row in rows]):.1f} |"
        )
    lines.extend(
        [
            "",
            "Outputs: `results.jsonl`, `summary.csv`, and this report.",
            "Heavy backends are optional; missing packages appear as backend errors.",
        ]
    )
    return "\n".join(lines) + "\n"


def _average(values: list[int | float]) -> float:
    return sum(values) / len(values) if values else 0.0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_FILE)
    parser.add_argument("--only", help="Run one case id")
    parser.add_argument("--limit", type=int, help="Limit selected cases")
    parser.add_argument(
        "--backend",
        action="append",
        choices=pdf_backends.SUPPORTED_BACKENDS,
        help="Backend to run. May be repeated. Defaults to all known backends.",
    )
    parser.add_argument("--output-dir", type=Path)
    return parser.parse_args(argv)


def timestamped_output_dir() -> Path:
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    return DEFAULT_RESULTS_ROOT / stamp


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cases = select_cases(load_cases(args.cases), only=args.only, limit=args.limit)
    backends = args.backend or DEFAULT_BACKENDS
    results: list[dict[str, Any]] = []
    for case in cases:
        results.extend(run_case(case, backends))
    output_dir = args.output_dir or timestamped_output_dir()
    write_outputs(output_dir, results)
    print(f"Wrote {len(results)} rows to {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
