# trawl 안정성·속도 개선 리포트

작성일: 2026-05-04  
작성 범위: `/Users/lyla/workspaces/trawl` 현재 워크트리, 외부 유사 서비스/연구 조사, 로컬 테스트와 소규모 reader benchmark

## 요약

`trawl`은 "URL 전체를 마크다운으로 덤프"하는 도구가 아니라, 단일 URL에서 자연어 질의에 맞는 근거 청크만 반환하는 로컬 Python/MCP reader다. 현재 구현은 이미 상당히 잘 정리되어 있다. API-first fetcher, Playwright fallback, extractor scoring, heading/record-aware chunking, bge-m3 retrieval, cross-encoder rerank, fetch/profile/cache/telemetry, MCP untrusted-content boundary가 모두 들어가 있다.

이번 점검에서 확인한 핵심 병목은 세 가지다.

1. 안정성을 위해 Playwright와 MCP pipeline 실행이 단일 lock/worker로 직렬화되어 있어, 한 번의 느린 브라우저 fetch가 다른 요청을 막는다.
2. 반복 호출 속도를 크게 줄일 수 있는 embedding cache가 기본 비활성(`TRAWL_EMBED_CACHE_TTL=0`)이라 fetch cache hit 뒤에도 문서 embedding 비용이 반복된다.
3. 점검 당시 embedding 서버 장애 시에는 reranker처럼 우아한 fallback이 아니라 retrieval error로 끝났다. BM25-only degraded mode가 있으면 MCP 도구 안정성이 더 좋아진다.

권장 우선순위는 다음 순서다.

| 우선순위 | 작업 | 기대 효과 | 위험 |
|---|---|---|---|
| P0 | `trawl doctor` + embedding/reranker/fetch cache health check | 운영 장애 원인 즉시 분리 | 낮음 |
| P0 | embedding 장애 시 BM25-only fallback | 모델 서버 다운에도 근거 일부 반환 | 중간, 품질 gate 필요 |
| P1 | embedding cache 기본 활성 또는 docs/profile host에 한정 활성 | 반복 질의 latency 큰 폭 감소 | 낮음, key가 text hash 기반 |
| P1 | MCP fast path와 browser path 실행 격리 | 느린 Playwright 호출의 queue blocking 완화 | 중간 |
| P1 | Firecrawl/Crawl4AI provider adapter를 benchmark에 실제 구현 | 외부 대비 품질·속도 회귀 측정 가능 | 낮음 |
| P2 | Scrapling optional fallback fetcher spike | anti-bot/동적 사이트 회복률 개선 | 중간, 의존성 무거움 |
| P2 | cache revalidation(ETag/Last-Modified) | TTL보다 빠르고 신선한 반복 fetch | 중간 |
| P2 | hybrid/contextual retrieval default 재평가 | code/API 질의 안정성 개선 | 중간, p95 gate 필요 |

## 현재 구조 분석

### 진입점과 결과 계약

`fetch_relevant()`는 public entry point이며 `PipelineResult`는 timing, profile, rerank, cache, contextual retrieval, enrichment, diagnostics를 폭넓게 담는다. 특히 `n_chunks_embedded`, `rerank_capped`, `retrieval_diagnostics`는 성능 튜닝에 필요한 계측을 이미 갖추고 있다.

근거 파일:

- `src/trawl/pipeline.py:66` `TRAWL_CHUNK_BUDGET` 기본 100
- `src/trawl/pipeline.py:76` `PipelineResult`
- `src/trawl/pipeline.py:183` chunk payload provenance
- `src/trawl/pipeline.py:928` full pipeline

좋은 점:

- public API가 "never raises" 정책을 따른다.
- profile fast path, host-transfer, raw passthrough, PDF probe, API fetcher, Playwright fallback 순서가 비용이 싼 경로부터 비싼 경로로 정렬되어 있다.
- passthrough는 query 없이 JSON/XML/RSS/Atom을 바로 반환해 불필요한 embedding을 피한다.

개선 포인트:

- `fetch_cache.get()` hit 후에도 chunking/retrieval/rerank는 매번 수행된다. chunking은 싸지만 embedding은 비싸다.
- `PipelineResult.error`는 단일 문자열이라 "완전 실패"와 "degraded fallback 성공"을 구분하기 어렵다. `warnings` 또는 `degraded_reason` 필드를 추가하면 안정성 관찰이 쉬워진다.

### Fetch/렌더링

현재 Playwright fetcher는 process-wide Chromium을 재사용하되 `_lock`으로 모든 fetch를 직렬화한다. MCP server도 `ThreadPoolExecutor(max_workers=1)`로 전체 pipeline을 단일 worker에 태운다.

