# C6 follow-up — Playwright shadow-DOM traversal — design (2026-04-20)

Branch: `spike/playwright-shadow-dom` (off `develop` at `2b7a96e`).

Parent context:
[2026-04-20-mdn-reranker-diagnostic-design.md](2026-04-20-mdn-reranker-diagnostic-design.md)
and its outcome note (`notes/mdn-reranker-diag-outcome.md`). The
diagnostic proved that the C6 MDN failure is not retrieval-layer
and not reranker-layer — it is **fetch-layer**. MDN renders code
blocks via 23 `<mdn-code-example>` custom elements backed by Shadow
DOM. Playwright's `page.content()` does not traverse shadow roots, so
the extractor never sees `JSON.stringify` / `method: 'POST'` /
`body:` / `application/json` that live inside them.

This spike closes that gap by inlining shadow-root `innerHTML` into
the light DOM **before** `page.content()` is called, so Trafilatura
sees the code blocks.

## 사전 검증 (이 design doc 쓰기 전)

`mdn_reranker_diag.py` probe + `page.evaluate()` 직접 호출로 다음을
확인:

1. MDN `/Using_Fetch` 페이지에 `<mdn-code-example>` 태그 23 개.
2. 각 element 의 `shadowRoot.innerHTML` 에 완전한 code block HTML
   (`<pre class="brush: js notranslate"><code>` + syntax-highlight
   span) 이 존재.
3. shadowRoot 내용 에 `JSON.stringify`, `method: "POST"`, `body:`,
   `application/json`, `Content-Type` 모두 포함 (assertion 이
   요구하는 identifier 전부).
4. `networkidle` + 2s wait 시점 이미 shadow DOM 은 hydration 완료.

즉 **content 는 있고 접근 가능** — 단지 `page.content()` 가
shadow root 를 skip 하기 때문에 not reachable. 이건 순수한
extraction 수정 작업.

## 문제 요약

```html
<!-- page.content() 가 보는 것 -->
<p>... using code like this:</p>
<mdn-code-example class="brush: js notranslate"></mdn-code-example>

<!-- shadowRoot.innerHTML 안에 실제로 있는 것 -->
<pre class="brush: js notranslate"><code>
const response = await fetch("https://example.org/post", {
  method: "POST",
  body: JSON.stringify({ username: "example" }),
});
</code></pre>
```

Assertion `chunks_contain_any: ["JSON.stringify", "Content-Type",
"method:"]` 은 light DOM 만 보는 extractor 에 의해 항상 fail.

## 접근법

### 코드 변경 (narrow scope)

`src/trawl/fetchers/playwright.py` 단일 파일. 세 가지 추가:

1. **`SHADOW_DOM_UNWRAP_TAGS`** — tuple of custom-element tag names
   whose shadow-root 내용을 inline 할 것. 초기값: `("mdn-code-example",)`.
   allow-list 방식 — 무차별 unwrap 은 페이지 프레임워크 내부 chrome
   (copy buttons, nav) 을 오염시킬 수 있어 금지.
2. **`_unwrap_shadow_dom(page)`** — `_open_context` 에서
   `_wait_for_content_ready` 직후, `page.content()` 직전 호출.
   `page.evaluate()` 로 DOM 순회하며 allow-list 매칭 element 의
   `el.innerHTML = el.shadowRoot.innerHTML` 치환. idempotent, no-op
   on pages without matching elements.
3. **Env gate `TRAWL_SHADOW_DOM_UNWRAP`** — default `"1"` (on).
   `"0"` 로 disable 가능. opt-out 용 — "disable" 이 디폴트가 아니라
   "opt-out" 만 제공 (correctness fix 성격).

### 왜 default ON 인가 (C6 / tokenizer spike 와 다른 이유)

- C6 hybrid retrieval: **랭킹 알고리즘 변경** → 다른 slice 에서 역효과
  가능 → 기본 off 후 measurement 로 default 전환 여부 판단. 보수적.
- BM25 tokenizer / HyDE extras: **retrieval scoring 구조 변경** →
  동일 이유 로 opt-in 후 측정.
- 본 spike: **extraction correctness fix** → 원래 페이지 의도대로
  "visible 콘텐츠" 를 capture 하는 것. C8 (per-fetch cache) / C9
  (per-host ceiling) 과 같은 범주 — 이전 behavior 가 버그에 가깝고
  신 behavior 가 correct. 기본 on, 필요 시 opt-out.
- 단, allow-list 가 좁을 때만 이 가정 성립. `SHADOW_DOM_UNWRAP_TAGS`
  가 MDN tag 하나뿐 이면 non-MDN 페이지 에 0 영향. 향후 tag 추가
  시마다 allow-list 재평가.

### 비-목표

- **광범위 shadow DOM unwrap (all tags) 금지.** 프레임워크 내부
  custom element (copy buttons, UI chrome) 이 Trafilatura 에 noise
  투입. allow-list 유지.
- **MDN 전용 프로파일 rule 도입 금지.** 현재 프로파일 시스템 은
  CSS selector 기반 main content 선택용. shadow DOM 은 그 이전
  단계 문제 — fetcher layer 에서 해결 하는 게 맞음.
