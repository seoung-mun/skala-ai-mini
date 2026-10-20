# ADR-0002: MCP 서버를 stdio subprocess로 두고, 도구를 목표 단위로 통합하며, 결과는 ok 키로 통일한다

- 상태: Accepted
- 관련: `src/finagent/mcp_server.py`, `mcp_client.py`, `schemas.ToolResult`

## 배경
조회·계산 같은 결정론 기능을 LLM 에이전트에 노출해야 한다.
베이스 프로젝트의 ADR-0002(MCP를 subprocess+stdio로)를 따를지, 도구를 어떤 단위로 나눌지, 실패를 어떻게 알릴지 정해야 했다.

## 결정
- **경계:** 백엔드 lifespan에서 MCP 서버를 subprocess 1개로 띄우고 stdio로 통신한다.
- **도구:** `query_products`(조회), `calculate`(이자·상환액)를 둔다. 처음에는 `calc_saving/deposit/loan` 3개였는데, 같은 목표("계산")라서 하나로 합쳤다(ACI 원칙, AI Agents in Depth 4.2). 문서는 Resource(`product://{id}`), 답변 절차는 Prompt(`compare_procedure`)로 둔다.
- **결과 형식:** 모든 도구는 `ToolResult(ok, tool_name, data, error)`를 반환한다. 인자 오류·타임아웃도 `ok=false`로 담고, 연결이 끊긴 경우만 예외로 올린다. 타임아웃만 지수 백오프로 재시도한다.
- **버전 고정:** `fastmcp==2.14.7`, `mcp==1.30.0`.

## 대안 비교
| 대안 | 장점 | 단점 |
|---|---|---|
| A. 인프로세스 함수 호출 | 빠름 | MCP 표준을 쓰는 의미가 없고, 도구 오류가 서버 전체를 죽일 수 있음 |
| **B. subprocess + stdio (채택)** | 표준 클라이언트 재사용, 프로세스 격리 | 단일 머신 배포 전제 |
| C. 원격 HTTP MCP | 독립 확장 | 이 규모에는 과설계, 인증·네트워크 장애 처리 필요 |

## 결과·트레이드오프
- 실측: `fastmcp 4.x`는 `mcp 2.x`를 요구해 다른 패키지와 충돌했다. 그래서 버전을 고정하고, `langchain-mcp-adapters` 없이 `ClientSession`을 직접 쓴다.
- 도구를 합쳐서 LLM이 고를 도구 수가 줄었다. 대신 `calculate` 하나의 인자 검증이 복잡해졌다(`product_type`별로 다른 의미).
- `metric="avg"`(평균금리) 조회를 도구 단에서 금지한다. 리뷰에서 우회 가능성이 재현돼 허용 목록으로 막았다.

## 재검토 조건
여러 백엔드 인스턴스가 하나의 MCP 서버를 공유해야 하면 C로 전환한다.
