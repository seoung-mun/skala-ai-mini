# API 계약 (백엔드 ↔ 프론트)

- 버전: **0.11.0** (2026-10-22)
- 백엔드 `127.0.0.1:8100`, 프론트는 8600에서 같은 경로로 프록시한다.
- 예시 스트림: `doc/contract/stream_easy.sse`, `stream_hard.sse`, `stream_error.sse`, `stream_v1.sse`

## 변경 이력

| 버전 | 날짜 | 변경 |
|---|---|---|
| 0.11.0 | 2026-10-22 | LLM 라우팅 전환(하위 호환 필드 추가). `RouteDecided`에 `operation`, `comparison_scope`, `rank`, `conditions`, `validation_attempts` 추가, `intent`에 거절 사유 `future_rate` \| `unsupported` 추가, 라우터가 조건을 되물으면 `RouteDecided` 다음 바로 `FinalAnswer`. 답변 상품 id를 다음 턴 조건으로 저장 |
| 0.10.0 | 2026-10-21 | 검증 기준 정리: 대상·조건·항목·수치·단위가 틀리면 반려, 표·순서·의역은 허용. 근거와 연결하지 못한 수치·설명은 반려하지 않고 `Validation.unverified`(선택, 문자열 배열)로 알리며 답 끝에 확인 안내를 붙임. 답에 쓴 계산식이 틀리면 반려. 미래 금리 확정 질문은 거절 응답. 이자 계산 Decimal 절사(원 미만 버림). 추가 필드만 있어 기존 클라이언트 호환 |
| 0.9.0 | 2026-10-21 | 사용자 조회 조건을 자동 완화하지 않음. 계산용 연이율과 우대 폭 분리, 상품 이자·상환 방식 보존, 후속 계산 조건 유지, 상품·기간·금리 종류별 근거 검증. 실패한 모델 응답 사용량도 합산. 외부 응답 필드 유지; 내부 MCP 조회 행에 `interest_type`, `repayment`, `rpay_type_nm` 선택 필드 추가 |
| 0.8.0 | 2026-10-21 | `POST /sessions/reset`로 해당 대화의 서버 이력·슬롯 초기화. 같은 대화 요청과 초기화는 직렬 처리. 화면은 성공 후 새 ID로 전환하고 실패 시 기존 대화를 보존. 가입정보의 근거 없는 추가 주장·원문 부정 반려 및 비교 대상 밖 검색 후보 제외 |
| 0.7.0 | 2026-10-21 | `metadata.writing_runs`에 작성 회차별 입력·출력 토큰 및 제공 가능한 모델 로딩·입력 처리·생성 시간(ms) 추가. 명시적 가입정보 인용은 원문에서 코드로 구성. 대화는 본문·카드, 분석·출처는 별도 화면 |
| 0.6.0 | 2026-10-21 | `RouteDecided.intent` 추가(`recall`, `finance_followup`, `finance_new`, `chat`, `out_of_scope`). 검증 범위에 명시적 요청 항목의 누락 검사 추가. 회상은 상품 카드 없이 대화 기록에서 응답 |
| 0.5.0 | 2026-10-21 | `AnswerProgress` 추가: 작성 시작·검증 시작과 해당 회차 작성 소요 시간 알림 (하위 호환). 검증 전 답변 본문은 보내지 않음 |
| 0.1.0 | 2026-10-20 | 최초 작성 |
| 0.2.0 | 2026-10-20 | Calculation에 `after_tax`(세후 이자, 선택) 추가, `inputs` 키를 calculate 도구 기준으로 변경, metadata에 토큰 수 추가 (하위 호환) |
| 0.4.0 | 2026-10-21 | `PlanCreated`에 `planner`(`llm` \| `rule`), `reason`(문자열), `removed`(코드 게이트가 뺀 작업 이유 목록) 추가 (하위 호환) |
| 0.3.0 | 2026-10-20 | (v3) `highlight.unit`에 `"%p"`(우대 폭) 추가, Calculation `inputs.mode`(`forward` \| `goal`) 추가 — goal이면 `result`가 필요 월 납입액·필요 원금·최대 대출 원금 (하위 호환) |

## 1. 엔드포인트

### `GET /health`
```json
{"status": "ok", "version": "v2", "llm": "ollama:qwen3:8b", "data_month": "202609"}
```

### `POST /sessions/reset`
요청: `{"thread_id":"t-1"}`. 성공: `{"ok":true,"thread_id":"t-1"}`.
해당 대화의 서버 이력과 슬롯을 삭제하며, 없는 ID도 성공한다. thread_id는 공백 제외 1~100자다.
같은 대화의 진행 중인 요청이 끝난 뒤 초기화하며 다른 대화에는 영향을 주지 않는다.
프론트는 성공 후 메시지를 비우고 새 ID를 발급한다. 실패하면 원래 대화를 유지한다.
개발용 preview 모드는 서버 요청 없이 로컬 대화만 초기화한다.

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
| `RouteDecided` | 첫 이벤트 (v2) | `route`: `chat` \| `out_of_scope` \| `easy` \| `hard`, `reason`: 문자열, `rewrite`(후속 질문 재작성을 시도했을 때만): `{original, question, applied: bool, reason}` |
| `PlanCreated` | hard만 | `tasks`: `[{id, agent, desc}]`, agent = `structured` \| `rag` \| `calc`; `planner`: `llm` \| `rule` (LLM 계획이 실패하면 `rule`이고 `reason`에 이유), `reason`: 문자열, `removed`: 문자열 배열 |
| `TaskCompleted` | 조사 작업마다 | `id`, `agent`, `ok`: bool, `retries`: int, `elapsed_ms`: int, `error`: 문자열 \| null (실패 원문. 개발·추적용이며 사용자 화면에는 `FinalAnswer.warnings`의 고정 문구만 보인다) |
| `ReplanCheck` | hard만 | `round`: int, `is_sufficient`: bool, `reason`: 문자열 |
| `Validation` | 답 작성 후 | `attempt`: int, `verdict`: `pass` \| `fail`, `reason`: 문자열 \| null, `unverified`(선택): 근거와 연결하지 못한 수치·설명 목록 |
| `AnswerProgress` | 각 답변 작성 시작 및 작성 종료 후 검증 직전 | `stage`: `writing` \| `validating`, `attempt`: int. `validating`에는 `writing_ms`: int (해당 회차 작성 시간, LLM 호출·입력 준비 포함) |

