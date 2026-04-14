"""Unit tests for trawl.profiles.profile and trawl.profiles.cache.

No network, no VLM, no Playwright. All tests monkeypatch the profile
cache and visits file locations to a pytest tmp_path so they don't
touch the developer's real ~/.cache/trawl.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """Redirect the profile cache and visits file to tmp_path for every test."""
    profile_dir = tmp_path / "profiles"
    visits_file = tmp_path / "visits.json"
    monkeypatch.setenv("TRAWL_PROFILE_DIR", str(profile_dir))
    monkeypatch.setenv("TRAWL_VISITS_FILE", str(visits_file))
    # Reload modules so the env var is picked up.
    import importlib

    from trawl.profiles import cache as cache_mod
    from trawl.profiles import profile as profile_mod

    importlib.reload(profile_mod)
    importlib.reload(cache_mod)
    yield tmp_path


def test_url_hash_is_deterministic_and_12_chars():
    from trawl.profiles.profile import url_hash

    h1 = url_hash("https://example.com/")
    h2 = url_hash("https://example.com/")
    h3 = url_hash("https://example.com/other")
    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 12
    assert all(c in "0123456789abcdef" for c in h1)


def test_profile_dir_is_created(tmp_path):
    from trawl.profiles.profile import profile_dir

    d = profile_dir()
    assert d.exists()
    assert d.is_dir()


def test_save_and_load_profile_roundtrip():
    from trawl.profiles.mapper import MappedAnchor, MapResult
    from trawl.profiles.profile import (
        build_profile,
        load_profile,
        save_profile,
    )
    from trawl.profiles.vlm import ItemHints, VLMResponse

    vlm_resp = VLMResponse(
        page_type="docs",
        structure_description="Test page with a heading and body text.",
        content_anchors=["heading text", "body paragraph", "footer"],
        noise_labels=["top nav"],
        item_hints=ItemHints(
            has_repeating_items=False,
            item_description=None,
            example_row_anchors=[],
        ),
        raw="",
    )
    map_result = MapResult(
        selector="main.content",
        lca_tag="MAIN",
        lca_path=["HTML", "BODY", "MAIN"],
        subtree_html="<main class='content'>...</main>",
        subtree_chars=1200,
        anchors_found=[
            MappedAnchor(
                anchor="heading text",
                found_count=1,
                container_path=["HTML", "BODY", "MAIN"],
                container_chars=1200,
            ),
            MappedAnchor(
                anchor="body paragraph",
                found_count=1,
                container_path=["HTML", "BODY", "MAIN"],
                container_chars=1200,
            ),
        ],
        anchors_missed=["footer"],
        notes=[],
        outlier_anchors=[],
    )
    profile = build_profile(
        url="https://example.com/page",
        vlm_response=vlm_resp,
        map_result=map_result,
        vlm_endpoint="http://localhost:8080/v1/chat/completions",
        vlm_model="gemma",
        min_chars_used=300,
    )

    path = save_profile(profile)
    assert path.exists()
    assert path.name.endswith(".json")

    data = json.loads(path.read_text())
    assert data["schema_version"] == 1
    assert data["url"] == "https://example.com/page"

    loaded = load_profile("https://example.com/page")
    assert loaded is not None
    assert loaded.url == profile.url
    assert loaded.mapper.main_selector == "main.content"
    assert loaded.mapper.verification_anchors == ["heading text", "body paragraph"]
    assert loaded.vlm.page_type == "docs"


def test_load_profile_missing_returns_none():
    from trawl.profiles.profile import load_profile

    assert load_profile("https://never-seen-this-url.example/") is None


def test_load_profile_corrupt_returns_none():
    from trawl.profiles.profile import load_profile, profile_path_for

    url = "https://example.com/corrupt"
    path = profile_path_for(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not valid json {{{", encoding="utf-8")

    # Corrupt file should be treated as missing, not raise.
    assert load_profile(url) is None


def test_verification_anchors_exclude_outliers():
    from trawl.profiles.mapper import MappedAnchor, MapResult
    from trawl.profiles.profile import build_profile
    from trawl.profiles.vlm import ItemHints, VLMResponse

    vlm_resp = VLMResponse(
        page_type="schedule_or_results",
        structure_description="Schedule page.",
        content_anchors=["2026.04", "KBO리그", "KIA 6", "롯데 3", "NC 5"],
        noise_labels=[],
        item_hints=ItemHints(
            has_repeating_items=True, item_description="game row", example_row_anchors=[]
        ),
        raw="",
    )
    map_result = MapResult(
        selector="ul.games",
        lca_tag="UL",
        lca_path=["HTML", "BODY", "DIV", "UL"],
        subtree_html="...",
        subtree_chars=418,
        anchors_found=[
            MappedAnchor(anchor="2026.04", found_count=1, container_path=[], container_chars=0),
            MappedAnchor(anchor="KBO리그", found_count=1, container_path=[], container_chars=0),
            MappedAnchor(anchor="KIA 6", found_count=1, container_path=[], container_chars=0),
            MappedAnchor(anchor="롯데 3", found_count=1, container_path=[], container_chars=0),
            MappedAnchor(anchor="NC 5", found_count=1, container_path=[], container_chars=0),
        ],
        anchors_missed=[],
        notes=[],
        outlier_anchors=["2026.04", "KBO리그"],
    )
    profile = build_profile(
        url="https://example.com/kbo",
        vlm_response=vlm_resp,
        map_result=map_result,
        vlm_endpoint="http://localhost:8080/v1/chat/completions",
        vlm_model="gemma",
        min_chars_used=300,
    )
    assert profile.mapper.verification_anchors == ["KIA 6", "롯데 3", "NC 5"]


def test_all_outliers_produces_empty_verification_anchors():
    """Sanity: if every found anchor is an outlier, verification_anchors
    is empty. This case is handled as drift in the pipeline fast path
    (see test_pipeline-side coverage), but verify the build step here.
    """
    from trawl.profiles.mapper import MappedAnchor, MapResult
    from trawl.profiles.profile import build_profile
    from trawl.profiles.vlm import ItemHints, VLMResponse

    vlm_resp = VLMResponse(
        page_type="other",
        structure_description="weird page",
        content_anchors=["a", "b"],
        noise_labels=[],
        item_hints=ItemHints(
            has_repeating_items=False, item_description=None, example_row_anchors=[]
        ),
        raw="",
    )
    map_result = MapResult(
        selector="div.x",
        lca_tag="DIV",
        lca_path=["HTML", "BODY", "DIV"],
        subtree_html="...",
        subtree_chars=100,
        anchors_found=[
            MappedAnchor(anchor="a", found_count=1, container_path=[], container_chars=0),
            MappedAnchor(anchor="b", found_count=1, container_path=[], container_chars=0),
        ],
        anchors_missed=[],
        notes=[],
        outlier_anchors=["a", "b"],
    )
    profile = build_profile(
        url="https://example.com/all-outliers",
        vlm_response=vlm_resp,
        map_result=map_result,
        vlm_endpoint="http://localhost:8080/v1/chat/completions",
        vlm_model="gemma",
        min_chars_used=300,
    )
    assert profile.mapper.verification_anchors == []


def test_visit_counter_starts_at_zero_and_increments():
    from trawl.profiles.cache import get_visit_count, track_visit

    url = "https://example.com/"
    assert get_visit_count(url) == 0

    assert track_visit(url) == 1
    assert track_visit(url) == 2
    assert track_visit(url) == 3

    assert get_visit_count(url) == 3


def test_visit_counter_separates_urls():
    from trawl.profiles.cache import get_visit_count, track_visit

    track_visit("https://a.example/")
    track_visit("https://a.example/")
    track_visit("https://b.example/")

    assert get_visit_count("https://a.example/") == 2
    assert get_visit_count("https://b.example/") == 1


def test_visit_counter_handles_corrupt_file(tmp_path, monkeypatch):
    visits_file = tmp_path / "visits.json"
    visits_file.write_text("not valid json {{{")
    monkeypatch.setenv("TRAWL_VISITS_FILE", str(visits_file))

    import importlib

    from trawl.profiles import cache as cache_mod

    importlib.reload(cache_mod)

    # Corrupt file is treated as empty; track_visit still works and rewrites.
    assert cache_mod.get_visit_count("https://example.com/") == 0
    assert cache_mod.track_visit("https://example.com/") == 1
    # File is now valid JSON.
    import json

    assert isinstance(json.loads(visits_file.read_text()), dict)


def test_list_host_profiles_filters_by_host(isolated_cache, make_profile):
    from trawl.profiles.profile import list_host_profiles, save_profile

    save_profile(make_profile("https://www.example.com/a/1"))
    save_profile(make_profile("https://www.example.com/a/2"))
    save_profile(make_profile("https://other.example.com/x"))

    results = list_host_profiles("www.example.com")
    assert len(results) == 2
    assert {p.url for p in results} == {
        "https://www.example.com/a/1",
        "https://www.example.com/a/2",
    }


def test_list_host_profiles_sorted_descending(isolated_cache, make_profile):
    from trawl.profiles.profile import list_host_profiles, save_profile

    p1 = make_profile("https://host.test/a")
    p1.generated_at = "2026-01-01T00:00:00+00:00"
    save_profile(p1)

    p2 = make_profile("https://host.test/b")
    p2.generated_at = "2026-03-01T00:00:00+00:00"
    save_profile(p2)

    p3 = make_profile("https://host.test/c")
    p3.generated_at = "2026-02-01T00:00:00+00:00"
    save_profile(p3)

    results = list_host_profiles("host.test")
    assert [p.url for p in results] == [
        "https://host.test/b",
        "https://host.test/c",
        "https://host.test/a",
    ]


def test_list_host_profiles_skips_corrupt(isolated_cache, caplog, make_profile):
    from trawl.profiles.profile import list_host_profiles, profile_dir, save_profile

    save_profile(make_profile("https://host.test/good"))
    # Write a corrupt file into the profile cache
    (profile_dir() / "deadbeef.json").write_text("{not valid json", encoding="utf-8")

    with caplog.at_level("WARNING", logger="trawl.profiles.profile"):
        results = list_host_profiles("host.test")

    assert len(results) == 1
    assert results[0].url == "https://host.test/good"
    assert any("skipping" in rec.message for rec in caplog.records)


def test_extract_fresh_anchors_picks_short_nontrivial_lines():
    from trawl.profiles.profile import extract_fresh_anchors

    text = """
