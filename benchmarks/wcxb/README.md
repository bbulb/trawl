# WCXB extraction benchmark

One-shot external benchmark of `trawl.extraction.html_to_markdown` vs a
same-environment Trafilatura baseline on the WCXB dev split (1,497 pages,
7 page types, 1,613 domains, CC-BY-4.0).

Full design: [`../../docs/superpowers/specs/2026-04-14-wcxb-benchmark-design.md`](../../docs/superpowers/specs/2026-04-14-wcxb-benchmark-design.md).

## Run

```bash
# 1. Download the snapshot (~one-time, uses pinned manifest.json)
mamba run -n trawl python benchmarks/wcxb/fetch.py

# 2. Run the benchmark
mamba run -n trawl python benchmarks/wcxb/run.py
```

Results land under `benchmarks/results/wcxb_<timestamp>/`:
- `raw.json` — per-page F1, precision, recall, time, output length, errors
- `report.md` — overall + per-type summary, top wins/losses, error counts

Useful flags:
- `--limit 50` smoke-test subset
- `--type article` restrict to one page type (7 types: article, forum, product,
  collection, listing, documentation, service)
- `--no-baseline` trawl only (faster, no comparison)

## Layout

- `fetch.py` — downloads files listed in `manifest.json` to `data/dev/`
  (gitignored) and verifies SHA-256.
- `run.py` — orchestrator + argparse CLI (evaluate_page, evaluate_page_with_baseline, run_all).
- `aggregate.py` — pure-function aggregation + report rendering.
- `evaluate.py` — vendored WCXB word-F1 evaluator (CC-BY-4.0, see `ATTRIBUTION.md`).
- `manifest.json` — pinned per-file SHA-256 for the snapshot. Regenerate with
  `python fetch.py --refresh-manifest`.