근거 파일:

- `src/trawl/fetchers/playwright.py:93` process-wide browser holder
- `src/trawl/fetchers/playwright.py:151` global lock
- `src/trawl/fetchers/playwright.py:221` text stability wait
- `src/trawl/fetchers/playwright.py:322` fetch
- `src/trawl_mcp/server.py:28` single pipeline worker

좋은 점:

- sync Playwright greenlet/thread 문제를 회피한다.
- content-ready wait는 fixed sleep보다 빠르고, networkidle 무한 대기 문제를 줄인다.
- shadow DOM allow-list가 MDN code 예제를 회수한다.

개선 포인트:

- 단일 worker는 안정적이지만 tail latency에 취약하다. Firecrawl은 batch scrape job을 비동기로 운영하고, Scrapling은 async session에서 `max_pages`로 브라우저 탭 pool을 제공한다.[^firecrawl][^scrapling-stealth]
- API-first/GitHub/Wikipedia/StackExchange/PDF/passthrough 같은 browser-free 경로까지 동일 MCP worker 뒤에 줄 서는 구조다.

권장 설계:

1. `fetch_page` 호출을 먼저 lightweight router에서 분류한다.
2. browser-free route는 별도 small thread pool에서 처리한다.
3. Playwright route만 single-thread 또는 small process pool로 제한한다.
4. 장기적으로 sync Playwright worker process N개를 두고 per-host concurrency semaphore를 적용한다.

### Extraction/chunking

현재 extraction은 Trafilatura recall/precision, BeautifulSoup fallback, optional Readability 후보를 점수화한다. raw length 선택에서 벗어나 query coverage, heading/code/table, link density, boilerplate penalty를 반영한다.

근거 파일:

- `src/trawl/extraction.py:105` `extract_html`
- `src/trawl/extraction.py:134` 후보 extractor 목록
- `src/trawl/extraction.py:201` candidate scoring
- `src/trawl/chunking.py:72` `chunk_markdown`
- `src/trawl/chunking.py:96` adaptive chunk size
- `src/trawl/chunking.py:141` record sentinel 보존

좋은 점:

- 예전 roadmap의 R2 상당 부분이 이미 구현되어 있다.
- record sentinel을 보존하는 쪽을 우선하는 로직은 리스트/카드형 페이지 안정성에 중요하다.
- chunk metadata에 `source_url`, selector/xpath, char span이 남는다.

개선 포인트:

- candidate score와 선택 이유가 telemetry에 남지 않는다. extractor regression을 디버깅하려면 후보별 점수·길이·보일러플레이트율을 opt-in으로 남기는 편이 좋다.
- ReaderLM-v2 같은 HTML→Markdown 모델은 긴/복잡 HTML에서 강점이 있지만, 기본 경로로 넣기에는 latency/라이선스/운영 부담이 크다. optional benchmark backend로만 검증하는 것이 맞다.[^readerlm]

### Retrieval/rerank

retrieval은 bge-m3 dense embedding을 기본으로 하고, BM25 prefilter와 hybrid RRF path를 갖고 있다. reranker는 title/section/body 입력과 payload cap, HTTP error fallback을 갖춘 상태다.

근거 파일:

- `src/trawl/retrieval.py:23` embedding endpoint 기본값
- `src/trawl/retrieval.py:89` document embedding cache hook
- `src/trawl/retrieval.py:240` retrieve
- `src/trawl/retrieval.py:310` BM25 chunk budget prefilter
- `src/trawl/retrieval.py:347` hybrid branch
- `src/trawl/reranking.py:29` reranker cap 설명
- `src/trawl/reranking.py:186` rerank fallback

좋은 점:

- `TRAWL_CHUNK_BUDGET=100` 기본값은 longform retrieval cost를 잘 제어한다.
- reranker는 장애 시 cosine top-k로 fallback한다.
- bge-m3 sparse endpoint hook과 fusion diagnostics가 이미 있다.

개선 포인트:

- embedding 장애에는 fallback이 없다. 현재 `httpx.HTTPError`면 retrieval result error가 되고 최종 result는 빈 chunks가 된다.
- bge-m3는 dense, sparse, multi-vector를 한 모델 계열에서 지원하는데 현재 기본은 dense다. BGE-M3 논문은 100개 이상 언어, dense/sparse/multi-vector, 최대 8,192 token granularity를 강조한다.[^bge]
- Contextual Retrieval은 chunk 앞에 문서/청크 context를 붙여 retrieval 실패를 줄이는 접근이다. Anthropic은 embedding/BM25 양쪽에 context를 prepend하는 방향을 제안한다.[^anthropic] 현재 `contextual.py`는 deterministic context를 구현하지만 default off이며, 기존 측정 문서에 retrieval p95 증가 이슈가 남아 있다.

