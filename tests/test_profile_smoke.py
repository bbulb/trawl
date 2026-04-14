"""End-to-end smoke test for the profile feature.

Runs the full render → VLM → mapper → save → load → fetch_relevant
(profile fast path) flow against the same KBO schedule URL that
tests/test_cases.yaml uses. Skipped cleanly when the VLM endpoint is
unreachable.

Invoke:
    python tests/test_profile_smoke.py
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

import httpx
import yaml

VLM_URL = os.environ.get(
    "TRAWL_VLM_URL",
    "http://localhost:8080/v1",
)


def _vlm_reachable() -> bool:
    # We don't actually need a successful vision response — just need to
    # know the endpoint is there. A HEAD or an empty POST both work.
    try:
        # llama-server returns 400 or 405 for HEAD on /v1/chat/completions,
        # but 200 on GET /v1/models. Use the latter as the reachability probe.
        base = VLM_URL.rstrip("/") + "/models"
        r = httpx.get(base, timeout=3.0)
        return r.status_code == 200
    except (httpx.HTTPError, Exception):
        return False


def _load_kbo_ground_truth() -> tuple[str, dict]:
    """Return (url, ground_truth_dict) for the kbo_schedule case."""
    cases_path = Path(__file__).parent / "test_cases.yaml"
    cases = yaml.safe_load(cases_path.read_text())["cases"]
    for c in cases:
        if c["id"] == "kbo_schedule":
            return c["url"], c.get("ground_truth") or {}
    raise RuntimeError("kbo_schedule case not found in test_cases.yaml")


def _matches_ground_truth(chunks: list[dict], gt: dict) -> tuple[bool, list[str]]:
    blob = "\n\n".join((c.get("heading") or "") + "\n" + (c.get("text") or "") for c in chunks)
    failures = []
    for s in gt.get("must_contain_all") or []:
        if s not in blob:
            failures.append(f"missing required: {s!r}")
    any_list = gt.get("must_contain_any") or []
    if any_list and not any(s in blob for s in any_list):
        failures.append(f"none of any-group present: {any_list!r}")
    any2_list = gt.get("must_contain_any_2") or []
    if any2_list and not any(s in blob for s in any2_list):
        failures.append(f"none of any-group-2 present: {any2_list!r}")
    pattern = gt.get("must_contain_pattern")
    if pattern and not re.search(pattern, blob):
        failures.append(f"pattern not matched: {pattern!r}")
    return len(failures) == 0, failures


def main() -> int:
    if not _vlm_reachable():
        print(f"SKIP: VLM endpoint {VLM_URL} is not reachable.")
        return 0

    # Isolate cache dirs so this test doesn't touch the developer's real cache.
    with tempfile.TemporaryDirectory(prefix="trawl-profile-smoke-") as tmp:
        os.environ["TRAWL_PROFILE_DIR"] = str(Path(tmp) / "profiles")
        os.environ["TRAWL_VISITS_FILE"] = str(Path(tmp) / "visits.json")

        # Re-import after setting env vars so profile/cache modules pick them up.
        import importlib

        for modname in [
            "trawl.profiles.profile",
            "trawl.profiles.cache",
            "trawl.profiles",
            "trawl.pipeline",
            "trawl",
        ]:
            if modname in sys.modules:
                importlib.reload(sys.modules[modname])
        from trawl import fetch_relevant, to_dict
        from trawl.profiles import generate_profile, load_profile

        url, ground_truth = _load_kbo_ground_truth()
        print(f"Target URL: {url}")
        print(f"Ground truth: {ground_truth}")

        # 1. Generate a profile.
        print()
        print("--- generate_profile ---")
        summary = generate_profile(url)
        print(f"summary: {summary}")
        assert summary.get("ok") is True, summary
        assert summary.get("main_selector"), summary
        assert summary["main_selector"].lower() not in {"body", "html"}, summary
        assert len(summary.get("verification_anchors") or []) >= 1, summary

        # 2. Verify the profile file exists on disk.
        loaded = load_profile(url)
        assert loaded is not None
        assert loaded.mapper.main_selector == summary["main_selector"]
        print(f"profile saved, url_hash={loaded.url_hash}")

        # 3. Run fetch_relevant with NO query and confirm profile fast path.
        print()
        print("--- fetch_relevant (no query) ---")
        result = fetch_relevant(url)
        d = to_dict(result)
        print(f"path: {d['path']}, profile_used: {d['profile_used']}, n_chunks: {len(d['chunks'])}")
        assert d["profile_used"] is True, d
        assert d["path"] in ("profile_direct", "profile_direct_large", "profile_retrieval"), d
        assert d["error"] is None, d
        assert len(d["chunks"]) >= 1, d

        # 4. Check that the returned chunks pass the KBO ground truth.
        ok, failures = _matches_ground_truth(d["chunks"], ground_truth)
        if not ok:
            print(f"FAIL: ground truth not satisfied: {failures}")
            for i, c in enumerate(d["chunks"]):
                text = (c.get("text") or "")[:200]
                print(f"  chunk[{i}]: {text!r}")
            return 1
        print("ground truth: PASS")

    print()
    print("OK: trawl profile smoke test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
