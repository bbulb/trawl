# trawl 개선 로드맵 — 2026-05-18

**작성일**: 2026-05-18
**상태**: proposed
**전제**: develop @ `7afbf97` (v0.4.4 back-merged). 4/22 핸드오프 이후 P0 stability foundations (commit `8d77f89`) 실행 완료. 본 로드맵은 남은 P1/P2 plan 4개 + 외부 도입 spike 2개를 단일 실행 큐로 묶는다.

## 목적

`notes/improvement-roadmap-2026-04-27.md` 와 `docs/stability-speed-improvement-report-2026-05-04.md` 의 후속을 정리한다. 각 단계는 별도 `/goal` 호출이며, spike 는 trawl 의 pre-registered gate 규율을 따른다 (design doc → 측정 → 채택/기각).

## 측정 baseline (2026-05-04, cache-controlled, 48 rows)

| 모드 | pass | flipped-fail | p95 retrieval (concept / identifier) |
|---|---:|---:|---:|
| dense | 100% | 0 | **1887 / 1697 ms** |
| hybrid | 100% | 0 | 1925 / 1729 ms (+2%) |
| contextual-auto | 100% | 0 | 2181 / 2264 ms (**+16% / +33%**) |
| contextual-forced | 100% | 0 | 2186 / 2273 ms |

- Warm-repeat 효과: cold 1656ms → warm **65ms (−96%)**.
- trawl vs jina (6 케이스): pass 100% 동률, jina 대비 토큰 **23x 적음** (1022 vs 23510).

## 실행 순서 & 의존성

```
[1. P1 Goal 1: cache 관측 baseline]
       ├──> [2. Spike A: Qwen3-Embedding A/B]   (병렬)
       └──> [3. Spike B: EMBED_CACHE_TTL flip]  (병렬)
              │
              ▼
       [4. P1 Goal 2: MCP browser queue]
              │
              ▼
       [5. P2 Goal 4: cache-controlled retrieval 재측정]
              │
              ▼
       [6. P2 Goal 3: Scrapling trigger + HTTP revalidation]
```

P1 Goal 1 이 모든 측정의 baseline 이라 **가장 먼저**. Spike A·B 는 독립이라 1 완료 후 병렬 진행 가능. P2 Goal 4 는 1·3 이 끝나야 cache-confound 없이 측정 가능.

## 한 화면 요약표

| # | 단계 | 한국어 trigger | 사전 조건 | 핵심 게이트 |
|---|---|---|---|---|
| 1 | P1 Goal 1: cache 관측 | `P1 Goal 1 진행해줘` | — | parity 15/15, query/chunk 누출 0 |
| 2 | Spike A: Qwen3-Embedding | `Qwen3-Embedding swap spike 진행해줘` | GGUF 다운로드 | 5개 게이트 전부 |
| 3 | Spike B: cache TTL flip | `embedding cache default-on spike 진행해줘` | #1 | warm p95 −80% 이상 |
| 4 | P1 Goal 2: MCP queue | `P1 Goal 2 진행해줘` | #1 | passthrough blocking 0 |
| 5 | P2 Goal 4: retrieval 재측정 | `P2 Goal 4 진행해줘` | #1, #3 | contextual gate +20% 재검토 |
| 6 | P2 Goal 3: Scrapling | `P2 Goal 3 진행해줘` | — (마지막) | 기본 경로 회귀 0 |

---

## 단계별 `/goal` 텍스트

### 1. P1 Goal 1 — embedding cache 관측 + reader-comparison provider adapter

```
/goal Implement the next P1 speed/observability slice for trawl using docs/stability-speed-improvement-report-2026-05-04.md and docs/superpowers/plans/2026-05-04-stability-speed-remaining-work.md. Add embedding cache hit/miss metadata through RetrievalResult, PipelineResult, telemetry, and reader-comparison output. Add a warm-repeat benchmark mode that can compare cold vs warm repeated queries with TRAWL_EMBED_CACHE_TTL=86400. Implement mocked Firecrawl and Crawl4AI reader-comparison adapters that skip cleanly when credentials/packages are unavailable. Update README/docs and verify with targeted tests plus `mamba run -n trawl python -m pytest`.
```

- **사전 조건**: 없음.
- **게이트**: parity 15/15 유지, ruff/pytest pass, telemetry 원본 query/chunk text 누출 0.

### 2. Spike A — Qwen3-Embedding-0.6B drop-in A/B

