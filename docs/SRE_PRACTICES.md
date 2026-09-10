# SRE Practices — Self-Healing MLOps Agent

이 문서는 이 시스템의 신뢰성 목표(SLO/SLI)를 숫자로 정의하고, 실제로 있었던
인시던트를 SRE postmortem 형식으로 정리하고, 자동화가 사람에게 넘겼을 때
사람이 뭘 해야 하는지(runbook)를 담는다. 설계 배경은
[`specs/spec-sre-practices.md`](../specs/spec-sre-practices.md) 참고.

이 문서는 캡스톤 발표·학회 재발표·SRE 포트폴리오/취업 어필 세 용도로 같이
쓴다 — 별도 문서로 쪼개지 않는다.

---

## 1. SLO / SLI / Error Budget

이 시스템은 "무조건 자동으로 고친다"가 아니라 "검증된 만큼만 자동화한다"는
[Progressive Autonomy](../README.md) 원칙으로 설계됐다. 그래서 SLO도 하나의
뭉뚱그린 정확도 숫자가 아니라, 파이프라인 단계별로 나눠서 정의한다.

### 1.1 SLI 정의

| SLI | 정의 | 측정 방법 |
|---|---|---|
| **탐지 지연(Detection Latency)** | 로그에 에러가 찍힌 시점부터 `log_watcher`가 파이프라인을 가동하기까지 걸리는 시간 | `Debouncer`가 동일 시그니처를 묶어 처리(폭주 방지) — 실측 기반 평균 지연은 아직 별도 계측 안 함(향후 계측 항목) |
| **L1(빠른 기억) 응답 지연** | ChromaDB 벡터 검색으로 과거 해결책을 찾아 반환하는 데 걸리는 시간 | `agent_metrics.db`의 `resolution_source='L1_CACHE'` 행의 `latency_sec` |
| **L2(AI 추론) 응답 지연** | Groq API 호출 + self-reflection까지 포함한 지연 | `agent_metrics.db`의 `resolution_source='L2_LLM'` 행의 `latency_sec` — 2026-08-27 Groq 전환 후 실측 평균 **0.65초**(전환 전 23.7초 대비 36배) |
| **자동 실행 정확도(카테고리별)** | Shadow mode 승급 심사에 쓰는 FN(미탐)·FP(오탐) 비율 | `experiments/run_fp_fn_analysis.py --since <배포시각>`으로 배포 이후 구간만 집계 (배포 이전 이력이 섞이면 recall이 영구히 낮게 나오는 함정이 있음 — 2026-09-08 세션에서 실측으로 확인된 교훈) |
| **완전 자동 실행 성공률(end-to-end)** | L2/Rule이 생성한 명령어가 보안 화이트리스트·self-reflection·실제 실행까지 전부 통과해 성공한 비율 | `experiments/run_l2_production_path_check.py` — 2026-09-07/08 VM 실측 **8%** |

> **`8%`를 읽는 법(중요, 오독 방지)**: 이건 "AI가 8%만 맞다"는 정확도가 아니다.
> 나머지 92%는 대부분 실패가 아니라 **사람 승인으로 정상적으로 넘어간 것**이다
> (Progressive Autonomy의 `approve_then_execute` 단계가 기본값이기 때문).
> 이 숫자는 "사람 개입 없이 안전하게 완전 자동 실행되는 비율"로만 써야 한다.

### 1.2 SLO (승급 게이트 기준 — 2026-09-03 `/grill-me` 세션에서 확정)

Progressive Autonomy는 4단계(읽기전용 → 제안 → 승인후실행 → 자동)이고, 각
승급은 카테고리별로 아래 기준을 **사람이 리포트를 보고 수동으로** 승인해야
일어난다(자동 승급 로직은 코드 어디에도 없음 — `src/autonomy_store.py`,
`scripts/set_autonomy_level.py`).

| 승급 | 필요 표본 | 필요 기간 | 핵심 SLO |
|---|---|---|---|
| 제안 → 승인후실행 | 카테고리당 ≥ 50건 | ≥ 2주 | **FN(미탐) ≤ 5%** — 이 단계에서는 사람도 놓치는 미탐이 최악의 실패 |
| 승인후실행 → 자동 | 카테고리당 ≥ 50건 | ≥ 2주 | **FP(오탐) ≤ 5%** — 자동실행 단계의 오탐은 그 자체로 새 장애를 만듦 |

승급 판정은 `experiments/run_shadow_gate_report.py`가 기존 `agent_metrics.db`/
`chaos_injector.log`만 읽어서 산출한다(별도 트래킹 저장소를 새로 만들지 않음).

### 1.3 Error Budget

이 시스템은 "에러 버짓 %"를 별도 계산기로 관리하지 않는다 — 이미 코드로
**즉시 집행되는** 버짓 소진 메커니즘이 있다:

