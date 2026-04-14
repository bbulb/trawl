"""Unit tests for the host-transfer profile path.

No network, no VLM. Playwright is stubbed via a fake render_session
context manager so the tests exercise the selection logic, the
size-range check, and the copy-save side effect without bringing up
a browser.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """Redirect the profile cache to tmp_path for every test."""
    import importlib

    monkeypatch.setenv("TRAWL_PROFILE_DIR", str(tmp_path / "profiles"))
    monkeypatch.setenv("TRAWL_VISITS_FILE", str(tmp_path / "visits.json"))
    from trawl.profiles import cache as c_mod
    from trawl.profiles import profile as p_mod

    importlib.reload(p_mod)
    importlib.reload(c_mod)
    yield


# _mkprofile moved to tests/conftest.py as the `make_profile` fixture.


class _FakeElement:
    def __init__(self, text: str, outer_html: str = "<div>stub</div>"):
        self._text = text
        self._outer = outer_html

    def inner_text(self) -> str:
        return self._text

    def evaluate(self, _expr: str) -> str:
        return self._outer


class _FakePage:
    def __init__(self, selector_results: dict[str, list[_FakeElement]]):
        self._results = selector_results

    def query_selector_all(self, selector: str) -> list[_FakeElement]:
        return self._results.get(selector, [])


class _FakeRenderResult:
    def __init__(self, page: _FakePage):
        self.page = page


@contextmanager
def _fake_render_session(page: _FakePage):
    yield _FakeRenderResult(page)


def test_transfer_returns_none_on_no_host_candidates(monkeypatch):
    """Exact miss, no same-host profiles -> returns None and skips render."""
    from trawl import pipeline

    # Make render_session raise so we can prove it was never called
    def explode(_url):
        raise AssertionError("render_session should not be called")

    monkeypatch.setattr(pipeline.playwright, "render_session", explode)

    result = pipeline._profile_transfer_path(
        "https://absent.example.com/foo",
        query=None,
        k=None,
        t_start=0.0,
    )
    assert result is None


def test_transfer_accepts_first_in_range_match(monkeypatch, make_profile):
    """Stub render_session to return a page with one in-range element.
    Verify the result is built and a copy lands on disk."""
    from trawl import pipeline
    from trawl.profiles.profile import load_profile, save_profile

    # Seed a host-local profile with subtree_char_count=1000
    parent = make_profile(
        "https://host.test/first",
        selector="div.main",
        char_count=1000,
    )
    save_profile(parent)

    # The fake page returns text of length 1500 (1.5x, in-range)
    content_text = "line one\n" + "x" * 1490
    fake = _FakePage(
        {
            "div.main": [
                _FakeElement(content_text, "<div><p>line one</p><p>body content</p></div>")
            ],
        }
    )
    captured_url = {}

    def render_stub(u):
        captured_url["url"] = u
        return _fake_render_session(fake)

    monkeypatch.setattr(
        pipeline.playwright,
        "render_session",
        render_stub,
    )

    new_url = "https://host.test/second"
    result = pipeline._profile_transfer_path(
        new_url,
        query=None,
        k=None,
        t_start=0.0,
    )

    # render_session must have been called with the NEW url, not the parent's
    assert captured_url["url"] == new_url

    assert result is not None
    assert result.profile_used is True
    assert result.url == new_url
    # Copy persisted
    copy = load_profile(new_url)
    assert copy is not None
    assert copy.url == new_url
    assert copy.mapper.main_selector == "div.main"
    # Fresh anchors extracted from the matched text (first 5-50 char line)
    assert copy.mapper.verification_anchors == ["line one"]


def test_transfer_rejects_all_out_of_range(monkeypatch, make_profile):
    """All candidate selector hits are outside [0.3x, 3.0x] -> None."""
    from trawl import pipeline
    from trawl.profiles.profile import save_profile

    parent = make_profile(
        "https://host.test/parent",
        selector="div.main",
        char_count=1000,
    )
    save_profile(parent)

    # 100 chars = 0.1x (below 0.3x floor); 4000 chars = 4.0x (above 3.0x ceiling)
    fake = _FakePage(
        {
            "div.main": [
                _FakeElement("x" * 100, "<div>tiny</div>"),
                _FakeElement("y" * 4000, "<div>huge</div>"),
            ],
        }
    )
    monkeypatch.setattr(
        pipeline.playwright,
        "render_session",
        lambda _u: _fake_render_session(fake),
    )

    result = pipeline._profile_transfer_path(
        "https://host.test/other",
        query=None,
        k=None,
        t_start=0.0,
    )
    assert result is None


def test_transfer_survives_save_exception(monkeypatch, caplog, make_profile):
    """If save_profile raises during transfer, the transfer still
    returns a valid PipelineResult — persistence failure must not
    mask a successful extraction."""
    from trawl import pipeline
    from trawl.profiles.profile import save_profile

    parent = make_profile(
        "https://host.test/parent",
        selector="div.main",
        char_count=1000,
    )
    save_profile(parent)

    fake = _FakePage(
        {
            "div.main": [
                _FakeElement(
                    "line one\n" + "x" * 1490,
                    "<div><h2>Title</h2><p>line one</p><p>body content here</p></div>",
                )
            ],
        }
    )
    monkeypatch.setattr(
        pipeline.playwright,
        "render_session",
        lambda _u: _fake_render_session(fake),
    )

    # Make save_profile raise to simulate disk-full during copy persist
    from trawl import profiles as profiles_pkg

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(profiles_pkg, "save_profile", boom)

    with caplog.at_level("WARNING", logger="trawl.pipeline"):
        result = pipeline._profile_transfer_path(
            "https://host.test/other",
            query=None,
            k=None,
            t_start=0.0,
        )

    assert result is not None  # transfer succeeded despite save failure
    assert any("save of copy" in rec.message for rec in caplog.records)
    from trawl.profiles.profile import url_hash

    assert result.profile_hash == url_hash("https://host.test/other")
