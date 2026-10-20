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
src/finagent/parser.py     정규화·이상치 플래그·마크다운·섹션 분리
src/finagent/search.py     키워드 검색(BM25, 한글 2-gram)
src/finagent/llm.py        Ollama 어댑터 (chat(system, user) 인터페이스, 테스트에서는 가짜 주입)
src/finagent/baseline.py   v1: 키워드 top-5 -> LLM 1회
src/finagent/scoring.py    골든셋 코드 채점
eval/build_golden.py       골든셋 20문항 (정답은 실데이터에서 코드로 계산)
eval/run_eval.py           측정 -> eval/results/{system}.jsonl, {system}_summary.md
web/                       프론트엔드 (프론트 세션 소유)
```

마크다운 섹션 마커: `<!-- retrieval: keyword -->`는 키워드·구조화 조회 대상,
`<!-- retrieval: embedding -->`은 임베딩 대상이다. v1은 마커와 무관하게 전부 키워드로 검색한다.

## 명령

```bash
uv run pytest -q                       # 단위 테스트
uv run python scripts/build_data.py    # 마크다운 재생성
uv run python eval/build_golden.py     # 골든셋 재생성
uv run python eval/run_eval.py baseline --model qwen3:8b   # 측정(로컬 Ollama 필요)
```

## 포트

- 백엔드 FastAPI: `127.0.0.1:8100`
- 프론트: `127.0.0.1:8600` (백엔드로 `/chat`, `/chat/stream` 프록시)

## 완료 기준

관련 테스트 통과, 디버그 출력 없음, 계약 예시(`doc/contract/*.sse`)와 실제 응답 형식 일치.
