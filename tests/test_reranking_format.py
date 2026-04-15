"""Unit tests for reranker document-string assembly.

We test the pure string-building branch only -- no network. The real
rerank() call goes over HTTP; these tests target a refactored private
helper `_build_documents(scored, page_title, include_title)`.
"""

from trawl.chunking import Chunk
from trawl.retrieval import ScoredChunk
from trawl.reranking import _build_documents


def _sc(text, heading_path=None):
    return ScoredChunk(
        chunk=Chunk(text=text, heading_path=heading_path or []),
        score=0.0,
    )


def test_title_and_heading():
    docs = _build_documents(
        [_sc("body text", ["Top", "Sub"])],
        page_title="The Page",
        include_title=True,
    )
    assert docs == ["Title: The Page\nSection: Top > Sub\n\nbody text"]


def test_title_only():
    docs = _build_documents(
        [_sc("body text", [])],
        page_title="The Page",
        include_title=True,
    )
    assert docs == ["Title: The Page\n\nbody text"]


def test_heading_only():
    docs = _build_documents(
        [_sc("body text", ["Top"])],
        page_title="",
        include_title=True,
    )
    assert docs == ["Top\n\nbody text"]


def test_neither():
    docs = _build_documents(
        [_sc("body text", [])],
        page_title="",
        include_title=True,
    )
    assert docs == ["body text"]


def test_include_title_disabled_drops_title_keeps_heading():
    docs = _build_documents(
        [_sc("body text", ["Top"])],
        page_title="The Page",
        include_title=False,
    )
    assert docs == ["Top\n\nbody text"]


def test_include_title_disabled_without_heading():
    docs = _build_documents(
        [_sc("body text", [])],
        page_title="The Page",
        include_title=False,
    )
    assert docs == ["body text"]


def test_embed_text_preferred_over_text():
    # When chunk has embed_text set, that is what is fed to the reranker
    # (mirrors current behaviour in reranking.py).
    c = Chunk(text="short", heading_path=[], embed_text="longer embed text")
    docs = _build_documents(
        [ScoredChunk(chunk=c, score=0.0)],
        page_title="T",
        include_title=True,
    )
    assert docs == ["Title: T\n\nlonger embed text"]
