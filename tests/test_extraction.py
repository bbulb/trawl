from __future__ import annotations

from trawl import chunking, extraction, pipeline


def test_html_to_markdown_scores_candidates_instead_of_picking_longest(monkeypatch):
    html = "<html><body><main><h1>Install</h1><pre><code>pip install trawl</code></pre></main></body></html>"

    monkeypatch.setattr(
        extraction,
        "_safe_trafilatura",
        lambda _html, **_kwargs: "# Install\n\n```bash\npip install trawl\n```",
    )
    monkeypatch.setattr(
        extraction,
        "_bs_fallback",
        lambda _html: " ".join(["cookie newsletter menu subscribe"] * 80),
    )

    out = extraction.extract_html(html, query="pip install trawl")

    assert out.extractor.startswith("trafilatura")
    assert "pip install trawl" in out.markdown
    assert "cookie newsletter" not in out.markdown


def test_chunk_metadata_carries_extractor_provenance_and_char_span():
    chunks = chunking.chunk_markdown(
        "# Install\n\nInstall with `pip install trawl`.\n\nThen run trawl-mcp.",
        extractor="trafilatura-recall",
        source_url="https://example.com/docs",
        source_selector="main",
        source_xpath="/html/body/main",
    )

    assert chunks
    chunk = chunks[0]
    assert chunk.extractor == "trafilatura-recall"
    assert chunk.source_url == "https://example.com/docs"
    assert chunk.source_selector == "main"
    assert chunk.source_xpath == "/html/body/main"
    assert chunk.heading_path == ["Install"]
    assert chunk.char_span == (11, 11 + len(chunk.text))

    as_dict = pipeline._chunk_to_dict(chunk, score=0.42, title="Docs")
    assert as_dict["extractor"] == "trafilatura-recall"
    assert as_dict["source_url"] == "https://example.com/docs"
    assert as_dict["source_selector"] == "main"
    assert as_dict["source_xpath"] == "/html/body/main"
    assert as_dict["heading_path"] == ["Install"]
    assert as_dict["title"] == "Docs"
    assert as_dict["char_span"] == [11, 11 + len(chunk.text)]
