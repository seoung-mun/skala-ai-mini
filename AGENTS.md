# finagent — 금융상품 근거 에이전트 (AI Mini-Project 과제 2번)

금감원 「금융상품한눈에」 공시 데이터(예금·적금·주담대·전세대출·신용대출, 988개 상품)로
질문에 답하는 에이전트. 쉬운 질문은 바로 답하고, 비교·계산 같은 어려운 질문은 계획 →
도구 조사 → 검증을 거쳐 답한다. 개선 전(v1 베이스라인)과 개선 후(v2)를 같은 골든셋으로 비교한다.

## 작업 소유 (두 세션이 동시에 작업한다)

| 영역 | 소유 | 경로 |
|---|---|---|
| 백엔드·에이전트·데이터·평가 | 백엔드 세션 | `src/`, `scripts/`, `eval/`, `data/`, `tests/`, `pyproject.toml` |
| 프론트엔드 | 프론트 세션 | `web/` |
| 공용 계약 | 둘 다 읽기, 변경은 아래 절차로 | `doc/API_CONTRACT.md`, `doc/contract/` |

- 자기 소유가 아닌 경로는 수정하지 않는다. 필요하면 `doc/REQUESTS.md`에 요청을 한 줄 남긴다.
- 계약 변경: `doc/API_CONTRACT.md`의 버전과 변경 이력을 먼저 고친 뒤 구현한다. 필드 삭제·이름 변경은
  상대 세션 확인 전에는 하지 않는다. 필드 추가는 하위 호환이므로 이력만 남기면 된다.
- 프론트는 백엔드 없이 `doc/contract/*.sse` 예시를 재생하는 목업으로 개발할 수 있어야 한다.

## 불변 조건

- `.env`, 인증키, 자격 증명 파일은 읽거나 출력하지 않는다. 키는 `scripts/fetch_finlife.py`만 사용한다.
- 금리·금액·이자·순위는 공시 데이터와 계산 도구에서만 나온다. LLM이 만든 숫자는 검증을 통과해야 출력한다.
- 공시 평균금리(`lend_rate_avg`, `crdt_grad_avg`)는 참고값이다. 범위 밖 값이 많아(은행 주담대 36/68) 계산·순위에 쓰지 않는다.
- 빈 값은 `None`으로 두고 임의 기본값으로 채우지 않는다. 오류는 조용히 넘기지 말고 구체적인 메시지로 실패시킨다.
- 오늘 날짜로 판매종료를 판정하지 않는다(시계 의존). 공시 종료일만 그대로 표시한다.

## 구조

```
scripts/fetch_finlife.py   금감원 API 5종 × (은행, 저축은행) 수집 -> data/raw/*.json
scripts/build_data.py      data/raw -> data/md/{kind}/{group}/{id}.md (실행마다 재생성)
src/finagent/config.py     .env를 한 번 읽어 고정 (CONFIG, 자리표시 값은 키 없음 처리)
src/finagent/schemas.py    LLM 입출력·도구 결과 pydantic 모델 (모든 출력에 ok 키)
src/finagent/parser.py     정규화·이상치 플래그·마크다운·섹션 분리
src/finagent/catalog.py    구조화 조회·계산 (순수 함수)
src/finagent/slots.py      슬롯 추출·규칙 라우팅
src/finagent/search.py     키워드 검색(BM25, 한글 2-gram)
src/finagent/retrieval.py  하이브리드 검색(BM25 + KURE, RRF)
src/finagent/llm.py        Ollama·OpenAI 어댑터 (structured(system, 입력모델, 출력모델))
src/finagent/mcp_server.py MCP: query_products, calculate, product://{id}, compare_procedure
src/finagent/mcp_client.py stdio 클라이언트 (ToolResult 반환, 타임아웃만 재시도)
src/finagent/graph/        v2 감독 그래프(nodes) + 서브에이전트 3종(agents: structured·rag·calc, Send 병렬)
src/finagent/validate.py   숫자·인용·1위 검증 (순수 함수)
src/finagent/server.py     FastAPI /chat, /chat/stream (계약 문서 기준)
src/finagent/baseline.py   v1: 키워드 top-5 -> LLM 1회 (비교용으로 비구조화 그대로 유지)
src/finagent/scoring.py    골든셋 코드 채점
eval/build_golden.py       골든셋 40문항 (정답은 실데이터에서 코드로 계산)
eval/routing_set.jsonl     라우팅 평가 세트 20문항
eval/run_eval.py           v1·v2 반복 측정 -> eval/results/, compare.md
web/                       프론트엔드 (프론트 세션 소유)
```

마크다운 섹션 마커: `<!-- retrieval: keyword -->`는 키워드·구조화 조회 대상,
`<!-- retrieval: embedding -->`은 임베딩 대상이다. v1은 마커와 무관하게 전부 키워드로 검색한다.

## 문서

- 개선 결과·지표·트레이드오프: `doc/IMPROVEMENTS.md`
- 설계 결정: `doc/adr/` (0001~0010)
- API 계약: `doc/API_CONTRACT.md`

## 명령

```bash
uv run pytest -q                       # 단위 테스트
uv run python scripts/build_data.py    # 마크다운 재생성
uv run python eval/build_golden.py     # 골든셋 재생성
uv run python eval/run_eval.py baseline --repeat 3   # v1 측정(로컬 Ollama 필요)
uv run python eval/run_eval.py v2 --repeat 3
uv run python eval/run_eval.py compare               # eval/results/compare.md
uv run python -m finagent.server                     # API 서버 (.env.example 참고)
```

## 포트

- 백엔드 FastAPI: `127.0.0.1:8100`
- 프론트: `127.0.0.1:8600` (백엔드로 `/chat`, `/chat/stream` 프록시)

## 완료 기준

관련 테스트 통과, 디버그 출력 없음, 계약 예시(`doc/contract/*.sse`)와 실제 응답 형식 일치.
