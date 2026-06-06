# PDF backend comparison (S4 / R5) — outcome (2026-06-06)

Roadmap item **S4** in
`docs/superpowers/plans/2026-06-05-trawl-improvement-roadmap.md`
(R5 from 2026-04-27). Decision: **KEEP PyMuPDF. No structured backend
adopted, no heavy dependency installed.** Staged per the user's choice
— prove the problem with PyMuPDF first, pay for a contender (Docling /
torch) only if PyMuPDF demonstrably fails on representative cases. The
trigger was not met.

## Starting state

`benchmarks/pdf_backend_comparison.py` + `pdf_backends.py` shipped
earlier (PyMuPDF implemented; markitdown/unstructured/docling behind
lazy imports + a `.[pdf-backends]` extra; mineru a stub) but were never
run and had a single case (`arxiv_lora`, `expected_tables: []`). The
harness could not exercise S4's gate ("표 질의에서 structured backend
개선") because it had no table-cell queries, and PyMuPDF already scored
recall 1.0 on the one case.

## Two methodology corrections (vs the harness as written)

1. **Measure the metric trawl actually uses.** The harness scores a
   structured `extraction.tables` field via `table_hit`. trawl's
   production PDF path (`pdf.py`) is PyMuPDF → **markdown** → chunk →
   embed → retrieve and never consumes `tables`. A backend that wins on
   `table_hit` wins on a field trawl ignores; the adoption-relevant
   metric is **markdown fact recall end-to-end through
   `fetch_relevant`**. So the decisive measurement here was run through
   `fetch_relevant`, not the harness's isolated table scorer.
2. **Representative, not reverse-engineered, fixtures.** Curated four
   real paper results tables a trawl user would read, with ground-truth
   table-cell values, plus one deliberately pathological dense matrix —
   and let it fall.

## Measurements

### End-to-end (`fetch_relevant`, PyMuPDF, production path)

| case | table-cell query | result |
|---|---|---|
| Transformer (1706.03762) | big-model BLEU EN-DE / EN-FR | **PASS** — `28.4`, `41.8`, `EN-DE`, `big` all retrieved |
| LoRA (2106.09685) | GPT-3 175B WikiSQL / MNLI | **PASS** — `WikiSQL`, `MNLI` retrieved |
| BERT (1810.04805) | large GLUE MNLI / SST-2 | **PASS** — `MNLI`, `SST`, `GLUE` retrieved |
| BGE-M3 (2402.03216) | MIRACL nDCG@10 Korean / Japanese | **MISS** — language cells not retrieved |

### Extraction-level (harness, PyMuPDF) — all five cases recall 1.0

Notably the BGE-M3 case scores **recall 1.0 at the extraction level**
(its asserted fact "MIRACL" is in the markdown). The end-to-end miss is
therefore **not an extraction-completeness failure** — PyMuPDF's
markdown contains the facts — but a **retrieval-ranking + query-
vocabulary** problem: the 18-language matrix linearizes to header-less
number rows (`Dense+Sparse 70.4 79.6 80.7 ...`), so the per-language
cells carry no semantic anchor to rank for a language query, and the
table uses ISO codes (`ko`/`ja`) while the query says "Korean"/
"Japanese". Swapping the extraction backend changes neither of those.

## Why keep PyMuPDF

- **Representative tables already work end-to-end.** Three real paper
  results tables retrieve their cell values through the current
  PyMuPDF path. The advisor's discriminating test — "name a realistic
  case where PyMuPDF linearizes a table into unretrievable garbage" —
  has no representative answer; only the pathological 18-column matrix
  fails.
- **The one failure is not extraction-fixable.** PyMuPDF's markdown is
  complete (recall 1.0); the BGE-M3 miss is retrieval-ranking +
  query-vocab. A structured backend (Docling) preserving the table
  would still land in the unused `tables` field unless **table-aware
  chunking** is built to make the header↔cell association embeddable —
  a separate, larger scope — and would still not bridge the
  Korean↔`ko` vocabulary gap.
- **Cost not justified.** Docling/MinerU pull torch / model weights;
  markitdown is ≈ PyMuPDF for tables (a vacuous probe). Paying for a
  heavy optional dependency to maybe help a pathological,
  non-representative, downstream-blocked case fails the gate ("개선이
  있어야 채택").

## Durable artifacts

- `benchmarks/pdf_backend_cases.yaml` — four representative table cases
  + one documented pathological matrix, ground-truth cell values. A
  real corpus for any future backend spike.
- `.[pdf-backends]` extra and `pdf_backends.py` stay as-is (lazy,
  optional, unused by default) for future use.

## When to revisit

Re-spike only with a new signal: a representative (non-pathological)
PDF where PyMuPDF's markdown drops table facts end-to-end, **and** a
decision to build table-aware chunking so a structured backend's output
can actually be consumed. Absent both, swapping the extraction backend
cannot move `fetch_relevant`.

## Reproduce

```
mamba run -n trawl python benchmarks/pdf_backend_comparison.py --backend pymupdf
# end-to-end table-cell retrieval check is via fetch_relevant on the
# four paper PDFs above (see the case queries).
```