권장 설계:

1. embedding 실패 시 `BM25 fallback`을 반환한다. `path="bm25_degraded"` 또는 `retrieval_mode="bm25_fallback"`을 기록한다.
2. `TRAWL_EMBED_CACHE_TTL`을 기본 활성화하는 실험을 한다. cache key가 text hash, model, base URL, contextual mode/version을 포함하므로 stale content 위험은 낮다.
3. hybrid retrieval은 code/API/identifier 질의에 한해 auto gate로 재측정한다. default on은 p95 `<= +20%`, flipped-to-fail `0` 조건을 만족할 때만 한다.

## 소규모 실측 결과

### Unit tests

명령:

```bash
mamba run -n trawl python -m pytest tests/test_reader_comparison.py tests/test_retrieval_embedding_cache.py tests/test_pipeline_embedding_cache_metrics.py tests/test_contextual_auto.py tests/test_pipeline_contextual.py tests/test_retrieval_hybrid.py
mamba run -n trawl python -m pytest
```

결과:

```text
targeted retrieval/cache/contextual suite: 54 passed in 0.78s
410 passed in 20.24s
```

참고: 기준 검증 환경은 `environment.yml`로 생성한 mamba env `trawl`이다. 로컬 virtualenv가 있으면 Python/도구 버전이 달라질 수 있으므로 이 저장소에서는 `mamba run -n trawl ...` 명령을 사용한다.

### Reader comparison

명령:

```bash
mamba run -n trawl python benchmarks/reader_comparison.py \
  --provider trawl --provider jina --provider trafilatura \
  --output-dir benchmarks/results/reader-comparison/report-analysis-2026-05-04
```

요약:

| Provider | Rows | Pass rate | Avg latency ms | Avg estimated tokens |
|---|---:|---:|---:|---:|
| trawl | 6 | 1.00 | 3651.3 | 1022.0 |
| jina | 6 | 1.00 | 4069.5 | 23510.8 |
| trafilatura | 6 | 1.00 | 404.5 | 11627.3 |

해석:

- 6개 안정 케이스에서는 셋 다 fact coverage 100%였다.
- `trawl`은 Jina 대비 약 23.0x, Trafilatura 대비 약 11.4x 적은 토큰을 반환했다.
- 이 run은 fetch cache/서비스 warm 상태의 영향을 받았을 수 있어 latency 결론은 제한적이다. 그래도 "token efficiency는 매우 강하고, latency는 local extraction보다 느리며, Jina와는 같은 자릿수"라는 현재 제품 포지션은 확인된다.

### Cache-controlled retrieval re-measurement

결과 위치:

- `benchmarks/results/reader-comparison/retrieval-modes-cache-2026-05-04/`

실행 방식:

- `TRAWL_EMBED_CACHE_TTL=86400`을 `--warm-repeat-embed-cache-ttl 86400`으로 고정했다.
- dense, hybrid, contextual-auto, contextual-forced를 각각 별도 run으로 실행했다.
- 각 mode는 별도 빈 `TRAWL_EMBED_CACHE_PATH`를 사용했다. dense와 hybrid는 같은 document embedding key를 공유하므로, 한 process에서 mode를 연속 실행하면 hybrid cold row가 dense cache를 재사용할 수 있기 때문이다.
- 각 mode는 6개 case를 cold/warm 2회씩 실행했고, 이후 48개 row를 aggregate report로 합쳤다.

전체 gate 요약:

| Mode | Rows | Pass rate | Flipped-to-fail | Avg rank movement | Retrieval p50 ms | Retrieval p95 ms | p95 vs dense | Avg tokens | Gate |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| dense | 12 | 1.00 | 0 | 0.0 | 556.5 | 1967.3 | baseline | 1022.0 | baseline |
| hybrid | 12 | 1.00 | 0 | 0.0 | 544.5 | 2008.6 | +2.1% | 1252.8 | fail, no code/API pass or rank improvement |
| contextual-auto | 12 | 1.00 | 0 | 0.0 | 609.0 | 2365.5 | +20.2% | 962.8 | fail, p95 over gate |
| contextual-forced | 12 | 1.00 | 0 | 0.0 | 604.0 | 2376.1 | +20.8% | 962.8 | fail, p95 over gate |

Query type별 p95:

| Query type | Dense p95 ms | Hybrid p95 ms | Contextual-auto p95 ms | Contextual-forced p95 ms |
|---|---:|---:|---:|---:|
| concept | 1886.5 | 1924.8 (+2.0%) | 2180.8 (+15.6%) | 2185.5 (+15.8%) |
| identifier | 1697.2 | 1729.0 (+1.9%) | 2263.8 (+33.4%) | 2272.5 (+33.9%) |

Code/API subset은 `mdn_fetch_post`와 `github_fastapi_readme`의 cold/warm 4개 row로 봤다. 모든 mode가 pass rate 1.00, average rank movement 0.0이었다. 즉 hybrid는 p95와 flip gate는 통과했지만 code/API subset의 pass 또는 rank 개선을 만들지 못했다. contextual-auto와 contextual-forced는 subset의 token output을 줄였지만 rank/pass 개선이 없고 identifier p95가 +33%대라 default 후보로 부적합하다.

Cache behavior:

| Mode | Phase | Rows | Fetch cache hits | Avg embed hits | Avg embed misses | Avg retrieval ms |
|---|---|---:|---:|---:|---:|---:|
| dense | cold | 6 | 0/6 | 0.0 | 66.7 | 1445.3 |
| dense | warm | 6 | 6/6 | 66.7 | 0.0 | 55.8 |
| hybrid | cold | 6 | 6/6 | 0.0 | 66.7 | 1467.8 |
| hybrid | warm | 6 | 6/6 | 66.7 | 0.0 | 63.7 |
| contextual-auto | cold | 6 | 6/6 | 0.0 | 66.7 | 1857.0 |
| contextual-auto | warm | 6 | 6/6 | 66.7 | 0.0 | 67.7 |
| contextual-forced | cold | 6 | 6/6 | 0.0 | 66.7 | 1853.5 |
| contextual-forced | warm | 6 | 6/6 | 66.7 | 0.0 | 73.0 |

해석:

- Embedding cache는 의도대로 동작했다. cold row는 document embedding miss가 발생했고 warm row는 같은 수만큼 hit가 발생했다.
- Warm retrieval은 mode와 무관하게 약 56-73ms 범위까지 내려갔다.
- Retrieval default 변경 gate는 이번 run에서 어떤 non-dense mode도 모두 통과하지 못했다. `TRAWL_HYBRID_RETRIEVAL`과 `TRAWL_CONTEXTUAL_RETRIEVAL` 기본값은 그대로 유지하는 것이 맞다.

## 유사 서비스와 연구에서 얻은 시사점

| 대상 | 확인한 사실 | trawl에 적용할 점 |
|---|---|---|
| Jina Reader | `r.jina.ai`는 URL을 LLM-friendly text로 바꾸며 공식 표기 평균 latency 7.9s와 rate limit/토큰 과금 모델이 있다.[^jina] | full-page baseline으로 계속 비교. trawl은 output-token 효율을 차별화 지표로 유지 |
| Firecrawl | scrape endpoint는 markdown/html/rawHtml/screenshot/links/json 등을 지원하고, batch scrape와 enhanced mode/ZDR 옵션이 있다.[^firecrawl] | benchmark adapter 구현. production dependency보다 hard-site fallback 후보 |
| Crawl4AI | async crawler, markdown, CSS/LLM extraction, dynamic page, `css_selector`/`target_elements`, cache/session 옵션이 있다.[^crawl4ai-quickstart][^crawl4ai-sdk] | profile selector extraction과 benchmark adapter에 참고. multi-URL crawling은 scope 밖 |
| Scrapling | single request부터 full crawl까지 다루고, optional fetcher deps와 stealth/Cloudflare/session/max_pages 기능을 제공한다.[^scrapling][^scrapling-stealth] | optional fallback fetcher spike. 기본 dependency로 넣지 말고 실패 회복률 gate 필요 |
| BGE-M3 | dense/sparse/multi-vector retrieval과 100+ 언어, 최대 8192 token 입력을 지원하는 모델 계열이다.[^bge] | 현재 dense 중심에서 identifier 질의 hybrid auto를 재측정 |
| Contextual Retrieval | chunk-specific context를 prepend해 embedding/BM25 retrieval을 개선하는 접근이다.[^anthropic] | deterministic contextual auto를 cache-aware로 재측정. p95 gate 없이는 default 금지 |
| Late Chunking | 긴 문서를 먼저 인코딩하고 나중에 chunk pooling해 주변 문맥 손실을 줄인다.[^late] | 기존 bge-m3 late chunking spike는 reject 유지. 모델 교체 전까지 재시도 우선순위 낮음 |
| Context 비교 연구 | contextual retrieval은 semantic coherence가 좋지만 더 많은 compute가 필요하고, late chunking은 효율적이나 completeness trade-off가 있다.[^reconstruct] | 속도 목표가 있으므로 contextual은 auto/specific query로 제한 |
| ReaderLM-v2 | 1.5B HTML→Markdown/JSON 모델로 512K token 문서를 처리하는 연구다.[^readerlm] | optional extraction backend 후보. 기본 path에는 부적합 |

