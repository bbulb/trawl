# C7 — PDF Content-Type HEAD probe — design (2026-04-19)

Branch: `feat/c7-head-probe-pdf` (stacked on `feat/agent-patterns-scaffold`)

## 문제

현재 `_is_pdf_url(url)` 가 PDF 라우팅을 결정하는 유일한 신호:

```python
def _is_pdf_url(url: str) -> bool:
    lower = url.lower()
    return lower.endswith(".pdf") or "/pdf/" in lower
```

URL suffix만 보므로 다음과 같은 케이스가 빠진다:

- `https://example.com/whitepaper` 같은 suffix-less PDF 다운로드 링크
- 일부 기업 docs/research 사이트의 redirect (`/download/123` → `application/pdf`)
- IEEE Xplore document ID URL
- 학술 출판사의 access link

이런 URL이 들어오면 trawl은 Playwright로 PDF 뷰어 UI(Chrome/PDF.js)를 렌더하고 그
DOM을 추출 — 결과는 viewer chrome 텍스트만 나오고 PDF 본문은 0 bytes.
ARCHITECTURE.md "Future work" 항목 4번에 이미 명시된 known gap.

## 접근법

기존 `passthrough.probe()` 패턴을 그대로 빌려 **PDF용 probe 함수**를 `fetchers/pdf.py`
에 추가하고, suffix-miss 시 Playwright 진입 전에 한 번 HEAD를 친다.

```python
# src/trawl/fetchers/pdf.py
def probe(url: str, *, timeout_s: float = 3.0) -> bool:
    """HEAD url, return True if Content-Type names application/pdf.
    
    Returns False on any HTTP error, redirect issue, non-pdf type, or
    timeout. Failure must never make trawl slower than the current
    behavior — caller falls through to Playwright when False.
    """
    try:
        with httpx.Client(timeout=timeout_s, follow_redirects=True) as client:
            resp = client.head(url)
    except httpx.HTTPError:
        return False
    if resp.status_code >= 400:
        return False
    ct = (resp.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    return ct == "application/pdf"
```

`pipeline._run_full_pipeline` 의 분기 변경:

```python
if _is_pdf_url(url):
    fetched = pdf.fetch(url)
    fetcher_name = "pdf"
else:
    pt_result = _try_passthrough(url, query, t_start)
    if pt_result is not None:
        return pt_result
    # NEW: HEAD probe for PDF
    if pdf.probe(url):
        fetched = pdf.fetch(url)
        markdown = fetched.markdown
        fetcher_name = "pdf-probed"
    else:
        fetched, markdown, fetcher_name = _fetch_html(url)
```

`fetcher_name = "pdf-probed"` 로 suffix-hit과 구분 — 텔레메트리 분석 시 두
경로의 분포를 볼 수 있다.

## 성공 기준

- [ ] `pdf.probe()` 단위 테스트: HEAD 200 + `application/pdf` → True; HEAD 200 +
      `text/html` → False; HEAD 404 → False; HTTPError → False; 타임아웃 → False.
- [ ] `pipeline` integration 테스트: monkeypatched `pdf.probe` 가 True 반환 시
      `fetcher_used == "pdf-probed"` 인지 확인.
- [ ] 파리티 매트릭스 15/15 회귀 0 (HEAD 추가가 기존 케이스 latency를 크게
      늘리지 않는지).
- [ ] `agent_patterns/coding.yaml` 의 arXiv PDF 패턴이 여전히 통과 (suffix-hit
      경로는 무수정).

## 측정

- 기존 15-case 평균 fetch_ms 추가 측정 — HEAD 1회의 +50~100ms overhead 가
  실측되는지.
- C7 도입 후 `agent_patterns/coding.yaml` 의 single_fetch 패턴들에서
  `fetcher_used == "pdf-probed"` 비율 (현 시점 0%, 일부 future 패턴에서 1+).

## 리스크 & 완화

| 리스크 | 완화 |
|---|---|
| HEAD 거부 호스트 (405/501) | catch → False → 기존 동작 |
| HEAD 후 GET이 다른 ConType 반환 (rare) | post-fetch detection (별건 PR로) |
| 모든 fetch에 HEAD 추가 → 글로벌 latency 증가 | timeout 3s, fast 호스트는 50~100ms 영향. 측정 후 필요시 toggle 추가 |
| HEAD가 redirect chain 따라가다 거대 응답 | `follow_redirects=True` + httpx 기본 max_redirects(20) |

## Out of scope

- post-Playwright Content-Type 감지 (Chromium PDF viewer 렌더 직후의 재라우팅)
- HEAD 응답 캐시 (C8과 함께 다룸)
- DataDome/Cloudflare 보호된 PDF (HEAD가 챌린지 페이지 헤더 반환할 수 있음)
- `application/x-pdf` 등 비표준 변형 (필요시 추가)

## 비결정

- `fetcher_used` 값 — `pdf-probed` vs `pdf` 단일 사용 (probe 여부는 telemetry
  에 별도 필드?). 우선 `pdf-probed`로 분리, 통계 누적 후 통합 여부 판단.
