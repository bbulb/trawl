# WCXB extraction benchmark — design

Date: 2026-04-14
Source: `RESEARCH.md` 후보 C2.
Status: brainstorm approved, awaiting implementation plan.

## Goal

trawl의 `extraction.py` 품질을 **외부 공개 벤치마크(WCXB)** 로 1회 측정한다.

- 최초 실행 결과를 README에 박아 "외부 기준에서의 품질"을 공개 가능한
  숫자로 제시 (대외 주장 용도).
- 같은 러너가 앞으로 회귀 감지(Phase 2)와 내부 튜닝(Phase 3)의 기반이 되도록
  결과 포맷을 미리 충분히 넓게 잡는다.
- `tests/test_cases.yaml`의 12-케이스 parity matrix는 **그대로** 유지한다.
  WCXB는 별개 벤치마크로 `benchmarks/` 하위에만 존재한다.

## Non-goals (Phase 1)

- CI 통합.
- 서브셋 러너(`--subset=smoke`)와 Δ-vs-baseline 자동 비교.
- Readability·newspaper4k 등 추가 baseline.
- `tests/test_pipeline.py`와의 통합.
- WCXB test 스플릿(511페이지) 사용 — honest held-out set으로 남겨둔다.

## Background

### 현재 상태
- 내부 평가: `tests/test_cases.yaml` 12 케이스 + `benchmarks/profile_eval_cases.yaml`
  36 케이스. 모두 직접 선택 → 편향, 커버리지 주장 약함.
- 외부 비교 없음.

### WCXB 요약
- License: CC-BY-4.0.
- 2,008 HTML.gz 페이지 + 사람 리뷰 ground truth (dev 1,497 / test 511).
- Ground truth JSON: `main_content` (순수 텍스트, 헤딩 개별 줄),
  `with[]` (필수 스니펫), `without[]` (금지 스니펫).
- 7 page types: news, product, forum, documentation, service, listing,
  collection.
- 공식 지표: **word-level F1** — 예측/정답 텍스트를 공백 분리한 단어 집합의
  precision·recall 조화평균.

### trawl extraction
`src/trawl/extraction.py` `html_to_markdown(html) -> str`:
1. Trafilatura precision 모드 (markdown, tables, links, no images, no comments).
2. Trafilatura recall 모드 (동일 옵션, `favor_recall=True`).
3. BeautifulSoup fallback — 의도된 noise 태그만 제거, 나머지 텍스트.
4. 세 후보 중 **길이가 가장 긴** 것을 반환.

이 3-way가 순수 Trafilatura 대비 품질을 얼마나 바꾸는지가 WCXB로 답할 핵심
질문.

## Design decisions

네 번의 Q&A로 확정:

| 결정 | 선택 | 이유 |
|---|---|---|
| 스플릿 | dev 1,497만 사용 | test 511은 향후 honest held-out으로 보존 |
| 마크다운 정렬 | trawl 마크다운 출력을 그대로 WCXB `evaluate.py`에 | trawl의 실제 출력이 마크다운이므로 그 형태 그대로 측정하는 것이 공정. 동일 비교를 위해 Trafilatura baseline도 `output_format="markdown"` 으로 실행 |
| Baseline | trawl + Trafilatura 동일 환경 재실행 | 3-way + 마크다운 오버헤드가 득/실인지 직접 측정 |
| 데이터 저장 | fetch 스크립트 + gitignored 로컬 캐시 | 벤치마크 표준 관례, `tests/results/` gitignore 정책과 일관 |
| 리포트 포맷 | JSON raw + Markdown summary | 사람이 읽을 수 있고, C3·C4 후보 분석에 raw 재사용 가능 |

## Architecture

### 파일 레이아웃

```
benchmarks/wcxb/
  fetch.py              WCXB dev 스냅샷 다운로드 + 해시 검증
  run.py                러너
  evaluate.py           WCXB 공식 평가 함수 (upstream에서 vendor, CC-BY)
  ATTRIBUTION.md        CC-BY 4.0 출처 표기
  data/                 gitignored — 다운로드된 HTML.gz + ground truth JSON
  README.md             one-shot 실행 안내

benchmarks/results/
  wcxb_<timestamp>/
    raw.json
    report.md
```

### 구성 요소

