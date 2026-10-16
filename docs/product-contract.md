# 제품 계약 초안

상태: 계획 검토용 초안. 구현한 계약이나 검증된 동작이 아닙니다.

## 사용자와 입력

사용자는 작은 서비스의 변경을 검토하는 개발자입니다.
입력은 합성 Markdown 문서, 변경 대상 서비스 ID, 분석에 사용할 스냅샷 ID입니다.
작업을 누가 수행했는지는 관계 계산의 전제와 무관합니다.

서비스는 webapi, answer, retrieval, reranker, suggest로 시작합니다.
초기 문서는 구성, 변경 제안, 별칭, 설계 결정, 과거 장애, 운영 절차와 테스트 문서로 구성합니다.
코드베이스에서 모든 실제 의존성을 자동 추출했다고 주장하지 않습니다.

## 모델과 사실

초기 타입은 Service, API, Document, TestCase입니다.
계산에 필요한 관계는 아래와 같이 제한합니다.

| 관계 | 의미 | 입력 근거 |
|---|---|---|
| depends_on(Service, Service) | 관리 대상 사이의 직접 HTTP 호출 | 구성·API 문서 |
| about(Document, Service) | 특정 문서 버전이 서비스를 설명함 | 원문 메타데이터와 검토 |
| covers(TestCase, Service) | 기존 테스트 문서에 명시된 대상 | 테스트 원문 |

API 메서드·경로·소유 서비스는 별도 엔터티 속성으로 보존합니다.
필드 단위 사용 관계나 이벤트·DB 의존성은 별도 모델링 없이 HTTP 호출과 섞지 않습니다.
covers는 테스트와 서비스의 명시적인 연결이며 테스트가 충분하다는 뜻이 아닙니다.

사실 후보는 assertion_id, revision, subject, predicate, object, source_id,
source_version, section, quote, review_status를 포함해야 합니다.
quote의 원문 포함 여부와 타입은 기계적으로 확인하고, 관계 의미와 방향은 사람이 검토합니다.
인용 구절이 존재한다는 사실만으로 관계의 의미가 검증되지는 않습니다.
기계 검증은 지정한 source/version/hash와 section 안에 quote가 실제로 있는지도 확인합니다.
원문에 없는 quote 또는 다른 절·버전의 인용이면 승인·export를 차단합니다.
기계 검증을 통과해도 의미나 방향이 틀리면 사람 검토에서 반려합니다.

## 검토와 스냅샷

후보 revision은 pending으로 시작하고 approved 또는 rejected로 판정합니다.
수정은 새 pending revision을 만들며 과거 판정과 사유를 보존합니다.
출처 버전이나 후보 내용이 달라졌으면 예전 승인을 새 내용에 적용하지 않습니다.

문서 자체의 상태와 사실 후보의 검토 상태는 별도 필드입니다.
검토된 변경 제안도 실제 배포 상태를 뜻하지 않습니다.
스냅샷은 선택한 문서 버전·검토 revision·고유 관계 집합·규칙 버전으로 고정한 분석 모델입니다.
baseline 또는 proposed라는 분석 목적과 deployment_status=unverified를 분리해 표시합니다.

출처 하나의 철회와 새 시나리오에서의 관계 제거는 서로 다른 사건입니다.
같은 관계를 지지하는 유효 출처가 여러 개이면 하나의 출처 철회만으로 관계를 제거하지 않습니다.
과거 스냅샷은 해당 시점의 분석 기록으로 남기고 새 스냅샷에서 변경을 반영합니다.

각 스냅샷 manifest는 사용한 합성 Markdown 원문의 bytes hash와 보존된 blob을 참조합니다.
source_id/version 쌍에 다른 내용이 들어오면 덮어쓰지 않고 새 문서 revision을 요구합니다.
원문 blob이 없거나 hash가 맞지 않으면 과거 근거의 재현을 실패로 처리합니다.
보존 대상은 이 프로젝트의 합성 입력이며 교육 자료나 개인 노트 원문을 복제하는 계약은 아닙니다.

## 변경 제안 계약

관계 추출 후보와 변경 제안을 분리합니다.
변경 제안은 proposal_id, revision, base_snapshot_id, base_snapshot_hash,
operation=add|remove, 대상 (predicate, subject, object), 변경 요청의 원문 근거와 검토 상태를 가집니다.
pending 제안은 기준 스냅샷에 적용하지 않습니다.

승인된 제안도 지정한 기준 ID·hash에만 적용해 새 스냅샷을 만듭니다.
다른 기준에 자동 재적용하지 않으며 기준이 바뀌면 재검토를 요구합니다.
없는 관계의 remove, 이미 존재하는 관계의 add, 같은 batch 안의 충돌 연산은 입력 오류로 거부합니다.
변경 제안의 remove는 새 시나리오의 override이고, 기존 출처의 철회나 과거 스냅샷의 삭제가 아닙니다.
새 스냅샷의 manifest에 기준, 적용한 제안 revision, 출처 묶음과 override를 함께 남깁니다.

## 계산과 설명

1. 승인된 직접 호출 관계로 길이 1 이상의 reach를 계산합니다.
2. 변경 대상에 도달하는 서비스 중 대상 자신을 제외해 상위 점검 후보를 구합니다.
3. 변경 대상 자체와 상위 후보 각각에 명시적으로 연결된 기존 테스트·설명 문서를 결합합니다.
4. 검토된 입력 그래프에서 유효한 경로를 찾아 사실 ID와 출처로 연결합니다.
5. 최종 설명에 원문 사실, 규칙 결론, 추가 제안, 미확인을 나눕니다.

변경 대상 자체의 검토와 상위 후보의 개수는 구분합니다.
결과는 target_checks와 upstream_candidates 영역을 나누고 target 전용 테스트도 보존합니다.
문서가 없는 서비스도 그래프의 후보라면 유지합니다.
경로는 검토된 입력 그래프의 witness이며 native proof tree 또는 모든 도출 경로의 열거가 아닙니다.

## 기준 예제

| ID | src | dst |
|---|---|---|
| F01 | webapi | answer |
| F02 | answer | retrieval |
| F03 | retrieval | reranker |
| F04 | webapi | suggest |
| F05 | suggest | reranker |

S1에서 reranker의 상위 후보는 retrieval, answer, webapi, suggest입니다.
F03 제거 제안이 pending이면 S1은 그대로입니다.
검토한 제거안을 별도 S2에 반영하면 상위 후보는 suggest, webapi입니다.
webapi의 대체 경로가 유지되어야 하며 실제 배포 상태는 여전히 미확인입니다.

## 실행 상태

- analysis_status: success, empty, error를 구분합니다.
- 알 수 없는 target과 유효하지 않은 snapshot은 명시적인 입력 오류입니다.
- 유효 질의의 결과가 없을 때만 empty를 사용합니다.
- engine_mode: live_engine, recorded_replay, unavailable을 구분합니다.
- llm_mode: live_cli, recorded_output, not_used를 별도로 기록합니다.
- 엔진이나 LLM 실패 뒤 예전 실행 결과를 최신 결과처럼 표시하지 않습니다.
- LLM은 고정된 질의 템플릿과 작은 읽기 범위만 사용합니다. 임의 Datalog나 문서 내 명령을 실행하지 않습니다.

구독 CLI에는 합성 입력과 고정 요청만 전달하고, 별도 작업공간에서 읽기 전용 sandbox로 실행합니다.
필요 없는 shell·웹·MCP 등 도구와 도구의 임의 외부 접근은 지원 설정에서 제한해야 합니다.
모델 요청에 필요한 인증·통신과 모델 도구의 외부 접근은 구분합니다.
인증 파일을 입력 작업공간으로 복제하거나 프로젝트가 직접 읽지 않습니다.
T02에서 실제 제한과 timeout·출력 크기·비정상 종료를 확인하며, 제한을 강제할 수 없으면 live_cli 경로를 사용 가능으로 표시하지 않습니다.
문서의 악성 지시로 파일 변경·추가 명령 실행이 일어나지 않는지 검증합니다.
반환 JSON 검사만으로 실행 중 부작용을 막았다고 주장하지 않습니다.

분석 결과에는 snapshot ID, 입력·규칙 hash, 엔진 버전, 후보, 출처, 기존 테스트,
제안, 미확인, 실행 상태를 저장합니다.

## 사용자 경험

첫 기준 실행 경로는 로컬 CLI입니다. CLI에서 후보를 검토하고 스냅샷을 생성합니다.
최소 UI는 검토 기록·원문 화면과 영향·비교 결과 화면의 읽기 전용 뷰어입니다.
UI 안에서 승인까지 처리하는 HTTP 쓰기 API는 핵심 흐름 완성 후 여유가 있을 때 추가합니다.
두 화면을 제공한다는 계획을 다중 사용자 웹서비스 구축으로 확대하지 않습니다.
