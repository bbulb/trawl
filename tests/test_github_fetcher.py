"""Unit tests for GitHub fetcher -- URL parsing and API fetch."""

from __future__ import annotations


def test_parse_repo_root():
    from trawl.fetchers.github import _parse_github_url

    r = _parse_github_url("https://github.com/anthropics/claude-code")
    assert r == ("anthropics", "claude-code", "readme", {})


def test_parse_repo_tree():
    from trawl.fetchers.github import _parse_github_url

    r = _parse_github_url("https://github.com/anthropics/claude-code/tree/main")
    assert r == ("anthropics", "claude-code", "readme", {})


def test_parse_issue():
    from trawl.fetchers.github import _parse_github_url

    r = _parse_github_url("https://github.com/anthropics/claude-code/issues/123")
    assert r == ("anthropics", "claude-code", "issue", {"number": "123"})


def test_parse_pull():
    from trawl.fetchers.github import _parse_github_url

    r = _parse_github_url("https://github.com/anthropics/claude-code/pull/456")
    assert r == ("anthropics", "claude-code", "pull", {"number": "456"})


def test_parse_blob():
    from trawl.fetchers.github import _parse_github_url

    r = _parse_github_url("https://github.com/anthropics/claude-code/blob/main/README.md")
    assert r == ("anthropics", "claude-code", "blob", {"ref": "main", "path": "README.md"})


def test_parse_blob_nested():
    from trawl.fetchers.github import _parse_github_url

    r = _parse_github_url("https://github.com/owner/repo/blob/main/src/lib/utils.py")
    assert r == ("owner", "repo", "blob", {"ref": "main", "path": "src/lib/utils.py"})


def test_parse_non_github():
    from trawl.fetchers.github import _parse_github_url

    assert _parse_github_url("https://www.example.com/page") is None


def test_parse_unsupported_path():
    from trawl.fetchers.github import _parse_github_url

    r = _parse_github_url("https://github.com/owner/repo/discussions/789")
    assert r is None


def test_fetch_repo_readme():
    """Integration test: fetch claude-code README via API."""
    from trawl.fetchers.github import fetch

    result = fetch("https://github.com/anthropics/claude-code")
    assert result.ok
    assert result.fetcher == "github"
    assert len(result.markdown) > 500
    assert "claude" in result.markdown.lower() or "Claude" in result.markdown


def test_fetch_invalid_url():
    from trawl.fetchers.github import fetch

    result = fetch("https://www.example.com/page")
    assert not result.ok
    assert "invalid" in (result.error or "").lower()