- **`fetch.py`** — upstream WCXB repo에서 dev 스플릿 1,497 HTML.gz + 대응
  JSON 다운로드. 해시 매니페스트로 재현성 보장. idempotent (이미 있으면 skip).
  네트워크 실패 시 exit 1 + 부분 상태 표시.
- **`run.py`** — `data/` 순회, 각 페이지에 대해:
  1. `trawl.extraction.html_to_markdown(html)` 실행 + 시간 측정.
  2. `trafilatura.extract(html, **trawl과 동일한 옵션)` 실행 + 시간 측정.
     trawl 호출과 옵션을 맞춰야 "덧붙인 3-way + BS fallback의 효과"를
     고립시킬 수 있다. precision/recall 플래그는 Trafilatura 기본값.
  3. WCXB `evaluate.py`로 두 출력 vs `ground_truth.main_content` word-F1,
     precision, recall 계산.
  4. `with[]` / `without[]` 스니펫 존재 여부 체크 → boolean 카운트 기록
     (raw에만, 요약에선 참고용).
  5. dict 한 건 append.
- **`evaluate.py`** — WCXB upstream의 평가 로직을 그대로 vendor. 외부
  네트워크 의존 제거, 버전 고정. Upstream 변경 시 수동 재동기화 (Phase 2 주제).
- **집계 & 리포트** — 전체·타입별 평균 F1, median time. Δ = trawl F1 −
  trafilatura F1 기반 상·하위 10개 페이지. 에러 카운트.

### 데이터 흐름

```
upstream WCXB repo
   | fetch.py (one-shot)
   v
benchmarks/wcxb/data/*.html.gz + *.json  (gitignored)
   |
   | run.py: for each page
   |   html = gunzip
   |   trawl_out = html_to_markdown(html)
   |   traf_out  = trafilatura.extract(html, markdown opts)
   |   f1s       = evaluate(trawl_out, traf_out, ground_truth)
   v
benchmarks/results/wcxb_<ts>/{raw.json, report.md}  (gitignored)
   |
   | manual: copy 2-row summary into README.md
   v
README Evaluation section
```

## Runner interface

```bash
python benchmarks/wcxb/fetch.py               # 데이터 받기 (최초 1회)

python benchmarks/wcxb/run.py                 # dev 1,497 전체, trawl + baseline
python benchmarks/wcxb/run.py --limit 50      # 스모크
python benchmarks/wcxb/run.py --type forum    # 타입 필터 (7종)
python benchmarks/wcxb/run.py --no-baseline   # trawl만, 속도 최적화용
```

### Error handling

- 빈 출력 → F1=0.0 기록, 계속 진행.
- 예외 → `error: "<ExceptionType>: <msg>"`에 기록, F1=null, 집계 제외.
- 전체 실패율 ≥ 5% → `run.py` exit 1 + stderr 경고.
- tqdm 같은 의존성 추가 없음. 매 100 페이지마다 stderr 한 줄 진행 로그:
  `[N/1497] trawl avg F1=0.93, traf avg F1=0.94`.

## Report formats

### `raw.json`

페이지별 한 항목씩의 배열. 후속 분석(C3·C4)에서 재활용.

```json
[
  {
    "id": "news_bbc_0042",
    "url": "https://...",
    "page_type": "news",
    "trawl":      {"f1": 0.94, "precision": 0.92, "recall": 0.96,
                   "time_ms": 31, "output_len": 2104, "error": null},
    "trafilatura":{"f1": 0.96, "precision": 0.95, "recall": 0.97,
                   "time_ms": 18, "output_len": 1890, "error": null},
    "with_snippets_hit":    {"trawl": 5, "trafilatura": 5, "total": 5},
    "without_snippets_hit": {"trawl": 0, "trafilatura": 0, "total": 3}
  }
]
```

### `report.md`

```markdown
# WCXB dev benchmark — 2026-04-14 15:23

Corpus: WCXB dev split, 1,497 pages, 7 page types.
Commit: <short sha>

## Overall
| Extractor   | F1    | Precision | Recall | Median time |
|-------------|-------|-----------|--------|-------------|
| trawl       | 0.938 | 0.921     | 0.957  |  28 ms      |
| trafilatura | 0.951 | 0.943     | 0.960  |  17 ms      |

Δ F1 (trawl − trafilatura) = −0.013

## By page type
| Type        |  N  | trawl F1 | traf F1 | Δ      |
|-------------|-----|----------|---------|--------|
| news        | 412 | 0.961    | 0.968   | −0.007 |
| product     | 203 | 0.891    | 0.847   | +0.044 |
... (7 rows)

## Top 10 trawl wins (Δ F1)
(ids + Δ values)

## Top 10 trawl losses (Δ F1)
(ids + Δ values)

## Errors
trawl: 0 / 1497
trafilatura: 2 / 1497 (ids: ...)
```

