"""Download the WCXB dev split snapshot locally (idempotent).

`manifest.json` pins per-file SHA-256 for the pinned commit. Run:

    python benchmarks/wcxb/fetch.py

to populate `benchmarks/wcxb/data/dev/{html,ground-truth}/`. Re-runs skip
files whose hash already matches.

Regenerating the manifest (maintainer task) is invoked with
`--refresh-manifest`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path


WCXB_REPO = "Murrough-Foley/web-content-extraction-benchmark"
WCXB_COMMIT = "c039d5ee9f5a3a984a0e167e63aacd04e76e78a9"
DEV_PATH = "dev"  # relative inside the repo


class HashMismatch(RuntimeError):
    pass


def verify_sha256(path: Path, expected: str) -> bool:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest() == expected


def download_one(url: str, dest: Path, expected_sha256: str) -> bool:
    """Download url to dest. Skip if dest exists with matching hash.

    Returns True if a download occurred, False if skipped. Raises
    HashMismatch if downloaded bytes don't match the expected hash.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and verify_sha256(dest, expected_sha256):
        return False
    with urllib.request.urlopen(url) as resp, dest.open("wb") as f:
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
    if not verify_sha256(dest, expected_sha256):
        dest.unlink(missing_ok=True)
        raise HashMismatch(f"{url} -> {dest}: hash mismatch")
    return True


def _load_manifest(manifest_path: Path) -> dict:
    if not manifest_path.exists():
        raise SystemExit(
            f"Manifest not found at {manifest_path}. Regenerate with "
            f"--refresh-manifest or check out a commit that includes it."
        )
    return json.loads(manifest_path.read_text())


def _fetch_all(manifest: dict, data_dir: Path) -> tuple[int, int]:
    base = (
        f"https://raw.githubusercontent.com/{WCXB_REPO}/{WCXB_COMMIT}/{DEV_PATH}"
    )
    downloaded = skipped = 0
    for i, (rel_path, sha) in enumerate(sorted(manifest.items()), start=1):
        url = f"{base}/{rel_path}"
        dest = data_dir / rel_path
        did = download_one(url, dest, sha)
        if did:
            downloaded += 1
        else:
            skipped += 1
        if i % 200 == 0:
            print(f"[{i}/{len(manifest)}] dl={downloaded} skip={skipped}", file=sys.stderr)
    return downloaded, skipped


def _refresh_manifest(manifest_path: Path) -> None:
    """Enumerate dev/ via git trees API and write manifest.json."""
    api = (
        f"https://api.github.com/repos/{WCXB_REPO}/git/trees/{WCXB_COMMIT}"
        "?recursive=1"
    )
    with urllib.request.urlopen(api) as r:
        tree = json.load(r)
    if tree.get("truncated"):
        raise SystemExit(
            "Git trees response truncated. Upstream grew past the trees API "
            "cap; manual strategy required."
        )
    entries = [
        e for e in tree["tree"]
        if e["type"] == "blob"
        and (
            e["path"].startswith("dev/html/")
            or e["path"].startswith("dev/ground-truth/")
        )
    ]
    print(f"Found {len(entries)} dev blobs; hashing each...", file=sys.stderr)

    manifest: dict[str, str] = {}
    for i, e in enumerate(entries, start=1):
        rel = e["path"][len("dev/"):]  # strip leading "dev/"
        raw_url = (
            f"https://raw.githubusercontent.com/{WCXB_REPO}/{WCXB_COMMIT}/{e['path']}"
        )
        data = urllib.request.urlopen(raw_url).read()
        manifest[rel] = hashlib.sha256(data).hexdigest()
        if i % 200 == 0:
            print(f"[{i}/{len(entries)}] hashed", file=sys.stderr)

    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    print(
        f"Wrote {manifest_path} with {len(manifest)} entries", file=sys.stderr
    )


def _main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--data-dir", default=Path("benchmarks/wcxb/data/dev"), type=Path
    )
    p.add_argument(
        "--manifest", default=Path("benchmarks/wcxb/manifest.json"), type=Path
    )
    p.add_argument(
        "--refresh-manifest", action="store_true",
        help="Regenerate manifest.json from the pinned upstream commit.",
    )
    args = p.parse_args()

    if args.refresh_manifest:
        _refresh_manifest(args.manifest)
        return 0

    manifest = _load_manifest(args.manifest)
    downloaded, skipped = _fetch_all(manifest, args.data_dir)
    print(
        f"Fetched {downloaded}, skipped {skipped}, total {len(manifest)}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(_main())
