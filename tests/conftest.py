"""Shared pytest fixtures for trawl's unit tests."""

from __future__ import annotations

import pytest


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
