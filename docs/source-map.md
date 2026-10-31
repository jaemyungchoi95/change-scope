# 출처와 외부 자료

자료 확인일: 2026-10-15, 추가 확인 2026-10-16·2026-10-21. 아래 각 절의 상태 서술은 해당 확인일 기준이며, 현재 상태는 README와 평가 결과 문서를 따릅니다.

## 교육 과정

교육 과정에서 다룬 개념을 참고해 설계했습니다. 교육 자료 원본은 이 저장소에 복제하지 않습니다.
트랙 필수 기술(Wirelog CLI·Datalog·Python/Pydantic·pytest)은 Wirelog CLI 결과 대조(T01a), Datalog 규칙(`rules/impact.dl`),
Pydantic 스키마 경계(T03 이후), pytest 기본 suite로 반영했습니다.

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

## 2026-10-21 실제 실행 확인

- Wirelog CLI: https://github.com/semantic-reasoning/wirelog/tree/c353350232c35356b34085abf5780a2fcb81e80f (v0.70.0). 공식 CLI의 --version과 .output upstream 출력을 사용했습니다.
- OpenAI 설정 참조: https://learn.chatgpt.com/docs/config-file/config-reference ; 보안 설명: https://learn.chatgpt.com/docs/security . 로컬 codex-cli 0.162.1 도움말·실제 제한 probe·정상/악성 smoke와 대조했습니다.
- Wirelog CLI 직접 사용은 현재 로컬 검증·사용자 검토 대기입니다. Pydantic은 T03 보완으로 남아 있습니다. 기존 미검증 문구는 당시 확인 상태입니다.