## 개선 권고 상세

### P0. 운영 진단 명령 추가

문제: trawl의 실제 장애는 코드보다 주변 서비스에서 난다. embedding server(`:8081`), reranker(`:8083`), Playwright browser install, cache path 권한, VLM URL 설정이 모두 runtime dependency다.

제안:

- `trawl-doctor` CLI 또는 `python -m trawl.diagnostics`
- 확인 항목:
  - Python version, package version
  - Playwright browser 설치 여부
  - `TRAWL_EMBED_URL` `/embeddings` smoke
  - `TRAWL_RERANK_URL` `/rerank` smoke, unavailable이면 warning
  - cache path read/write 권한
  - telemetry path 권한
  - `profile_page` 노출 여부와 VLM health

Gate:

- 모델 서버 down이면 exit code는 non-zero, 하지만 optional reranker down은 warning.
- CI에서는 network/local service 없이도 unit test 가능하도록 doctor tests는 mocked.

구현 결과:

- `trawl-doctor`와 `python -m trawl.diagnostics`를 추가했다.
- `--json`과 `--no-network`를 지원한다.
- Python, Playwright Chromium, fetch/embedding/telemetry cache path, embedding endpoint, optional reranker endpoint, optional VLM 설정을 점검한다.
- required check failure만 non-zero exit code로 반영한다. reranker와 VLM은 optional warning이다.

### P0. Embedding 장애 시 BM25 fallback

문제: reranker는 unavailable이면 cosine으로 fallback하지만, 점검 당시 embedding server가 죽으면 retrieval이 빈 결과로 실패했다.

제안:

- `retrieval.retrieve()`에서 embedding HTTPError가 나면 BM25 ranking으로 top-k chunks를 구성하는 fallback option 추가.
- `PipelineResult`에 `retrieval_mode="bm25_fallback"`, `error=None`, `warnings=["embedding unavailable: ..."]` 형태를 검토.
- MCP 응답은 `ok=true`로 두되 degraded metadata를 명시한다.

Gate:

- embedding 서버를 monkeypatch로 실패시키는 pipeline test 추가.
- docs/wiki/code 대표 fixture에서 BM25 fallback fact recall을 측정한다.

구현 결과:

- `retrieval.retrieve()`는 embedding `httpx.HTTPError`를 BM25 degraded retrieval로 전환한다.
- fallback 결과는 `error=None`, `retrieval_mode="bm25_fallback"`, `fusion_weights={"bm25": 1.0}`, `warning="embedding unavailable; using BM25 fallback: ..."`을 담는다.
- `PipelineResult.warnings`, MCP `fetch_page` JSON payload, opt-in telemetry event에 warning metadata를 노출한다.
- reranker가 켜져 있고 사용 가능하면 BM25 candidate window를 그대로 rerank하고, reranker가 없으면 BM25 순서를 반환한다.

### P1. Embedding cache 기본 활성 실험

문제: `embedding_cache.DEFAULT_TTL_SECONDS = 0`이라 반복 질의가 문서 embedding을 재계산한다.

제안:

- 1안: default TTL 24h로 변경.
- 2안: docs/profile/API fetcher 경로에만 default on.
- 3안: README에서 운영 권장값으로 `TRAWL_EMBED_CACHE_TTL=86400` 제시하고 benchmark로 효과 검증.

왜 안전한가:

- cache key가 text hash, model, endpoint, contextual mode/version을 포함한다.
- page content가 바뀌면 text hash도 바뀐다.

Gate:

- reader comparison warm repeat: retrieval p50/p95 감소 측정.
- cache max size trim test 유지.
- contextual retrieval on/off 각각 cache key 분리 확인.

구현 결과:

- `RetrievalResult`와 `PipelineResult`에 document embedding cache hit/miss counter를 추가했다.
- opt-in telemetry JSONL과 reader-comparison 출력 CSV/JSONL/report에 cache counter metadata를 노출했다.
- reader comparison에 `--repeat`와 `--warm-repeat-embed-cache-ttl 86400` warm-repeat mode를 추가해 cold/warm 반복 질의를 같은 run에서 비교할 수 있게 했다.

### P1. MCP/browser queue 분리

문제: 현재 MCP server는 모든 pipeline 호출을 single worker에 넣는다. 이 설계는 greenlet 안정성을 얻는 대신 browser-free 호출까지 같이 막는다.