foo
short
이건 다섯자이상
also this line is in range
way too long: xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
still okay
irrelevant but counted
"""
    result = extract_fresh_anchors(text)
    # First 3 lines whose stripped length is in [5, 50]. "foo" and "short"
    # are too short, empty lines and the 100+ char line are excluded.
    assert len(result) == 3
    for s in result:
        assert 5 <= len(s) <= 50


def test_extract_fresh_anchors_handles_empty_text():
    from trawl.profiles.profile import extract_fresh_anchors

    assert extract_fresh_anchors("") == []
    assert extract_fresh_anchors("   \n\n   \n") == []
    assert extract_fresh_anchors(None) == []  # type: ignore[arg-type]


def test_extract_fresh_anchors_caps_at_n():
    from trawl.profiles.profile import extract_fresh_anchors

    text = "\n".join(f"line {i} ok" for i in range(10))
    assert len(extract_fresh_anchors(text, n=3)) == 3
    assert len(extract_fresh_anchors(text, n=5)) == 5


def test_extract_fresh_anchors_skips_out_of_bounds():
    from trawl.profiles.profile import extract_fresh_anchors

    text = "\n".join(
        [
            "a",  # too short (1)
            "abcd",  # too short (4)
            "perfect",  # length 7 -> kept
            "x" * 51,  # too long (51)
            "another good one",  # length 16 -> kept
        ]
    )
    result = extract_fresh_anchors(text, n=5)
    assert result == ["perfect", "another good one"]


def test_copy_profile_updates_url_and_anchors(isolated_cache, make_profile):
    from trawl.profiles.profile import (
        copy_profile_for_new_url,
        load_profile,
        save_profile,
        url_hash,
    )

    parent = make_profile("https://host.test/parent", selector="div.main", char_count=2000)
    parent.mapper.verification_anchors = ["old1", "old2", "old3"]
    save_profile(parent)

    new_url = "https://host.test/child"
    copy = copy_profile_for_new_url(parent, new_url, ["fresh1", "fresh2"])

    assert copy.url == new_url
    assert copy.url_hash == url_hash(new_url)
    assert copy.mapper.verification_anchors == ["fresh1", "fresh2"]
    # Structural fields inherited verbatim
    assert copy.mapper.main_selector == "div.main"
    assert copy.mapper.lca_tag == parent.mapper.lca_tag
    assert copy.mapper.lca_path == parent.mapper.lca_path
    assert copy.mapper.subtree_char_count == 2000
    assert copy.vlm.page_type == parent.vlm.page_type
    assert copy.vlm.structure_description == parent.vlm.structure_description
    # Persisted to disk
    loaded = load_profile(new_url)
    assert loaded is not None
    assert loaded.url == new_url
    assert loaded.mapper.verification_anchors == ["fresh1", "fresh2"]
    # Parent still there, unchanged
    reparent = load_profile("https://host.test/parent")
    assert reparent is not None
    assert reparent.mapper.verification_anchors == ["old1", "old2", "old3"]