```
/goal Run a pre-registered A/B spike replacing the BGE-M3 dense embedding model with Qwen3-Embedding-0.6B-GGUF on llama-server :8081. First, author the design doc at docs/superpowers/specs/2026-05-18-qwen3-embedding-swap-design.md following the RRF-k spike pattern: hypothesis, alternative model card link (https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF), exact serving command (`llama-server --model qwen3-embedding-0.6b.gguf --embedding --pooling last --port 8081`), env override matrix (TRAWL_EMBED_URL / TRAWL_EMBED_MODEL), and the pre-registered gate table:
  - `python tests/test_pipeline.py` stays 15/15
  - `python tests/test_agent_patterns.py --shard coding` no regression vs baseline
  - reader-comparison net assertion delta >= +1, flipped-to-fail = 0
  - Korean cases (pricing_page_ko, korean_wiki_person, korean_news_ranking) regression = 0
  - retrieval p95 with TRAWL_EMBED_CACHE_TTL=0 within baseline +20%
Then run baseline (BGE-M3) and experiment (Qwen3) measurements through `benchmarks/reader_comparison.py`, parity matrix, and agent_patterns coding shard. Record raw artifacts under benchmarks/results/qwen3-embedding-swap/<ts>/. Decision: adopt only if every gate passes. On adoption ship a single-line default change in env defaults + CHANGELOG note. On rejection commit design doc + outcome note (notes/qwen3-embedding-swap-outcome.md) and revert env. Verify with `mamba run -n trawl python -m pytest` and `mamba run -n trawl python tests/test_pipeline.py`.
```

- **사전 조건**: 별도 llama-server 슬롯에 Qwen3 GGUF 미리 준비. P1 Goal 1 과 병렬 가능.
- **게이트**: 위 5개 항목 전부 통과. 한국어 케이스 한 개라도 회귀면 즉시 기각.

### 3. Spike B — `TRAWL_EMBED_CACHE_TTL` default 0 → 3600

```
/goal Run a pre-registered spike to flip TRAWL_EMBED_CACHE_TTL default from 0 (disabled) to 3600 (1 hour). First, author the design doc at docs/superpowers/specs/2026-05-18-embed-cache-default-on-design.md following the chunk-budget-default-on pattern: motivation (warm-repeat -96% latency in benchmarks/results/reader-comparison/retrieval-modes-cache-2026-05-04/), the one-line src/trawl/embedding_cache.py default change, opt-out env (`TRAWL_EMBED_CACHE_TTL=0`), disk usage bound (TRAWL_EMBED_CACHE_MAX_MB default 512, LRU evict), and the pre-registered gate:
  - `python tests/test_pipeline.py` stays 15/15
  - cold retrieval p95 unchanged (cache miss path untouched)
  - warm retrieval p95 reduction >= 80% vs cold baseline
  - disk usage stays within TRAWL_EMBED_CACHE_MAX_MB cap after warm sweep
  - cache key fields (model, endpoint, prefix version, contextual mode) unchanged so existing caches still hit
Then run cold/warm paired measurement via the warm-repeat mode added in P1 Goal 1 against the 15 parity URLs and the 6 reader-comparison URLs. Record raw artifacts under benchmarks/results/embed-cache-default-on/<ts>/. Decision: flip the default if every gate passes, otherwise keep 0 and ship the design + outcome note (notes/embed-cache-default-on-outcome.md). Update CLAUDE.md "Current status" + CHANGELOG on adoption. Verify with `mamba run -n trawl python -m pytest`.
```

- **사전 조건**: P1 Goal 1 완료 (warm-repeat 측정 도구 필요).
- **게이트**: warm p95 ≥80% 감소 + cold p95 동등 + 디스크 cap 안. 한 줄 default flip.

### 4. P1 Goal 2 — MCP browser queue separation

```
/goal Implement MCP/browser queue separation for trawl using docs/stability-speed-improvement-report-2026-05-04.md and docs/superpowers/plans/2026-05-04-stability-speed-remaining-work.md. Route browser-free fetch_page calls through a general executor while keeping Playwright/profile work on the single browser executor. Add concurrency tests showing passthrough/API/PDF-style calls are not blocked by one slow Playwright/profile call, preserve greenlet/thread safety, update docs, and verify with targeted tests plus `mamba run -n trawl python -m pytest`.
```