- **reranker / retrieval 변경 금지.** 본 spike 는 fetch-layer only.
- **Playwright wait 재설계 금지.** 기존 `_wait_for_content_ready` 가
  사전 probe 에서 shadow hydration 충분 확인. 추가 shadow-specific
  wait 는 불필요 (2초 이내 hydration 완료).

## Pre-registered decision gates

baseline = `TRAWL_SHADOW_DOM_UNWRAP=0` 의 현재 develop 동작 (PR #30
이후 15/16 pass, MDN 만 fail).

| 결과 | 조건 | 액션 |
|---|---|---|
| **(a) 채택** | (1) `TRAWL_SHADOW_DOM_UNWRAP=1` 로 MDN pattern FAIL → PASS, (2) `code_heavy_query` 16 패턴 **net_assertion_delta ≥ +1 AND flipped_to_fail == 0**, (3) `tests/test_pipeline.py` 15/15 유지 | `feat(fetchers): inline shadow-DOM content for known custom elements` PR 로 머지. env var 유지 (opt-out 용). default on. `CLAUDE.md` knobs 표 업데이트. |
| **(b) 기각** | MDN flip fails OR net_delta < +1 | design doc + runner 만 기록. 변경 revert. `RESEARCH.md` pointer 를 URL re-targeting / assertion 완화 로 갱신. |
| **(c) 파리티 회귀** | `TRAWL_SHADOW_DOM_UNWRAP=1` 에서 파리티 < 15/15 OR `code_heavy_query` 에서 regression 발생 | 변경 revert. 회귀 case 기록. shadow DOM allow-list 좁히거나 접근 방식 재평가. |

### Threshold 선택 근거

- **MDN flip 은 필수 조건**. 진단에서 확증된 원인 을 고친 spike 이므로
  MDN 이 안 풀리면 이 spike 의 전제 (shadow DOM 접근 으로 extraction
  완성) 자체가 틀린 거. 반드시 PASS 해야 함.
- **net_delta ≥ +1 (사실상 MDN 한 건)**: `code_heavy_query` 안에서
  MDN 이 유일 실패 → MDN 이 flip 하면 +1, 다른 패턴 이 regression
  없으면 15 → 16 (100%). 만약 MDN flip 해도 다른 패턴 이 fail 하면
  net_delta < 1 → gate 탈락 → revert.
- **flipped_to_fail == 0**: shadow DOM unwrap 이 MDN 외 페이지 에 악영향
  없는지 확인. `<mdn-code-example>` 는 MDN 에만 있으므로 이론상 0.
  가능한 side effect: (a) `<mdn-code-example>` 가 다른 페이지 에도
  있다면 content 갑자기 다량 추가 → chunk 수 변화 → 랭킹 ripple. (b)
  innerHTML assignment 가 페이지 state 를 훼손 → 뒤따르는 `page.content()`
  실패. 둘 다 measurement 로 확인.
- **parity 15/15**: 15-case 파리티 에 MDN 은 없으므로 shadow DOM unwrap
  이 영향 없어야 정상. 회귀 발생 시 allow-list 수정 or revert.

## 파일 변경 (spike 전체)

- `docs/superpowers/specs/2026-04-20-playwright-shadow-dom-design.md`
  — 본 문서 (신규, PR 포함).
- `src/trawl/fetchers/playwright.py` — `SHADOW_DOM_UNWRAP_TAGS`
  상수 + `_unwrap_shadow_dom` helper + call site + env gate. gate (a)
  통과 시 유지. (b)(c) 시 revert.
- `tests/test_playwright_shadow_dom.py` — 신규 unit test (optional:
  mock Playwright or integration-test MDN URL).
- `benchmarks/shadow_dom_sweep.py` — 측정 러너 (신규, PR 포함).
- `notes/shadow-dom-measurement.md` — 결과 + 결론 (gitignored).

## 측정 계획

### 실행 순서

1. `mamba activate trawl`; `:8081` / `:8083` healthcheck (`:8082` HyDE
   는 이번 spike 에서 불필요).
2. 코드 구현.
3. `python benchmarks/shadow_dom_sweep.py --dry-run` — 계획 확인.
4. `python benchmarks/shadow_dom_sweep.py` — 본 측정.
   - 2 modes × 16 patterns × 2 iter = 64 runs.
   - MDN 특이 여부: HyDE 없음, rerank 기본값 유지. 순수 fetch/extract
     변경 만 측정.
   - 결과: `benchmarks/results/shadow-dom-sweep/<ts>/`.
5. Parity:
   - `TRAWL_SHADOW_DOM_UNWRAP=1 python tests/test_pipeline.py` →
     15/15 요구.
   - Baseline (unwrap=0) 은 이미 develop 상태 로 15/15 가 알려져
     있으므로 skip 가능. 그래도 sanity 로 한 번 실행.
6. `notes/shadow-dom-measurement.md` 작성 + gate decision 적용.

### Summary.json 스키마

