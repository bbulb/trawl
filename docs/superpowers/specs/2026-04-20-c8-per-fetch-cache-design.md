# C8 — Per-fetch result cache — design (2026-04-20)

Branch: `feat/c8-per-fetch-cache` (stacked on `feat/c16-assertion-keys`)

## 문제

같은 URL을 수초~수분 간격으로 반복 호출해도 매번 Playwright+extraction을
다시 돈다. `tests/agent_patterns/workflows.yaml` 의 repeat_visits 패턴
3개 (`openclaw_dashboard_warm_profile_naver_finance`,
`hermes_dashboard_warm_profile_hn_front`, `claude_code_repeated_pypi_check`)
가 이 cost를 정확히 측정한다 — 각각 3-5회 fetch 를 stack 하는데 매 호출이
독립적으로 Playwright 를 띄운다.

ARCHITECTURE.md "Future work" 항목 6 (`per-fetch caching`) + RESEARCH.md
C8 에 이미 정리된 후속. 본 PR 은 MVP 수준의 디스크 캐시 도입.

## 접근법

`~/.cache/trawl/fetches/<sha256(url)>.json` 에 fetch 결과 저장. TTL 기반
만료 + 크기 상한 LRU. **전체 파이프라인이 아니라 fetch 단계만** 캐시 —
chunking / embedding / retrieval 은 query 의존이므로 hit 에서도 재실행.

### 캐시 대상

```python
@dataclass
class CachedFetch:
    url: str
    markdown: str
    page_title: str
    fetcher_used: str        # "playwright+trafilatura" / "pdf" / "wikipedia" / ...
    content_type: str | None # 빈 값 허용
    cached_at: float         # unix epoch
    fetch_elapsed_ms: int    # 원본 cost (diagnostics)
```

`page_title` 은 fetch 직후에 계산해서 함께 저장. `html` 자체는 저장하지
않음 (수 MB 단위 가능). `content_type` 은 post-detection 패스스루 분기에
필요.

### 캐시 저장/제외 대상

**저장 O**
- Playwright+Trafilatura HTML 경로 (가장 비용 큼: ~2-5s)
- `_API_FETCHERS` 체인 성공 (youtube/github/stackexchange/wikipedia — API
  quota 절약에도 도움)
- `pdf` / `pdf-probed` 경로 (~1-3s)

**저장 X (MVP scope)**
- Profile fast path / transfer path — 이미 빠르고, 드리프트 검증이 캐시와
  상호작용해 복잡
- Passthrough (JSON/RSS/XML) — httpx GET 이라 이미 저비용 (~100-500ms),
  API 쪽이 TTL 을 의도한 대로 관리하는 게 맞음
- Error result — 실패를 캐시하면 일시적 네트워크 장애가 TTL 동안 지속

### TTL & 크기 상한

- `TRAWL_FETCH_CACHE_TTL` — default `300` (5 min). `0` 이면 비활성.
- `TRAWL_FETCH_CACHE_MAX_MB` — default `100`. 초과 시 `cached_at` 기준
  oldest-first 으로 evict (파일 mtime 사용).
- `TRAWL_FETCH_CACHE_PATH` — default `~/.cache/trawl/fetches`.

TTL 선택 근거. 5분은 대부분의 "현재 세션에서 같은 페이지 재조회"를 커버
하되, 뉴스/스케줄 같은 시간민감형 페이지가 오래된 캐시를 보게 되는 구간을
짧게 유지. 프로젝트의 기존 in-session profile 캐시(메모리)와 달리 이
캐시는 프로세스 경계를 넘어간다 (MCP stdio 서버 재시작 / CLI 여러 번 호출).

### 저장 포맷

JSON 단일 파일 per URL. 아톰성은 `tempfile` + `os.replace`.

```json
{
  "schema": 1,
  "url": "https://finance.naver.com/sise/sise_market_sum.naver",
  "markdown": "...",
  "page_title": "시가총액 : 네이버 증권",
  "fetcher_used": "playwright+trafilatura",
  "content_type": "text/html; charset=UTF-8",
  "cached_at": 1713571200.123,
  "fetch_elapsed_ms": 4321
}
```

`schema: 1` 은 미래에 formatshift 시 reader 가 버전 불일치를 인지하고
cache miss 처리하기 위한 선언.

### 크기 상한 관리

간단한 접근: `put()` 호출 직후에 cap 초과 여부만 점검 (매 get 마다 하지
않음 — 비싸서). 초과 시 cached dir walk → `(mtime, size, path)` sort →
20% 만큼 삭제 (cap 80% 아래로 내려가도록 한 번에). watermark.

