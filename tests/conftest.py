"""Shared pytest fixtures for trawl's unit tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_embed_cache(monkeypatch, tmp_path):
    """Isolate the document embedding cache per test.

    With the default-on `TRAWL_EMBED_CACHE_TTL=3600`, tests that share
    chunk texts (e.g. the chunk-budget suite's `doc N alpha` fixtures)
    can otherwise leak cached embeddings across tests via the user's
    real `~/.cache/trawl/embeddings/` directory. Tests that exercise
    specific cache behaviour override `TRAWL_EMBED_CACHE_PATH` /
    `TRAWL_EMBED_CACHE_TTL` themselves after this fixture runs.
    """

    monkeypatch.setenv("TRAWL_EMBED_CACHE_PATH", str(tmp_path / "_embed_cache"))


@pytest.fixture
def make_profile():
    """Factory fixture — returns a callable that builds a minimal valid Profile.

    Used by profile and host-transfer tests. Keeps the expensive import of
    Profile/ProfileVLMSection/ProfileMapperSection inside the factory so
    tests that don't need them don't pay for the import.
    """

    def _factory(
        url: str,
        *,
        selector: str = "div.main",
        char_count: int = 1000,
        verification_anchors: list[str] | None = None,
    ):
        from datetime import datetime, timezone

        from trawl.profiles.profile import (
            Profile,
            ProfileMapperSection,
            ProfileVLMSection,
            url_hash,
        )

        anchors = verification_anchors if verification_anchors is not None else ["a1", "a2"]
        return Profile(
            schema_version=1,
            url=url,
            url_hash=url_hash(url),
            generated_at=datetime.now(timezone.utc).isoformat(),
            vlm=ProfileVLMSection(
                endpoint="http://test/v1",
                model="gemma",
                page_type="other",
                structure_description="",
                anchors_requested=[],
                anchors_found=[],
                anchors_missed=[],
                noise_labels=[],
                item_hints={},
            ),
            mapper=ProfileMapperSection(
                main_selector=selector,
                lca_tag="DIV",
                lca_path=["HTML", "BODY", "DIV"],
                subtree_char_count=char_count,
                min_chars_used=300,
                notes=[],
                verification_anchors=anchors,
            ),
        )

    return _factory
