"""PDF extraction backends used by the R5 comparison benchmark.

The production default remains PyMuPDF. Heavy document parsers are kept
behind lazy imports so installing `trawl` does not pull them into the base
environment.
"""

from __future__ import annotations

import io
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_BACKEND = "pymupdf"
SUPPORTED_BACKENDS = ("pymupdf", "markitdown", "unstructured", "docling", "mineru")


@dataclass
class PdfExtraction:
    backend: str
    markdown: str
    tables: list[dict[str, Any]] = field(default_factory=list)
    elapsed_ms: int = 0
    error: str | None = None


def extract_pdf_bytes(content: bytes, *, backend: str = DEFAULT_BACKEND) -> PdfExtraction:
    """Extract Markdown-like text and optional structured tables from PDF bytes."""
    normalized = backend.strip().lower()
    started = time.monotonic()
    try:
        if normalized == "pymupdf":
            return _extract_with_pymupdf(content, started)
        if normalized == "markitdown":
            return _extract_with_markitdown(content, started)
        if normalized == "unstructured":
            return _extract_with_unstructured(content, started)
        if normalized == "docling":
            return _extract_with_docling(content, started)
        if normalized == "mineru":
            return _error(normalized, started, "MinerU backend is not implemented in this spike")
        return _error(
            normalized,
            started,
            f"unknown PDF backend: {backend}; expected one of {', '.join(SUPPORTED_BACKENDS)}",
        )
    except Exception as exc:  # pragma: no cover - optional backend runtime behavior
        return _error(normalized, started, f"{type(exc).__name__}: {exc}")


def _extract_with_pymupdf(content: bytes, started: float) -> PdfExtraction:
    try:
        import pymupdf  # noqa: PLC0415
    except ImportError:
        return _error("pymupdf", started, "pymupdf not installed (pip install pymupdf)")

    doc = pymupdf.open(stream=content, filetype="pdf")
    pages: list[str] = []
    tables: list[dict[str, Any]] = []
    try:
        for page_number, page in enumerate(doc, start=1):
            pages.append((page.get_text("text") or "").strip())
            tables.extend(_extract_pymupdf_tables(page, page_number))
    finally:
        doc.close()

    markdown = "\n\n".join(page for page in pages if page)
    return PdfExtraction(
        backend="pymupdf",
        markdown=markdown,
        tables=tables,
        elapsed_ms=_elapsed_ms(started),
    )


def _extract_pymupdf_tables(page: Any, page_number: int) -> list[dict[str, Any]]:
    find_tables = getattr(page, "find_tables", None)
    if find_tables is None:
        return []
    try:
        found = find_tables()
    except Exception:
        return []
    table_objects = getattr(found, "tables", found)
    tables: list[dict[str, Any]] = []
    for index, table in enumerate(table_objects or []):
        extract = getattr(table, "extract", None)
        if extract is None:
            continue
        try:
            rows = extract()
        except Exception:
            continue
        normalized_rows = _normalize_rows(rows)
        if normalized_rows:
            tables.append({"page": page_number, "index": index, "rows": normalized_rows})
    return tables


def _extract_with_markitdown(content: bytes, started: float) -> PdfExtraction:
    try:
        from markitdown import MarkItDown  # noqa: PLC0415
    except ImportError:
        return _error("markitdown", started, "markitdown is not installed")

    converter = MarkItDown()
    result = converter.convert_stream(io.BytesIO(content), file_extension=".pdf")
    markdown = getattr(result, "text_content", "") or ""
    return PdfExtraction(
        backend="markitdown", markdown=markdown.strip(), elapsed_ms=_elapsed_ms(started)
    )


def _extract_with_unstructured(content: bytes, started: float) -> PdfExtraction:
    try:
        from unstructured.partition.pdf import partition_pdf  # noqa: PLC0415
    except ImportError:
        return _error("unstructured", started, "unstructured is not installed")

    with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
        tmp.write(content)
        tmp.flush()
        elements = partition_pdf(filename=tmp.name)
    markdown = "\n\n".join(str(element).strip() for element in elements if str(element).strip())
    return PdfExtraction(backend="unstructured", markdown=markdown, elapsed_ms=_elapsed_ms(started))


def _extract_with_docling(content: bytes, started: float) -> PdfExtraction:
    try:
        from docling.document_converter import DocumentConverter  # noqa: PLC0415
    except ImportError:
        return _error("docling", started, "docling is not installed")

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    try:
        converted = DocumentConverter().convert(str(tmp_path))
        document = converted.document
        if hasattr(document, "export_to_markdown"):
            markdown = document.export_to_markdown()
        else:
            markdown = str(document)
    finally:
        tmp_path.unlink(missing_ok=True)
    return PdfExtraction(
        backend="docling", markdown=markdown.strip(), elapsed_ms=_elapsed_ms(started)
    )


def _normalize_rows(rows: Any) -> list[list[str]]:
    normalized: list[list[str]] = []
    for row in rows or []:
        if row is None:
            continue
        normalized.append(["" if cell is None else str(cell) for cell in row])
    return normalized


def _error(backend: str, started: float, message: str) -> PdfExtraction:
    return PdfExtraction(
        backend=backend, markdown="", elapsed_ms=_elapsed_ms(started), error=message
    )


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
