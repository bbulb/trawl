# Record detection — conclusion (2026-04-18)

Branch: `spike/record-detection`
Design: [2026-04-18-record-detection-design.md](./2026-04-18-record-detection-design.md)

**결과: ADOPT.** Parity 15/15, 3개 반복-구조 장르 추가, false-positive 필터
검증됨.

## Parity matrix

- 원 12 케이스 → 15 케이스 (wanted_jobs, hada_news, aladin_bestsellers 추가)
- **15/15 통과**
- MCP stdio smoke test 통과

### 청크 변화 (원 12 케이스, before → after)

| case | n_chunks | page_chars | top-1 rerank |
|---|---:|---:|---:|
| kbo_schedule | 2 → **8** | 930 → 1133 | 0.52 → 0.57 |
| korean_news_ranking | 49 → 86 | 21065 → 22427 | -1.97 → -2.15 |
| korean_wiki_person | 189 → 190 | 61110 → 61245 | 1.94 → 1.95 |
| pricing_page_ko | 14 → 14 | 동일 | 동일 |
| 그 외 8개 | 동일 | 동일 | 동일 |

`pricing_page_ko`는 초기 구현에서 127개로 과도 파편화되었으나
`MAX_GROUPS_PER_PAGE=8` cap 도입 후 annotation이 바이패스되어 원복됨.

### 새 케이스 요약

| case | groups | 대표 chunk score | chunks |
|---|---|---:|---:|
| wanted_jobs (`li.Card_Card__aaatv` × 20) | 1 | 4.61 | 22 |
| hada_news (`div.topic_row` × 20) | 1 | -0.64 | 21 |
| aladin_bestsellers (`div.ss_book_box` × 50) | 1 | 3.50 | 55 |

## 장르별 판정 (감사 결과)

8개 후보 URL (스포츠·뉴스·날씨·금융·쇼핑·채용·커뮤니티) 감사 결과:

| 장르 | 결과 | 비고 |
|---|---|---|
| 카드 기반 반복 (jobs / commerce / aggregator) | ✅ ADOPT | 1 그룹, 고품질 top-k |
| 카드 기반 sports schedule (KBO) | ✅ ADOPT | 주 목표 |
| 카드 기반 news ranking | ⚠️ ADOPT | 통과하나 top-1 점수 약간 하락 |
| 테이블 기반 (sports standings, finance) | 🟰 NO-OP | 기존 테이블 원자 보존 경로로 충분 |
| 복잡한 멀티-위젯 (날씨) | ❌ SKIP → profile_page | MAX_GROUPS cap 작동, VLM profile이 더 나은 경로 |
| 죽은 URL (daum news) | ❌ DROP | 404 |
| 클래스 없는 `<tr>` 반복 (HN) | 🟰 MISSED | 서명에 class 필수라 미감지, 기존 chunking이 적절히 처리 |

**결론: 장르별 분기 불필요.** 단일 감지 로직 + 필터 세트로 모든 장르가
안전하게 처리됨:
- 감지 성공 → 레코드 청크로 품질 향상
- 감지 실패 / 과도 감지 → MAX_GROUPS cap 또는 서명 미스로 기존 경로 유지
- 복잡 페이지 → `profile_page` (VLM, 쿼리 인식) 권장

## 발견·수정한 버그

감사 중 3가지 구현 버그가 드러남:

1. **U+2063 invisible separator가 trafilatura에 의해 제거됨.**
   초기 sentinel `\u2063TRAWL-REC\u20630\u20630\u2063`가 `TRAWL-REC00`으로
   축약되어 group/index 구분 소실. → ASCII-only sentinel `[[TRAWL-REC|0|0]]`
   로 전환. 모든 추출 경로 생존 확인.

2. **`<form>` 래퍼 decompose로 레코드 누락 (Aladin).** 50개 책 카드가
   `<form>` 안에 있어 `_bs_fallback`이 decompose → sentinel 있는 recall
   출력(2107자)보다 sentinel 없는 bs 출력(3834자)이 길어서 선택됨.
   → `_bs_fallback`이 sentinel 포함된 subtree는 보존하도록 수정.

3. **과도 감지 (weather).** 32 그룹 감지 → 267 청크로 파편화. 사이드바·
   지역 리스트가 대부분. → `MAX_GROUPS_PER_PAGE=8` cap 추가. 초과 시
   annotation 전체 스킵.

## 구현 파일

### 신규: `src/trawl/records.py`

- `annotate_records(html) -> (annotated_html, [RecordGroup])`
- Structural signature: `tag|sorted_classes`
- False-positive 필터: 조상 `nav|aside|footer|header|pre|code`, role=`tablist|tab|tabpanel|navigation|...`, class `tab(Item|panel|...)`, 멤버 `aria-hidden|hidden`, 기존 `NOISE_CLS_RE`, 텍스트 길이 중앙값 ≥ 20자, 서명에 class 필수, 연속 ≥ 3 sibling, MAX_GROUPS_PER_PAGE=8
- Sentinel: `[[TRAWL-REC|{gid}|{idx}]]` / `[[TRAWL-RECEND|{gid}]]`

### 변경: `src/trawl/extraction.py`

- `html_to_markdown` 첫 단계에 `annotate_records` 호출
- `records_present`일 때 sentinel 포함 추출 candidate 우선 선택
- `_bs_fallback`이 sentinel 포함 noise tag는 decompose하지 않음
- `TRAWL_RECORDS=0`으로 전체 비활성화 가능

### 변경: `src/trawl/chunking.py`

- `Chunk`에 `record_group_id`, `record_index` 필드 추가 (Optional)
- `_split_by_record_sentinels`: sentinel 라인 기준 record / non-record 분리,
  sentinel 라인은 제거
- Record span은 `max_chars`와 무관하게 원자 유지

### 추가: `tests/_audit_record_cases.py`

8 URL 감사 스크립트. 향후 장르 확장·회귀 점검에 재사용.

## Follow-up

- CLAUDE.md "Things NOT to change" 테이블에 records 상수 추가
  (`MIN_RECORDS_PER_GROUP`, `MIN_RECORD_TEXT_LEN_MEDIAN`, `MAX_GROUPS_PER_PAGE`,
  `TAB_CLS_RE`)
- `korean_news_ranking`의 top-1 -0.196 하락 경과 관찰 — rerank 입력 확대
  효과인지, 실질 품질 저하인지 다음 세션에서 검증
- 더 많은 commerce / forum 장르 케이스 추가 가능 (쿠팡, 11번가, 디시 등) —
  봇 차단·auth-wall 확인 필요
- Record group당 "요약 청크" 추가 여부 검토 (현재는 개별 record만)