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