`route` 의미: `chat` 인사·잡담(검색 없음), `out_of_scope` 금융상품 범위 밖(정중히 거절),
`easy` 단일 상품 사실 조회(즉답 경로), `hard` 비교·집계·계산(계획·도구·검증 경로).

`RouteDecided.intent`는 선택 필드이며 대화 회상(`recall`)도 `route=chat`이다. `route=out_of_scope`이면 `intent`는 거절 사유(`future_rate` \| `unsupported`)다.
`RouteDecided`의 `operation`·`comparison_scope`·`rank`·`conditions`는 LLM 라우터 해석 결과(선택 필드)이며 화면은 표시하지 않아도 된다.
`Validation=pass`는 구현된 수치·근거 및 요청 항목 검사 통과를 뜻하며, 모든 자연어 의미의 정확성을 보장하지 않는다.
현재 완료 조건은 명시적 여러 상품 비교의 상품별 금리, 가입대상·가입방법·가입제한,
요청한 세전·세후 이자와 부족한 조회 개수의 명시를 검사한다. 가입 정보는 공시 원문 일치 기준이다.

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
- `highlight`는 없을 수 있다(null). `value`는 숫자, 표시 형식은 프론트가 정한다. `unit`은 `"%"` 또는 `"%p"`(우대 폭)이다.
- `data_flags`: 공시 데이터 이상 경고 문자열(예: 평균금리가 범위 밖). 비어 있지 않으면 경고 아이콘으로 표시.

**Citation** — 답의 근거가 된 원문
```json
{"product_id": "saving_202609_0010001_WR0001F", "section": "우대조건", "quote": "가.급여/연금 이체:연 0.7%p"}
```

**Calculation** — 계산 도구 결과 (hard에서만)
```json
{"name": "적금 이자", "inputs": {"product_type": "saving", "amount_won": 500000, "term_months": 12,
 "rate_pct": 2.45, "interest_type": "simple", "repayment": "equal_payment"},
 "formula": "월납입 × T(T+1)/2 × 연이율 / 1200, 원 미만 절사, 세후 = 세전 × (1 - 0.154)",
 "result": 79625, "after_tax": 67362, "unit": "원"}
```
- `result`는 세전(대출은 월 상환액), `after_tax`는 세후 이자(대출이면 없음).
- `inputs.mode`가 `"goal"`이면 역산이다. `inputs.amount_won`은 목표 세후 만기 수령액(대출은 월 상환 가능액)이고, `result`는 필요한 월 납입액·원금(대출은 최대 대출 원금), `after_tax`는 없다. `name`으로 구분해 표시하면 된다.

**warnings** — 항상 1개 이상. 기본 문구:
`"2026년 9월 공시 기준이며 실제 적용 금리는 개인 조건에 따라 다릅니다. 가입·대출 권유가 아닙니다."`

**Metadata**
```json
{"version": "v2", "latency_ms": 8423, "llm_calls": 2, "retries": 0, "validation_attempts": 1,
 "cost_usd": 0.0172, "data_month": "202609", "thread_id": "t-1", "prompt_tokens": 1247, "completion_tokens": 140}
```
- `writing_runs`는 작성 회차별 목록이다(없으면 `[]`). 각 항목은 `attempt`, `writing_ms`, 제공된 경우 `prompt_tokens`, `completion_tokens`, `load_ms`, `prompt_ms`, `gen_ms`를 포함한다. 시간 단위는 밀리초이며 공급자가 제공하지 않은 단계별 시간은 생략한다. 전체 응답 시간이 아닌 작성 호출 측정값이다.
- 대화 화면은 답변·상품·계산 카드, 분석 과정은 진행·검증·계산식·주의 문구·실행 정보, 출처 화면은 조회 상품 식별 정보와 원문 인용을 표시한다. 이전 저장 답변에 인용이 없으면 상품 출처 정보만 표시하고 그 한계를 명시한다.
- `cost_usd`는 토큰 단가 기반 추정치다. Ollama는 가상 비용이고 OpenAI는 실제 과금되는 호출의 추정 비용이며, 청구서 금액을 의미하지 않는다. 캐시 할인·실행 전후의 별도 호출 비용은 반영하지 않는다.

### `Error`
```json
{"type": "Error", "code": "LLM_UNAVAILABLE", "message": "로컬 LLM에 연결하지 못했습니다."}
```
`code`: `BAD_REQUEST` | `LLM_UNAVAILABLE` | `TOOL_FAILED` | `TIMEOUT` | `INTERNAL`

## 4. v1(베이스라인)과의 차이

v1은 라우팅·계획·검증이 없어 진행 이벤트 없이 `FinalAnswer`만 보낸다. 이때 `route`는 `"baseline"`,
`products`·`citations`·`calculations`는 빈 배열, `metadata.version`은 `"v1"`이다. 프론트는 둘 다 처리해야 한다
(발표 화면에서 Before/After를 같은 UI로 보여주기 위함).
