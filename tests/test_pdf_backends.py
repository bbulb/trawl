from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

from trawl.fetchers import pdf_backends


class _FakeTable:
    def extract(self):
        return [["Metric", "Value"], ["Revenue", "$10"]]


class _FakePage:
    def get_text(self, mode: str):
        assert mode == "text"
        return "Revenue was $10.\n"

    def find_tables(self):
        return SimpleNamespace(tables=[_FakeTable()])


class _FakeDoc:
    def __iter__(self):
        return iter([_FakePage()])

    def close(self):
        pass


def test_pymupdf_backend_preserves_markdown_and_structured_tables(monkeypatch):
    fake_pymupdf = SimpleNamespace(open=lambda stream, filetype: _FakeDoc())
    monkeypatch.setitem(sys.modules, "pymupdf", fake_pymupdf)

    result = pdf_backends.extract_pdf_bytes(b"%PDF fake", backend="pymupdf")

    assert result.backend == "pymupdf"
    assert result.error is None
    assert result.markdown == "Revenue was $10."
    assert result.tables == [
        {
            "page": 1,
            "index": 0,
            "rows": [["Metric", "Value"], ["Revenue", "$10"]],
        }
    ]


def test_unknown_pdf_backend_returns_error():
    result = pdf_backends.extract_pdf_bytes(b"%PDF fake", backend="not-a-backend")

    assert result.backend == "not-a-backend"
    assert result.markdown == ""
    assert "unknown PDF backend" in result.error


def test_load_pdf_backend_cases_validates_expected_facts(tmp_path: Path):
    from benchmarks import pdf_backend_comparison as pbc

    cases_file = tmp_path / "pdf_cases.yaml"
    cases_file.write_text(
        """
cases:
  - id: bad
    category: manual
    source: https://example.test/manual.pdf
    query: revenue
    expected_facts: []
    failure_class:
      on_fetch_error: fetch
      on_empty_output: extraction
      on_missing_facts: extraction
""",
        encoding="utf-8",
    )

    try:
        pbc.load_cases(cases_file)
    except ValueError as exc:
        assert "expected_facts" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_build_pdf_backend_result_scores_tables_and_facts():
    from benchmarks import pdf_backend_comparison as pbc

    case = {
        "id": "finance_table",
        "category": "finance",
        "source": "https://example.test/report.pdf",
        "query": "revenue",
        "expected_facts": [{"id": "revenue", "any_of": ["Revenue"]}],
        "expected_tables": [{"id": "revenue_table", "any_of": ["Revenue", "$10"]}],
        "failure_class": {"on_empty_output": "extraction", "on_missing_facts": "extraction"},
    }
    extraction = pdf_backends.PdfExtraction(
        backend="pymupdf",
        markdown="Revenue was $10.",
        tables=[{"page": 1, "index": 0, "rows": [["Metric", "Value"], ["Revenue", "$10"]]}],
        elapsed_ms=7,
    )

    result = pbc.build_backend_result(case, extraction)

    assert result["status"] == "ok"
    assert result["answer_grounding_hit"] is True
    assert result["table_hit"] is True
    assert result["n_tables"] == 1