### 동시성

MCP 서버는 `fix(mcp): 파이프라인 호출을 전용 단일 워커 스레드에 고정`
패치로 단일 워커로 수렴됨. 같은 프로세스 내 경합 없음. 여러 프로세스가
같은 URL 을 동시에 쓰는 경우 tempfile + os.replace 가 마지막 쓰기 승리로
수렴 — 동일 내용일 확률이 크므로 문제 없음.

## 파이프라인 통합

```python
# src/trawl/pipeline.py::_run_full_pipeline 진입 직후
cached = fetch_cache.get(url)
if cached is not None:
    markdown = cached.markdown
    page_title = cached.page_title
    fetcher_name = cached.fetcher_used + "+cached"   # telemetry 구분자
    fetch_ms = 0
    # (fetched 객체가 없으므로 content-type post-detection branch는 skip —
    # 캐시 hit 은 성공한 HTML fetch 였다는 증명이라 필요하지 않음)
else:
    # 기존 fetch 로직 수행
    ...
    # 성공 후 putting:
    fetch_cache.put(url, CachedFetch(
        url=url, markdown=markdown, page_title=page_title,
        fetcher_used=fetcher_name, content_type=ct,
        cached_at=time.time(), fetch_elapsed_ms=fetched.elapsed_ms,
    ))

# 이후 chunk → retrieve → enrich 는 기존 경로 재사용
```

`fetcher_used` suffix `+cached` 는 telemetry/agent-patterns 가 캐시 hit
여부를 식별하도록 하는 장치. 기존 `agent_patterns` assertion `fetcher_used`
값을 깨뜨리지 않으려면 prefix 매칭으로 검사해야 하지만 — 더 간단히:
`cache_hit: bool` 을 `PipelineResult` 에 추가.

결정: `PipelineResult.cache_hit: bool = False` 필드 추가 (field w/ default).
`fetcher_used` 는 원본 그대로 두어 기존 assertion 무영향. `cache_hit` 은
새 assertion key 후보로도 남김 (본 PR 에서는 필드만 추가, assertion DSL
추가는 scope 밖).

## 예상 효과

repeat_visits 패턴 step 2+ (= warm 상태)에서:
- Playwright navigation 생략: 2-4s 감소
- Trafilatura extraction 생략: 100-200ms 감소
- chunking / embedding / retrieval 은 그대로 수행: ~700-1200ms

수치 예측: 3회 fetch → 첫 회 5s + 재방문 2회 × 1s = **기존 15s → 7s (53%
단축)**. `budgets.total_ms_p95` 를 구체적으로 맞춰놓은 workflows.yaml 패턴
에서 곧바로 regression/improvement 확인 가능.

## 롤백 경로

`TRAWL_FETCH_CACHE_TTL=0` 으로 즉시 off. 기본 on 이지만 env 로 회피 가능
하므로 production-mbig switch 가 별도 배포 없이 가능.

## 테스트 계획

### 유닛 (`tests/test_fetch_cache.py`)
- put + get round-trip
- TTL 경과 시 miss
- `ttl=0` → put no-op, get 항상 miss
- sha256(url) 파일명 충돌 0 보장 (스키마 버전 mismatch 시 miss)
- size cap 넘으면 oldest 삭제
- 깨진 JSON 파일은 조용히 skip

### 통합 (`tests/test_pipeline_cache.py`)
- monkey-patched fetcher: 첫 호출 fetch 진입, 두 번째 호출 fetcher 미호출 +
  `cache_hit=True` + chunks 동일
- TTL 경과 후 fetcher 다시 호출됨
- `TRAWL_FETCH_CACHE_TTL=0` 이면 매번 fetcher 호출

### 수동
- 2026-04-20 workflows.yaml repeat_visits 패턴 live run 재측정 (PR body 에
  첨부). 단, live run 은 PR #11 머지 이후에만 전체 catalog 가 올라오므로
  본 PR 은 유닛/통합 테스트 선행 + budget 예측만 문서화.

## scope 밖 (후속)

- HTTP ETag/Last-Modified revalidation — 캐시 hit 조건 확장
- Chunk + embedding 레벨 캐시 — chunking 50ms + embedding 500ms-1s 추가 절감
- Per-host TTL override — 뉴스 1min vs docs 1h 같은 차등
- Passthrough 결과 캐시 — API 쿼터 민감 페이지에 유용하지만 TTL 설정이
  까다롭고 passthrough 자체가 빠름
