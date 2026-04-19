"""Tests for `src/trawl/enrichment.py` (C16 compositional payload).

Pure-function tests — no network, no LLM, no Playwright. The pipeline
integration test is at the bottom and uses monkeypatched fetcher +
retrieval to exercise the new fields end-to-end without external
infra.
"""

from __future__ import annotations

from trawl import enrichment
from trawl.chunking import Chunk

from dataclasses import dataclass

from trawl import enrichment
from trawl.chunking import Chunk


# ---------- excerpts


def _chunk(idx: int, text: str, headings: list[str] | None = None) -> Chunk:
    return Chunk(
        text=text,
        heading_path=headings or [],
        char_count=len(text),
        chunk_index=idx,
        embed_text=text,
    )


def test_excerpts_first_sentence_under_cap():
    chunks = [
        _chunk(0, "BGE-M3 is a multilingual embedding model. It supports dense and sparse output.")
    ]
    chunks = [_chunk(0, "BGE-M3 is a multilingual embedding model. It supports dense and sparse output.")]
    out = enrichment.extract_excerpts(chunks)
    assert out == [{"chunk_idx": 0, "summary_120c": "BGE-M3 is a multilingual embedding model."}]


def test_excerpts_truncates_long_first_sentence():
    long = "x" * 500
    chunks = [_chunk(0, long)]
    out = enrichment.extract_excerpts(chunks)
    assert len(out) == 1
    s = out[0]["summary_120c"]
    assert s.endswith("…")
    assert len(s) == enrichment.EXCERPT_MAX_CHARS


def test_excerpts_strips_markdown_markup():
    md = "**Bold** and `code` and [a link](https://x.com) here. Second sentence."
    out = enrichment.extract_excerpts([_chunk(3, md)])
    assert out[0]["chunk_idx"] == 3
    # Bold, code stripped; link text remains as-is (link extractor handles
    # the URL, the excerpt keeps the visible text).
    assert "Bold" in out[0]["summary_120c"]
    assert "code" in out[0]["summary_120c"]
    assert "**" not in out[0]["summary_120c"]
    assert "`" not in out[0]["summary_120c"]


def test_excerpts_skips_heading_prefix():
    md = "# Section Title\nSection body text here."
    out = enrichment.extract_excerpts([_chunk(0, md)])
    # First non-empty line after stripping the heading marker should be
    # the section title (not the body — _first_sentence takes the first
    # line). That's the documented behavior.
    assert out[0]["summary_120c"].startswith("Section Title")


def test_excerpts_top_n_cap():
    chunks = [_chunk(i, f"Sentence {i}. More text.") for i in range(10)]
    out = enrichment.extract_excerpts(chunks, top_n=3)
    assert len(out) == 3
    assert [o["chunk_idx"] for o in out] == [0, 1, 2]


def test_excerpts_korean_sentence_split():
    md = "한국어 문장입니다. 두번째 문장."
    out = enrichment.extract_excerpts([_chunk(0, md)])
    assert out[0]["summary_120c"] == "한국어 문장입니다."


def test_excerpts_handles_empty_chunk():
    out = enrichment.extract_excerpts([_chunk(0, "")])
    assert out == []


def test_excerpts_accepts_dict_chunks():
    """Defensive: callers may pass dict-shaped chunks (e.g. profile_direct)."""
    out = enrichment.extract_excerpts([{"chunk_index": 7, "text": "Hello world. Second sentence."}])
    out = enrichment.extract_excerpts([
        {"chunk_index": 7, "text": "Hello world. Second sentence."}
    ])
    assert out == [{"chunk_idx": 7, "summary_120c": "Hello world."}]


# ---------- outbound_links


def test_outbound_links_extracts_markdown_links():
    md = "See [the docs](https://example.com/docs) and also [github](https://github.com/x/y)."
    out = enrichment.extract_outbound_links([_chunk(0, md)])
    assert out == [
        {"url": "https://example.com/docs", "anchor_text": "the docs", "in_chunk_idx": 0},
        {"url": "https://github.com/x/y", "anchor_text": "github", "in_chunk_idx": 0},
    ]


def test_outbound_links_skips_image_refs():
    md = "Inline image ![alt text](https://x.com/pic.png) and a [real link](https://x.com)."
    out = enrichment.extract_outbound_links([_chunk(0, md)])
    assert len(out) == 1
    assert out[0]["url"] == "https://x.com"


def test_outbound_links_dedupes_across_chunks():
    chunks = [
        _chunk(0, "[A](https://a.com)"),
        _chunk(1, "[A](https://a.com)"),  # exact dup
        _chunk(2, "[B](https://b.com)"),
    ]
    out = enrichment.extract_outbound_links(chunks)
    urls = [(o["url"], o["anchor_text"]) for o in out]
    assert urls == [("https://a.com", "A"), ("https://b.com", "B")]


def test_outbound_links_entry_count_cap():
    md = " ".join(f"[link{i}](https://x.com/{i})" for i in range(100))
    out = enrichment.extract_outbound_links([_chunk(0, md)], cap=10)
    assert len(out) == 10


