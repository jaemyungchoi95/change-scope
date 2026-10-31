# ChangeScope

서비스 하나를 바꿀 때 **함께 다시 점검할 서비스와 기존 테스트, 그 근거 원문**을 찾아 주는 로컬 개발 도구입니다.
OSSAI Wirelog 트랙의 개인 프로젝트로, 문서 기반 관계 추출 → 사람 검토 → Datalog 추론(PyreWire/Wirelog) → 근거 경로 제시를 연결합니다.

> 서비스 도달 경로는 **점검 후보**이며, 실제 장애·변경의 안전성·테스트의 충분성을 보장하지 않습니다.
> 승인(approved)은 문서 검토 상태이고 실제 배포 상태와 다릅니다(`deployment_status=unverified`).

## 해결하는 질문

> reranker를 교체하려고 합니다. 어떤 서비스를 다시 점검하고, 어떤 테스트를 돌려야 하나요?
> 변경안(예: retrieval → reranker 호출 제거)을 반영하면 점검 범위가 어떻게 달라지나요?

```text
webapi -> answer -> retrieval -> reranker
webapi -> suggest ------------> reranker
```

reranker의 상위 점검 후보는 answer, retrieval, suggest, webapi입니다. retrieval → reranker 제거안을 승인해 만든 시나리오에서는
suggest와 webapi가 남고, webapi는 suggest를 통한 대체 경로로 계속 영향을 받습니다. 검토 대기 중인 제안은 기준 모델을 바꾸지 않습니다.

## 현재 상태

| 단계 | 내용 | 상태 |
|---|---|---|
| T01 · T01a · T02 | 실제 엔진 계산, Wirelog CLI 결과 대조, LLM 실행 경계 | 완료 |
| T03 · T04 · T05 | 추출 후보의 사람 검토, 수정·새 원문 버전·철회, 근거 경로(witness) | 완료 |
| T06 · T07 · T08 | 기존 테스트·근거 보고서, 변경안 시나리오 비교, 로컬 화면 | 구현·시연 완료 |
| T09 | holdout 평가(축소판) | 실행 완료, 사람 검토 end-to-end는 대기 |
| T10 | 재현·인계·공개 준비 | 진행 중 |

평가: [평가 결과](docs/evaluation-results.md)

## 빠른 시작 (새 환경)