- **사전 조건**: P1 Goal 1. Spike A·B 와 무관 → 병렬 가능.
- **게이트**: slow Playwright + 빠른 passthrough 동시성 테스트에서 passthrough p95 baseline 유지, parity 15/15.

### 5. P2 Goal 4 — cache-controlled hybrid/contextual 재측정

```
/goal Implement cache-controlled hybrid/contextual retrieval re-measurement for trawl using docs/stability-speed-improvement-report-2026-05-04.md and docs/superpowers/plans/2026-05-04-stability-speed-remaining-work.md. Add benchmark/report support for dense, hybrid, contextual-auto, and contextual-forced modes; record flipped-to-fail, rank movement, retrieval p50/p95, and token output; do not change defaults unless gates pass; update docs and verify with targeted tests plus `mamba run -n trawl python -m pytest`.
```

- **사전 조건**: P1 Goal 1 + Spike B 완료 (embed cache 켠 상태에서 측정해야 cache-confound 없음).
- **게이트**: contextual-auto p95 +20% gate 통과 시 default-on 후보. 미통과 시 default off 유지 결정 commit.

### 6. P2 Goal 3 — Scrapling fallback trigger + HTTP cache revalidation

```
/goal Implement the P2 fetch recovery slice for trawl using docs/stability-speed-improvement-report-2026-05-04.md and docs/superpowers/plans/2026-05-04-stability-speed-remaining-work.md. Add optional lazy Scrapling fallback behind TRAWL_SCRAPLING_FALLBACK=1, add HTTP cache revalidation fields and mocked 304/200/stale tests, keep default dependencies unchanged, update docs, and verify with targeted tests plus `mamba run -n trawl python -m pytest`.
```

- **사전 조건**: 가장 마지막. Scrapling 은 optional, 기본 의존성 변경 없음.
- **게이트**: 기본 경로 (`TRAWL_SCRAPLING_FALLBACK=0`) parity 15/15, mocked 304/200/stale 케이스 pass.

---

## 일괄 실행 (master /goal)

본 로드맵은 단계별 spike 규율 (게이트 미통과 시 기각/되돌리기) 을 따른다. 정상 워크플로는 위 한국어 trigger 를 사용해 **세션 1개 = /goal 1개 = PR 1개** 패턴으로 진행한다.

자동화가 필요하면 다음 master /goal 을 사용. 경고: 단일 세션에 6개를 묶는 것은 trawl 의 "spike 1 = PR 1" 패턴과 충돌하고 audit trail 이 흐려질 수 있음. 게이트 실패 시 즉시 중단하는 규율은 master 안에서도 유지된다.

```
/goal docs/superpowers/plans/2026-05-18-trawl-improvement-roadmap.md 의 단계 1 → 6 을 의존성 순서대로 모두 실행한다. 각 단계는 단계별로 별도 PR 로 commit 한 뒤 게이트를 검증하고, 통과 시에만 다음 단계로 이동한다. 게이트 실패 시 즉시 중단하고 결과/원인을 보고한다. Spike A (단계 2) 와 Spike B (단계 3) 는 단계 1 완료 후 의존성이 없으므로 병렬 실행 가능 — 단, 같은 세션에서 직렬로 처리해도 무방. 각 단계의 사전 조건과 pre-registered gate 는 로드맵 문서의 해당 절을 따른다.
```

## 새 세션 시작 체크리스트

1. `gh pr list --state open` 으로 미해결 PR 확인.
2. `git log origin/develop --oneline -5` 로 develop HEAD 확인.
3. `mamba run -n trawl python tests/test_pipeline.py` smoke 로 현재 parity 확인.
4. 위 한국어 trigger 또는 master /goal 입력.
5. spike 단계 (2·3·5) 는 측정 시작 전 design doc 을 먼저 commit + `gh pr create` (RRF-k spike 패턴).
6. 완료 후 결과를 `notes/<spike>-outcome.md` 로 commit + CLAUDE.md "Current status" 업데이트.

## 참고 자료

- Stability speed improvement report: `docs/stability-speed-improvement-report-2026-05-04.md`
- 남은 P1/P2 plan 원본: `docs/superpowers/plans/2026-05-04-stability-speed-remaining-work.md`
- 4/27 개선 로드맵 (R1-R7): `notes/improvement-roadmap-2026-04-27.md`
- Reader comparison 측정 결과: `benchmarks/results/reader-comparison/retrieval-modes-cache-2026-05-04/`
- Qwen3-Embedding GGUF: https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF
