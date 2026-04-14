"""Unit tests for Stack Exchange fetcher -- URL parsing and API fetch."""

from __future__ import annotations


def test_parse_stackoverflow_question():
    from trawl.fetchers.stackexchange import _parse_se_url

    r = _parse_se_url("https://stackoverflow.com/questions/419163/what-does-if-name-main-do")
    assert r == ("stackoverflow", "419163")


def test_parse_stackoverflow_short():
    from trawl.fetchers.stackexchange import _parse_se_url

    r = _parse_se_url("https://stackoverflow.com/q/419163")
    assert r == ("stackoverflow", "419163")


def test_parse_superuser():
    from trawl.fetchers.stackexchange import _parse_se_url

    r = _parse_se_url("https://superuser.com/questions/123456/some-title")
    assert r == ("superuser", "123456")


def test_parse_serverfault():
    from trawl.fetchers.stackexchange import _parse_se_url

    r = _parse_se_url("https://serverfault.com/questions/99999/title")
    assert r == ("serverfault", "99999")


def test_parse_askubuntu():
    from trawl.fetchers.stackexchange import _parse_se_url

    r = _parse_se_url("https://askubuntu.com/questions/55555/title")
    assert r == ("askubuntu", "55555")


def test_parse_sub_stackexchange():
    from trawl.fetchers.stackexchange import _parse_se_url

    r = _parse_se_url("https://unix.stackexchange.com/questions/11111/title")
    assert r == ("unix", "11111")


def test_parse_answer_permalink():
    from trawl.fetchers.stackexchange import _parse_se_url

    r = _parse_se_url("https://stackoverflow.com/a/419163")
    assert r == ("stackoverflow", "419163")


def test_parse_non_se_url():
    from trawl.fetchers.stackexchange import _parse_se_url

    assert _parse_se_url("https://www.example.com/page") is None


def test_parse_se_non_question():
    from trawl.fetchers.stackexchange import _parse_se_url

    assert _parse_se_url("https://stackoverflow.com/users/12345") is None


def test_fetch_stackoverflow_question():
    """Integration test: fetch the __name__ == __main__ question via API."""
    from trawl.fetchers.stackexchange import fetch

    result = fetch("https://stackoverflow.com/questions/419163/what-does-if-name-main-do")
    assert result.ok
    assert result.fetcher == "stackexchange"
    assert len(result.markdown) > 500
    assert "__name__" in result.markdown or "__main__" in result.markdown


def test_fetch_invalid_url():
    from trawl.fetchers.stackexchange import fetch

    result = fetch("https://www.example.com/page")
    assert not result.ok
    assert "invalid" in (result.error or "").lower()
