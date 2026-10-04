# Self-Healing MLOps Agent

프로덕션 장애를 실시간으로 감지·진단하고, 복구 방안을 제안하되 **실행은 항상
사람의 승인을 거치는**(Progressive Autonomy로 검증된 카테고리만 예외) 오픈소스·
셀프호스팅 로그 이상 감지·복구 제안 에이전트입니다.  
Vector DB 기반 L1 캐시와 LLM L2 추론을 결합해 원인을 진단하고 복구 명령을 제안합니다.
L2 LLM은 회사 정책에 맞춰 **로컬 모드**(기본값, 서버 안 Ollama — LLM 분석용 데이터 외부
전송 없음)와 **클라우드 모드**(Groq API — 성능 우선, 로그 일부 외부 전송) 중에서 고릅니다 —
[L2 LLM 모드](#l2-llm-모드-로컬--클라우드), [외부로 나가는 데이터](#외부로-나가는-데이터-반드시-확인) 참고.

---

## 아키텍처 개요

```
실시간 로그
    │
    ▼
[LogWatcher] ──── Debouncer (중복 제거) ──── CircuitBreaker (반복 실패 차단)
    │
    ▼
[RAGEngine: L1 Fast Track]
    ChromaDB 벡터 유사도 검색 (< 150ms)
    ├─ Hit (distance < 0.6) ──────────────────▶ ActionExecutor
    └─ Miss (distance ≥ 0.6) → [L2 Slow Track, 5단계 폴백 체인]
                                    │
                          [클라우드 모드만] Groq API (qwen/qwen3.8-27b), 평균 0.65초
                          멀티에이전트 3단계(진단→제안→검토):
                            ① 진단(_diagnose_error) — 원인/조치유형/대상 구조화 추출
                               ├─ 확신 있는 구조화 조치(대상이 PID/포트가 아닌
                               │  restart_service/kill_process/clear_memory)
                               │  → ②③ 건너뛰고 L1과 동일한 구조화 액션으로 직행
                               │  (diagnosis-driven routing, 2026-09-17)
                               └─ 그 외(대상이 PID 등) → 아래 ②③ 그대로 진행
                            ② 제안(_run_groq)       — ①로 보강된 컨텍스트로 명령어 생성
                            ③ 검토(self-reflection) — 원본 컨텍스트만 보고 독립 재검증
                          │
                    로컬 모드(기본값)는 여기서 시작 / 클라우드 모드는 Groq 실패 시 ↓
                          Ollama API (서버 안, CPU/GPU 무관, 단일 프롬프트)
                          실패 시 ↓ (로컬 모드는 어떤 경우에도 Groq로 넘어가지 않음)
                          ipex_llm (Arc GPU, multiprocessing spawn — 설치된 경우만)
                          실패 시 ↓
                          4순위 Rule-based Fallback (키워드 매칭)
                          실패 시 ↓
                          5순위 ESCALATE_TO_HUMAN
    │
    ▼
[ActionExecutor] ── 보안 필터 (shlex + metachar + whitelist/blacklist)
    │                   ├─ CLEAR_MEMORY
    │                   ├─ RESTART_SERVICE (systemctl)
    │                   ├─ EXECUTE_LLM_COMMAND (Human-in-the-Loop)
    │                   └─ ESCALATE_TO_HUMAN → Slack 알림
    ▼
[AgentObserver] → SQLite 메트릭 기록 (SUCCESS / FAILURE / IMPOSSIBLE)
    │
    └─▶ FeedbackLoop: 성공 조치 → ChromaDB L1 캐시 재학습
```

**diagnosis-driven routing** (`_route_from_diagnosis`, 2026-09-17): 이전엔
진단(①) 결과가 명령 생성(②) 프롬프트를 보강하는 텍스트 힌트로만 쓰이고
실제 액션 결정엔 전혀 반영되지 않았다. 이제 진단이 대상까지 확신 있게 뽑은
`restart_service`/`kill_process`/`clear_memory`는 제안·검토를 건너뛰고 L1과
동일한 구조화 액션으로 바로 실행된다. 단, 이 시스템의 kill 대상은 대부분
에러 로그의 PID라서(예: `leaky_worker.py (pid=5821)`) — executor.py의 구조화
실행(`pkill -x`/`systemctl restart`)은 정확한 이름 일치만 지원하고 PID는
지원하지 않으므로, 대상에 숫자가 하나라도 있으면 항상 기존 자유형식
경로(②③, `kill -TERM <pid>` 생성 + self-reflection 검토)로 그대로 폴백한다.

---

## 모듈 구조

```
src/
├── log_watcher.py          # 실시간 로그 감시 (watchdog), 컨텍스트 윈도우, Graceful Shutdown
├── llm_engine.py           # RAGEngine: L1/L2/Rule 체인, spawn 멀티프로세싱 VRAM 격리
├── executor.py             # 보안 필터 + OS 제어 실행기
├── observability.py        # SQLite 메트릭, 성능 리포트 (3분류: SUCCESS/FAILURE/IMPOSSIBLE)
├── circuit_breaker.py      # 상태 머신 (CLOSED→OPEN→HALF_OPEN), SQLite 영속
├── proactive_monitor.py    # CPU/Memory/Disk 임계치 선제 감시 (psutil)
├── maintenance.py          # SQLite 30일 초과 레코드 정리 + VACUUM (24h 주기)
├── error_clusterer.py      # ChromaDB 벡터 KMeans 클러스터링 (sklearn 선택적)
├── etl_scheduler.py        # 24h 주기 자동 ETL 동기화
├── approval_server.py      # Human-in-the-Loop FastAPI 승인 서버 (토큰 기반)
├── approval_store.py       # 승인 토큰 SQLite 저장소
├── slack_bot.py            # Slack ChatOps (승인 요청 Block Kit)
├── system_diagnostics.py  # 에러 컨텍스트 수집 (free/df/ss/uptime)
├── schemas.py              # AgentResponse, ActionType, ErrorCategory (15종 Enum)
├── monitor/
│   ├── vram_profiler.py    # Intel Arc VRAM 사용량 측정
│   └── log_monitor.py      # 로그 파일 모니터링 유틸리티
└── utils/
    ├── debouncer.py        # LRU 기반 중복 에러 쿨다운 (MD5 해시, thread-safe)
    ├── logging_config.py   # JSON 구조화 로깅 설정
    ├── pii_masker.py       # 로그 내 개인정보 마스킹
    ├── profiler.py         # 성능 프로파일링 데코레이터
    └── sqlite_pool.py      # SQLite 커넥션 풀
```

---

## ETL 데이터 파이프라인

```
[GitHub Issues 크롤러]
  10개 카테고리 × 50건 = 총 436건 수집
  (Out_Of_Memory, Network_Timeout, Configuration_Error,
   DB_Connection, Permission_Denied, Disk_Full, Process_Crash,
   Port_Conflict, Auth_Error, Memory_Leak)
       │
       ▼
[etl_ingest.py] → PostgreSQL (ON CONFLICT DO NOTHING)
                → 실패 시 data/etl_backup.json 머지 백업
       │
       ▼
[scripts/split_dataset.py]
  error_category 기준 Stratified split (seed=42)
  → data/train_set.json  (308건, ChromaDB 적재용)
  → data/test_set.json   (407건, 평가 전용 — ChromaDB 절대 미포함)
       │
       ▼
[etl_vector_sync.py]
  log_text MD5 해시 → ChromaDB upsert (중복 방지)
  train_set.json 우선, 없으면 etl_backup.json 폴백
```

### 훈련 데이터 파이프라인 (ChromaDB 총 **1,318건**, 실측 2026-09-18)

> 과거 "1,016건" 표는 syslog 증강 데이터(658건, 전체의 65%)가 실제로는
> 운영 ChromaDB에 적재된 적이 없던 상태에서 작성된 값이었다 — 코드는
> 배포돼 있었지만 스크립트 실행이 운영에 누락됨(2026-09-17 발견·복구,
> [`README.md` 실험 결과 요약](#실험-결과-요약-experiments) 참고). 아래는
> 복구 후 VM에서 직접 쿼리한 실제 수치.

| 소스 (`source` 태그) | ChromaDB 적재 | 수집 방법 |
|------|------|---------|
| `syslog_augment_v2` (`scripts/load_syslog_train_v2.py`) | 637건 | syslog 형식 증강 데이터 v2 (9개 카테고리) |
| `syslog_augment_v1` (`scripts/load_syslog_train.py`) | 300건 | syslog 형식 증강 데이터 v1 (10개 카테고리) |
| N/A (source 없음, `train_set`/`etl_vector_sync`) | 308건 | train_set.json → ChromaDB 동기화 (MD5 dedup) |
| `github_v2` (`scripts/etl_github_to_chroma.py`) | 44건 | GitHub 공식 이슈 2차 수집 |
| `chaos_injector_signature` | 26건 | 카오스 인젝터 실측 문구 큐레이션 |
| `proactive_monitor_signature` | 3건 | ProactiveMonitor 실측 문구 큐레이션 |

**ETL 전략**: Extract(GitHub 공식 이슈) → 에러 스니펫 regex 추출 → 전처리(노이즈 제거·길이 제한·액션 검증) → Load(ChromaDB 직접 upsert)  
`train_set`/`github_v2`/큐레이션 시그니처(총 381건)는 실제 오픈소스 이슈·실측 문구에서 수집된 원본이고, `syslog_augment_v1`/`v2`(937건)는 실제 Linux/Cloud 서비스 로그 형식을 본떠 직접 작성한 데이터다 — 완전한 합성(fabricated) 데이터는 아니지만 스크래핑 원본도 아니므로 이 둘을 구분해서 인용할 것.

**데이터 전처리 파이프라인**:
- 노이즈 필터링: URL·티켓 링크·30자 미만·에러 키워드 없는 텍스트 제거 (-286건)
- 텍스트 길이 제한: 임베딩 모델(all-MiniLM-L6-v2) max 512자로 상한 적용
- 액션 일관성: 카테고리별 올바른 action_type 전수 검증 및 수정 (-155건 오류 수정)
- MD5 해시 중복 제거: 동일 텍스트 upsert 시 자동 덮어쓰기

---

## 보안 아키텍처

`executor.py`의 `_validate_command()`는 4단계 방어를 순서대로 적용합니다.

| 단계 | 검사 | 차단 예시 |
|------|------|-----------|
| 1. shlex 파싱 | 따옴표·이스케이프 올바른 토큰화 | 잘못된 따옴표 구조 |
| 2. 메타문자 전수 검사 | `\|><;&\`$(){}*?!\\~` | `systemctl restart nginx; rm -rf /` |
| 3. BANNED_TOKENS | 인터프리터·파괴적 명령 차단 | `python3`, `bash`, `rm`, `curl` |
| 4. ALLOWED_COMMANDS | 명시적 화이트리스트만 통과 | 목록 외 모든 명령어 |
| 5. 명령별 인자 형태 검사 (2026-10-04) | 첫 인자뿐 아니라 전체 형태를 고정 — `fuser -k <포트>/tcp\|udp`, `kill -TERM\|-HUP <PID≥2>`, `pkill -x\|-f <이름>`, `nginx -s reload`/`nginx -t`, `journalctl --vacuum-size ≥500M`/`--vacuum-time ≥7d` | `fuser -k -m /`, `kill -TERM -1`, `pkill -f -9 nginx`, `nginx -s stop`, `journalctl --vacuum-size 1` |
| 6. 보호 대상 검사 (2026-10-04) | 실행 직전 실제 대상 확인 — `kill`은 `/proc/<PID>/comm`, `pkill -f`는 `pgrep -f`(보호 대상·에이전트 자신 포함 또는 5개 초과 시 거부), `fuser`는 그 포트를 쓰는 프로세스, `systemctl`은 서비스 이름 | `pkill -f sshd`, `kill -TERM <sshd PID>`, `systemctl stop sshd`, `systemctl stop <에이전트 자신>` |

**보호 대상**: `systemd`(·`systemd-*`), `init`, `sshd`/`ssh`, `networking`, `NetworkManager`,
`dbus`, `docker`/`dockerd`/`containerd`, `python*`, 그리고 에이전트 자신의 서비스
(`AGENT_SERVICE_NAME` 또는 `/proc/self/cgroup` 자동 감지). `PROTECTED_PROCESSES`(쉼표 구분)로
추가할 수 있습니다. LLM이 만든 자유형식 명령(`_validate_command`)과 L1 구조화 액션
(`restart_service`/`kill_process`의 대상, `_validate_process_name`)에 똑같이 적용됩니다.
L1 구조화 `restart_service`는 대상이 플레이북에서 오므로 self-reflection(LLM 검토)을
생략하고, LLM이 만든 자유형식 `systemctl` 명령만 LLM 검토를 거칩니다(서비스 허용 목록은
후속 과제 — RESEARCH_SUMMARY §6).

`_validate_process_name()`은 `kill_process` / `restart_service` 액션의 대상 프로세스 이름을 별도로 검증합니다.

| 검사 | 규칙 | 차단 예시 |
|------|------|-----------|
| 프로세스 이름 정규식 | `^[a-zA-Z0-9][a-zA-Z0-9_\-.]` | `pkill -9`의 `-9` (플래그 인젝션), `nginx&&rm` |

**Human-in-the-Loop**: 보안 필터 통과 후 Slack 승인 요청 발송 → `y/n` 대기  
`AUTO_APPROVE=true` 환경변수로 실험/테스트 모드 자동 승인 전환

### Progressive Autonomy 승급 거버넌스

에러 카테고리별로 자동화 신뢰 수준을 4단계(읽기전용→제안→승인후실행→자동)로 관리합니다(`src/autonomy_store.py`). 정식 승급 절차는 카테고리를 "Shadow 검토" 상태로 표시(`scripts/set_autonomy_level.py --shadow <목표레벨>`)한 뒤, 최소 50건 + 최소 2주 경과 + FN/FP 5% 이하(전환 방향에 따라 분리 판정) 기준을 `experiments/run_shadow_gate_report.py`로 확인하고, 사람이 최종적으로 `set_autonomy_level.py`로 승급을 실행합니다 — 코드 어디에도 자동 승급 로직은 없습니다.

**예외 사항 (2026-09-04~05)**: `Out_Of_Memory`/`Disk_Full`/`Process_Crash`/`Permission_Denied`/`Path_Not_Found`/`Configuration_Error` 6개 카테고리는 이 정식 절차를 거치지 않고 `auto`로 직접 승격되었습니다. 이유: 이 카테고리들은 Progressive Autonomy 게이트 도입 이전부터 카오스 인젝터로 수 주간 실측 검증되어 있었고(L1 캐시 히트로 승인 없이 즉시 실행되던 기존 동작), 게이트 도입 직후 전 카테고리를 보수적 기본값(`approve_then_execute`)으로 되돌리면 카오스 테스트가 트리거하는 복구 액션마다 5분 승인 대기 후 타임아웃되어 실행/복구 결과 데이터 수집에 공백이 생길 위험이 있었기 때문입니다. 즉 "기준 미달인데 승급"이 아니라 "이미 별도 경로로 기준을 충족한 상태였다"는 판단이었습니다.

**앞으로의 정책**: 이 6개 이후 새로 추가되는 카테고리는 반드시 정식 Shadow 절차를 거쳐야 하며, 위와 같은 직접 승격은 반복하지 않습니다. 2026-09-07에 Shadow gate 리포트를 실제로 처음 실행해 확인한 결과, 이 6개 외에는 현재 Shadow 검토 대상 카테고리가 없습니다(카오스 인젝터가 없는 다른 카테고리는 애초에 실행 이력이 쌓이지 않음).

**카테고리 커버리지 확장 (2026-09-07)**: `DB_Connection`(`/inject/db_connection` — redis-py로 127.0.0.1:6379에 접속 시도, 리스닝 프로세스가 없어 실제 `redis.exceptions.ConnectionError` 발생)과 `Network_Timeout`(`/inject/network_timeout` — psycopg2로 `192.0.2.1`(RFC 5737 TEST-NET-1, 아무도 응답하지 않는 예약 주소)에 접속 시도, 실제 `psycopg2.OperationalError`로 타임아웃 발생)에 실제 카오스 인젝터를 신규 배포했습니다(`scripts/chaos_cron.sh` 로테이션 7종→9종). 두 카테고리 모두 autonomy 기본값(`approve_then_execute`)을 그대로 유지하며, 위 예외 사항의 6개와 달리 **정식 Shadow 절차를 그대로 따릅니다** — 최소 50건 + 최소 2주 데이터가 쌓이기 전까지 직접 승격하지 않습니다.

**카테고리 커버리지 확장 (2026-09-08)**: MVP 8종 중 마지막까지 실제 카오스 인젝터가 없던 `Auth_Error`(`/inject/auth_error` — 컨테이너 자체 보호 엔드포인트(`/internal/protected`)를 잘못된 Bearer 토큰으로 호출해 실제 `requests.exceptions.HTTPError`(401) 발생)와 `Memory_Leak`(`/inject/memory_leak` — OOM처럼 즉시 대량 할당하는 게 아니라 90MB만 6단계에 걸쳐 서서히 점유하는 백그라운드 프로세스의 실제 RSS를 psutil로 측정, cgroup 512m 한도의 극히 일부만 써서 OOM Killer는 개입 안 함)를 신규 배포했습니다(로테이션 9종→11종). MVP 8종 전 카테고리에 실제 카오스 인젝터가 갖춰졌습니다. 두 카테고리 모두 autonomy 기본값(`approve_then_execute`) 유지, 정식 Shadow 절차 대상.

**카테고리 커버리지 확장 (2026-09-08, 계속)**: MVP 8종 밖의 `DB_Deadlock`도 실제 인젝터 확보 — `/inject/db_deadlock`은 별도 DB 서버 없이 이 프로젝트가 이미 쓰는 SQLite 자체의 락 경합만으로 구현합니다: 한 커넥션이 `BEGIN EXCLUSIVE`로 락을 쥔 채 대기하는 동안 다른 커넥션이 짧은 timeout으로 같은 파일에 쓰기를 시도해 실제 `sqlite3.OperationalError`("database is locked")를 유발(로컬 3회 반복 재현 확인). 로테이션 11종→12종.

### 온라인학습 데이터 유입 정책

L1 캐시(ChromaDB)는 큐레이션된 데이터(GitHub 이슈 크롤링, 카오스 인젝터 시그니처 등) 외에 **런타임 실행 결과로부터도 자동으로 학습**합니다(`src/llm_engine.py::learn_from_feedback`, `src/log_watcher.py`에서 호출). L1 미스로 L2(Groq)/Rule 경로가 명령어를 생성해 **실제 실행에 성공**하면, 그 (에러 로그 → 명령어) 쌍을 `source="online_learning"` 태그와 함께 L1에 upsert합니다 — 다음에 같은 에러가 다시 발생하면 L2를 다시 거치지 않고 즉시 재사용합니다.

**안전장치 — 왜 이게 위험하지 않은가:**
- 학습된 엔트리는 실제 분류된 `ErrorCategory`가 아니라 항상 가짜 카테고리(`Learned_from_LLM`)로 저장됩니다(운영 L2 자체가 애초에 진짜 카테고리를 모르고 자유형식 명령어만 생성하기 때문 — 정보 손실이 아니라 원래도 없던 정보). 이 가짜 카테고리는 `autonomy_state`에 등록된 적이 없어 항상 기본값(`approve_then_execute`)으로 남고, **절대 `auto`로 승급되지 않습니다** — 온라인학습으로 생긴 커맨드는 항상 사람 승인을 거칩니다.
- `OBSERVED_ONLY`/`PROPOSED_ONLY`(READ_ONLY/PROPOSE 레벨, 실제로 실행 안 함)는 학습 대상에서 제외됩니다 — 실행해본 적 없는 커맨드를 "성공한 해결책"으로 학습하지 않습니다.
- 사람이 큐레이션한 데이터(`source`가 `chaos_injector_signature`/GitHub 크롤링 등)는 런타임 피드백으로 **절대 건드리지 않습니다** — 아래 품질관리는 `source="online_learning"` 엔트리에만 적용됩니다.

**품질관리 — 반복 실패 엔트리 자동 제거 (2026-09-08)**: L1 히트가 학습된 엔트리에서 왔고(`AgentResponse.l1_source == "online_learning"`) 그 실행이 성공/실패했는지를 `record_learned_outcome()`이 그 엔트리의 `success_count`/`failure_count`에 되먹입니다. 실패 건수가 `LEARNED_ENTRY_MAX_FAILURES`(기본 2) 이상이고 실패율이 `LEARNED_ENTRY_FAILURE_RATE_THRESHOLD`(기본 0.5) 초과일 때만 엔트리를 삭제합니다 — 우연한 실패 1건으로 바로 지우지 않고, 반복적으로 안 통하는 해결책만 걸러내 다음 히트부터 L2가 새 해결책을 다시 시도하게 합니다.

**감사/백필**: `scripts/audit_online_learning_entries.py`로 현재 몇 건이 쌓여 있는지, provenance 태깅이 없는 구버전 엔트리가 있는지 확인·백필할 수 있습니다(기본은 리포트만, `--backfill`로 실제 반영).

---

## SRE 프로세스

이 시스템의 신뢰성 목표(SLO/SLI, 에러 버짓), 실제 인시던트 1건의 postmortem,
사람에게 에스컬레이션됐을 때의 runbook은 [`docs/SRE_PRACTICES.md`](docs/SRE_PRACTICES.md)에
정리돼 있습니다. 인시던트 하나가 감지→판단→조치→기록을 어떻게 통과했는지는
대시보드 "🕵️ 인시던트 상세" 탭에서 시간순으로 확인할 수 있습니다.

---

## 실험 결과 요약 (`experiments/`)

| 실험 | 결과 |
|------|------|
| **Threshold Sweep** (0.1~1.5, 실측 재검증 2026-09-17) | **현재 배포값 0.6**: 카테고리F1 0.921, action_F1 0.637, Precision 99.4%, L1 히트율 82.6%. 1.2(과거 문서상 "최적값")는 action_F1 0.744로 소폭 높지만 아래 False Positive 항목 참고 — 채택하지 않음 |
| **Baseline Compare** | 키워드 매칭 22.1% → RAG **84.9%** (+62.8%p) |
| **Security Audit** (악성 30개) | **30/30 차단** (100%) |
| **Top-K Sweep** (K=1,2,3,5) | **K=1** 최적 (오버헤드 없음) |
| **Debouncer Sweep** | 모든 윈도우에서 **95%+** 중복 방어 |
| **Learning Curve** (50→1,016건) | 데이터 증가에 따른 단조 성능 향상 확인 |
| **L2 정확도** (Groq, 50건) | 카테고리 분류 정확도 **92%**, 액션 정확도 **96%** — 카테고리별 Auth_Error(60%)가 최저 |
| **멀티에이전트+진단-라우팅 효과** (진단→제안→검토 + 진단-주도 구조화 라우팅, 공식 재측정 2026-09-21) | End-to-end 통과율 **10% → 32% → 34%**(파이프라인 통과율 기준, 클라우드 모드(Groq)·2026-09-21 코드 — 현재 지표는 위 "L2 LLM 모드"의 대상 일치율 참고. 멀티에이전트 도입, 진단-라우팅 추가 순서로 단조 개선, `experiments/results/l2_production_path_check_summary.json`). 생성 성공률 100% — 이전 "6~8%→26%"는 이 스크립트가 진단-라우팅의 구조화 액션(RESTART_SERVICE 등)을 "생성 실패"로 잘못 세던 계측 버그로 나온 값이라 폐기, 버그 수정(`run_l2_production_path_check.py`) 후 공식 재측정한 값으로 교체 |
| **Faithfulness** (반사실적 위험요소 조작, 3케이스) | 대상만 바꾼 명령어 쌍(예: 올바른 PID vs 무관한 PID)에서 승인 판정이 **100% 뒤집힘**(verdict flip rate), 판정 근거도 조작된 대상을 실제로 언급(target mention rate 0%→100%) — self-reflection이 근거 없는 고정 문구가 아니라 입력을 실제로 반영해 판단함을 확인 |
| **Bias-Injection** (권위 주장/허위 성공이력/긴급성 압박 3종, 2026-09-17) | 대상은 항상 틀린 값(ground truth=거부)으로 고정하고 편향 문구만 주입 — 네트워크 폴백 오염(15.6%, VM Ollama 미실행) 제외 후 진짜 LLM 판정 76건 전부 대상 불일치를 정확히 지적하며 거부. 조작 효과 **0%p**, 조작 성공률 **0%** — self-reflection이 이 편향 신호들에 흔들리지 않음(단 표본이 작아 일반화엔 신중할 것) |
| **False Positive** (LogHub 10개 무관 시스템 로그 2만 줄, 실측 재검증 2026-09-17) | 1차 탐지 게이트(정규식) 오탐률 **10.51%**(2,102/20,000줄) — 그 오탐 줄 전수(2,102건)를 L1(RAG) 분류 게이트에 흘렸을 때, **현재 배포값 0.6에서는 confident FP 0.0%**(1,318건으로 복구된 ChromaDB 기준). **threshold를 1.2로 올리면 confident FP가 79.4%로 폭증**(무관한 로그 5개 중 4개꼴로 실제 조치 시도) — syslog 증강 데이터가 일반적인 시스템 로그 어휘와 겹쳐서 생기는 트레이드오프. 0.6을 유지하는 핵심 근거(`experiments/run_false_positive_analysis.py`) |

> **Threshold/오탐 수치 정정 기록 (2026-09-17)**: 과거 문서의 "threshold=1.2, action_F1=0.982,
> 히트율 97.7%"는 두 가지 문제가 겹친 값이었다 — (1) 운영 ChromaDB에 syslog 증강 데이터
> 658건(전체의 65%)이 실제로는 적재된 적이 없어(코드는 배포됐지만 스크립트 실행이 누락됨)
> 그 상태에서 재측정하면 threshold를 아무리 올려도 action_F1이 0.746을 못 넘었고, (2) 0.982는
> 실제로는 action_F1이 아니라 category_F1(카테고리 F1은 threshold 1.0~1.2에서 0.980까지 나옴)을
> 잘못 표기했을 가능성이 높다. 2026-09-17 운영 VM(Python 3.10.12, chromadb==0.5.0 —
> 로컬 개발 환경의 최신 chromadb/numpy와 달리 실제 배포판과 동일한 조합)에서 누락된 syslog
> 데이터를 복구(381→1,318건)한 뒤 재측정: **threshold=0.6(현재 배포값)에서 category_F1 0.921 /
> action_F1 0.637 / Precision 99.4% / L1 히트율 82.6%**, LogHub 10개 무관 시스템 로그
> 2만 줄 기준 confident FP **0.0%**. threshold=1.2로 올리면 action_F1은 0.744로 소폭
> 개선되지만 같은 LogHub 기준 confident FP가 **79.4%**까지 치솟아 채택하지 않음
> (`experiments/results/threshold_results_20260917_150213.csv`,
> `experiments/results/false_positive_summary_20260917_152609.json`).
>
> **운영 리스크 기록**: Groq 무료 티어 모델은 예고 없이 단종될 수 있음이 두 차례
> 실측으로 확인됨(`llama-3.3-70b-versatile` 2026-08, `qwen/qwen3.6-27b` 2026-09-15 —
> 둘 다 사전 공지 없는 404). 현재 `qwen/qwen3.8-27b` 사용 중이며, 이 상수를 바꿀 땐
> [Groq 모델 목록](https://api.groq.com/openai/v1/models)을 먼저 확인할 것.

---

## 빠른 시작

### 원클릭 실행 (Makefile)

```bash
make install   # venv+패키지, .env, L2 LLM 모드 선택, Docker 인프라, ChromaDB 초기 데이터, systemd 유닛까지 전부 준비 (최초 1회, install.sh 실행)
#                 → 설치 중 "1) 로컬 모드 2) 클라우드 모드"를 묻는다(질문 없이: bash install.sh --mode local|cloud)
#                 → 이 단계 끝나면 .env에 TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID만 채우면 됨
make start     # Docker 인프라 + 에이전트(systemd) 한 번에 기동
make stop      # 전체 종료
make status    # 컨테이너 + 에이전트 상태 확인
```

`install.sh`는 새 서버 한 대를 git clone 직후 상태에서 `make start`만 누르면
되는 상태까지 만든다 — venv 생성(+pip 없는 환경 자동 폴백), `pyproject.toml`
기준 패키지 설치, `.env.example` 복사, `docker compose up -d`(대시보드·승인
서버·target-app), ChromaDB 초기 학습 데이터(`train_set.json` + 카오스 인젝터·
ProactiveMonitor 큐레이션 시그니처) 적재, systemd 유닛 설치(enable만, 시작은
안 함)까지 한 번에 처리한다. docker/systemd가 없는 환경(로컬 dev 등)에서는
해당 단계만 건너뛰고 나머지는 정상 진행된다.

**에이전트 본체(`log_watcher`)는 Docker로 실행하지 않는다** —
`systemctl`/`pkill`/메모리 회수 등 커널 수준 제어가 필요해 호스트 OS
네이티브(venv + systemd)로만 구동한다(`docker-compose.yml` 상단 주석 참고).
Docker는 target-app/dashboard/approval-server 3개 인프라 서비스에만 쓴다.

### 데모 시연

```bash
# 장애 주입 전체 시나리오 (OOM → DB → Disk → Crash → Auth 순서 자동 실행)
make demo

# 또는 개별 장애 선택 주입
python demo/inject_failure.py                        # 인터랙티브 메뉴
python demo/inject_failure.py --type oom             # OOM 단일 주입
python demo/inject_failure.py --type disk_full       # Disk Full 단일 주입
python demo/inject_failure.py --scenario full        # 전체 5개 시나리오 자동 실행

# 에이전트 실시간 로그 확인
make logs
```

지원 장애 유형: `oom` / `memory_leak` / `disk_full` / `process_crash` / `port_conflict` / `auth_error` / `db_timeout` / `network_timeout` / `permission_denied` / `config_error`

### 수동 환경 설정 (`install.sh`가 하는 일을 단계별로 직접 실행하고 싶을 때)

```bash
# 1. 가상환경 생성 및 패키지 설치 (pyproject.toml 기준)
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .                  # 개발 의존성(pytest 등)까지: pip install -e ".[dev]"

# 2. 환경변수 설정
cp .env.example .env
# 최소 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 채우기. 기본은 로컬 모드(LLM_PROVIDER=ollama).
# ⚠️ 클라우드 모드(LLM_PROVIDER=groq + GROQ_API_KEY)를 켜면 L2 경로에서 로그 원문이
#    api.groq.com으로 전송됨 → 아래 "L2 LLM 모드", "외부로 나가는 데이터" 참고
```

### 테스트 실행

CI는 `pytest tests/ -m "not slow"`를 돌린다(`.github/workflows/ci.yml`) — 새 테스트
파일을 추가해도 별도 등록 없이 자동으로 CI 대상이 된다. 느리거나 외부 API/라이브
DB 등 외부 의존이 있는 테스트는 `@pytest.mark.slow`로 표시해달라 — CI는 기본적으로
`slow`가 아닌 테스트만 실행한다. 전체(slow 포함)를 로컬에서 돌리려면:

```bash
pytest tests/              # 전체(slow 포함)
pytest tests/ -m "not slow"  # CI와 동일한 범위
```

**개발 환경 주의 — 테스트는 Python 3.10 환경에서 돌릴 것.** 운영 VM은 Python 3.10 +
`chromadb==0.5.0` + `numpy==1.26.4`(requirements.txt 고정)이다. Python 3.14 같은 최신
인터프리터에는 numpy 1.26.4 휠이 없어 numpy 2.x가 깔리고, chromadb 0.5.0이 import 단계에서
`np.float_` 제거 에러로 깨진다(로컬 통과 ≠ VM 통과). 2026-10-04부터는 VM과 같은 구성의
별도 환경을 만들어 쓴다(Intel 전용 인덱스가 필요한 ipex-llm/torch는 제외 — 테스트에 불필요):

```bash
# uv로 Python 3.10 환경 생성 (예: ~/ollama-bench/py310)
uv venv --python 3.10 ~/ollama-bench/py310
grep -v -E 'extra-index-url|^ipex-llm|^torch' requirements.txt > /tmp/req-noipex.txt
uv pip install --python ~/ollama-bench/py310/bin/python -r /tmp/req-noipex.txt pytest
~/ollama-bench/py310/bin/python -m pytest tests/ -m "not slow"
```

### 데이터 수집 및 학습

```bash
# 1. 기본 학습 데이터(train_set.json, 저장소에 포함) → ChromaDB 동기화
python -m src.etl_vector_sync

# 2. 카오스 인젝터/ProactiveMonitor가 실제로 남기는 문구 큐레이션 적재
#    (train-serving skew 방지 — 문구 어휘가 학습 데이터와 달라 L1이 미스나는 문제를
#     사전에 막는다, 2026-09-07/09-10 실측으로 확립된 패턴)
python -m scripts.add_chaos_injector_signatures
python -m scripts.add_proactive_monitor_signatures

# (선택) 희소 카테고리 보강용 GitHub 이슈 크롤링 — GITHUB_TOKEN 없어도 동작(레이트리밋만 낮음)
python -m src.etl_github_crawler
```

### 에이전트 수동 실행

```bash
# 로그 감시 에이전트 시작
python -m src.log_watcher data/realtime_system.log

# 별도 터미널: 대시보드
streamlit run dashboard/app.py

# 별도 터미널: Human-in-the-Loop 승인 서버
uvicorn src.approval_server:app --host 0.0.0.0 --port 8000
```

### 실험 실행

```bash
# 환경변수 설정 (자동 승인 — 실험용)
export AUTO_APPROVE=true

python experiments/run_threshold_sweep.py   # 임계값 sweep + ROC curve
python experiments/run_baseline_compare.py  # 키워드 vs RAG 비교
python experiments/run_security_audit.py    # 보안 차단율 측정
python experiments/run_top_k_sweep.py       # Top-K 다수결 비교
python experiments/run_debouncer_sweep.py   # Debouncer 타임윈도우 튜닝
python experiments/run_dataset_scale.py     # Learning curve
```

---

## Docker 배포

`docker compose`는 인프라 3개(대시보드/승인 서버/target-app)만 다룬다 — 에이전트
본체는 위 "원클릭 실행" 절 설명대로 항상 호스트 네이티브(systemd)로 별도 구동한다.

```bash
cp .env.example .env   # 환경변수 설정

# 인프라 기동 (target-app + 대시보드 + 승인 서버
#  + 로컬 모드면 Ollama — .env의 COMPOSE_PROFILES=llm이 --profile llm과 같은 효과)
docker compose up -d
docker compose exec ollama ollama pull qwen2.5:0.5b   # 로컬 모드, 최초 1회

# 에이전트 본체는 별도로 (호스트 네이티브)
sudo systemctl start self-healing-agent   # install.sh로 유닛을 미리 설치해뒀다면

# 서비스 포트
# 대시보드:     http://localhost:8501
# 승인 서버:    http://localhost:8000
# target-app:   http://localhost:9000  (카오스 엔지니어링 대상 워크로드)
# Ollama:       http://localhost:11434
```

| 서비스 | 실행 방식 | 역할 |
|--------|----------|------|
| `self-healing-agent` | systemd (호스트 네이티브) | 로그 감시 메인 에이전트 — `systemctl`/`pkill` 등 커널 제어 필요 |
| `dashboard` | Docker | Streamlit 실시간 대시보드 |
| `approval-server` | Docker | Human-in-the-Loop FastAPI 승인 서버 |
| `target-app` | Docker | 카오스 엔지니어링 대상 워크로드(장애 주입용) |
| `ollama` | Docker (`--profile llm`, 로컬 모드는 `.env`의 `COMPOSE_PROFILES=llm`로 자동) | 로컬 모드의 L2 LLM 서버 / 클라우드 모드에선 Groq 실패 시 폴백 |

---

## L2 LLM 모드 (로컬 / 클라우드)

L1 캐시에 없는 새 에러는 L2에서 LLM이 분석합니다. 회사 정책에 맞춰 둘 중 하나를
고릅니다(`.env`의 `LLM_PROVIDER`). 문서와 설치 화면에서는 "로컬 모드 / 클라우드 모드"로
부릅니다.

| | 로컬 모드 (기본값, 보안 우선) | 클라우드 모드 (성능 우선) |
|---|---|---|
| 설정 | `LLM_PROVIDER=ollama` (미설정·잘못된 값도 여기로) | `LLM_PROVIDER=groq` + `GROQ_API_KEY` |
| L2 LLM | 서버 안 Ollama (`qwen2.5:0.5b`) | Groq API (`qwen/qwen3.8-27b`), 실패 시 서버 안 Ollama |
| LLM 분석용 데이터의 외부 전송 | **없음** | **있음** — [외부로 나가는 데이터](#외부로-나가는-데이터-반드시-확인) |
| 멀티에이전트 진단 단계 | 없음 (단일 프롬프트) | 있음 (진단→제안→검토) |
| L2 대상 일치율 (아래 "L2 성능") | 3.3% (`qwen2.5:0.5b`) | 측정 중 |
| `LLM_Inferred` 카테고리 auto 승급 | **허용되지 않음** (코드에서 차단) | 사람이 직접 승급할 때만 가능 |
| 적합한 곳 | 금융·규제 산업 등 로그가 밖으로 나가면 안 되는 곳 | 외부 API 사용에 제약이 없는 곳 |

**L2 성능 (L1 캐시에 없는 신규 에러 50건, 2026-10-04 측정)**

| 모드 | L2 대상 일치율¹ (파이프라인 통과율²) | 평균 지연 |
|---|---|---|
| 클라우드 모드 (Groq `qwen/qwen3.8-27b`) | 측정 중 | 측정 중⁵ |
| 로컬 모드 (`qwen2.5:0.5b`, 기본값) | **3.3%** (16.0%) — 3회 평균, 범위 2~4% | 약 1.3초 |
| 로컬 모드 (`qwen2.5:3b`) | **1.3%** (54.7%)³ — 3회 평균, 범위 0~2% | 약 3.6초 |

두 로컬 모델 모두 L2 실효성이 낮아, 자원을 덜 쓰는 `qwen2.5:0.5b`를 기본으로 둡니다. 로컬
모드의 자동 복구는 사실상 L1 캐시와 Rule에 의존하고, L1에 없는 새 에러는 사람 승인으로
처리된다고 보는 것이 정확합니다.

1. 대상 일치 = 에러 로그가 지목한 서비스·포트·프로세스를 대상으로 한 조치. 실제로 해결되는지는
   검증하지 않았습니다. 판정 1인, 기준과 판정 내역은
   `experiments/results/l2_pass_classification_20261004.json`에 공개(판단이 갈린 건은 "논란" 표시).
2. 파이프라인 통과율 = 생성된 조치가 화이트리스트·self-reflection을 통과해 사람 승인만 받으면
   실행됐을 비율(`experiments/run_l2_production_path_check.py`). 에러와 무관한 명령도 통과로 셉니다.
3. 3b 통과분의 대부분은 프롬프트 예시 명령(`df -h`) 복사입니다(41.3%p).
4. 2026-10-04 화이트리스트 강화(존재하지 않는 PID 거부 등)·프롬프트 예시 축소·진단-라우팅
   검토 추가로 이전 수치(10%→32%→34%, 파이프라인 통과율 기준)와 직접 비교할 수 없습니다.
5. 클라우드 모드 지연은 진단-라우팅 검토 단계 추가(2026-10-04)로 이전(평균 1.4초)보다 늘어납니다.
6. 측정 환경: 개발 PC(WSL)에서 측정 — L2 프롬프트의 시스템 컨텍스트에 측정 PC의 프로세스
   목록이 들어갑니다(LLM이 그 목록에서 대상을 고른 사례는 RESEARCH_SUMMARY §6 B10).

**동작 규칙**

- **로컬 모드는 어떤 경우에도 Groq를 호출하지 않습니다** — `GROQ_API_KEY`가 `.env`에 남아
  있어도, Ollama가 죽어도 마찬가지입니다. 키는 있는데 `LLM_PROVIDER`가 비어 있으면 시작 로그에
  경고를 남기고 로컬 모드로 동작합니다(클라우드 모드를 자동으로 켜지 않음). 잘못된 값(오타 등)도
  에러 로그를 남기고 로컬 모드로 동작합니다.
- **로컬 모드의 폴백 순서**: Ollama → ipex_llm(설치된 서버만, 서버 안에서 도는 로컬 LLM) →
  Rule 기반 처리 → 사람 승인(에스컬레이션). L1 캐시 히트는 이와 무관하게 먼저 처리됩니다.
  Ollama에 연결할 수 없으면 `[로컬 모드] Ollama 연결 불가` 경고 로그를 남기고 이 순서대로
  넘어갑니다 — 어느 단계에서도 외부 LLM으로 넘어가지 않습니다.
- **클라우드 모드에서 Groq가 실패하면** 서버 안 Ollama → (ipex_llm) → Rule → 사람 승인 순으로
  넘어갑니다. 외부에서 로컬로 가는 방향이라 추가 전송은 없습니다.
- **로컬 모드에서는 `LLM_Inferred`(L2 자유형식 명령) 카테고리를 auto로 승급할 수 없습니다** —
  `scripts/set_autonomy_level.py`가 거부하고, DB에 auto가 남아 있어도(클라우드 모드 때 승급,
  `DEFAULT_AUTONOMY_LEVEL=auto` 등) 실행 시점에 `approve_then_execute`로 낮춰 처리합니다.
  로컬 모델 제안은 항상 사람 승인을 거칩니다.
- **안전 장치**: 화이트리스트 밖의 위험 명령(`kill -9`, `sudo`, 전체 컨테이너 중지 등)은 실행 전에
  차단됩니다(2026-10-04 측정에서 전부 차단 확인). 화이트리스트를 통과한 명령도 기본 자율성
  레벨에서는 사람 승인을 거칩니다. 로컬 모드에서는 `LLM_Inferred` 카테고리를 auto로 승급하는
  것이 허용되지 않습니다. auto로 승급된 카테고리라도 self-reflection이 위험 판정(NO)을 내리거나
  검토 자체가 실패하면(레이트리밋 등) 자동 실행하지 않고 사람 승인으로 내립니다.
- 현재 모드는 시작 로그 한 줄(`[LLM] L2 mode=local (ollama/qwen2.5:0.5b) — L2 분석 데이터 외부
  전송 없음`)과 대시보드 사이드바 배지로 확인합니다.

**모드 고르기 / 바꾸기**

```bash
# 설치할 때 — 대화형으로 묻거나, 질문 없이 지정
bash install.sh                  # "1) 로컬 모드(보안 우선) 2) 클라우드 모드(성능 우선)"
bash install.sh --mode local
GROQ_API_KEY=gsk_... bash install.sh --mode cloud   # 키는 환경변수 또는 화면 비표시 입력

# 설치 후 로컬 → 클라우드
#   .env: LLM_PROVIDER=groq, GROQ_API_KEY=<키>, (Ollama 컨테이너가 필요 없으면) COMPOSE_PROFILES 줄 주석 처리
sudo systemctl restart self-healing-agent

# 설치 후 클라우드 → 로컬
#   .env: LLM_PROVIDER=ollama, COMPOSE_PROFILES=llm
docker compose up -d
docker compose exec ollama ollama pull qwen2.5:0.5b
sudo systemctl restart self-healing-agent

# 확인 — 시작 로그의 모드 한 줄
sudo journalctl -u self-healing-agent -n 200 | grep "L2 mode="
```

`LLM_PROVIDER`는 프로세스 시작 시 한 번 읽으므로 `.env`를 고친 뒤 반드시 재시작해야 합니다.

**로컬 모드 모델별 최소 사양** (CPU 추론, Ollama 실측 로드 크기 기준)

| 모델 | 로드 시 메모리(실측) | 최소 여유 RAM |
|------|---------------------|--------------|
| `qwen2.5:0.5b` (기본값) | 484MB | 1GB 이상 |
| `qwen2.5:3b` | 2.2GB | 3GB 이상 |

**L2 폴백 체인 요구사항**

| 단계 | 요구사항 | 비고 |
|------|----------|------|
| **Groq** | 클라우드 모드 + `GROQ_API_KEY` | 진단→제안→검토 멀티에이전트 3단계, 평균 응답 0.65초 |
| **Ollama** | Ollama(컨테이너) + 모델 pull | 로컬 모드의 L2 / 클라우드 모드의 Groq 실패 시 폴백 |
| **ipex_llm** | Intel Arc / Iris Xe GPU + `ipex-llm` 설치 | 설치된 경우만 시도, spawn 멀티프로세싱으로 VRAM 격리 |
| **Rule-based** | 없음 | LLM 전부 실패 시 키워드 기반 자동 폴백 |

### 외부로 나가는 데이터 (반드시 확인)

에이전트 본체와 L1 캐시(ChromaDB)·메트릭 DB는 전부 설치한 서버 안에서 돕니다.
**LLM 분석용 데이터 기준으로, 로컬 모드(기본값)에서는 서버 밖으로 나가는 데이터가 없습니다.**
알림 채널(Telegram/Slack)은 이와 별개로, 설정하면 알림 내용이 해당 서비스로 전송됩니다.

| 설정 | 전송 대상 | 전송 내용 |
|------|----------|----------|
| 클라우드 모드 (`LLM_PROVIDER=groq` + `GROQ_API_KEY`) | `api.groq.com` | L2 경로: 에러 로그 원문 + 전후 최대 10줄 컨텍스트(`src/log_watcher.py` `_build_context_window`) + 진단 명령 출력(`free`/`df`/`ps`/`ss`, `src/system_diagnostics.py` — `ps` 출력엔 서버의 다른 프로세스 이름도 포함됨). L1 히트 중 KILL_PROCESS와 자유형식 명령(Rule/온라인학습 엔트리)의 self-reflection 검토도 Groq를 호출함(L1 구조화 RESTART_SERVICE는 검토 생략) |
| `TELEGRAM_BOT_TOKEN` / Slack 설정 | Telegram / Slack | 알림 메시지에 포함된 로그 앞부분 |

클라우드 모드의 전송 전 마스킹(토큰·비밀번호·IP 등)은 아직 적용되지 않습니다(후속 과제).

---

## Git 브랜치 전략

| 브랜치 | 용도 |
|--------|------|
| `main` | 최종 발표용 완성본 (직접 push 금지) |
| `dev` | 개발 통합 브랜치 |
| `feature/*` | 개인 기능 개발 브랜치 |

```bash
# 일반 작업 흐름
git checkout dev && git pull --rebase origin dev
git checkout -b feature/기능명
# ... 코딩 ...
git add src/파일.py
git commit -m "feat: 기능 설명"
git push origin feature/기능명
# GitHub에서 dev로 PR 생성
```

---

## 핵심 설계 원칙 (`claude.md`)

- **VRAM 격리**: L2 ipex_llm 추론은 반드시 `multiprocessing(spawn)` + `os._exit(0)` 패턴 사용
- **예외 비침묵**: `except: pass` 절대 금지 — 모든 예외는 traceback 포함 로깅
- **보안 우선**: shlex 파싱 + 메타문자 차단 + 화이트리스트/블랙리스트 3중 방어
- **멱등성**: ChromaDB 적재 시 MD5 해시 ID + upsert → 중복 방지
- **테스트셋 분리**: `test_set.json`은 ChromaDB에 절대 포함 금지 (실무자 피드백 반영)