제안:

- router stage에서 URL 종류를 빠르게 판정한다.
- passthrough/PDF/API fetcher는 general executor에서 처리한다.
- Playwright/profile generation만 browser executor에서 처리한다.
- 더 큰 변경은 Playwright worker process pool로 격리한다.

Gate:

- 동시에 1개 느린 Playwright fake call + 5개 passthrough fake call을 넣었을 때 passthrough가 기다리지 않는 test.
- 기존 greenlet/thread regression test 유지.

구현 결과:

- MCP server를 `trawl-browser` 단일 worker와 `trawl-general` small pool로 분리했다.
- `fetch_page`는 cached/host profile 후보가 있는 URL은 browser worker에 유지하고, passthrough suffix, direct PDF URL, GitHub/Wikipedia/StackExchange/YouTube native API route는 general worker에서 먼저 실행한다.
- general worker에서는 `fetch_relevant(..., allow_browser=False)`를 사용해 Playwright 직접 호출과 API fetcher의 Playwright fallback을 차단한다.
- API fetcher가 browser fallback 필요성을 반환하면 MCP handler가 같은 요청을 browser worker에서 재시도해 기존 payload shape를 유지한다.
- concurrency test는 느린 browser-classified call 1개가 실행 중이어도 passthrough/API/PDF-style call 5개가 general worker에서 완료됨을 검증한다.
- browser-disabled pipeline regression test는 API fallback이 general worker에서 Playwright를 호출하지 않음을 검증한다.

### P1. Reader benchmark provider 완성

문제: `benchmarks/reader_comparison.py`는 Firecrawl/Crawl4AI provider choice를 갖지만 실제 adapter는 skip stub이다.

제안:

- Firecrawl: `FIRECRAWL_API_KEY`가 있을 때 markdown scrape adapter 구현.
- Crawl4AI: package import 가능하면 local async crawler adapter 구현.
- provider output에는 raw provider error, timeout, token estimate, status를 동일 schema로 기록.

Gate:

- credential/package 없으면 현재처럼 clean skip.
- adapter unit tests는 mocked HTTP/import로만 작성.
- `--provider trawl --provider jina --provider firecrawl --provider crawl4ai`가 최소 report를 쓴다.

구현 결과:

- Firecrawl provider는 `FIRECRAWL_API_KEY`가 있을 때 `https://api.firecrawl.dev/v2/scrape` markdown scrape를 호출하고, 없으면 skipped row를 기록한다.
- Crawl4AI provider는 package import가 가능할 때 lazy async crawler adapter를 사용하고, import가 불가능하면 skipped row를 기록한다.
- 두 adapter 모두 mocked tests로 success/skip schema를 검증한다.

### P2. Scrapling optional fallback

문제: 현재 Playwright+stealth는 passive challenge에는 대응하지만 hard anti-bot/Cloudflare Turnstile류는 한계가 있다. Scrapling은 StealthyFetcher, Cloudflare solver, session/max_pages, proxy 관련 기능을 문서화한다.[^scrapling-stealth]

제안:

- optional extra: `scrapling` 또는 `scrapling[fetchers]`
- env flag: `TRAWL_SCRAPLING_FALLBACK=1`
- trigger:
  - Playwright timeout
  - extracted markdown empty
  - known anti-bot text pattern
- trawl extraction/chunking/retrieval은 그대로 사용하고 Scrapling은 HTML supplier로만 사용한다.

Gate:

- fallback이 켜져도 reader-comparison smoke와 기존 parity matrix regression 0.
- protected-site fixture나 captured HTML에서 recovery count가 측정되어야 한다.
- dependency import는 lazy.

구현 결과:

- `src/trawl/fetchers/scrapling.py`를 추가했다. `TRAWL_SCRAPLING_FALLBACK=1`일 때만 실행하고, `scrapling.fetchers` import는 호출 시점까지 지연한다.
- 기본 의존성은 바꾸지 않고 `.[scrapling]` optional extra만 추가했다.
- fallback trigger는 Playwright error, empty markdown, anti-bot marker다. `TRAWL_SCRAPLING_MODE=auto|dynamic|stealthy`를 지원하며 `auto`는 anti-bot marker일 때만 `StealthyFetcher`를 고른다.
- Scrapling은 HTML 공급자로만 사용한다. extraction, chunking, retrieval, rerank, warnings/telemetry 계약은 기존 trawl pipeline을 그대로 탄다.
- unit test는 fake Scrapling module로 lazy import, dynamic/stealthy 선택, disabled behavior, Playwright failure recovery를 검증한다.

