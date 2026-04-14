import hashlib
from pathlib import Path

import pytest

from benchmarks.wcxb.fetch import download_one, verify_sha256, HashMismatch


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_download_one_idempotent_with_matching_hash(tmp_path):
    src = tmp_path / "src.txt"
    src.write_bytes(b"hello wcxb")
    expected = _sha256(src)

    dest = tmp_path / "dest.txt"
    assert download_one(src.as_uri(), dest, expected) is True   # downloaded
    assert download_one(src.as_uri(), dest, expected) is False  # skipped


def test_download_one_raises_on_hash_mismatch(tmp_path):
    src = tmp_path / "src.txt"
    src.write_bytes(b"hello")
    dest = tmp_path / "dest.txt"
    with pytest.raises(HashMismatch):
        download_one(src.as_uri(), dest, "0" * 64)


def test_verify_sha256_true_for_match(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"abc")
    assert verify_sha256(p, _sha256(p)) is True


def test_verify_sha256_false_for_mismatch(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"abc")
    assert verify_sha256(p, "0" * 64) is False
