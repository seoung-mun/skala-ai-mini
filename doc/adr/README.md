# ADR 목록

베이스라인 대비 개선 결과·지표는 [IMPROVEMENTS.md](../IMPROVEMENTS.md)에 있다.

형식: 배경 → 결정 → 대안 비교 → 결과·트레이드오프 → 재검토 조건. 근거 수치는 모두 이 프로젝트에서 실측한 값이다.

| 번호 | 결정 | 상태 |
|---|---|---|
| [0001](0001-data-sections.md) | 공시 데이터를 상품별 마크다운으로, 섹션마다 검색 방식 표시 | Accepted |
| [0002](0002-mcp-boundary-and-tools.md) | MCP는 stdio subprocess, 도구는 목표 단위로 통합, 결과는 ok 키 | Accepted |
| [0003](0003-rule-first-routing.md) | 라우팅은 규칙 우선, LLM은 폴백 | Accepted |
| [0004](0004-subagents-by-source.md) | 서브에이전트는 데이터 출처별, Send로 병렬 | Accepted |
| [0005](0005-numbers-from-tools-code-validation.md) | 숫자는 도구만, 답변은 코드가 단위별 검증 | Accepted |
| [0006](0006-hybrid-retrieval-rerank.md) | 슬롯 필터 + 하이브리드 검색 + 리랭킹 | Accepted |
| [0007](0007-structured-llm-io.md) | LLM 입출력 pydantic 구조화, ok 키 | Accepted |
| [0008](0008-no-token-streaming.md) | 토큰 비스트리밍, 검증 후 1회 전송 | Accepted |
| [0009](0009-frozen-baseline-evaluation.md) | 베이스라인 동결 + 재채점 | Accepted |
| [0010](0010-local-llm-memory.md) | 로컬 qwen3:8b, num_ctx 8192 | Accepted |
