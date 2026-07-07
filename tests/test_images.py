"""Pure-function tests for image-dominant page detection."""

from __future__ import annotations

from trawl import pipeline
from trawl.images import scan_content_images


def test_image_heavy_product_page_detects_content_images():
    html = """
    <html><body>
      <nav>
        <img src="/logo.png">
        <img ec-data-src="/nav-banner.jpg">
      </nav>
      <main>
        <img src="" ec-data-src="/products/01.jpg">
        <img src="" ec-data-src="/products/02.jpg">
        <img src="" ec-data-src="/products/03.jpg">
        <img src="" ec-data-src="/products/04.jpg">
        <img src="" ec-data-src="/products/05.jpg">
        <img src="" ec-data-src="/products/06.jpg">
        <img src="" ec-data-src="/products/07.jpg">
        <img src="" ec-data-src="/products/08.jpg">
        <img src="" ec-data-src="/products/09.jpg">
        <img src="" ec-data-src="/products/10.jpg">
      </main>
    </body></html>
    """
    dominant, urls = scan_content_images(html, 1200, "https://shop.example/item/123")

    assert dominant is True
    assert urls == [
        "https://shop.example/products/01.jpg",
        "https://shop.example/products/02.jpg",
        "https://shop.example/products/03.jpg",
        "https://shop.example/products/04.jpg",
        "https://shop.example/products/05.jpg",
        "https://shop.example/products/06.jpg",
        "https://shop.example/products/07.jpg",
        "https://shop.example/products/08.jpg",
        "https://shop.example/products/09.jpg",
        "https://shop.example/products/10.jpg",
    ]


def test_text_heavy_article_returns_no_images():
    html = """
    <article>
      <img src="/a.jpg">
      <img src="/b.jpg">
      <img src="/c.jpg">
    </article>
    """
    assert scan_content_images(html, 6000, "https://example.com/post") == (False, [])


def test_lazy_load_attr_priority_uses_first_non_empty_value():
    html = """
    <main>
      <img src="" ec-data-src="/picked.jpg" data-original="/later.jpg">
      <img src="/two.jpg">
      <img src="/three.jpg">
      <img src="/four.jpg">
      <img src="/five.jpg">
    </main>
    """
    dominant, urls = scan_content_images(html, 500, "https://example.com/base/")

    assert dominant is True
    assert urls[0] == "https://example.com/picked.jpg"


def test_data_svg_and_tiny_images_are_skipped():
    html = """
    <main>
      <img src="data:image/png;base64,abc">
      <img src="/icon.svg">
      <img src="/spinner.gif">
      <img src="/favicon.ico">
      <img src="/small-width.jpg" width="99">
      <img src="/small-height.jpg" height="20">
      <img src="/one.jpg">
      <img src="/two.jpg">
      <img src="/three.jpg">
      <img src="/four.jpg">
      <img src="/five.jpg">
    </main>
    """
    dominant, urls = scan_content_images(html, 250, "https://example.com/page")

    assert dominant is True
    assert urls == [
        "https://example.com/one.jpg",
        "https://example.com/two.jpg",
        "https://example.com/three.jpg",
        "https://example.com/four.jpg",
        "https://example.com/five.jpg",
    ]


def test_duplicate_urls_are_deduped():
    html = """
    <main>
      <img src="/same.jpg">
      <img src="/same.jpg">
      <img src="/two.jpg">
      <img src="/three.jpg">
      <img src="/four.jpg">
      <img src="/five.jpg">
    </main>
    """
    dominant, urls = scan_content_images(html, 300, "https://example.com/page")

    assert dominant is True
    assert urls == [
        "https://example.com/same.jpg",
        "https://example.com/two.jpg",
        "https://example.com/three.jpg",
        "https://example.com/four.jpg",
        "https://example.com/five.jpg",
    ]


def test_kill_switch_disables_scan(monkeypatch):
    monkeypatch.setenv("TRAWL_IMAGE_SCAN", "0")
    html = "<main>" + "".join(f'<img src="/{i}.jpg">' for i in range(5)) + "</main>"

    assert scan_content_images(html, 100, "https://example.com/") == (False, [])


def test_malformed_html_does_not_raise():
    assert scan_content_images("<html><body><img src='/x.jpg'", 10, "https://example.com") == (
        False,
        [],
    )


def test_pipeline_result_to_dict_includes_content_images():
    r = pipeline.PipelineResult(
        url="https://example.com",
        query="q",
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
        content_images=["https://example.com/product.jpg"],
    )

    assert pipeline.to_dict(r)["content_images"] == ["https://example.com/product.jpg"]
