# 자체 로그 누적 설계 — 수집 범위 · 동의 · 비식별화 정책

이 문서는 코드가 아니라 **설계 문서**다. 90일 데이터 축적 과정에서 쌓이는
로그/메트릭을 *어디까지, 누구 동의로, 어떻게 가공해서* 모을지를 정해두고,
향후 스키마·코드 구현의 근거로 쓴다. 이 문서 자체로는 어떤 수집 코드도
착수하지 않는다.

- v1 (2026-09 초): 팀 운영 VM 한정 — 수집 동의 + 익명화 초안
- v2 (2026-09-23): 셀프호스팅 사용자 데이터 공유(opt-in) 시나리오 추가,
  현행 코드 데이터 인벤토리(§2) 실측, 필드 단위 비식별화 규칙(§6) 구체화

> **잠정 문서다.** 본 문서의 수치·범위는 prod VM DB 스캔
> ([RESEARCH_SUMMARY §6 긴급 백로그 B1](RESEARCH_SUMMARY.md#긴급-백로그-우선순위-높음-2026-09-23-등록))
> 완료 전까지 임시 추정치이며, 스캔 결과에 따라 개정될 수 있다. §2의 실측은
> 로컬 개발 환경 데이터 기준이다.

> 법률 검토 문서가 아니다. 개인정보보호법 등 규제 적합성 판단은 실제 외부
> 수집을 시작하기 전에 별도로 받아야 하며, 이 문서는 그 검토에 넘길 기술적
> 사실관계와 기본 원칙을 정리한 것이다.

---

## 1. 왜 · 무엇을 위해 모으나

### 1.1 배경

현재 ChromaDB L1 캐시에 쌓인 데이터(총 1,318건, [README](../README.md#훈련-데이터-파이프라인-chromadb-총-1318건-실측-2026-09-18) 참고)는 세 갈래다.

| 소스 | 성격 | 한계 |
|---|---|---|
| GitHub 이슈 크롤링 (`source` 없음 / `github_v2`) | 공개 이슈 트래커의 에러 스니펫 | 우리 배포 환경의 실제 에러 분포와 무관 |
| 카오스 인젝터 시그니처 | 인위적으로 주입한 장애 패턴 | 커버리지가 우리가 미리 상상한 실패 케이스로 제한됨 |
| `source="online_learning"` (`src/llm_engine.py` `learn_from_feedback`) | L2/Rule이 실제로 성공시킨 (에러→커맨드) 쌍 | **이미 코드로 존재** — 배포된 만큼만, 우연히 마주친 에러만 쌓임 |

LogHub 로그(10개 무관 시스템, 2만 줄)는 오탐률(FP) **검증용**으로만 쓰고
있고, 애초에 타깃 앱과 무관한 일반 시스템 로그라 학습 데이터 확장 소스로
쓰기엔 맞지 않다. 그래서 "우리가 실제로 돌리는 환경의 로그를 어떻게
누적할지"를 먼저 설계해둔다.

### 1.2 수집 목적 (이 목적 밖으로는 쓰지 않는다)

| 코드 | 목적 | 필요한 데이터 |
|---|---|---|
| **P1 성능 개선** | L1 임계값(`_RAG_THRESHOLD`) 재조정, 온라인 학습 엔트리 품질 평가, L2 프롬프트 개선, QLoRA 재학습 데이터 | 에러 텍스트(비식별화본), 채택 경로, 명령어, 성공/실패 |
| **P2 벤치마크 재현** | FP/FN 분석, MTTR·탐지 지연·레이턴시 재측정을 같은 입력으로 다시 돌릴 수 있게 | 위 + 레이턴시·카테고리·타임스탬프(상대값) |
| **P3 데모/발표 사례** | 실제로 겪은 장애 사례를 재생해 보여주기 | **Tier 0(팀 운영) 데이터만.** Tier 1(외부 사용자) 데이터는 동의 시 P3를 별도로 허용한 경우에 한해, 비식별화본만 |

"고객 데모"는 P3에 포함되지만, 외부 사용자 데이터를 다른 잠재 고객에게
보여주는 것은 가장 민감한 사용이라 **기본 제외 + 별도 동의**로 둔다.

### 1.3 90일이라는 기간

축적 캠페인 기간이다(데이터 보관 기간과는 별개 — §7). 현재 코드와의 관계:

- `AgentObserver.export_csv(days=90)` — export 기본값은 이미 90일.
- **상충**: `src/maintenance.py`의 `RETENTION_DAYS = 30`이 `metrics` 테이블을
  매일 30일 초과분 삭제한다. 지금 코드 그대로면 90일치가 로컬에 남지 않는다.
  → 구현 시 둘 중 하나: (a) 축적 기간 동안 팀 운영 인스턴스만 `RETENTION_DAYS`를
  90 이상으로 올린다, (b) 30일보다 짧은 주기(예: 주 1회)로 §5의 export 번들을
  떠서 누적한다. **권장은 (b)** — 로컬 보관기간 기본값을 바꾸면 모든 셀프호스팅
  사용자의 로컬 보관량이 같이 늘어나기 때문(§8 점검 3).

---

## 2. 현황: 코드가 실제로 수집·저장하는 데이터 (2026-09-23 실측)

"새 수집"을 설계하기 전에, **이미** 저장되고 있는 것부터 정리한다. 모든
테이블은 `./data/agent_metrics.db` (SQLite) 한 파일에 있다.

| 저장소 | 필드 | 민감 정보 가능성 | 근거 |
|---|---|---|---|
| `metrics` | `error_log` | **높음** — 에러 줄 + 전후 최대 10줄 컨텍스트(`[LOG CONTEXT]`). 에러가 아닌 일반 줄(접근 로그, 요청 경로 등)도 섞임 | `src/log_watcher.py` `_build_context_window` |
| | `error_detail` | **높음** — 실행 실패 시 `traceback.format_exc()`·stderr 원문. 절대경로(`/home/<계정>/<repo>/src/...`) 포함 | `src/executor.py` `_result` 호출부 |
| | `command` | 중간 — PID, 파드명, 서비스명, 경로 | |
| | `reasoning`, `l2_diagnosis`, `l1_evidence` | 중간 — LLM 출력/과거 사건 요약이 로그·`df`/`ss` 출력을 인용할 수 있음 | `src/system_diagnostics.py` |
| | `timestamp`, `latency_sec`, `detection_latency_sec`, `action_type`, `resolution_source`, `success`, `result_category`, `error_type`, `error_category`, `l1_nearest_*`, `self_reflection_safe` | 낮음 — 열거형·수치 | |
| `pending_approvals` | `error_log`, `command`, `reason` | 높음 (위와 동일) | `src/approval_store.py` |
| | **`decided_by`** | **개인정보** — `telegram:<user_id>(<username 또는 이름>)` 또는 `web:<클라이언트 IP>` | `src/telegram_bot.py`, `src/approval_server.py` `_client_identity` |
| | `token` | 비밀값 — 승인 URL 토큰(만료 후에도 남음) | |
| `circuit_breaker` | `error_sig` | 낮음 — 에러 첫 줄 100자의 MD5. 단, 짧은 줄은 사전 대입으로 원문 역산 가능 | `src/circuit_breaker.py` `_sig` |
| `autonomy_state` / `shadow_events` | `updated_by`, 레벨 변경 이력 | 중간 — 수동 CLI 호출자가 넣는 자유 문자열(사람 이름일 수 있음) | `src/autonomy_store.py` |
| ChromaDB `online_learning` | `documents` = `error_log` **원문 그대로**, id = `learned_<원문 MD5>` | 높음 | `src/llm_engine.py` `learn_from_feedback` |
| ChromaDB `github_v2` 등 크롤링 | 공개 이슈 본문 | 중간 — 제3자 계정명·IP 포함 (§2.2) | `src/etl_github_crawler.py` |
| 앱 로그 (stdout / `log_file`) | JSON 로그 | 중간 — `error_log[:50]`, 명령어 등이 INFO로 찍힘 | `src/utils/logging_config.py` |

진단 명령(`src/system_diagnostics.py`)은 `ps -eo pid,ppid,%mem,%cpu,comm`으로
**커맨드라인 인자를 읽지 않는다** — 인자에 섞인 비밀값이 새는 경로는 현재 없다.
이 성질은 유지해야 한다(`comm` → `args`로 바꾸지 말 것).

### 2.1 이미 존재하는 외부 전송 경로 (프로젝트 팀과 무관한 제3자)

이 문서의 "외부 공유"(§4)와 별개로, 현재 코드는 이미 다음 제3자에게 로그를 보낸다.

| 경로 | 보내는 내용 | 조건 |
|---|---|---|
| Groq API (`api.groq.com`) | `error_log` 전체 + `gather_system_context` 출력(`free`/`df`/`ps comm`/`ss`) | `GROQ_API_KEY` 설정 시 (1순위 LLM) |
| Slack Webhook | `error_log[:300]`, 명령어 | `SLACK_WEBHOOK_URL` 설정 시 |
| Telegram Bot API | 에러 요약, 명령어, 승인 버튼 | 텔레그램 설정 시 |

Ollama 전용 모드(`GROQ_API_KEY` 미설정)면 LLM 판단 단계에서는 로그가 인스턴스
밖으로 나가지 않는다. README에는 "1순위 Groq API"라는 사실만 있고, **로그
원문이 제3자에게 전송된다는 고지는 없다** → 긴급 백로그 B2(§9). (특정 업종용 기능이 아니라, 모든 셀프호스팅 사용자가 자기
데이터가 어디로 가는지 알아야 한다는 문제다.)

### 2.2 로컬 데이터 실측 스캔 결과

로컬 개발 환경의 `agent_metrics.db`(metrics 47건, pending_approvals 7건),
로그 파일, ChromaDB(75건)를 정규식으로 스캔한 결과:

| 위치 | 발견 | 의미 |
|---|---|---|
| ChromaDB `online_learning` | 내부 IP `10.0.x.x` 원문 저장 | 온라인 학습이 **마스킹 없이** 원문을 저장한다는 것이 실데이터로 확인됨 |
| ChromaDB `github_v2` | `/home/<제3자 계정>` 형태 경로, 공인 IP | 공개 데이터라도 재배포(데이터셋 공개, 발표 자료) 시 비식별화 필요 |
| git 추적 `data/*.json`, `data/qlora/*.jsonl`, `experiments/results/*.csv` | `/home/<계정>` 형태 수십 종(공개 데이터셋 유래) | 위와 동일 |
| metrics / pending_approvals (로컬) | 해당 없음 | 로컬 DB는 데모 데이터라 깨끗함. **prod VM DB는 미확인** — 구현 전 같은 스캔을 VM에서 1회 돌려야 함(§9) |

### 2.3 실사용자명 노출(9/23 문서 정비 사례)과 같은 유형이 수집 데이터에 남는 경로

9/23 README/SRE_PRACTICES.md 정비 때 문서에서 제거한 VM 리눅스 계정명은
문서뿐 아니라 **수집 데이터**에도 다음 경로로 들어올 수 있다.

| 경로 | 예시 | 들어가는 필드 |
|---|---|---|
| Python traceback의 절대경로 | `File "/home/<계정>/self-healing-mlops-agent/src/executor.py"` — systemd 유닛의 `WorkingDirectory=__REPO_DIR__`가 홈 아래면 실패 traceback마다 발생 | `error_detail`, `error_log` |
| 명령 stderr | `Permission denied: '/home/<계정>/...'` | `error_detail` |
| 인증/시스템 로그를 감시 대상으로 둔 경우 | `sudo: <계정> : TTY=pts/0 ; COMMAND=...`, `Accepted publickey for <계정> from <IP>`, `session opened for user <계정>` | `error_log` 컨텍스트 줄 |
| 승인자 식별 | `telegram:12345(<username>)` | `decided_by` |
| 수동 CLI | `set_autonomy_level.py`의 변경자 인자 | `autonomy_state.updated_by`, `shadow_events` |
| 크론·셸 프롬프트 | `crontab -u <계정>`, `<계정>@<host>:~$` | 로그 원문 |

→ 경로 패턴(`/home/<x>/`)만 잡는 정규식으로는 `sudo: <계정> :`처럼 경로가
아닌 위치의 계정명을 놓친다. 그래서 §6.2에 **인스턴스 고유 식별자
정확일치 치환**(export 시점에 그 인스턴스의 실제 계정명·호스트명을 읽어
문자열 그대로 치환) 규칙 R1을 둔다.

참고: 해당 계정명은 HEAD에서는 제거됐지만 `origin/main` git 히스토리
(df290b07에서 추가 → c18ba728에서 제거)에는 남아 있다. 히스토리 재작성은
이 문서 범위 밖이며, 필요 여부는 별도 판단.

---

## 3. 수집 범위: 두 개의 Tier

| | **Tier 0 — 팀 운영 인스턴스** | **Tier 1 — 셀프호스팅 사용자의 자발적 공유** |
|---|---|---|
| 대상 | 팀이 운영하는 GCP VM의 타깃 앱/에이전트 | 이 프로젝트를 직접 배포해서 쓰는 외부 사용자(잠재 고객)의 인스턴스 |
| 동의 주체 | 팀원·공동 운영자·시연 협조자 (v1 원칙 그대로) | 그 인스턴스의 **운영 책임자**(로그가 나오는 시스템에 대한 권한을 가진 사람/조직) |
| 기본값 | 캠페인 기간 중 수집 (팀 내 동의 기록 후) | **비수집.** 명시적 opt-in 없이는 어떤 데이터도 인스턴스 밖으로 나가지 않는다 |
| 전송 방식 | 팀이 VM에서 직접 export | 사용자가 만든 **export 번들을 사용자가 직접 전달** (§5) |
| 사용 목적 | P1, P2, P3 | P1, P2 (P3는 별도 허용 시에만) |

v1은 "제3자 서비스에 붙이는 경우는 다루지 않는다"고 스코프 밖으로 뒀었다.
v2는 그중 **"셀프호스팅 사용자가 자기 판단으로 데이터를 보내주는 경우"만**
Tier 1로 편입한다. 팀이 제3자 서버에 에이전트를 직접 설치·운영하는 경우는
여전히 스코프 밖(별도 계약 사안)이다.

### 3.1 수집하는 것 / 하지 않는 것 (Tier 공통 allowlist)

allowlist 방식이다 — 아래 표에 없는 필드는 export 대상이 아니다. `metrics`에
새 컬럼이 추가돼도 이 표를 갱신하기 전까지는 export되지 않아야 한다.

| 범주 | 필드 | 공유 레벨(§4.2) |
|---|---|---|
| 조치 메타데이터 | `action_type`, `resolution_source`, `result_category`, `success`, `error_type`, `error_category`, `self_reflection_safe`, `l1_nearest_category` | L1 |
| 수치 | `latency_sec`, `detection_latency_sec`, `l1_nearest_distance` | L1 |
| 시각 | `timestamp` → 가공(§6.1) | L1 |
| 승인 이력 | 승인/거부/만료 **결과와 소요시간만** (`status`, `decided_at - created_at`), 승인 채널 종류(`telegram`/`web`) | L1 |
| 자율성 레벨 이력 | `category`, `from_level`, `to_level`, `event`, 시각(가공) | L1 |
| 에러 텍스트 | `error_log` → 마스킹(§6.2) | L2 |
| 조치 내용 | `command`, `reasoning`, `l2_diagnosis`, `l1_evidence`, `error_detail` → 마스킹 | L2 |

**절대 수집하지 않는 것** (레벨·동의와 무관):
`decided_by`의 사용자 식별자 부분, 승인 `token`, `updated_by` 자유 문자열,
`.env`·설정 파일 내용, 환경변수 값, Slack/Telegram 웹훅 URL·봇 토큰,
`gather_system_context` 원본 출력, ChromaDB 임베딩 벡터, 앱 JSON 로그 원문.

---

## 4. 동의 정책

### 4.1 원칙

1. **기본값 비수집.** 설치 직후, 설정을 건드리지 않은 인스턴스는 프로젝트
   팀으로 아무것도 보내지 않는다. 이를 위해 에이전트에 **자동 전송(텔레메트리
   push) 코드를 넣지 않는다** — "기본값 off인 전송 기능"보다 "전송 기능 자체가
   없음"이 검증하기 쉽고, 외부망이 막힌 환경에서도 똑같이 쓸 수 있다.
2. **opt-in은 행위로 표현한다.** 체크박스가 아니라, 사용자가 export 명령을
   직접 실행하고 → 결과 번들을 직접 열어보고 → 직접 보내는 세 단계 자체가
   동의 행위다. 어느 단계에서든 멈추면 아무것도 전달되지 않는다.
3. **범위는 레벨로 쪼갠다.** 텍스트 없는 집계만 보내고 싶은 사용자와,
   비식별화된 에러 텍스트까지 보내도 되는 사용자를 구분한다(§4.2).
4. **철회 가능해야 한다.** 받은 데이터를 인스턴스 단위로 식별·삭제할 수
   있게 설계한다(§4.4).
5. **동의 문구는 버전 관리한다.** 문구가 바뀌면 이전 버전으로 동의한 번들은
   이전 문구 범위 안에서만 쓴다.

### 4.2 공유 레벨

| 레벨 | 내용 | 텍스트 포함 | 용도 |
|---|---|---|---|
| **L0** (기본) | 공유 안 함 | — | — |
| **L1 집계** | §3.1 중 L1 필드만. 행 단위 메타데이터·수치 | 없음 | P2(레이턴시·성공률·카테고리 분포 재현), P1 일부(임계값 분석) |
| **L2 비식별 텍스트** | L1 + 마스킹된 에러/조치 텍스트 | 있음 (마스킹 후) | P1 전체, P2 전체 |
| **+P3** (L2 옵션) | L2 데이터를 데모/발표 사례로 써도 됨 | — | P3 |

**P3 사용 승인 절차 (팀 내부):** 사용자의 `allow_p3_demo: true`는 필요조건일
뿐이다. 실제로 특정 사례를 데모/발표에 쓰려면 **프로젝트 리드(현재 저장소
관리자)가 사례별로 승인**하고, 사례를 고른 사람이 아닌 다른 1인이 마스킹
결과를 육안 확인한다(2인 확인 — 팀이 1인이면 지도교수가 확인자). 승인 내역
(instance_id, 사례 순번, 승인자 역할, 날짜, 사용처)은 `consent_log`에 남긴다.
Tier 0 데이터의 P3 사용도 팀 외부로 나가는 것이므로 같은 2인 확인을 거친다.

### 4.3 동의 기록 (번들 안의 `consent.json`)

번들마다 아래 기록을 포함한다. 팀은 이 파일이 없거나 형식이 틀린 번들은
열지 않고 폐기한다.

```json
{
  "consent_version": "2026-09-23.v1",
  "instance_id": "<export 최초 실행 시 로컬에서 생성한 UUIDv4>",
  "level": "L1 | L2",
  "allow_p3_demo": false,
  "period": {"from": "<ISO 날짜>", "to": "<ISO 날짜>"},
  "exported_at": "<ISO 시각, UTC, 시 단위 절삭>",
  "contact": "<선택. 철회/문의 회신용. 비워도 됨>",
  "agent_version": "<git short sha>"
}
```

- `instance_id`는 호스트명·MAC·IP 등 **어떤 기존 식별자에서도 파생하지 않는
  랜덤 UUID**다. 로컬 `./data/`에 저장해 다음 export 때 재사용한다(같은
  인스턴스의 번들을 묶어서 철회할 수 있도록). 사용자가 이 파일을 지우면 새 ID가
  생긴다 — 이것도 사용자가 고를 수 있는 연결 끊기 수단이다.
- `contact`는 유일하게 사람을 식별할 수 있는 필드라 선택 항목이며, 팀은 이
  값을 데이터 본문과 **분리된 장소**에 보관한다(§7).
- 팀 쪽에는 `consent_log`(instance_id, consent_version, level, allow_p3,
  수신일, 철회일)를 둔다. v1에서 제안한 `CONSENT.md`는 Tier 0(팀 내부)
  기록용으로 유지한다.

### 4.4 동의 철회

| 대상 | 처리 | 기한 |
|---|---|---|
| 앞으로의 수집 | 사용자가 export를 안 하면 끝 (자동 전송이 없으므로 별도 조치 불필요) | 즉시 |
| 팀이 보관 중인 번들 원본 | `instance_id`로 식별해 삭제 | 요청 후 30일 이내 |
| ChromaDB에 반영된 엔트리 | metadata `consent_instance_id`로 필터해 삭제 → 이를 위해 Tier 1 유래 엔트리는 **반드시** 이 필드를 달고 들어가야 함 | 30일 이내 |
| 벤치마크 결과(집계 수치) | 이미 공개된 집계 수치(예: "성공률 X%")는 되돌릴 수 없음 → 원본 행만 삭제, 이후 재측정부터 제외 | 다음 재측정 |
| 학습된 모델 가중치(QLoRA 등) | 가중치에서 개별 데이터를 제거할 수 없음 → **Tier 1 데이터를 가중치 학습에 쓰기 전에** 동의 문구에 이 한계를 명시해야 하고, 철회 시 다음 재학습부터 제외 | 다음 재학습 |

철회 요청 경로에는 인증이 없으므로 `instance_id`를 아는 것을 소유 증명으로
본다(번들을 보낸 쪽만 알고 있음). 철회 처리 결과는 `consent_log`에 날짜만
남기고 번들 내용은 남기지 않는다.

### 4.5 Tier 0 (팀 운영 인스턴스)

v1 원칙 그대로다: 수집 전 고지(범위·목적·저장 위치) → `CONSENT.md`에
"누가/언제/무엇에" 기록 → 철회 시 해당 소스 식별·삭제. 차이점은 Tier 0도
**팀 외부로 나가는 순간**(발표 자료, 공개 데이터셋, 대회 제출물) Tier 1과
같은 §6 비식별화를 거친다는 것.

---

## 5. 전달 방식: 로컬 export 번들

Tier 1의 유일한 전달 수단. 구현 형태 제안(코드는 이번 범위 밖):

```
scripts/export_shared_bundle.py --level L1|L2 --days 30 [--allow-p3] [--out DIR]
  → DIR/bundle_<instance_id 앞 8자>_<YYYYMMDD>/
       consent.json
       metrics.jsonl          # §3.1 allowlist 필드만, §6 가공 후
       approvals.jsonl        # 결과·소요시간·채널만
       autonomy_events.jsonl
       masking_report.json    # 규칙별 치환 건수, 잔존 검사 결과(§6.4)
       README.txt             # 사람이 읽는 요약: 몇 건, 어떤 필드, 어떤 레벨
```

- export 스크립트는 **네트워크 호출을 하지 않는다.** 파일만 만든다.
- 사용자가 직접 열어 확인할 수 있게 사람이 읽을 수 있는 JSONL로 만든다
  (압축·암호화는 전달 단계에서 사용자가 선택).
- §6.4 잔존 검사가 실패하면 번들을 만들지 않고 종료한다(부분 번들 금지).
- 마스킹은 **export 사본에만** 적용한다. 로컬 DB와, 파이프라인이 진단에
  쓰는 텍스트는 건드리지 않는다(v1 원칙 유지 — IP를 지우면 어느 서버
  문제인지 진단 근거가 사라짐).
- Tier 0 축적도 같은 스크립트를 쓴다(팀 VM에서 주 1회, §1.3 (b)).

---

## 6. 비식별화 규칙 (필드 단위)

처리 방식 용어:

| 방식 | 뜻 |
|---|---|
| **DROP** | 필드를 번들에서 뺀다 |
| **KEEP** | 그대로 둔다 (열거형·수치) |
| **MASK** | §6.2 패턴 치환을 적용한 텍스트 |
| **PSEUDO** | 번들 안에서 일관된 가명으로 치환 (`<IP_1>`, `<IP_2>` …). 같은 번들 안에서 같은 원본은 같은 가명 → "같은 서버에서 반복된 장애"라는 구조는 남고 실제 값은 사라짐. 가명 매핑표는 **번들에 넣지 않고 export 종료 시 메모리에서 폐기** |
| **GEN** | 일반화 (시각 절삭, 구간화 등) |

해싱(HMAC 등)은 쓰지 않는다. 번들 간 연결이 필요한 경우가 없고(연결은
`instance_id`로 충분), IP·계정명처럼 후보 공간이 작은 값은 해시해도 사전
대입으로 역산되기 때문이다. 같은 이유로 **원문 MD5에서 나온 ID
(`learned_<md5>`, `error_sig`)는 DROP**하고, 필요하면 마스킹된 텍스트로
새 ID를 만든다.

### 6.1 필드별 처리표

| 테이블.필드 | L1 | L2 | 비고 |
|---|---|---|---|
| `metrics.id` | DROP | DROP | 번들 내 순번으로 대체 |
| `metrics.timestamp` | GEN | GEN | 시 단위 절삭(`2026-09-23T14:00Z`) + 번들 첫 이벤트 기준 상대초 `t_offset_sec` 별도 필드. 상대초는 이벤트 간 간격(P2 재현)용 |
| `metrics.error_log` | DROP | MASK + PSEUDO | `[LOG CONTEXT]` 블록은 L2에서도 **에러 줄 ±3줄로 축소** 후 마스킹 — 비에러 컨텍스트 줄이 가장 예측 불가능한 내용(접근 로그, 요청 파라미터)을 담기 때문 |
| `metrics.error_detail` | DROP | MASK | traceback 프레임 경로는 `<REPO>/src/executor.py:123` 형태로 정규화(R1·R3) |
| `metrics.command` | DROP | MASK + PSEUDO | PID는 KEEP(재부팅마다 바뀌어 식별력 없음, 진단 재현에 필요) |
| `metrics.reasoning` / `l2_diagnosis` / `l1_evidence` | DROP | MASK + PSEUDO | |
| `metrics.action_type`, `resolution_source`, `result_category`, `success`, `error_type`, `error_category`, `self_reflection_safe`, `l1_nearest_category` | KEEP | KEEP | 코드에 정의된 값 집합 밖의 값이면 `OTHER` |
| `metrics.latency_sec`, `detection_latency_sec`, `l1_nearest_distance` | KEEP | KEEP | |
| `pending_approvals.token` | DROP | DROP | |
| `pending_approvals.decided_by` | GEN | GEN | `telegram:…` → `telegram`, `web:…` → `web`, NULL → `unknown`. **ID·이름·IP는 어떤 레벨에서도 나가지 않음** |
| `pending_approvals.status` | KEEP | KEEP | |
| `pending_approvals.created_at` / `decided_at` / `expires_at` | GEN | GEN | 시 단위 절삭 + `decision_latency_sec = decided_at - created_at` 파생 필드 |
| `pending_approvals.error_log` / `command` / `reason` | DROP | MASK + PSEUDO | `metrics`와 같은 가명표 공유 |
| `autonomy_state.updated_by`, `shadow_events`의 자유 문자열 | DROP | DROP | |
| `shadow_events.category/from_level/to_level/event` | KEEP | KEEP | |
| `circuit_breaker.*` | DROP | DROP | `error_sig`가 원문 MD5, 나머지는 metrics에서 재구성 가능 |
| ChromaDB 전체 | DROP | DROP | export 대상 아님. 팀이 받은 L2 번들로 **팀 쪽에서** 재임베딩 |

### 6.2 텍스트 마스킹 규칙 (적용 순서대로)

순서가 중요하다 — 구체적인 패턴을 먼저, 일반 패턴을 나중에 적용해야 긴
토큰이 부분 치환으로 깨지지 않는다.

| # | 대상 | 치환 | 탐지 방법 |
|---|---|---|---|
| R1 | **인스턴스 고유 식별자** (정확일치) | 계정명 → `<USER>`, 호스트명·FQDN → `<HOST>`, 저장소 경로 → `<REPO>` | export 시점에 로컬에서 읽는다: 현재 OS 계정명, 에이전트 실행 계정, `/home/*` 디렉터리명 목록, `hostname`/`hostname -f`, 저장소 절대경로. 대소문자 무시, 단어 경계 기준. §2.3의 "경로가 아닌 위치의 계정명"을 잡는 규칙 |
| R2 | 비밀값 | `<SECRET>` | 알려진 접두어(`gsk_`, `sk-`, `xoxb-`, `xoxp-`, `ghp_`, `AKIA`, `Bearer `), `KEY=`/`TOKEN=`/`PASSWORD=`/`SECRET=` 뒤 값, JWT(`eyJ…\.…\.…`), 32자 이상 base64/hex 연속열. 추가로 로컬 `.env`의 **값 목록**을 읽어 정확일치 치환(파일 내용 자체는 번들에 들어가지 않음) |
| R3 | 파일 경로 | `/home/<x>/`, `/Users/<x>/`, `C:\Users\<x>\`, `/root/` → `<HOME>/` | R1 이후 남은 경로 패턴 |
| R4 | 이메일 | `<EMAIL_n>` (PSEUDO) | |
| R5 | URL | 호스트가 `localhost`/`127.0.0.1`이면 유지, 나머지 호스트 → `<HOST_n>`. 쿼리스트링은 통째로 `?<QUERY>` | |
| R6 | IPv4 / IPv6 | `<IP_n>` (PSEUDO). 예외 KEEP: `127.0.0.1`, `0.0.0.0`, `::1`, 문서용 대역(`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`) — 카오스 인젝터가 쓰는 고정값이라 진단 재현에 필요. 사설 대역(10/8, 172.16/12, 192.168/16)도 **가명 처리**(내부 망 구조 노출 방지) | |
| R7 | MAC 주소 | `<MAC>` | |
| R8 | K8s 리소스명 | 파드 해시 접미사(`-<8~10자>-<5자>`) → `-<HASH>`; 네임스페이스·디플로이먼트명은 에이전트 설정에 등록된 타깃 이름이 아니면 `<K8S_n>` | |
| R9 | 도메인 | R5에서 안 잡힌 FQDN → `<HOST_n>`. 예외: 잘 알려진 공개 레지스트리(`pypi.org`, `github.com`, `docker.io` 등 allowlist) | |
| R10 | 긴 숫자열 | 9자리 이상 숫자 → `<NUM>` (주민번호·카드번호·계좌번호 형태가 로그에 우연히 섞이는 경우 대비. 포트·PID·상태코드는 8자리 미만이라 영향 없음) | |

치환 토큰은 모두 `<대문자>` 또는 `<대문자_숫자>` 형식으로 통일해 이후 파서가
쉽게 인식하게 한다. 마스킹 후 텍스트는 원래 로그 줄 구조(타임스탬프 위치,
레벨 키워드, 예외 클래스명, 에러 메시지 본문)를 유지해야 한다 — `ERROR`,
`OOMKilled`, `ConnectionRefusedError` 같은 진단 핵심 토큰은 어떤 규칙에도
걸리지 않아야 한다(§6.4 회귀 테스트로 확인).

### 6.3 공개 데이터(크롤링·LogHub) 재배포 시

§2.2처럼 git에 추적 중인 `data/*.json`, `data/qlora/*.jsonl`,
`experiments/results/*.csv`에는 공개 이슈 유래 제3자 계정명·IP가 있다.
수집 동의 문제는 아니지만(원래 공개된 텍스트), **데이터셋을 별도로
공개하거나 발표 자료에 원문을 인용할 때**는 R3~R10을 같은 방식으로
적용한다. 현재 저장소 파일을 일괄 수정하는 것은 이 문서 범위 밖(§9).

### 6.4 검증 절차

1. **잔존 검사 (자동, export마다):** 마스킹 완료 텍스트에 R1 정확일치 목록과
   R2·R3·R6 패턴을 다시 돌려 **0건이어야 통과**. 1건이라도 남으면 번들 생성
   중단 + `masking_report.json`에 위치만 기록(값은 기록하지 않음).
2. **회귀 테스트 (자동, CI):** 마스킹 모듈 단위 테스트에 (a) 반드시 잡혀야
   하는 샘플 — §2.3 표의 예시 줄 전부, 실제 사례를 모사한 합성 계정명 — 과
   (b) 절대 바뀌면 안 되는 샘플 — 카오스 인젝터 시그니처 원문, 예외
   클래스명 — 을 고정 픽스처로 둔다. 실제 계정명은 픽스처에
   넣지 않는다.
3. **표본 사람 리뷰 (최초 1회 + 규칙 변경 시):** v1 절차 유지. 기존
   `learned_*` 엔트리와 prod VM `metrics` 중 N건(제안: 50건)의 마스킹 전/후를
   나란히 놓고 사람이 대조한다. 놓친 패턴은 R 규칙에 추가하고 (2)의
   픽스처에도 넣는다.
4. **L2 번들 수신 시 (팀 쪽):** 받은 번들에 같은 잔존 검사를 한 번 더
   돌린다. 실패한 번들은 열람하지 않고 폐기하고, 연락처가 있으면 통보.

---

## 7. 보관 기간

| 데이터 | 위치 | 보관 | 비고 |
|---|---|---|---|
| **셀프호스팅 인스턴스 로컬 DB** | 사용자 인스턴스 | 사용자 결정 사항 | `metrics` 30일 기본값은 **유지**(§1.3). 단 `pending_approvals`·`shadow_events`는 현재 삭제 정책이 없어 `decided_by`(개인정보)가 무기한 남는다 → 같은 `RETENTION_DAYS` 적용을 §9에 제안 |
| Tier 0 원문(마스킹 전) export | 팀 GCP VM | 마스킹 검증 완료 후 30일 뒤 폐기 | v1에서 미정이던 일수를 30일로 제안 |
| Tier 0 / Tier 1 비식별화 번들 | 팀 저장소(비공개) | 수신일로부터 12개월, 또는 철회 시까지 | **잠정값 — 확정 근거 없음, 추후 조정 가능.** 90일 축적 + 분석·발표 1주기를 덮는 정도로 잡은 값이며, 발표/출판 일정이 확정되면 그에 맞춰 재설정. 12개월 뒤에도 필요하면 재동의 |
| `consent_log` | 팀 저장소, 번들과 분리 | 마지막 번들 삭제 후 12개월 | 철회 처리 증빙용 |
| `contact` 값 | 팀 저장소, 번들·consent_log와 분리 | 해당 instance_id 번들 전부 삭제 시 함께 삭제 | |
| ChromaDB 반영분 | 팀 운영 인스턴스 | 원 번들 보관 기간과 연동 — 번들 삭제 시 `consent_instance_id`로 함께 삭제 | v1의 "삭제 정책 없이 유지"는 **Tier 0 유래 엔트리에만** 해당하도록 축소 |

---

## 8. 세그먼트 중립성 자기점검

이 문서는 "타깃 세그먼트가 확정되기 전에 안전하게 해둘 수 있는 작업"이어야
한다. 점검 결과:

| # | 점검 항목 | 결과 |
|---|---|---|
| 1 | 특정 업종 전용 기능이 있는가 | 없음. 기본값 비수집·로컬 export·레벨 분리는 모든 사용자에게 동일하게 적용된다. 규제 산업 고려는 "가장 보수적인 사용자에게도 문제없는 기본값을 고른다"는 기준으로만 반영했다 |
| 2 | 업종 규제 준수를 주장하는가 | 하지 않음. 특정 규정 이름이나 인증 적합성 주장이 없고, 서두에 법률 검토 아님을 명시 |
| 3 | 모든 사용자에게 영향을 주는 기본값을 바꾸는가 | 로컬 `RETENTION_DAYS` 등은 바꾸지 않고, 축적은 팀 인스턴스의 주기적 export로 해결(§1.3). 유일한 기본값 변경 제안(승인 이력 보관기간, §7)은 개인정보 최소 보관 원칙이라 세그먼트와 무관 |
| 4 | 되돌리기 어려운 결정이 있는가 | 없음. 자동 전송 코드를 만들지 않으므로, 세그먼트가 정해져 요구사항이 바뀌어도 "기능을 추가"하는 방향으로만 확장된다 |
| 5 | 세그먼트가 정해지면 다시 봐야 할 부분 | §6.2 R8(K8s)·R10(숫자열) 규칙의 강도, §7 보관 기간 수치, P3 데모 사용 정책. 이 부분은 세그먼트별로 조정 가능한 **파라미터**로 취급 |

---

## 9. 코드화 TODO (이번 문서 범위 밖)

v1 항목 포함, 우선순위 순. 이 문서 작성 중 발견된 **기존 코드 문제**(정책
구현과 무관하게 먼저 판단해야 하는 것)는 여기가 아니라
[RESEARCH_SUMMARY §6 긴급 백로그](RESEARCH_SUMMARY.md#긴급-백로그-우선순위-높음-2026-09-23-등록)에
우선순위 높음으로 따로 등록했다:

- B1 prod VM DB 스캔 (이 문서 수치 확정의 선행 조건)
- B2 Groq 제3자 전송 미고지 (§2.1) — README 고지 여부를 포함한 대응 방향 미결정
- B3 `decided_by` PII + `pending_approvals` 무기한 보관 (§2, §7)

아래는 정책 구현 TODO:

- [ ] 마스킹 모듈 — §6.2 R1~R10, 순서 고정 (위치 후보: `src/utils/anonymizer.py`)
- [ ] 마스킹 회귀 테스트 — §6.4 (2) 픽스처
- [ ] `scripts/export_shared_bundle.py` — §5, 네트워크 호출 없음, 잔존 검사 실패 시 중단
- [ ] `shadow_events` 보관기간 정책 (§7 — `pending_approvals`는 B3에서 함께 판단)
- [ ] ChromaDB metadata에 `consent_instance_id` / `tier` 필드 (Tier 1 유래
      엔트리 필수 — v1의 "동의 세션 ID" TODO를 이것으로 대체)
- [ ] 팀 쪽 `consent_log` 스키마와 철회 처리 절차 (§4.3, §4.4)
- [ ] Tier 0 축적용 주간 export 크론 (§1.3 (b))
- [ ] 마스킹 전/후 대조 리뷰 스크립트 (`experiments/` 컨벤션)
- [ ] `CONSENT.md` — Tier 0 협조자 확정 후 작성
- [ ] (별도 판단) 공개 데이터 재배포 시 `data/`·`experiments/results/` 일괄 비식별화 (§6.3)
- [ ] (별도 판단) git 히스토리에 남은 VM 계정명 처리 여부 (§2.3)