```json
{
  "generated_at": "...",
  "iterations": 2,
  "modes": ["shadow_dom_off", "shadow_dom_on"],
  "baseline_mode": "shadow_dom_off",
  "parity": {
    "shadow_dom_off": { ... 15/15 ... },
    "shadow_dom_on":  { ... 15/15 ... }
  },
  "per_mode": { ... },
  "diff_vs_baseline": {
    "shadow_dom_on": {
      "flipped_to_pass": ["claude_code_mdn_fetch_api"],
      "flipped_to_fail": [],
      "top1_identity_changed": N,
      "net_assertion_delta": N
    }
  },
  "gate_decision": "a_adopt" | "b_reject" | "c_regression"
}
```

## 리스크

1. **`<mdn-code-example>` tag 가 다른 페이지 에 존재 하나?** MDN 이
   외의 페이지 에는 이 tag 가 없을 가능성 높음 — 다른 페이지 성능
   영향 ≈ 0. 그래도 measurement 중 확인: `per_mode.patterns[].n_chunks_total`
   비교.
2. **Playwright evaluate 실패 / 예외 처리.** `el.shadowRoot` 이 null
   이거나 cross-origin shadow root 면 skip. try/catch 로 보호.
3. **MDN syntax-highlight `<span class="token ...">` 가 chunking 에
   노이즈 추가.** html_to_markdown 이 이를 strip 하는지 확인. Markdown
   변환 단계 에서 ` ```...``` ` 로 감싸야 code block 인식 가능.
4. **assertion 키워드 는 MDN flip 되면 정확히 맞지만, chunking 경계
   에서 code 와 식별자 가 분리 되면 `JSON.stringify` substring 이
   여전히 split 될 수 있음.** 사전 probe 가 assertion 3 identifier 모두
   shadow innerHTML 에 존재 확인 했으니 low risk. 그래도 측정 에서
   확인.
5. **allow-list 가 좁아서 future 페이지 에서 같은 문제 재발.** 본
   spike 는 MDN 만 타겟. 향후 각 vendor 커스텀 element 가 확인 되면
   `SHADOW_DOM_UNWRAP_TAGS` 에 추가 + 재측정. 허용된 점진적 확장.
6. **hydration 타이밍 이 host-stats 에 의해 줄어든 wait ceiling 보다
   느린 host.** 사전 probe 에서 MDN 은 networkidle + 2s 이내 hydration
   완료 확인. host_stats 는 network+content-ready 시간 based 이므로
   shadow hydration 도 포함된 상태. low risk.

## 측정 범위 (작게 유지)

- **패턴 slice**: `code_heavy_query` 16 개 (기존 C6 계열 스펙 과 동일).
- **Parity**: 15-case matrix (shadow_dom_on 한 번 실행; off 는
  이미 develop 에서 검증 됨).
- **Iteration**: mode × pattern 당 2 iter (재현성 확인).
- **추가 slice 없음**: search_ddg / wiki / finance 등 은 측정 하지
  않음. `<mdn-code-example>` 가 거기 없다는 assumption. 만약 future
  에 unwrap tag 가 다른 vendor 에도 추가 되면 slice 확장 필요.

## Follow-ups (본 spike 범위 밖)

1. **Custom element allow-list 확장.** docusaurus, GitBook, API
   reference 사이트 중 유사 패턴 조사. 발견되면 추가 measurement +
   별도 PR.
2. **Shadow DOM unwrap 의 `page.content()` 보다 일반적 추출 방식**.
   현재 방식 은 light DOM 에 merge 하고 capture 하는 2 단계. 만약
   `page.locator(...).inner_html()` 이 shadow root 를 traverse 하면
   단일 단계로 가능. 측정 단순화 에 가치.
3. **Default-on with environment-level opt-out guide.** 현재 `TRAWL_
   SHADOW_DOM_UNWRAP` 은 env 로만 toggle. 필요 시 fetch 시 parameter
   로도 노출 (per-call opt-out). 사용자 보고 있으면 별도 PR.
4. **Assertion 재평가 (MDN)**: spike 통과 하면 assertion 강화 가능
   — 현재 `chunks_contain_any` 는 3 중 1 만 요구. 이제 code block 가
   정상 추출 되면 `chunks_contain_all: ["JSON.stringify", "Content-Type"]`
   으로 강화 고려. 단, test_agent_patterns harness 의 다른 assertion
   들과 균형 필요.

## 첫 행동 체크리스트

1. `git checkout -b spike/playwright-shadow-dom` (실행됨).
2. 본 design doc commit.
3. `src/trawl/fetchers/playwright.py` 수정 (SHADOW_DOM_UNWRAP_TAGS
   + _unwrap_shadow_dom + call site + env gate).
4. `benchmarks/shadow_dom_sweep.py` 작성.
5. MDN 단일 패턴 sanity: `TRAWL_SHADOW_DOM_UNWRAP=1` 로
   `fetch_relevant` 호출 후 chunks 에 `JSON.stringify` 들어가는지 확인.
6. Full sweep + parity.
7. Gate decision + outcome note + PR.
