# C16 — Compositional payload enrichment — design (2026-04-19)

Branch: `feat/c16-compositional-payload` (stacked on `feat/c7-head-probe-pdf` → `feat/agent-patterns-scaffold`)

## 문제

trawl은 stateless single-shot 도구다 (`CLAUDE.md` "out of scope: crawling").
그러나 agent (Claude Code, openclaw, hermes)는 자주 **체인** 호출을 한다:

1. arXiv abstract 읽기 → 저자 이름 추출 → 저자 blog 검색
2. GitHub README 읽기 → CONTRIBUTING.md 링크 따라가기
3. Wikipedia 인물 페이지 → 관련 사건 페이지로 이동

현재 trawl 응답 (`PipelineResult.chunks`) 은 markdown 청크만 반환. agent가
다음 fetch URL 또는 query를 만들려면:
- 청크 markdown을 **재파싱** → 링크 추출 (LLM 호출 또는 자체 정규식)
- 본문에서 **엔티티 인식** → query 보강 (LLM 호출)
- 호스트별 follow-up 패턴 학습 (agent 측 휴리스틱)

이 비용을 **trawl 측에서 한 번** 지불하면 agent는 LLM 호출 0회로 chained
호출이 가능하다 (= compositional 워크플로 패턴이 의미 있어짐).

## 접근법

`PipelineResult` 에 4개 필드 추가. 모두 **derived from existing data** —
LLM 호출, 네트워크 호출, 청크 본문 외 추가 데이터 없음.

```python
@dataclass
class PipelineResult:
    # ... existing fields ...
    excerpts: list[dict]         # [{chunk_idx, summary_120c}] — top-3 청크 첫 문장
    outbound_links: list[dict]   # [{url, anchor_text, in_chunk_idx}] — markdown a[href] 보존
    page_entities: list[str]     # title + heading_path → noun-phrase 후보 (간단 규칙)
    chain_hints: dict            # 호스트별 follow-up 힌트 (arxiv/github/wikipedia/youtube/SO)
```

각 필드의 추출 규칙은 `src/trawl/enrichment.py` 에 4개 함수로 분리:

```python
extract_excerpts(chunks, top_n=3, max_chars=120) -> list[dict]
extract_outbound_links(chunks, *, cap=50, bytes_cap=10240) -> list[dict]
extract_page_entities(page_title, heading_paths, *, cap=20) -> list[str]
derive_chain_hints(url) -> dict
```

### excerpts

- top-N 청크의 markdown에서 markup 제거 → 첫 줄 → 첫 문장 (`. ` / `! ` /
  `? ` / `。` / `！` / `？` 단위)
- 120자 cap, 초과 시 ellipsis(`…`) 부착
- markdown 마크업 (`**bold**`, `` `code` ``, `# heading`, ` ```fence``` `) 제거
- 한·영·일·중 문장 종결 부호 모두 처리
- 빈 청크 / 코드만 있는 청크는 skip

### outbound_links

- markdown `[anchor](url)` 패턴 (`https?://` 만), 청크 단위 순회
- image refs (`![alt](url)`) 제외 (negative lookbehind)
- 중복 dedup `(url, anchor)` 키
- 50개 entry / 10 KB byte cap (whichever first) — sidebar 폭주 방지
- 상대 경로 / `#anchor` 는 추출 안 함 (agent가 base URL 모르면 위험)

### page_entities

- `page_title` + 모든 청크의 `heading_path` 토큰을 stack
- 영어: `\b(?:[A-Z][A-Za-z0-9'-]+(?:\s+[A-Z][A-Za-z0-9'-]+)+)\b` — 2+ 연속
  대문자 시작 토큰
- 한국어: `[\uAC00-\uD7AF]{2,}` — 2+ 연속 한글
- 일본어/중국어는 명시 처리 안 함 (한자 단독으로는 noun-phrase 판별 어려움)
- dedup-preserving, 20개 cap

### chain_hints

호스트별 dict. 시작 catalog (`_HOST_HINTS`):

| host | hints |
|---|---|
| arxiv.org | recommended_followup_filter, pdf_template, abs_template |
| github.com | recommended_followup_filter, raw_template |
| en/ko/ja.wikipedia.org | recommended_followup_filter, search_template |
| youtube.com | recommended_followup_filter |
| stackoverflow.com | recommended_followup_filter, tag_template |

알 수 없는 호스트는 빈 dict 반환. agent가 호스트별 follow-up 규칙을 자체
학습 안 해도 되도록 첫 진입점.

## Pipeline 통합

`_run_full_pipeline` 와 `_build_profile_result` 두 빌드 위치에서 enrichment
호출. 둘 다 emit되는 청크 (rerank 후 top-k 또는 profile_direct 전체)를
입력으로 사용.

`to_dict()` 는 이미 `asdict()` 사용이라 새 필드 자동 직렬화. MCP 응답
schema는 backward-compatible (필드 추가만, 제거/리네임 없음).

## 성공 기준

- [ ] 25 단위 테스트 통과 (각 enricher 별 5~10 케이스)
- [ ] 4 필드가 PipelineResult dataclass의 default factory로 빈 컨테이너 생성
- [ ] `to_dict(result)` 가 4 필드를 모두 직렬화
- [ ] 기존 offline 테스트 회귀 0 (`tests/test_passthrough.py`,
      `tests/test_profiles.py`, etc.)
- [ ] (live) 파리티 매트릭스 15/15 — 추출/회수 결과는 그대로, enrichment 만
      추가됨

## 측정

- enrichment overhead per call: 청크당 정규식 4~5회 + dict 조회 1회 →
  실측 < 50 ms (대부분 outbound_links 의 link finditer)
- 응답 크기 증가: top-3 excerpt 약 360 chars + outbound_links 평균 5~30 KB
  (cap 10 KB) + page_entities ~200 chars + chain_hints ~200 chars

## 리스크 & 완화

| 리스크 | 완화 |
|---|---|
| outbound_links가 sidebar/footer link로 폭주 | hard cap 50 entry / 10 KB |
| excerpts의 첫 문장이 의미 없는 marketing copy | top-3 다양성으로 대체 — 단일 청크 의존 안 함 |
| page_entities false positive ("The Apple App Store") | 단순 noun-phrase 후보일 뿐 — agent가 사용 여부 결정 |
| chain_hints 카탈로그 유지 부담 | 시작 catalog 6개 호스트만, 패턴 발견 시 추가 |
| 응답 크기 증가가 토큰 효율 selling point 훼손 | 4 필드 합 ~10 KB 미만 → 본문(평균 1k tokens) 대비 무시 가능 |

## 비결정

- `excerpts` 첫 문장 cut의 의미 보존 — 첫 줄 우선 → 더 긴 첫 문단을 선호?
  현재 첫 줄 기준, 사용자 피드백 후 조정.
- `outbound_links` 의 `in_chunk_idx` — top-k 청크 내 chunk_index? 또는
  원본 청크 매트릭스 index? 현재 `chunk.chunk_index` (원본 매트릭스).
- `chain_hints` 가 backward-compat이긴 하지만 미래에 `chain_hints.compress`
  같은 메타-옵션이 들어가면 nested dict 바로 감당 가능한지.

## Out of scope

- LLM 기반 entity extraction (NER) — 현재 정규식 휴리스틱
- outbound_links의 anchor text 의미 분석 — agent의 책임
- compositional 패턴 자체의 자동 실행 (`fetch_chained` MCP 툴) — 명시적
  out-of-scope, agent가 chained 호출
- 전체 페이지 entity NER — page_title + heading만 사용 (성능 + scope)
