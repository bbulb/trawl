# Record detection — conclusion (2026-04-18)

Branch: `spike/record-detection`
Design: [2026-04-18-record-detection-design.md](./2026-04-18-record-detection-design.md)

**결과: ADOPT.** Parity 12/12 유지, KBO 목표 달성, false positive 필터 검증됨.

## Parity matrix

Before: `tests/results/20260417-193404`
After:  `tests/results/20260418-171623`

**12/12 통과.** MCP stdio smoke test 통과.

## 청크 변화 (per-case)

| case | n_chunks | page_chars | top-1 rerank score |
|---|---:|---:|---:|
| kbo_schedule | 2 → **8** | 930 → 1109 | 0.519 → 0.574 |
| korean_news_ranking | 49 → 86 | 21065 → 22289 | -1.973 → -2.169 |
| pricing_page_ko | 14 → 127 | 11609 → 14328 | 2.052 → 2.305 |
| 그 외 9개 | 동일 | 동일 | 동일 |

## 관찰

### 주 목표 달성

KBO 일정 페이지가 2청크 → 8청크로 확장. 경기당 `팀명 + 시작시간 + 구장`이
하나의 청크 내에 원자 단위로 담긴다. 사용자 쿼리 "오늘 야구 경기 일정"에
대해 각 경기 카드가 분해되지 않고 반환됨. 외부 에이전트 피드백에서 "일정
데이터가 iframe/XHR/동적 DOM에 있는지 판단 불가"로 기술된 근본 증상이 청크
품질 측면에서 해소됨.

### 부작용 관찰

**korean_news_ranking** (49 → 86): 랭킹 아이템이 레코드로 감지되어 청크가
세분화. top-1 rerank score -0.196 하락했으나 여전히 ground truth 통과. 뉴스
아이템은 원래 각자 독립적이므로 세분화가 의미론적으로는 옳음. rerank 하락은
작은 청크가 많아져 rank 경쟁이 치열해진 것으로 추정.

**pricing_page_ko** (14 → 127): 가격 카드와 기능 불릿이 레코드로 감지되어
청크 수가 9배 증가. top-1 score +0.253 상승했으나 청크 파편화가 과도함.
retrieval·rerank 지연에 영향 가능 — 실측 필요.

### false-positive 필터 검증

**Docusaurus tabs** (english_tech_docs): `role=tablist` / `.tabItem_*`
클래스로 감지되어 스킵. 영향 없음.

**Code syntax highlighter** (`<span class="token-line">`): `<code>` 조상
필터로 스킵. 영향 없음.

**Naver section nav** (`<nav class="SportsHeader_section_links_*">`): 멤버
태그 자체가 `<nav>`이므로 멤버-검사로 스킵 추가. 영향 없음.

## 구현 요약

### 신규 모듈: `src/trawl/records.py`

- `annotate_records(html) -> (annotated_html, [RecordGroup])`
- Structural signature: `tag|sorted_classes`
- False-positive 필터 9단계 (design 문서 참고)
- Sentinel: `\u2063TRAWL-REC\u2063{gid}\u2063{idx}\u2063` / `\u2063TRAWL-RECEND\u2063{gid}\u2063`

### `src/trawl/extraction.py`

`html_to_markdown` 첫 단계에 `annotate_records` 호출. `TRAWL_RECORDS=0`으로
비활성화 가능. 실패 시 annotation을 무시하고 원본 HTML로 진행 (best-effort).

### `src/trawl/chunking.py`

- `Chunk` dataclass에 `record_group_id`, `record_index` 필드 추가 (Optional)
- `_split_by_record_sentinels`: sentinel 라인을 기준으로 record / non-record
  span 분리. sentinel 라인은 최종 출력에서 제거됨
- Record span은 `max_chars`와 무관하게 원자 유지 (테이블 보존 규칙과 동일)

## Follow-up 후보

- `benchmark_cases.yaml`에 반복-구조 카테고리 추가 (e-commerce 목록, 댓글,
  포럼 스레드)
- pricing_page의 127개 파편화 영향 실측 — rerank 지연, 유효 top-1 품질
- record group당 "요약 청크"도 emit할지 여부 검토 (현재는 개별 record만)
- CLAUDE.md "Things NOT to change" 테이블에 records 상수 추가
  - `MIN_RECORDS_PER_GROUP=3`, `MIN_RECORD_TEXT_LEN_MEDIAN=20`, TAB_CLS_RE,
    NOISE 확장
