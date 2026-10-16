# 출처와 외부 자료 경로

자료 확인일: 2026-10-15.

## 로컬 자료

- 강의 폴더: `[local-notes]`
- 종합 검토: `[local-notes]`
- 과거 프로젝트 후보: `[local-notes]`



## 트랙 필수 기술

  필수 기술은 Wirelog CLI·Datalog·Python/Pydantic·pytest입니다.
- 현재 구현은 PyreWire API 경로입니다. Wirelog CLI 직접 실행과 Pydantic 도입은 미검증/미구현으로 구분합니다.
- 확보한 트랙 소개·강의·공통 오리엔테이션에서 로컬 LLM 구동의 필수 요구는 확인되지 않았습니다.
  프로젝트명·활용 오픈소스·결과물 형태/도메인·활용 사례 또는 계획·성과·소감·선택적 개인 성과가 있습니다.

## 엔진 공식 자료

- [Wirelog](https://github.com/semantic-reasoning/wirelog)
- [PyreWire 1.1.2](https://pypi.org/project/pyrewire/1.1.2/)
- [v1.1.2 Quickstart](https://github.com/semantic-reasoning/PyreWire/blob/v1.1.2/docs/quickstart.md)
- [Snapshot과 step](https://github.com/semantic-reasoning/PyreWire/blob/v1.1.2/docs/semantics/step-vs-snapshot.md)
- [Session 구현](https://github.com/semantic-reasoning/PyreWire/blob/v1.1.2/src/pyrewire/session.py)
- [증분·snapshot 비교 예제](https://github.com/semantic-reasoning/PyreWire/blob/v1.1.2/examples/12_snapshot_vs_delta.py)
- [재귀 증분 예제](https://github.com/semantic-reasoning/PyreWire/blob/v1.1.2/examples/10_recursive_under_update.py)
- [중복과 multiplicity](https://github.com/semantic-reasoning/PyreWire/blob/v1.1.2/docs/semantics/zset.md)
- [지원 환경](https://github.com/semantic-reasoning/PyreWire/blob/v1.1.2/docs/support.md)

문서·소스 확인과 실행 확인은 구분합니다. 2026-10-16 T01에서 PyreWire 1.1.2를 설치하고 실제 S1 재귀 snapshot 질의를 확인했습니다.
MVP는 EasySession.insert와 snapshot을 우선 검증하며, insert_sym/remove_sym과 step은 평가 모드 계약을 별도로 확인합니다.

## 구독 CLI 공식 자료

- [구독 로그인과 API 키](https://learn.chatgpt.com/docs/auth)
- [비대화형 실행](https://learn.chatgpt.com/docs/non-interactive-mode)
- [가격 안내](https://learn.chatgpt.com/docs/pricing)

로컬 `codex --help`에서 `-C/--cd`, `exec`, `login`을 확인했습니다.
이 저장소의 LLM adapter는 아직 구현·실행하지 않았습니다.