요구 사항: Linux 또는 WSL2, Python 3.12, [uv](https://docs.astral.sh/uv/). 실제 LLM 기능만 Codex CLI 구독 로그인이 추가로 필요합니다.

```bash
git clone https://github.com/jaemyungchoi95/change-scope.git
cd change-scope
uv sync --locked                 # 최초 설치만 네트워크 필요
uv run --no-sync pytest -q       # LLM·네트워크 호출 없음
scripts/demo.sh                  # 추출 → 검토 → export → 분석 → 변경안 비교 → holdout 평가 (기록된 출력만 사용)
```

`scripts/demo.sh`는 `runs/demo/`에 결과와 Markdown 보고서를 만들고, 로컬 화면 실행 명령을 출력합니다.
데모의 승인은 사람 판정을 대신하는 스크립트이며 `reviewer=demo-script`로 기록됩니다.

## 로컬 화면

```bash
uv run --no-sync change-scope serve --snapshot runs/demo/s1/snapshot.json --workspace runs/demo/ws \
  --evidence data/fixtures/t06/catalog.json --scenario runs/demo/s2/scenario.json [--llm live]
# → http://localhost:8765 (127.0.0.1에만 열림)
```

- **① 후보 · 원문 · 검토 이력**: AI가 문서에서 찾은 호출 관계 후보가 어떤 단계를 거치는지, 분석에 반영된 것과 다시 검토할 것, 반려·철회된 것, 각 후보의 근거 원문·기계 검사·사람 판정·수정 이력.
- **② 영향 · 경로 · 비교**: 결론 한 줄, 점검 체크리스트(왜 점검하나·실행할 기존 테스트·테스트 없음 경고), 주의할 점, 변경안 반영 전후 비교, 접어 둔 상세 근거.
- 쓰기 동작은 "LLM 제안 생성" 버튼 하나뿐이며(`--llm live|recorded`), 제안은 '검토 전'으로만 표시되고 사실·테스트에 반영되지 않습니다.
  승인·반려는 CLI에서 합니다. 서버는 Host 헤더·전용 요청 헤더·Origin을 검사하고, 클라이언트가 파일 경로를 지정할 수 없습니다.

## 주요 명령

| 명령 | 하는 일 |
|---|---|
| `extract --recording R` / `extract --live` | 문서에서 pending 관계 후보 생성, 인용 위치 기계 검사 |
| `review show / decide / revise / withdraw / add-source` | 후보·원문 확인, 승인·반려(hash chain ledger), 수정 revision, 철회, 새 원문 버전 |
| `export` | 승인분만 담은 reviewed snapshot(근거 사본 동봉) |
| `analyze --snapshot S --target T [--evidence C] [--report R.md]` | 실제 PyreWire로 상위 후보 계산, witness 경로, 기존 테스트·문서, 주장 4종 |
| `proposal init / decide`, `scenario build` | 기준 snapshot에 묶인 변경 제안 검토와 시나리오 snapshot 생성 |
| `compare --base B --scenario S2 --target T` | 두 모델의 점검 범위 차이, 경로 변화, 적용 근거 |
| `evaluate --questions Q --snapshot S [--gold G --workspace W]` | 고정 문항 평가와 추출 정밀도·재현율 |
| `serve` | 로컬 읽기 전용 화면 |
| `verify-wirelog`, `llm-smoke` | Wirelog CLI 결과 대조, 고정 문서 LLM 경계 smoke |

공통 규칙: 결과는 JSON으로 출력하고 `--output`·`--report`·`--workspace`는 새 경로만 씁니다(기존 파일 보존).
종료 코드는 정상·빈 결과 `0`, 입력·무결성 오류 `2`, 엔진·LLM 오류 `3`, 출력 오류 `4`입니다.
오류는 `*_status=error`와 `null` 결과로 반환해 정상적인 빈 결과(`empty`, `[]`)와 구분합니다.

## 실제 LLM 사용 (명시적 opt-in)

LLM 경계는 로컬 **Codex CLI 구독 로그인**이며 추가 API 구매를 전제로 하지 않습니다. 기본 테스트와 데모는 실제 호출을 하지 않습니다.

```bash
uv run --no-sync change-scope llm-smoke --case adversarial --live          # 경계 검증 smoke
uv run --no-sync change-scope extract --source DOC.md --source-id my-doc --source-version 1 --live --workspace runs/my-ws
uv run --no-sync change-scope analyze --snapshot S --target reranker --evidence C --live-suggestions
```

- 허용 버전은 실제 sandbox 검증을 통과한 `codex-cli 0.162.1`, `0.163.0`만이며 다른 버전은 모델 호출 전에 차단합니다.
- 호출 전 read-only 프로필에서 보호 파일 쓰기와 loopback 접속이 차단되는지 확인하고, 실행 중 shell·웹·MCP·plugin 등 도구를 끕니다.
  timeout(기본 120초)과 출력 크기 제한, 작업 공간 hash 비교를 적용합니다. 계정 파일·토큰은 앱이 읽지 않습니다.
- 응답은 스키마·서비스 범위·인용 위치/ID를 검증하고, 후보는 항상 `pending`, 제안은 항상 '검토 전'입니다.
- 모델 식별자는 CLI 출력에 없어 `model_id=null`(미확인)입니다. 검증 기록: [T01a·T02 검증](docs/t01a-t02-verification.md).

## Wirelog CLI 결과 대조 (선택)

```bash
uv run --no-sync python scripts/build_wirelog.py   # 네트워크와 C/C++ compiler 필요, .local/에 빌드
uv run --no-sync change-scope verify-wirelog --snapshot data/fixtures/s1/snapshot.json --target reranker \
  --binary .local/wirelog-v0.70.0/build/wirelog_cli
WIRELOG_CLI="$PWD/.local/wirelog-v0.70.0/build/wirelog_cli" uv run --no-sync pytest -q tests/test_wirelog_cli.py
```

## 평가

[평가 결과](docs/evaluation-results.md): holdout 실제 AI 추출 precision·recall 1.0(관계 5개), 정답 4문항 4/4, 평가 유형 24개 중 검증 21·부분 1·미실행 2.
사람 검토를 거친 holdout end-to-end는 다음처럼 실행합니다(판정은 사람이 합니다).

```bash
uv run --no-sync change-scope extract --source data/holdout/topology.md --source-id holdout-topology \
  --source-version 1 --live --workspace runs/holdout-ws
uv run --no-sync change-scope review show --workspace runs/holdout-ws --text      # 후보마다 decide
uv run --no-sync change-scope export --workspace runs/holdout-ws --output runs/holdout-snap
uv run --no-sync change-scope evaluate --questions data/holdout/questions.json \
  --snapshot runs/holdout-snap/snapshot.json --gold data/holdout/gold-snapshot.json --workspace runs/holdout-ws
```

## 한계

- 입력은 합성 Markdown 문서와 서비스 5개(`webapi, answer, retrieval, reranker, suggest`), 관계는 직접 HTTP 호출(`depends_on`)뿐입니다.
- 기존 테스트·문서 연결(covers/about)은 사람이 작성한 catalog fixture에서 읽습니다. 별칭 정규화는 없습니다.
- 화면에서 문서 업로드·승인은 하지 않습니다. 서버에는 인증이 없으며 같은 PC의 로컬 사용자를 구분하지 않습니다.
- ledger 끝부분 삭제는 export한 snapshot의 `ledger_head_sha256`을 따로 보관해야 검출됩니다.
- 실행 환경은 Linux/POSIX(파일 잠금, sandbox 검사)를 전제로 합니다.

## 문서

[제품 계약](docs/product-contract.md) · [평가 계획](docs/evaluation.md) · [평가 결과](docs/evaluation-results.md) ·
[T01a·T02 검증 기록](docs/t01a-t02-verification.md) · [출처](docs/source-map.md)

## 라이선스

[MIT](LICENSE)
