# API 계약 (백엔드 ↔ 프론트)

- 버전: **0.1.0** (2026-10-20, 초안)
- 백엔드 `127.0.0.1:8100`, 프론트는 8600에서 같은 경로로 프록시한다.
- 예시 스트림: `doc/contract/stream_easy.sse`, `stream_hard.sse`, `stream_error.sse`, `stream_v1.sse`

## 변경 이력

| 버전 | 날짜 | 변경 |
|---|---|---|
| 0.1.0 | 2026-10-20 | 최초 작성 |

## 1. 엔드포인트

### `GET /health`
```json
{"status": "ok", "version": "v2", "llm": "ollama:qwen3:8b", "data_month": "202609"}
```

### `POST /chat`
요청
```json
{"message": "우리SUPER주거래적금 12개월 최고금리 얼마야?", "thread_id": "t-1"}
```
응답 = 스트림의 `FinalAnswer` 이벤트에서 `type`을 뺀 객체 (아래 3장). 실패하면 HTTP 4xx/5xx와
`{"code": "...", "message": "..."}`.

### `GET /chat/stream?message=...&thread_id=...`
- `Content-Type: text/event-stream`
- 이벤트마다 `data: <JSON 한 줄>\n\n`, 마지막은 항상 `data: [DONE]\n\n`
- 순서: 진행 이벤트 0개 이상 → `FinalAnswer` 또는 `Error` 정확히 1개 → `[DONE]`
- **답변 토큰은 스트리밍하지 않는다.** 검증 전 문장이 화면에 나갔다가 바뀌는 일을 막기 위해, 진행 단계만
  보내고 검증된 답은 `FinalAnswer`로 한 번에 보낸다.
- 프론트는 모르는 `type`을 무시해야 한다(하위 호환으로 이벤트가 추가될 수 있음).

## 2. 진행 이벤트

| type | 언제 | 필드 |
|---|---|---|
| `RouteDecided` | 첫 이벤트 (v2) | `route`: `chat` \| `out_of_scope` \| `easy` \| `hard`, `reason`: 문자열 |
| `PlanCreated` | hard만 | `tasks`: `[{id, agent, desc}]`, agent = `structured` \| `rag` \| `calc` |
| `TaskCompleted` | 조사 작업마다 | `id`, `agent`, `ok`: bool, `retries`: int, `elapsed_ms`: int |
| `ReplanCheck` | hard만 | `round`: int, `is_sufficient`: bool, `reason`: 문자열 |
| `Validation` | 답 작성 후 | `attempt`: int, `verdict`: `pass` \| `fail`, `reason`: 문자열 \| null |

`route` 의미: `chat` 인사·잡담(검색 없음), `out_of_scope` 금융상품 범위 밖(정중히 거절),
`easy` 단일 상품 사실 조회(즉답 경로), `hard` 비교·집계·계산(계획·도구·검증 경로).

## 3. 종료 이벤트

### `FinalAnswer`
```json
{
  "type": "FinalAnswer",
  "reply": "사용자에게 보여줄 최종 답 (마크다운 가능)",
  "route": "easy",
  "products": [ProductCard],
  "citations": [Citation],
  "calculations": [Calculation],
  "warnings": ["문자열"],
  "metadata": Metadata
}
```

**ProductCard** — 답에 등장한 상품
```json
{"id": "saving_202609_0010001_WR0001F", "kind": "saving", "kind_ko": "적금", "group": "bank",
 "bank": "우리은행", "product": "우리SUPER주거래적금", "sale": "판매중",
 "highlight": {"label": "12개월 최고금리", "value": 3.85, "unit": "%"},
 "data_flags": []}
```
- `kind`: `deposit` | `saving` | `mortgage` | `rent` | `credit`, `group`: `bank` | `savingsbank`
- `highlight`는 없을 수 있다(null). `value`는 숫자, 표시 형식은 프론트가 정한다.
- `data_flags`: 공시 데이터 이상 경고 문자열(예: 평균금리가 범위 밖). 비어 있지 않으면 경고 아이콘으로 표시.

**Citation** — 답의 근거가 된 원문
```json
{"product_id": "saving_202609_0010001_WR0001F", "section": "우대조건", "quote": "가.급여/연금 이체:연 0.7%p"}
```

**Calculation** — 계산 도구 결과 (hard에서만)
```json
{"name": "적금 단리 세전이자", "inputs": {"monthly_won": 500000, "term_months": 12, "rate_pct": 2.45},
 "formula": "월납입 × T(T+1)/2 × 연이율 / 1200 (원 미만 절사)", "result": 79625, "unit": "원"}
```

**warnings** — 항상 1개 이상. 기본 문구:
`"2026년 9월 공시 기준이며 실제 적용 금리는 개인 조건에 따라 다릅니다. 가입·대출 권유가 아닙니다."`

**Metadata**
```json
{"version": "v2", "latency_ms": 8423, "llm_calls": 2, "retries": 0, "validation_attempts": 1,
 "cost_usd": 0.0172, "data_month": "202609", "thread_id": "t-1"}
```
- `cost_usd`는 로컬 추론 기준 가상 단가로 계산한 값이다(실과금 아님).

### `Error`
```json
{"type": "Error", "code": "LLM_UNAVAILABLE", "message": "로컬 LLM에 연결하지 못했습니다."}
```
`code`: `BAD_REQUEST` | `LLM_UNAVAILABLE` | `TOOL_FAILED` | `TIMEOUT` | `INTERNAL`

## 4. v1(베이스라인)과의 차이

v1은 라우팅·계획·검증이 없어 진행 이벤트 없이 `FinalAnswer`만 보낸다. 이때 `route`는 `"baseline"`,
`products`·`citations`·`calculations`는 빈 배열, `metadata.version`은 `"v1"`이다. 프론트는 둘 다 처리해야 한다
(발표 화면에서 Before/After를 같은 UI로 보여주기 위함).