### P2. HTTP cache revalidation

문제: fetch cache는 TTL-only이며 ETag/Last-Modified revalidation이 없다.

제안:

- cache record에 `etag`, `last_modified`, `content_hash` 추가.
- TTL 만료 시 conditional GET/HEAD로 304면 markdown 재사용.
- volatile host/category는 짧은 TTL 유지.

Gate:

- mocked 304/200/stale cases 추가.
- news/schedule 같은 동적 페이지는 profile/fetch cache가 과하게 stale하지 않도록 env override 유지.

구현 결과:

- `fetch_cache.CachedFetch`에 `etag`, `last_modified`, `content_hash`를 추가했다. 기존 schema record에 이 필드가 없어도 계속 읽고, `content_hash`는 markdown hash로 보강한다.
- stale record에 validator가 있으면 conditional GET을 보내고, `304`면 cache timestamp/validator를 갱신한 뒤 저장 markdown을 재사용한다.
- `200`, revalidation error, missing validator는 stale markdown을 반환하지 않고 기존 fresh fetch path로 떨어진다. 따라서 news/schedule 같은 동적 페이지는 기존 `TRAWL_FETCH_CACHE_TTL`/disable override로 stale risk를 제어한다.
- Playwright/PDF fetch result에서 가능한 `ETag`/`Last-Modified`를 cache record로 넘긴다.
- mocked tests는 cache record field round-trip, legacy record 읽기, 304 refresh/reuse, 200 fresh replacement, missing-validator stale refetch를 검증한다.

### P2. Contextual/hybrid retrieval 재측정

문제: contextual retrieval과 hybrid retrieval은 품질 개선 후보지만 latency cost가 있다.

제안:

- cache-controlled benchmark를 추가한다.
- query type별로 mode를 분리한다:
  - identifier/code: hybrid BM25 + dense, optional bge_m3_sparse
  - longform concept: dense + rerank 유지
  - repeated docs with embed cache: contextual auto 재검토

Gate:

- flipped-to-fail 0.
- retrieval p95 증가 `<= +20%`.
- code/API subset net pass 개선 또는 rank improvement 명확.

구현 결과:

- `benchmarks/reader_comparison.py`에 `--retrieval-mode dense|hybrid|contextual-auto|contextual-forced`를 추가했다. 이 옵션은 `trawl` provider에만 적용되며 다른 baseline provider row는 중복하지 않는다.
- mode별 실행은 각 `trawl` 호출 주변에서만 `TRAWL_HYBRID_RETRIEVAL`과 `TRAWL_CONTEXTUAL_RETRIEVAL`을 덮어쓰고 즉시 복원한다. 기존 runtime default는 바꾸지 않았다.
- CSV/JSONL/report에는 requested/observed retrieval mode, query type, contextual-use flag, cache TTL, rank-1 identity hash, first satisfied fact rank, dense 대비 rank movement, flipped-to-fail, retrieval p50/p95, token output을 기록한다.
- report의 retrieval-mode summary는 dense row를 case/repeat/cache phase 기준 baseline으로 삼는다. default 변경 gate는 여전히 flipped-to-fail `0`과 retrieval p95 증가 `<= 20%`이며, 이 gate를 통과한 실측 run 전까지 `TRAWL_HYBRID_RETRIEVAL`/`TRAWL_CONTEXTUAL_RETRIEVAL` 기본값은 유지한다.

실측 결과:

- 2026-05-04 cache-controlled aggregate는 `benchmarks/results/reader-comparison/retrieval-modes-cache-2026-05-04/`에 기록했다.
- hybrid는 flipped-to-fail 0, retrieval p95 +2.1%로 latency gate를 통과했지만 code/API subset pass/rank improvement가 없었다.
- contextual-auto는 flipped-to-fail 0이지만 전체 p95 +20.2%로 gate를 약간 넘었고, identifier query p95는 +33.4%였다.
- contextual-forced는 flipped-to-fail 0이지만 전체 p95 +20.8%, identifier query p95 +33.9%로 gate를 넘었다.
- 결론: non-dense mode 중 default-on 조건을 충족한 mode가 없다. hybrid/contextual 기본값은 off 유지.

## 4주 실행안

1주차:

- `trawl-doctor` 구현 (P0 완료)
- embedding down BM25 fallback 설계/test (P0 완료)
- reader comparison Firecrawl/Crawl4AI mocked adapter skeleton

2주차:

- embedding cache default-on A/B
- warm-repeat benchmark 추가
- telemetry/report에 cache hit, embed cache hit/miss count 추가

3주차:

- MCP queue 분리 spike
- browser-free fast path concurrency test
- per-host concurrency/rate-limit 설계

4주차:

- Scrapling optional fallback spike
- protected/dynamic case 측정
- hybrid/contextual cache-controlled 재측정

후속 `/goal` 실행 큐:

- 남은 P1/P2 작업은 `docs/superpowers/plans/2026-05-04-stability-speed-remaining-work.md`에 이어서 실행할 수 있는 `/goal` 명령으로 정리했다.
- 권장 순서는 P1 speed/observability, MCP/browser queue 분리, P2 fetch recovery/cache revalidation, P2 retrieval mode 재측정이다.

## 검증 명령

이번 리포트 작성 중 실행한 명령:

```bash
mamba run -n trawl python -m pytest
mamba run -n trawl python -m pytest tests/test_reader_comparison.py tests/test_retrieval_embedding_cache.py tests/test_pipeline_embedding_cache_metrics.py tests/test_contextual_auto.py tests/test_pipeline_contextual.py tests/test_retrieval_hybrid.py
mamba run -n trawl python benchmarks/reader_comparison.py --provider trafilatura --limit 1 --output-dir benchmarks/results/reader-comparison/smoke-report-analysis
mamba run -n trawl python benchmarks/reader_comparison.py --provider trawl --limit 1 --output-dir benchmarks/results/reader-comparison/smoke-report-analysis-trawl
mamba run -n trawl python benchmarks/reader_comparison.py --provider trawl --provider jina --provider trafilatura --output-dir benchmarks/results/reader-comparison/report-analysis-2026-05-04
TRAWL_EMBED_CACHE_PATH=benchmarks/results/reader-comparison/retrieval-modes-cache-2026-05-04/dense-embed-cache mamba run -n trawl python benchmarks/reader_comparison.py --provider trawl --retrieval-mode dense --repeat 2 --warm-repeat-embed-cache-ttl 86400 --output-dir benchmarks/results/reader-comparison/retrieval-modes-cache-2026-05-04/dense
TRAWL_EMBED_CACHE_PATH=benchmarks/results/reader-comparison/retrieval-modes-cache-2026-05-04/hybrid-embed-cache mamba run -n trawl python benchmarks/reader_comparison.py --provider trawl --retrieval-mode hybrid --repeat 2 --warm-repeat-embed-cache-ttl 86400 --output-dir benchmarks/results/reader-comparison/retrieval-modes-cache-2026-05-04/hybrid
TRAWL_EMBED_CACHE_PATH=benchmarks/results/reader-comparison/retrieval-modes-cache-2026-05-04/contextual-auto-embed-cache mamba run -n trawl python benchmarks/reader_comparison.py --provider trawl --retrieval-mode contextual-auto --repeat 2 --warm-repeat-embed-cache-ttl 86400 --output-dir benchmarks/results/reader-comparison/retrieval-modes-cache-2026-05-04/contextual-auto
TRAWL_EMBED_CACHE_PATH=benchmarks/results/reader-comparison/retrieval-modes-cache-2026-05-04/contextual-forced-embed-cache mamba run -n trawl python benchmarks/reader_comparison.py --provider trawl --retrieval-mode contextual-forced --repeat 2 --warm-repeat-embed-cache-ttl 86400 --output-dir benchmarks/results/reader-comparison/retrieval-modes-cache-2026-05-04/contextual-forced
```

## 참고 자료

[^jina]: Jina Reader API, <https://jina.ai/reader/?conversion=ppt-weweb>
[^firecrawl]: Firecrawl Scrape docs, <https://docs.firecrawl.dev/features/scrape>
[^crawl4ai-quickstart]: Crawl4AI Quick Start, <https://docs.crawl4ai.com/core/quickstart/>
[^crawl4ai-sdk]: Crawl4AI Complete SDK Reference, <https://docs.crawl4ai.com/complete-sdk-reference/>
[^scrapling]: Scrapling documentation index, <https://scrapling.readthedocs.io/en/latest/index.html>
[^scrapling-stealth]: Scrapling stealth fetching docs, <https://scrapling.readthedocs.io/en/latest/fetching/stealthy.html>
[^anthropic]: Anthropic Contextual Retrieval, <https://www.anthropic.com/engineering/contextual-retrieval>
[^bge]: BGE-M3 paper, <https://arxiv.org/abs/2402.03216>
[^late]: Late Chunking paper, <https://arxiv.org/abs/2409.04701>
[^reconstruct]: Reconstructing Context, <https://arxiv.org/abs/2504.19754>
[^readerlm]: ReaderLM-v2 paper, <https://arxiv.org/abs/2503.01151>