## Repository integration

- `.gitignore` 추가: `benchmarks/wcxb/data/`, `benchmarks/results/wcxb_*/`.
- `environment.yml`: 변경 없음. trafilatura, beautifulsoup4, lxml은 이미 의존성.
- `CLAUDE.md` Quick Reference에 한 줄 추가:
  ```
  # WCXB extraction benchmark (one-shot)
  python benchmarks/wcxb/fetch.py && python benchmarks/wcxb/run.py
  ```
- `CLAUDE.md` Code layout의 `benchmarks/` 블록에 `wcxb/` 하위 엔트리 추가.
- `benchmarks/wcxb/ATTRIBUTION.md`: CC-BY-4.0 WCXB 출처·버전·해시 명시.

## README integration (post-run)

`README.md`의 Evaluation 섹션에 1회 반영:

- 2행짜리 표: trawl F1 vs 동일 환경 Trafilatura F1 (WCXB dev 1,497).
- 한 줄: "내부 12-case parity matrix에 더해, WCXB 공개 벤치로 외부 교차
  검증한 결과"라는 맥락.
- 타입별 분해 숫자는 `benchmarks/wcxb/report.md`로 링크.

## Success criteria

- `python benchmarks/wcxb/fetch.py` 가 idempotent하고 해시로 무결성 검증.
- `python benchmarks/wcxb/run.py` 가 1,497 페이지를 에러율 <5%로 완주.
- `raw.json`·`report.md` 가 생성되고, `report.md`의 2행 요약을 그대로
  README에 붙여넣을 수 있음.
- **러너 sanity check**: `run.py` 내부에 Trafilatura **default-mode**
  (markdown 플래그 없이, upstream 공개 측정과 동일 조건) 보조 경로를 한 번
  실행해 F1이 WCXB dev 전체 기준 공개 값 **0.791** 과 **±0.025 이내**로
  재현되는지 확인. 이게 벗어나면 러너 구현 또는 vendor한 `evaluate.py`가
  잘못된 것. 이 sanity check는 리포트에 `sanity: traf_default_f1 = 0.7xx`
  한 줄로만 남기고 메인 비교(marked 모드) 표와 섞지 않는다.

  > **주의**: 이 기준치는 WCXB dev 스플릿 1,497 pages 전체(7 page types)
  > 기준이다. 초기 설계 문서에 잠시 박혔던 "0.958 ±0.02"는 **article-only
  > 구버전 수치**였고, 전체 스플릿과 다른 숫자이므로 혼동 금지. 2026-04-14
  > 실제 실행에서 측정값 0.773(±0.018 vs 0.791)으로 vendor 정상 확인됨.

## Out of scope / future

- **Phase 2 (회귀 감지)**: `--subset=smoke` 스모크 세트 선정, CI 훅,
  Δ-vs-baseline 자동 비교 + 한계 초과 시 exit 1.
- **Phase 3 (내부 튜닝)**: 3-way 선택 로직의 per-type 최적값 탐색. 추가
  baseline (Readability, newspaper4k) 옵트인.
- WCXB test 스플릿 511페이지는 **Phase 2에서도 건드리지 않는다**; 향후
  논문/릴리스 시 honest held-out 숫자로만 사용한다.

## Open risks

1. **WCXB upstream의 평가 스크립트 형식 변화** — vendor한 `evaluate.py`가
   upstream에서 바뀌면 수동 재동기화 필요. Phase 1에선 commit hash 고정
   기록으로 대응, Phase 2에서 자동화.
2. **trawl의 마크다운 오버헤드가 F1을 크게 깎을 경우** — 설계 §Q2에서
   감안했으나, 실측 Δ가 크면 "stripped F1"을 보조 지표로 추가할지 검토.
   결정은 실측 결과 보고.
3. **다운로드 용량** — dev 1,497 HTML.gz 실측 크기는 fetch 스크립트
   돌려봐야 확인. 200MB 넘으면 메타파일 기반 lazy fetch 재고려.
