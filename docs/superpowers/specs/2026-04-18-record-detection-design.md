# Record detection — design (2026-04-18)

Branch: `spike/record-detection`

## 문제

현재 파이프라인이 **반복 DOM 구조** (경기 카드, 상품 카드, 댓글 목록 등)를
보존하지 않는다. Trafilatura가 평문으로 flatten → chunking이 `max_chars=450`
경계에서 아무데서나 자른다. KBO 일정 페이지(`kbo_schedule`)가 canonical
failure mode: parity matrix는 느슨한 ground truth로 통과하지만 실제 응답은
`page_chars≈930`, `n_chunks_total=2`로 쓸모없이 얇다.

테이블(`<table>`)만 `chunking.py`에서 원자 보존되고, `<div class="card">` × N
같은 카드 반복은 보존 로직이 없다.

## 접근법

**Structural signature 기반 sibling 반복 탐지** (Liu 2003 MDR의 경량 변형):

1. BS4로 DOM 스캔. 각 요소의 signature = `f"{tag}|{sorted(class)}"`.
2. 부모 노드 기준, 자식 중 같은 signature가 **≥3개** 연속 등장하면 record
   group 후보.
3. noise 필터: 조상 체인에 `NOISE_CLS_RE` (nav/sidebar/toc/menu/breadcrumb)
   또는 `NOISE_TAGS` (`nav`/`aside`/`footer`/`header`/`pre`/`code`) 매치하면
   제외. 멤버 자체의 태그/클래스도 동일 기준으로 재검사.
4. 탭 UI 필터: `role=tablist|tab|tabpanel`, 클래스에 `tab(Item|panel|...)`,
   멤버에 `aria-hidden=true` 또는 `hidden` 속성이 있으면 제외. 탭은 *대안*
   관계이지 병렬 레코드가 아님.
5. 레코드 텍스트 길이 중앙값 < 20자면 탭바/페이지네이션으로 보고 제외.
6. 서명에 class 필수 — 베어 `<li>` 반복은 일반 prose로 취급.
7. 남은 그룹들을 markdown 변환 직전에 **invisible separator sentinel**
   (`\u2063TRAWL-REC\u2063{gid}\u2063{idx}\u2063`)로 감싼다. 각 그룹의 끝에는
   `SENTINEL_END_PREFIX`로 마감.
8. `chunking.py`에서 sentinel 라인을 인식해 record 단위로 원자 청크를 생성.
   레코드 내부는 `max_chars`와 무관하게 절대 분할 금지 (테이블 보존과 동일
   규칙).

extraction 경로:
```
playwright.html → records.annotate_records (sentinel 주입) → trafilatura → chunking
```

Trafilatura는 HTML 주석을 제거하지만 일반 텍스트 노드는 보존. 실험으로 U+2063
기반 sentinel이 recall/precision/bs fallback 모두 통과 확인.

## 성공 기준

- **KBO 케이스**: `n_chunks_total` ≥ 5, `page_chars` ≥ 1000. 현재 2개/930자
  에서 경기 카드 단위로 분할.
- **parity matrix**: 12/12 유지 (필수).
- **false positive 체크**: pricing / article / docs 케이스에서 청크 품질 저하
  없음 — rerank top-1 score가 ±0.3 이내.

## 측정

`tests/results/<before>` vs `tests/results/<after>` per-case:
- `n_chunks_total`, `page_chars`, `output_chars`, top-1 rerank score
- KBO에 대해 chunks의 실제 내용 — 경기당 팀/시간/구장이 같은 청크에 들어
  오는지

## 리스크 & 완화

| 리스크 | 완화 |
|---|---|
| Trafilatura가 HTML 주석 제거 | 주석 대신 invisible separator 텍스트 노드 사용 — 실험 확인 |
| Docusaurus 탭 UI가 레코드로 오인 | role/class/aria-hidden 기반 필터 |
| 코드 하이라이트 `span.token-line` 오인 | `<pre>`/`<code>` 조상 필터 |
| nav/sidebar 반복을 레코드로 오인 | 기존 NOISE_CLS_RE 재사용 + 멤버 자체 태그 검사 |
| 가격표·위키 문단 bullet 오인 | 서명에 class 요구 + 텍스트 길이 중앙값 필터 |

## 비결정 (스파이크 중 판단)

- record group당 "전체 요약 청크"도 함께 넣을지 vs 개별 record만 emit할지
- 작은 카드(MIN_PLAIN_CHARS 미만)는 merge vs drop

## Out of scope

- XHR/network 로그
- DOM selector 후보 응답 필드 (profile_page 역할)
- LLM-based schema extraction (Firecrawl 영역)
