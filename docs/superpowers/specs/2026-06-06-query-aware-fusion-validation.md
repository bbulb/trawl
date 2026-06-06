# Query-aware RRF fusion (S3) — validation outcome (2026-06-06)

Roadmap item **S3** in
`docs/superpowers/plans/2026-06-05-trawl-improvement-roadmap.md`
("query-type aware fusion 가중", R3 remainder).

## Finding: S3 was already implemented

The roadmap framed S3 as unbuilt ("R3 절반 완료, hybrid only") and
proposed implementing rule-based query classification + identifier
BM25 up-weighting behind a `TRAWL_HYBRID_QUERY_WEIGHTS` toggle. That
premise is stale — the feature shipped in **`b81d57b`**
("feat(retrieval): add query-aware sparse fusion", 2026-04-27, *before*
the roadmap was written) and has been live since hybrid went default-on
(#58, 2026-05-19):

- `retrieval._classify_query` — regex, no LLM → `identifier` / `concept`
- `retrieval._fusion_weights` — identifier `{dense:0.6, bm25:3.0,
  bge_m3_sparse:3.0}`, concept `{dense:1.2, bm25:0.8, ...}`
- `retrieval._weighted_rrf_fuse` — per-ranker weighted RRF
- Unit tests in `tests/test_retrieval_hybrid.py`
  (`test_identifier_query_uses_query_aware_weights`,
  `test_concept_query_keeps_dense_weight_highest`)

Same stale-roadmap pattern as R1/R2/R4/C7. The genuinely missing piece
was the isolated A/B the S3 gate calls for — never run because the
feature was assumed unbuilt.

## The missing measurement (this spike)

`benchmarks/query_aware_fusion_ab.py`: for each of the 6
reader_comparison cases (3 identifier, 3 concept — verified there is
real identifier coverage, so the A/B is not vacuous), fetch end-to-end
twice flipping ONLY `_fusion_weights` between the shipped query-aware
values and all-1.0 (equal weight) via monkeypatch. Embeddings come from
the shared cache, so the two runs differ only in fusion ordering.
Graded metric (per the C6 lesson that coding pass/fail is too blunt for
a ranking-weight change): top-1 chunk identity + the rank at which each
expected fact first appears. Measured both rerank-on (end-to-end, what
ships) and rerank-off (pure fusion effect).

### Results

| mode | net facts-found Δ (weighted−equal) | top1 changed | identifier-only |
|---|---|---|---|
| rerank ON (end-to-end) | **+0** | 1/6 (python_asyncio, marginal: task_object rank 1→0) | net +0, 1/3 |
| rerank OFF (pure fusion) | **+0** | 1/6 (wiki_llm, a *concept* case, mixed: transformer 4 vs 2) | net +0, **0/3** |

Decisive observation: at the pure-fusion layer the identifier cases —
the queries the up-weighting is designed to help — are **identical**
between weighted and equal-weight RRF (0/3 top1 change, same fact
ranks). When BM25 and dense already rank the same chunk at the top
(true on these focused docs pages), the 5× weight swing cannot reorder
them. The only divergence is one concept case where down-weighting
BM25 ranked the primary fact slightly *lower* (4 vs 2). End-to-end the
reranker reorders the candidate window and masks even that.

## Decision: KEEP as-is, document as validated-neutral

- The S3 gate ("coding net assertion delta ≥ +1") is **not met** — the
  weighting is end-to-end neutral (+0), inert on its identifier
  targets, rerank-dominated. So this is not an "adopt?" decision; the
  feature already shipped and passes parity 15/15 + coding 24/24
  continuously.
- **No toggle added.** A default-on `TRAWL_HYBRID_QUERY_WEIGHTS` is
  unrequested configurability (CLAUDE.md) with no measured rollback
  need — the weighting causes no end-to-end regression.
- **No simplification to equal-weight RRF.** Removing it would touch
  the load-bearing retrieval path (CLAUDE.md guardrail) for zero
  measured end-to-end benefit, with its own regression risk. It is
  tested, harmless, and the mechanism is sound — it simply doesn't move
  the needle on this corpus because the rankers concur and rerank
  dominates.
- **No weight tuning.** The magnitude is not the problem; "rankers
  concur + rerank dominates" is structural to this single-URL,
  small-pool, rerank-on pipeline. Tuning the 5× swing won't change it.

## Reconciling with the cited rationale (arXiv 2604.01733)

The roadmap cited arXiv 2604.01733 ("From BM25 to Corrective RAG") for
a BM25 advantage on exact-match / identifier queries — and this spike
finds BM25 up-weighting *inert* on exactly those queries. Not a
contradiction: that paper measures the fusion/retrieval stage with **no
cross-encoder rerank**, on **text+table financial documents** where
dense and lexical rankings genuinely disagree. trawl's pipeline differs
on both axes — a bge-reranker-v2-m3 cross-encoder reorders the
candidate window after fusion, and single-page pools are small enough
that BM25 and dense already concur at the top. Both erase the
fusion-stage BM25 advantage the paper reports. The paper's finding
holds for its setting; it just doesn't transfer to trawl's
rerank-on, small-pool surface.

S3 is therefore **closed as already-implemented + validated-neutral**.
Do not re-spike absent a new signal — e.g. a future corpus of large
pages where dense and BM25 genuinely disagree at the top (observable as
weighted≠equal divergence in this harness), or a decision to drop the
reranker.

## Reproduce

```
mamba run -n trawl python benchmarks/query_aware_fusion_ab.py            # rerank on
mamba run -n trawl python benchmarks/query_aware_fusion_ab.py --no-rerank
```
Requires the bge-m3 endpoint (:8081); reranker (:8083) optional.
