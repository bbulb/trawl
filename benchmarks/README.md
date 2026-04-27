# Benchmarks

## Reader comparison

`reader_comparison.py` compares query-based reader results across providers.
It records latency, estimated returned tokens, fact Recall@k, MRR@k,
`answer_grounding_hit`, total trawl chunks when available, and failure phase.

Run the default stable case set:

```bash
python benchmarks/reader_comparison.py
```

Run a smoke subset:

```bash
python benchmarks/reader_comparison.py --provider trafilatura --limit 1
```

Select providers explicitly:

```bash
python benchmarks/reader_comparison.py --provider trawl --provider jina --limit 2
```

Results are written under `benchmarks/results/reader-comparison/<timestamp>/`:

- `results.jsonl` — one provider/case result per line
- `summary.csv` — compact tabular metrics
- `report.md` — provider-level summary

Optional provider notes:

- `jina` uses `https://r.jina.ai/<url>` and honors `JINA_API_KEY` when set.
- `trafilatura` runs when the local Python package is installed.
- `firecrawl` is recorded as skipped unless `FIRECRAWL_API_KEY` is set; the
  adapter is intentionally not wired in this first R1 pass.
- `crawl4ai` is recorded as skipped unless the Python package is importable; the
  adapter is intentionally not wired in this first R1 pass.

WCXB remains the extraction-F1 benchmark in `benchmarks/wcxb/`; reader comparison
is for URL/query/fact retrieval parity.

## PDF backend comparison

`pdf_backend_comparison.py` compares PDF extraction backends on the same PDF
bytes and records text fact recall plus structured table hits when a backend
can expose table rows separately from flattened Markdown.

Run the default case set with the default backend list:

```bash
python benchmarks/pdf_backend_comparison.py
```

Run only the production baseline:

```bash
python benchmarks/pdf_backend_comparison.py --backend pymupdf --limit 1
```

Install optional heavy backends when you want to compare them locally:

```bash
pip install -e '.[pdf-backends]'
```

Results are written under `benchmarks/results/pdf-backends/<timestamp>/`:

- `results.jsonl` — one backend/case result per line
- `summary.csv` — compact tabular metrics
- `report.md` — backend-level summary

PyMuPDF is the production default. MarkItDown, Unstructured, and Docling are
lazy-imported optional backends; MinerU is listed as an explicit skipped spike
target until a lightweight local adapter is chosen.
