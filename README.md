# ChangeScope

서비스 변경에 따라 다시 확인할 컴포넌트와 테스트 근거를 찾는 로컬 개발 도구입니다.
OSSAI Wirelog 트랙의 개인 프로젝트로, 문서 기반 지식 추출과 검토, Datalog 추론, 근거 검색을 연결합니다.

**현재 상태: T01 사용자 완료 승인. T01a Wirelog CLI 결과 대조와 T02 정상·악성 입력의 실제 구독 CLI smoke를 로컬 검증했고 사용자 검토를 기다립니다. T03~T10은 미착수입니다.**

## 해결할 질문

> reranker를 교체하려고 합니다. 어떤 서비스를 점검하고, 어떤 테스트 문서를 확인해야 하나요?

ChangeScope는 검토한 서비스 호출 관계에서 영향 후보를 계산하고, 기존 테스트와 원문 근거를 함께 제시합니다.
서비스 도달 경로는 점검 후보이며 실제 장애, 변경의 안전성, 테스트의 충분성을 보장하지 않습니다.

## MVP 범위

- 개인 개발, 구현 착수 후 약 2주를 목표로 합니다.
- 합성 문서 약 10개와 서비스 5개로 시작합니다.
- LLM이 추출한 관계는 사람이 검토한 뒤 분석 모델에 반영합니다.
- PyreWire 1.1.2로 Wirelog 관계 계산을 실행합니다.
- 현재 기준 모델과 변경 시나리오를 별도 스냅샷으로 비교합니다.
- Codex 구독으로 로그인한 로컬 CLI를 활용하고 추가 유료 API 구매를 전제로 하지 않습니다.
- CLI를 기준 실행 경로로 만들고, 검토 결과와 출처를 확인할 작은 화면을 추가합니다.

## 핵심 예시

```text
webapi -> answer -> retrieval -> reranker
webapi -> suggest ------------> reranker
```

reranker의 상위 후보는 retrieval, answer, webapi, suggest입니다.
retrieval에서 reranker를 호출하는 관계를 제거한 새 시나리오에서는 suggest와 webapi가 남습니다.
webapi는 대체 경로가 있으므로 후보에서 사라지지 않습니다.

검토 대기 중인 제거 제안은 기존 기준 모델을 바꾸지 않습니다.
스냅샷의 검토 승인은 문서 기반 모델의 검토를 뜻하며 실제 배포 상태와 구분합니다.

## 문서 읽는 순서

3. [제품 계약](docs/product-contract.md)
6. [평가 시나리오](docs/evaluation.md)
7. [출처 지도](docs/source-map.md)


## 작업 경로와 세션 시작

```bash
codex -C change-scope
```

교육 자료 원본은 저장소에 복제하지 않습니다.

## 구현 방향

Python과 uv를 사용하며, PyreWire는 `1.1.2`를 기준으로 기술 검증합니다.
새 EasySession의 snapshot 평가를 우선하고, 같은 세션의 증분 갱신은 별도 확장으로 다룹니다.
LLM 경계는 로컬 Codex CLI이며, 실제 호출은 명시적으로 실행할 때만 사용합니다.
기본 테스트는 기록된 LLM 출력과 실제 규칙 엔진을 사용해 추가 AI 호출 없이 실행하는 것을 목표로 합니다.

T01의 실제 재귀 질의에 이어 T01a에서 Wirelog CLI 0.70.0을 직접 실행했고, T02에서 Codex CLI 0.162.1의 제한된 실제 구독 호출을 확인했습니다. 기본 LLM 경계 테스트는 합성 process recording을 사용하며 실제 호출과 구분합니다.

## T01 로컬 실행

```bash
uv sync --locked
uv run --no-sync change-scope analyze --snapshot data/fixtures/s1/snapshot.json --target reranker
uv run --no-sync change-scope analyze --snapshot data/fixtures/s1/snapshot.json --target webapi
uv run --no-sync pytest -q
```

첫 명령은 초기 설치에 네트워크가 필요합니다. 설치 후 `uv run --no-sync` 명령은 LLM·네트워크 없이 실행합니다.
제한된 환경에서 uv cache 경로가 필요하면 `UV_CACHE_DIR=/tmp/change-scope-uv-cache`를 지정합니다.

`reranker` 결과는 `analysis_status=success`, 후보 `answer, retrieval, suggest, webapi`입니다.
`webapi`는 정상 질의의 `analysis_status=empty`, 후보 `[]`입니다.
출력에는 snapshot·입력·규칙 SHA-256, 실제 엔진 버전, 원문 metadata와 관계별 출처가 포함됩니다.
매 실행은 새 프로세스의 새 EasySession에서 고유 관계를 insert하고 snapshot만 사용합니다.

`--output runs/my-result.json`은 결과 파일을 새로 만듭니다. 기존 파일은 덮어쓰지 않으므로 새 경로를 사용합니다.
`--timeout 30`은 엔진 프로세스의 시작·질의를 합친 제한 시간(초)입니다.
종료 코드는 정상·empty `0`, 입력 오류 `2`, 엔진 오류·timeout `3`, 출력 저장 오류 `4`입니다.
원문 경로 해석 실패와 플랫폼이 지원하지 않는 과대 timeout도 구조화된 오류로 반환합니다.
분석 오류의 JSON은 `analysis_status=error`, `upstream_candidates=null`을 사용해 빈 결과와 구분합니다.
명령 사용법·잘못된 옵션은 argparse가 stderr에 출력하고 `2`로 종료합니다.

