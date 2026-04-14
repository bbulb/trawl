"""End-to-end smoke test for the host-transfer profile path.

Generates a profile for one Google Finance ticker, then calls
fetch_relevant on a different ticker on the same host and asserts
the transfer path kicked in (profile_used=True) and a copy landed
on disk under the new URL's hash.

Skipped cleanly when the VLM endpoint or network is unreachable.

Invoke:
    python tests/test_profile_transfer_smoke.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import httpx

PARENT_URL = "https://www.google.com/finance/quote/348080:KOSDAQ"
CHILD_URL = "https://www.google.com/finance/quote/GOOGL:NASDAQ"

VLM_URL = os.environ.get(
    "TRAWL_VLM_URL",
    "http://localhost:8080/v1",
)


def _vlm_reachable() -> bool:
    try:
        base = VLM_URL.rstrip("/") + "/models"
        r = httpx.get(base, timeout=3.0)
        return r.status_code == 200
    except Exception:
        return False


def main() -> int:
    if not _vlm_reachable():
        print("SKIP: VLM not reachable at", VLM_URL)
        return 0

    # Redirect profile cache to a temp dir so we don't touch the real one
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["TRAWL_PROFILE_DIR"] = str(Path(tmp) / "profiles")
        os.environ["TRAWL_VISITS_FILE"] = str(Path(tmp) / "visits.json")

        # Reload so the new env vars take effect
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

        from trawl.pipeline import fetch_relevant
        from trawl.profiles import generate_profile, profile_path_for

        print(f"Generating profile for parent: {PARENT_URL}")
        parent_summary = generate_profile(PARENT_URL)
        if not parent_summary.get("ok"):
            print(f"FAIL: parent profile generation failed: {parent_summary}")
            return 1
        print(f"  parent main_selector: {parent_summary.get('main_selector')}")
        print(f"  parent subtree_char_count: {parent_summary.get('subtree_char_count')}")

        print(f"Fetching child via transfer: {CHILD_URL}")
        result = fetch_relevant(CHILD_URL, query=None)
        if result.error:
            print(f"FAIL: child fetch error: {result.error}")
            return 1
        if not result.profile_used:
            print(f"FAIL: transfer did not fire (profile_used=False), path={result.path}")
            return 1
        print(f"  child profile_hash: {result.profile_hash}")
        print(f"  child path: {result.path}")
        print(f"  child chunks: {len(result.chunks)}")

        # Copy should be on disk
        child_path = profile_path_for(CHILD_URL)
        if not child_path.exists():
            print(f"FAIL: copy not persisted at {child_path}")
            return 1
        print(f"  copy persisted at: {child_path}")

        print("OK: transfer smoke test passed")
        return 0


if __name__ == "__main__":
    sys.exit(main())