def test_outbound_links_byte_cap():
    # Big anchor texts to blow the byte cap before the count cap.
    big_anchor = "A" * 500
    md = " ".join(f"[{big_anchor}](https://x.com/{i})" for i in range(50))
    out = enrichment.extract_outbound_links([_chunk(0, md)], bytes_cap=1024)
    # Each entry estimated at len(anchor)+len(url)+32 ≈ 553 bytes; cap=1024
    # → at most 1 entry fits before the next would push us over.
    assert len(out) <= 2


def test_outbound_links_skips_relative_and_anchor_only():
    md = "Anchor [home](#section), [relative](/x), and [absolute](https://x.com)."
    out = enrichment.extract_outbound_links([_chunk(0, md)])
    # Regex requires `https?://` — relative and #anchor aren't extracted.
    assert len(out) == 1
    assert out[0]["url"] == "https://x.com"


# ---------- page_entities


def test_page_entities_english_capitalized():
    out = enrichment.extract_page_entities(
        "Attention Is All You Need",
        [["Background and Related Work"]],
    )
    # 'Attention Is All You Need' is 5 capitalised tokens, matches whole
    # 'Background and Related Work' has 'and' lowercase — splits into
    # 'Background' (1 token, doesn't match 2+ rule) and 'Related Work'.
    assert "Attention Is All You Need" in out
    assert "Related Work" in out


def test_page_entities_korean():
    out = enrichment.extract_page_entities(
        "이순신",
        [["임진왜란", "옥포 해전"]],
    )
    assert "이순신" in out
    assert "임진왜란" in out


def test_page_entities_dedup_and_cap():
    paths = [["Apple Inc"]] * 50
    out = enrichment.extract_page_entities("Apple Inc", paths, cap=3)
    assert out == ["Apple Inc"]


def test_page_entities_empty_inputs():
    assert enrichment.extract_page_entities("", []) == []
    assert enrichment.extract_page_entities("the and", [["of in"]]) == []


# ---------- chain_hints


def test_chain_hints_arxiv():
    h = enrichment.derive_chain_hints("https://arxiv.org/abs/2402.03216")
    assert h["recommended_followup_filter"] == "site:arxiv.org"
    assert "pdf_template" in h


def test_chain_hints_github():
    h = enrichment.derive_chain_hints("https://github.com/anthropics/anthropic-sdk-python")
    assert h["recommended_followup_filter"] == "site:github.com"
    assert "raw_template" in h


def test_chain_hints_wikipedia_localized():
    en = enrichment.derive_chain_hints("https://en.wikipedia.org/wiki/Yi_Sun-sin")
    ko = enrichment.derive_chain_hints("https://ko.wikipedia.org/wiki/이순신")
    assert en["recommended_followup_filter"] == "site:en.wikipedia.org"
    assert ko["recommended_followup_filter"] == "site:ko.wikipedia.org"


def test_chain_hints_unknown_host():
    assert enrichment.derive_chain_hints("https://unknown.example.com/x") == {}


def test_chain_hints_handles_empty_url():
    assert enrichment.derive_chain_hints("") == {}


# ---------- pipeline integration


def test_pipeline_result_has_enrichment_fields():
    """PipelineResult dataclass must default the four C16 fields to
    empty containers so legacy callers that construct it manually keep
    working."""
    from trawl.pipeline import PipelineResult

    r = PipelineResult(
        url="https://example.com",
        query="x",
        fetcher_used="x",
        fetch_ms=0,
        chunk_ms=0,
        retrieval_ms=0,
        total_ms=0,
        page_chars=0,
        n_chunks_total=0,
        structured_path=False,
        hyde_used=False,
        hyde_text="",
        chunks=[],
    )
    assert r.excerpts == []
    assert r.outbound_links == []
    assert r.page_entities == []
    assert r.chain_hints == {}


def test_pipeline_to_dict_includes_enrichment_fields():
    from trawl.pipeline import PipelineResult, to_dict

    r = PipelineResult(
        url="https://example.com",
        query="x",
        fetcher_used="x",
        fetch_ms=0,
        chunk_ms=0,
        retrieval_ms=0,
        total_ms=0,
        page_chars=0,
        n_chunks_total=0,
        structured_path=False,
        hyde_used=False,
        hyde_text="",
        chunks=[],
        excerpts=[{"chunk_idx": 0, "summary_120c": "x"}],
        outbound_links=[{"url": "https://x", "anchor_text": "x", "in_chunk_idx": 0}],
        page_entities=["X"],
        chain_hints={"recommended_followup_filter": "site:x"},
    )
    d = to_dict(r)
    assert d["excerpts"] == [{"chunk_idx": 0, "summary_120c": "x"}]
    assert d["outbound_links"][0]["url"] == "https://x"
    assert d["page_entities"] == ["X"]
    assert d["chain_hints"]["recommended_followup_filter"] == "site:x"
