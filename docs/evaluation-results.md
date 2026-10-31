# 평가 결과 (T09, 축소판)

기준일: 2026-10-31. 실행 기록은 `runs/t09/`(Git 제외)에 있으며 아래 수치는 그 실행에서 확인한 값입니다.

## 1. Holdout 평가

Holdout은 개발 중 테스트에 쓰지 않은 별도 합성 문서 1개(`data/holdout/topology.md`, 관계 5개, 순환 포함)와
미리 고정한 정답 4문항(`data/holdout/questions.json`)입니다. 정답은 문서에서 사람이 손으로 도출했으며 독립 주석자의 정답은 아닙니다.

### 1-1. 실제 AI 추출 (live)

| 항목 | 값 |
|---|---|
| 명령 | `change-scope extract --source data/holdout/topology.md --source-id holdout-topology --source-version 1 --live --workspace runs/t09/holdout-ws` |
| 호출 | Codex CLI 0.163.0 구독 호출 1회, sandbox 사전 검사(쓰기·loopback 차단) 통과, 작업공간 불변 |
| 후보 | 5개, 모두 source check 통과 |
| 정답 관계 대비 | precision 1.0 / recall 1.0 (오탐 0, 누락 0) |
| 한계 | 문서가 5줄짜리 명시적 호출 목록이라 쉬운 사례입니다. 모델 식별자는 CLI 출력에 없어 미확인입니다. |

### 1-2. 정답 4문항 (실제 PyreWire 1.1.2)

`change-scope evaluate --questions data/holdout/questions.json --snapshot data/holdout/gold-snapshot.json`

| ID | 질문 | 기대 | 결과 |
|---|---|---|---|
| H01 | reranker 변경 시 상위 점검 후보 | answer, retrieval, suggest, webapi | 통과 |
| H02 | answer 변경 시 상위 후보(순환 포함, 자기 제외) | retrieval, suggest, webapi | 통과 |
| H03 | webapi 변경 시 상위 후보 | (없음) | 통과 |
| H04 | retrieval → reranker 제거 시 reranker 상위 후보 | (없음) | 통과 |

4/4 통과, 모든 문항에서 witness 경로 집합과 엔진 결과가 일치했습니다. 이 평가는 손으로 옮긴 정답 snapshot(`data/holdout/gold-snapshot.json`, 인용 위치 기계 검증)을 입력으로 하므로 **엔진·규칙·경로 평가**입니다.

### 1-3. 사람 검토를 거친 end-to-end (대기)

실제 추출 후보를 사람이 판정해 export한 snapshot으로 같은 4문항을 실행하면 추출부터 분석까지의 전체 경로가 평가됩니다.
판정은 사람의 몫이므로 작성자 검토 전까지 미실행입니다. 실행 절차는 README의 평가 절을 따릅니다.

## 2. 평가 유형 24개 대응

상태: **검증** = 자동 테스트 또는 기록된 실행으로 확인 / **부분** = 일부만 확인 / **미실행** = 구현·실행하지 않음.

| # | 사례 | 상태 | 근거 |
|---|---|---|---|
| 1 | S1 정상 분석 | 검증 | `test_cli::test_real_s1_and_saved_result`, holdout H01 |
| 2 | 직접 호출 | 검증 | `test_witness::test_s1_direct_indirect_and_alternative_paths` |
| 3 | 간접 호출 | 검증 | 같은 테스트: 간접 영향은 경로로만 설명, 직접 사실로 저장 안 함 |
| 4 | 대체 경로 | 검증 | `test_witness::test_alternative_path_survives_a_removed_edge`, `test_scenario::test_s2_compare_story` |
| 5 | 순환 관계 | 검증 | `test_cli::test_cycle_terminates_and_excludes_target`, `test_witness` 순환 2건, holdout H02 |
| 6 | 중복 관계 | 검증 | `test_cli::test_deduplicated_edges_keep_support` |
| 7 | 같은 관계의 두 출처 | 검증 | `test_revisions::test_new_source_version_flow`, `test_witness::test_witness_hops_list_every_effective_source` |
| 8 | 반대 방향 후보 | 검증 | `test_review::test_public_cli_review_flow` (사람 반려), T03 시연 |
| 9 | 명시적인 별칭 | 미실행 | 별칭 정규화 미구현 |
| 10 | 근거 없는 별칭 | 미실행 | 별칭 정규화 미구현 |
| 11 | 미승인 새 문서 | 검증 | 새 원문 등록 후 승인 승계 없음(`test_revisions`), pending 제안은 기준 불변(`test_scenario::test_pending_proposals_never_change_the_base`) |
| 12 | 후보 수정 | 검증 | `test_revisions`: 새 revision은 pending, 이전 판정 보존 |
| 13 | 출처 버전 변경 | 검증 | `test_revisions::test_decisions_follow_the_latest_revision` (stale_source 승인 차단) |
| 14 | 관련 문서 없음 | 검증 | `test_evidence::test_tests_documents_claims_and_report` (후보 유지, 미확인 표시) |
| 15 | 테스트 연결 없음 | 검증 | 같은 테스트: 테스트 없음(미확인)과 LLM 제안(검토 전) 분리 |
| 16 | 엔진 오류·timeout | 검증 | `test_cli::test_engine_fault_is_never_empty`, `test_cli::test_unsupported_timeout_returns_engine_error` |
| 17 | 유효한 빈 결과 | 검증 | `test_cli::test_valid_empty_is_successful_query`, holdout H03 |
| 18 | 잘못된 target·snapshot | 검증 | `test_cli::test_unknown_target_is_input_error`, `test_cli::test_invalid_snapshot_rejected` |
| 19 | 문서 내 명령·잘못된 CLI JSON | 검증 | 악성 문서 live smoke(0.162.1, 0.163.0), `test_llm::test_bad_recordings_fail_closed`, `test_extraction::test_schema_violations_reject_whole_extraction` |
| 20 | 전체 시연 replay | 부분 | 입력·규칙 hash와 엔진 버전 기록, golden snapshot 재분석(`test_reviewed::test_t03_golden_snapshot_still_analyzes`). 새 환경 전체 재현은 T10 |
| 21 | 변경 제안 기준 불일치·잘못된 add/remove | 검증 | `test_scenario::test_invalid_proposals_are_refused`, `test_scenario::test_proposals_are_not_reapplied_to_another_base` |
| 22 | 원문 변경 뒤 과거 snapshot 조회 | 검증 | `test_revisions::test_new_source_version_flow` (S_a 재분석 동일), `test_reviewed` golden |
| 23 | 없는 quote·다른 section/version·의미 오류 | 검증 | `test_extraction::test_source_check_failures_are_recorded_per_candidate`, `test_review::test_failed_source_check_blocks_approval_but_allows_reject`, 의미 오류는 사람 반려 |
| 24 | 변경 대상에만 연결된 테스트 | 검증 | `test_evidence::test_target_checks_survive_an_empty_upstream_result` |

검증 21 / 부분 1 / 미실행 2.

## 3. 실행하지 않은 항목

- 동일 입력의 검색-only 방식과 규칙 결합 방식의 비교(T09 원래 범위) — 미실행.
- 별칭 정규화(유형 9·10) — 미구현.
- 사람 검토를 거친 holdout end-to-end(1-3) — 작성자 검토 대기.
- 큰 문서·다수 문서에서의 추출 정확도 — 미측정. holdout은 문서 1개, 관계 5개입니다.
