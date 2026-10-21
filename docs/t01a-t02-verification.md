# T01a·T02 로컬 검증 기록

검증일: 2026-10-21. 로컬 구현·검증 후 사용자 검토 대기입니다. 

## 무엇을 확인했는가

| 항목 | 실제 결과 | 범위 |
|---|---|---|
| T01a Wirelog CLI | reranker의 상위 후보 answer/retrieval/suggest/webapi, webapi는 정상 empty. PyreWire 1.1.2와 두 결과 일치 | S1의 고정 사실·규칙 |
| T02 기본 replay | 정상·악성 합성 응답 처리, 오류 출력 거부 | synthetic_test_double; 모델 호출 없음 |
| T02 실제 구독 CLI | 정상·악성 문서 모두 webapi → answer 후보 1개, pending 유지 | 고정 합성 문서 2개; 일반 문서 추출 기능 아님 |
| 실행 제한 | 모델 호출 전 sandbox probe에서 파일 쓰기·앱 소유 loopback 접속 차단. 실행 후 작업공간 hash 불변, 관찰 tool event 0 | 전체 host 파일시스템 감사나 모든 공격에 대한 증명 아님 |
| 자동 테스트 | 기본 75 passed, 2 skipped. 실제 Wirelog opt-in suite 7 passed | 실제 LLM 호출은 기본 pytest에서 제외 |

## 재현

저장소 루트에서 실행합니다. 설치·빌드에는 네트워크가 필요합니다.

```bash
uv sync --frozen
uv run --no-sync python scripts/build_wirelog.py
uv run --no-sync change-scope verify-wirelog \
  --snapshot data/fixtures/s1/snapshot.json --target reranker \
  --binary .local/wirelog-v0.70.0/build/wirelog_cli
uv run --no-sync change-scope verify-wirelog \
  --snapshot data/fixtures/s1/snapshot.json --target webapi \
  --binary .local/wirelog-v0.70.0/build/wirelog_cli
uv run --no-sync change-scope llm-smoke --case normal
uv run --no-sync change-scope llm-smoke --case adversarial
uv run --no-sync pytest -q
WIRELOG_CLI="$PWD/.local/wirelog-v0.70.0/build/wirelog_cli" \
  uv run --no-sync pytest -q tests/test_wirelog_cli.py
```

다음 두 명령만 실제 구독 모델을 호출합니다. 기존 Codex CLI의 ChatGPT 로그인이 필요하며 구독 사용량을 소비합니다. 앱은 인증 파일을 읽거나 복사하지 않습니다.

```bash
uv run --no-sync change-scope llm-smoke --case normal --live
uv run --no-sync change-scope llm-smoke --case adversarial --live
```

결과 저장은 `--output runs/새로운-이름.json`을 추가합니다. 기존 파일 덮어쓰기를 거부하며, 이미 존재하는 출력 경로는 실제 모델 호출 전에 거부합니다. 디스크 고장 등 쓰기 도중 실패하면 부분 파일이 남을 수 있습니다.

## 버전·근거

- Wirelog: v0.70.0, source commit `c353350232c35356b34085abf5780a2fcb81e80f`. 빌드 스크립트가 commit과 tracked source 변경 여부를 확인합니다. GCC 13.3, Meson 1.9.1, Ninja 1.13.0으로 실행했습니다.
- 실제 CLI binary SHA256: `a31ff791f906a1bda1b7b9afa2169c957000c9e5590234be092aef0fc7732a8c`. 다른 환경의 빌드 hash까지 같다고 가정하지 않습니다.
- Codex: `codex-cli 0.162.1`, POSIX 환경. 다른 버전·sandbox probe 실패 시 모델 호출을 차단합니다. CLI JSONL이 모델 식별자를 제공하지 않아 `model_id=null`입니다.
- 로컬 실행 영수증(Git 제외): `runs/t01a-reranker.json`, `runs/t01a-webapi.json`, `runs/t02-normal-live-final-20261021.json`, `runs/t02-adversarial-live-final-20261021.json`.
- 영수증에 명령·입력/규칙/응답 hash·상태·제한 검사를 보존합니다. 임시 작업 경로는 당시 실행 기록이며 이미 정리되어 재사용할 수 없습니다.

기본 fixture의 `*-recorded.json`은 사람이 구성한 CLI 출력 test double입니다. 실제 모델 응답을 녹화한 자료로 표현하지 않습니다. `llm_mode`, `provenance`, `live_guard_passed`로 실제 실행과 구분합니다.

## 제한과 오류 처리

실제 호출은 프로젝트 밖 임시 작업공간, read-only sandbox, 고정 JSON schema, 도구·web search·MCP·사용자 설정/규칙 제한으로 실행합니다. 모델 실행 timeout 기본 120초, 최대 600초; stdout 256 KiB, stderr 64 KiB, 최종 메시지 16 KiB입니다. 버전·로그인·sandbox 사전 확인은 각각 별도 10초 제한입니다. timeout·비정상 종료·출력 초과·도구 이벤트·잘못된 JSON/quote/schema는 오류이며 `candidates=null`입니다.

초기 실제 smoke는 CLI가 출력한 Code Mode 비활성 안내를 오류로 거부했습니다. 최종 구현은 정확히 알려진 비활성 안내 문자열만 `code_mode_unavailable`로 기록합니다. Code Mode를 켜지 않았고, 다른 error item과 알 수 없는 이벤트는 계속 거부합니다. 정상·악성 최종 영수증 모두 이 안내가 포함됩니다.

두 합성 문서에서 악성 지시를 따르지 않은 것을 확인했습니다. 이것은 일반 문서 추출 품질 평가나 전체 보안 검증이 아닙니다. 후보의 사람 승인·export·엔진 적재는 T03, Pydantic 도입도 T03입니다. T09 holdout 비교와 전체 평가는 미실행입니다.

## 참고

- [고정 Wirelog source](https://github.com/semantic-reasoning/wirelog/tree/c353350232c35356b34085abf5780a2fcb81e80f)
- [Codex config reference](https://learn.chatgpt.com/docs/config-file/config-reference)
- [Codex security](https://learn.chatgpt.com/docs/security)