- **Circuit Breaker** (`src/circuit_breaker.py`): 동일 에러 시그니처가 **3회
  연속 실패**하면 그 시그니처에 대한 AI 자동 판단을 즉시 멈추고(`OPEN`)
  관리자에게 에스컬레이션한다. 이것이 사실상 "이 에러 유형에 대한 에러
  버짓 = 연속 실패 3회"라는 선언이다. 30분 후 `HALF_OPEN`으로 1회 시험
  재개.
- **온라인학습 자동 회수** (`src/llm_engine.py::record_learned_outcome`):
  온라인학습으로 생긴 해결책이 실패 2건 이상 **AND** 실패율 50% 초과가 되면
  그 지식 자체를 자동 삭제한다 — "학습된 해결책 하나"의 에러 버짓.

이 두 메커니즘은 신규로 만든 게 아니라 이미 운영 중인 안전장치를 SRE
용어(에러 버짓)로 재해석한 것이다.

---

## 2. Postmortem

> 이 문서 작성 시점(2026-09-10)에 가장 잘 검증된 실제 인시던트를 골랐다.
> 원래 초안(`spec-sre-practices.md`)은 카오스 OOM 인시던트를 예시로
> 들었지만, k8s 연동 작업(2026-09-09) 중 실제 VM에서 재현·수정·재검증까지
> 전 과정이 가장 상세히 기록된 이 인시던트로 대체했다 — 근거 없는 내용을
> 채우기보다 이미 실측된 사실만 쓰기로 한 판단.

### 2.1 인시던트: k8s 복구 실행 시 대상 리소스 오인 위험

| 항목 | 내용 |
|---|---|
| **날짜** | 2026-09-09 |
| **심각도** | Near miss(운영 서비스 미영향) — k8s 연동은 로컬/VM 검증 전용 병행 배포였고, 실제 운영 트래픽을 받는 `gcp-primary`(systemd) 경로는 전혀 건드리지 않음 |
| **영향** | 만약 이 문제가 감지되지 않고 그대로 나갔다면: `Memory_Leak` 카테고리 복구 시도가 대상 없이(`target=None`) 보안 검증에 막혀 **조용히 아무 조치도 안 됨**(가장 양호한 실패 형태). `Process_Crash`는 실제로 존재하지만 **전혀 무관한 리소스**를 대상으로 kubectl이 실행될 뻔했음(범용 placeholder `"pod"` 또는 LLM이 만든 임의 이름, 예: `"rsyslog"`) |
| **탐지 경로** | 자동 알람 아님 — k8s 연동 로컬→VM 이관 작업 중 실제 kubectl e2e 실행(`ActionExecutor._kill_process`/`_restart_service`를 실제 호출)으로 직접 관찰 |

**타임라인 (UTC 기준 추정, 분 단위 로그 없음 — 세션 기록 기반)**

1. **감지**: k8s 이관 작업 중 `TARGET_SERVER=gcp-k3s`로 `Memory_Leak`/`Process_Crash` 대상 복구를 실제 kubectl로 실행 — `target_process`가 `None`(Memory_Leak) 또는 범용 값(Process_Crash)으로 나옴을 확인.
2. **원인 파악**: L1 캐시/LLM이 반환하는 `target_process`는 systemd 시절 설계라 k8s 리소스 이름 체계와 애초에 매핑되지 않음을 코드 리뷰로 확인.
3. **조치**: `src/executor.py::_resolve_k8s_target_name()` 신규 — LLM/L1이 준 값을 맹목적으로 신뢰하지 않고, `kubectl get deployment`로 **실제 존재 여부를 확인한 뒤에만** 그대로 쓰고, 없으면 `config/servers.yaml`의 `k8s_target_app`(이 서버가 실제로 관리하는 단일 Deployment 이름, 예: `target-app`)으로 대체.
4. **검증**: `target_process="rsyslog"`(실존하지 않는 리소스)로 재현 → 자동 대체 확인 → 실제 `kubectl rollout restart` 성공. `Memory_Leak`도 `KILL_PROCESS` 성공까지 확인. 두 경우 모두 `agent_metrics.db`에 `SUCCESS`로 기록됨을 재확인.
5. **유사 패턴 재발견**: 같은 날, VM(`gcp-primary`, systemd) 경로에서도 동일한 근본 문제(`target_process`가 실제 systemd 유닛이 아닌 경우, 예: 무관하지만 실존하는 `rsyslog`를 조용히 재시작)를 발견 — `docker_target_app` 폴백을 동일한 원칙으로 추가(커밋 `5c9401c0`).

**근본 원인(Root Cause)**: 복구 대상 리소스 이름(`target_process`)을 생성하는
쪽(L1 캐시 큐레이션 데이터, LLM 추론)과 그 이름을 실제로 실행하는 쪽
(executor)이 서로 다른 시점에 설계돼, "이 이름이 실제로 존재하는가"를
검증하는 계층이 원래 없었다. systemd 단일 서버 시절엔 이름 하나만
관리하면 됐지만, 실행 방식(systemd/k8s)이 늘어나면서 이름 불일치가
표면화됨.

**잘된 점**: 실제 운영 트래픽(`gcp-primary`)과 완전히 분리된 병행 배포로
검증했기 때문에 이 문제가 사용자 영향 없이 발견됨(설계 결정이 사고를
예방함).

**액션 아이템**

| 액션 | 상태 |
|---|---|
| k8s 경로에 존재 여부 확인 후 폴백 (`_resolve_k8s_target_name`) | ✅ 완료 (커밋 `685a5861`) |
| systemd 경로에도 동일 원칙 적용 (`docker_target_app` 폴백) | ✅ 완료 (커밋 `5c9401c0`) |
| 회귀 테스트 (`tests/test_k8s_executor.py`) | ✅ 완료, CI 등록 |
| 대상 리소스 이름 생성 계층(L1 큐레이션/LLM) 자체를 k8s 인지형으로 재설계 | ⏳ 미착수 — 지금은 "생성 후 검증" 방식으로 방어, 근본적 재설계는 범위 밖으로 보류 |

---

## 3. Runbook — 에이전트가 사람에게 넘겼을 때

에이전트가 `ESCALATE_TO_HUMAN`으로 판단하거나 Progressive Autonomy가
`approve_then_execute` 단계인 카테고리에서 승인을 요청하면, Telegram(우선)
또는 Slack Webhook으로 알림이 온다. 아래는 그 알림을 받았을 때 확인할
순서다.

### 3.1 알림이 왔을 때

1. **알림 내용부터 읽는다** (`src/observability.py::log_event`가 보내는
   형식): 판단 소스(L1/L2/RULE), 시도한 액션, 실패 유형, 실패 상세.
2. **승인 요청(`approve_then_execute`)이라면** Telegram 메시지의
   `✅ 승인 (실행)` / `🚫 거절 (무시)` 버튼으로 결정한다 — **5분 내에
   응답하지 않으면 자동 타임아웃으로 조치가 취소된다**(`_await_approval`
   데몬 모드, `ApprovalTimeout`).
   - 메시지에 `⚠️ 자가 반성이 위험 판정` 문구가 포함돼 있으면, self-reflection이
     이 명령어에 우려를 표한 것이다(강제 차단은 아님 — 승인 여부는 여전히
     사람 판단). 명령어 내용을 특히 신중히 검토할 것.
3. **대시보드 "🕵️ 인시던트 상세" 탭**(`dashboard/app.py`)에서 해당
   인시던트를 찾아 감지 → L1/L2 판단 → self-reflection 결과 → 실행 조치 →
   최종 기록을 시간순으로 확인한다. `resolution_source`/`error_category`/
   `error_detail`로 최근 목록에서 식별.
4. **Circuit Breaker가 `OPEN`이라면**(탭2 "실시간 장애 조치" 상단 배너,
   또는 `data/circuit_breaker.db`) 동일 시그니처가 3회 연속 실패해 자동
   판단이 멈춘 상태다 — 근본 원인을 사람이 직접 조사해야 하며, 30분 후
   `HALF_OPEN`으로 자동 1회 재시도된다. 급하면 원인을 먼저 고치고 기다릴 것,
   임의로 상태를 강제 리셋하는 절차는 없다(의도적 — 자동 재시도보다 사람
   확인을 우선하는 설계).

### 3.2 조사가 필요할 때

- **최근 유사 사례 확인**: `experiments/run_fp_fn_analysis.py --since
  <조사 시작 시각>`으로 같은 카테고리의 최근 미탐/오탐 이력을 본다.
- **원본 로그 확인**: 대시보드 인시던트 상세 탭의 "감지" 단계에 표시되는
  `error_log`(최대 500자) 또는 `data/realtime_system.log` 원본.
- **패턴이 반복된다면**: `config/servers.yaml`의 `k8s_target_app`/
  `docker_target_app` 폴백 대상이 최신인지, 또는 §2.1 postmortem과 같은
  "대상 리소스 오인" 패턴인지 우선 의심한다.

### 3.3 하지 말아야 할 것

- 승인 대기 중인 명령어를 대시보드/DB를 거치지 않고 서버에서 직접 실행하지
  않는다 — 그러면 `record_learned_outcome`/온라인학습 되먹임이 이 실행
  결과를 반영하지 못해 학습 데이터가 왜곡된다.
- Circuit Breaker `OPEN` 상태를 근본 원인 확인 없이 코드/DB를 직접 건드려
  강제로 `CLOSED`로 되돌리지 않는다 — 이 버짓 소진은 의도된 안전장치다.
