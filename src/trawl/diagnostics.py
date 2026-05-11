"""Runtime health checks for trawl deployments."""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Literal

import httpx

from . import __version__, embedding_cache, fetch_cache, reranking, retrieval, telemetry

Status = Literal["ok", "warn", "fail"]


@dataclass
class CheckResult:
    name: str
    status: Status
    message: str
    required: bool
    detail: dict = field(default_factory=dict)


def run_checks(*, include_network: bool = True) -> list[CheckResult]:
    """Run local runtime and endpoint health checks."""
    rows = [
        check_python(),
        check_playwright_browser(),
        check_writable_path("fetch_cache", fetch_cache._cache_dir(), required=True),
        check_writable_path("embedding_cache", embedding_cache._cache_dir(), required=False),
        check_writable_path("telemetry", telemetry._target_path().parent, required=False),
        check_vlm_configured(),
    ]
    if include_network:
        rows.append(check_embedding_endpoint())
        rows.append(check_reranker_endpoint())
    else:
        rows.append(CheckResult("embedding", "warn", "network checks skipped", required=True))
        rows.append(CheckResult("reranker", "warn", "network checks skipped", required=False))
    return rows


def _package_version() -> str:
    try:
        return metadata.version("trawl")
    except metadata.PackageNotFoundError:
        return __version__


def check_python() -> CheckResult:
    version = platform.python_version()
    required_ok = sys.version_info >= (3, 10)
    return CheckResult(
        "python",
        "ok" if required_ok else "fail",
        "Python runtime available" if required_ok else "Python 3.10+ required",
        required=True,
        detail={"version": version, "trawl_version": _package_version()},
    )


def check_playwright_browser() -> CheckResult:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            executable = Path(pw.chromium.executable_path)
        if executable.exists():
            return CheckResult(
                "playwright",
                "ok",
                "Chromium browser executable found",
                required=True,
                detail={"executable": str(executable)},
            )
        return CheckResult(
            "playwright",
            "fail",
            "Chromium executable missing; run `playwright install chromium`",
            required=True,
            detail={"executable": str(executable)},
        )
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            "playwright",
            "fail",
            f"{type(e).__name__}: {e}",
            required=True,
        )


def check_writable_path(name: str, path: Path, *, required: bool) -> CheckResult:
    target_dir = path if path.suffix == "" else path.parent
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target_dir, prefix=".trawl-doctor-", delete=True):
            pass
        return CheckResult(
            name,
            "ok",
            "path is writable",
            required=required,
            detail={"path": str(target_dir)},
        )
    except OSError as e:
        return CheckResult(
            name,
            "fail" if required else "warn",
            f"{type(e).__name__}: {e}",
            required=required,
            detail={"path": str(target_dir)},
        )


def check_embedding_endpoint() -> CheckResult:
    base_url = retrieval.DEFAULT_EMBEDDING_URL
    model = retrieval.DEFAULT_EMBEDDING_MODEL
    try:
        response = httpx.post(
            f"{base_url}/embeddings",
            json={"model": model, "input": ["trawl doctor smoke"]},
            timeout=5.0,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data.get("data"), list) or not data["data"]:
            raise ValueError("missing data[] in embedding response")
        return CheckResult(
            "embedding",
            "ok",
            "embedding endpoint returned a vector",
            required=True,
            detail={"url": base_url, "model": model},
        )
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            "embedding",
            "fail",
            f"{type(e).__name__}: {e}",
            required=True,
            detail={"url": base_url, "model": model},
        )


def check_reranker_endpoint() -> CheckResult:
    base_url = reranking.DEFAULT_RERANKER_URL
    model = reranking.DEFAULT_RERANKER_MODEL
    try:
        response = httpx.post(
            f"{base_url}/rerank",
            json={"model": model, "query": "smoke", "documents": ["smoke document"]},
            timeout=5.0,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data.get("results"), list):
            raise ValueError("missing results[] in rerank response")
        return CheckResult(
            "reranker",
            "ok",
            "reranker endpoint returned scores",
            required=False,
            detail={"url": base_url, "model": model},
        )
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            "reranker",
            "warn",
            f"{type(e).__name__}: {e}",
            required=False,
            detail={"url": base_url, "model": model},
        )


def check_vlm_configured() -> CheckResult:
    url = os.environ.get("TRAWL_VLM_URL", "").strip()
    if url:
        return CheckResult(
            "vlm",
            "ok",
            "TRAWL_VLM_URL configured; profile_page can be exposed by MCP",
            required=False,
            detail={"url": url},
        )
    return CheckResult(
        "vlm",
        "warn",
        "TRAWL_VLM_URL unset; profile_page remains disabled",
        required=False,
    )


def exit_code(rows: list[CheckResult]) -> int:
    return 1 if any(row.required and row.status == "fail" for row in rows) else 0


def render_text(rows: list[CheckResult]) -> str:
    lines = ["trawl doctor"]
    for row in rows:
        label = row.status.upper()
        importance = "required" if row.required else "optional"
        lines.append(f"{label} {row.name:<16} [{importance}] {row.message}")
    return "\n".join(lines) + "\n"


def render_json(rows: list[CheckResult]) -> str:
    payload = {
        "ok": exit_code(rows) == 0,
        "checks": [asdict(row) for row in rows],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check trawl runtime health.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    parser.add_argument(
        "--no-network",
        action="store_true",
        help="Skip embedding and reranker endpoint requests.",
    )
    return parser.parse_args(argv)


def main(
    argv: list[str] | None = None,
    *,
    checks: Callable[[bool], list[CheckResult]] | None = None,
) -> int:
    args = parse_args(argv)
    run = checks if checks is not None else lambda include_network: run_checks(
        include_network=include_network
    )
    rows = run(not args.no_network)
    if args.json:
        print(render_json(rows), end="")
    else:
        print(render_text(rows), end="")
    return exit_code(rows)


def _cli_entry() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    _cli_entry()