현재 입력은 `fixture=true`인 명시적 승인 합성 fixture만 지원합니다.
원문 hash·출처·H2 절 내 quote 존재를 검사하지만 사람의 관계 의미 검토를 대체하지 않습니다.
후보 검토·수정·과거 blob 보존·witness·테스트 연결·시나리오 비교와 제품 UI는 후속 작업입니다.
`data/holdout/`의 별도 원문과 4문항 정답은 T09 평가용으로 고정했으며 아직 평가하지 않았습니다.

## T01a / T02 로컬 실행

```bash
# 최초 빌드에는 네트워크와 C/C++ compiler가 필요합니다. 소스·도구·빌드는 .local/에 둡니다.
uv run --no-sync python scripts/build_wirelog.py
uv run --no-sync change-scope verify-wirelog --snapshot data/fixtures/s1/snapshot.json --target reranker --binary .local/wirelog-v0.70.0/build/wirelog_cli
uv run --no-sync change-scope verify-wirelog --snapshot data/fixtures/s1/snapshot.json --target webapi --binary .local/wirelog-v0.70.0/build/wirelog_cli

# 기본은 synthetic_test_double의 기록 재생입니다. 인증·모델·네트워크를 사용하지 않습니다.
uv run --no-sync change-scope llm-smoke --case normal
uv run --no-sync change-scope llm-smoke --case adversarial
uv run --no-sync change-scope llm-smoke --recording src/change_scope/fixtures/llm/auth-error-recorded.json

# 명시적 opt-in: 기존 구독 CLI 인증·사용량·모델 통신을 사용합니다.
uv run --no-sync change-scope llm-smoke --case normal --live
uv run --no-sync change-scope llm-smoke --case adversarial --live

# 외부 CLI 설치가 없어도 기본 suite는 실행 가능합니다.
uv run --no-sync pytest -q
WIRELOG_CLI="$PWD/.local/wirelog-v0.70.0/build/wirelog_cli" uv run --no-sync pytest -q tests/test_wirelog_cli.py
```

기본 suite는 75 passed·2 skipped이며, 외부 Wirelog CLI를 명시한 별도 suite는 7 passed입니다.
기본 테스트는 실제 LLM 호출을 하지 않습니다. Wirelog CLI opt-in은 외부 바이너리만 실행하며 LLM을 호출하지 않습니다.
초기 설치·빌드와 live smoke에는 네트워크가 필요하므로 전체를 offline workflow로 표현하지 않습니다.

T02는 Linux/POSIX에서 두 고정 합성 문서만 시험하는 smoke 경로입니다. 임의 문서 추출·승인·export는 T03 이후입니다.
`--live`가 없으면 `llm_mode=recorded_output`, `provenance=synthetic_test_double`입니다.
실제 호출은 `llm_mode=live_cli`, `provenance=live_subscription_cli`로 표시합니다.
후보는 항상 `pending`이며 S1이나 승인 모델에 자동 적재하지 않습니다.

live 실행은 버전·구독 로그인·sandbox 사전 검사를 거칩니다. read-only 프로필에서 생성한 보호 파일 쓰기와
앱 소유 loopback 소켓 접속이 차단되지 않으면 모델 호출 전에 실패합니다.
실제 exec에는 read-only·approval never·web disabled·shell/apps/MCP/plugin/hooks 등의 제한을 적용합니다.
계정 파일·토큰은 CLI가 처리하고 앱은 직접 읽거나 복제하지 않습니다. API key 환경변수도 자식에게 전달하지 않습니다.

제한은 모델 실행 기본 120초(최대 600초), stdout 256 KiB, stderr 64 KiB, 최종 응답 16 KiB입니다.
버전·인증·sandbox 사전 검사는 각각 별도 제한 시간(10초)을 사용하므로 전체 wall time과 모델 제한 시간은 다릅니다.
오류는 `extraction_status=error`, `candidates=null`로 반환합니다. 기존 output 경로는 live 호출 전에 거부합니다.
`--output runs/my-new-result.json`으로 새 실행 기록을 저장할 수 있습니다.

Codex CLI 0.162.1에서 관찰한 정확한 Code Mode 차단 알림만 비실행 진단으로 구분합니다.
다른 error item·도구 이벤트·알 수 없는 프로토콜은 거부하며, 다른 CLI 버전에서는 live 경로를 비활성화합니다.
`live_guard_passed`는 사전 검사·작업공간 hash·관찰 이벤트·출력 검증 통과를 뜻하며 전체 호스트의 완전한 보안 감사가 아닙니다.
JSONL에서 실제 모델 식별자가 제공되지 않아 `model_id=null`을 미확인으로 남깁니다.

[검증 기록과 한계](docs/t01a-t02-verification.md)를 참고합니다. T01a·T02는 아직 사용자 완료 승인이 아닙니다.

## 범위 밖

임의 저장소의 전체 코드 분석, 다중 에이전트 작업 배분, 실제 CI·배포 연동,
대형 벡터 DB, 다중 사용자 서비스와 운영 자동 조치는 이번 MVP에 포함하지 않습니다.

## 저장소 상태

로컬 Git 저장소를 `main`으로 초기화하고 [GitHub 저장소](https://github.com/jaemyungchoi95/change-scope)를 생성했습니다.
2026-10-15 초기 준비 시 origin을 연결하고 빈 원격을 확인했습니다.
최신 이력·동기화 상태는 `git log --oneline -4`, `git status -sb`로 확인합니다.
로컬 검토 산출물은 Git에서 제외합니다.
