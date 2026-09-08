# Self-Healing MLOps Agent

Intel Arc / Iris Xe GPU 환경에서 동작하는 **비용 0원의 자율 장애 복구 에이전트**입니다.  
시스템 로그를 실시간으로 감시하고, Vector DB 기반 L1 캐시와 로컬 LLM L2 추론을 결합해 장애를 자동으로 진단·복구합니다.

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
    ├─ Hit (distance < 0.8) ──────────────────▶ ActionExecutor
    └─ Miss (distance ≥ 0.8) → [L2 Slow Track]
                                    │
                          ┌─────────┴──────────┐
                     Ollama API           ipex_llm (Arc GPU)
                     (CPU/GPU 무관)       (multiprocessing spawn)
                          │                    │
                          └─────────┬──────────┘
                               자가 반성 루프 (Safety Reviewer)
                                    │
                              Rule-based Fallback
                                    │
                              ESCALATE_TO_HUMAN
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

### 훈련 데이터 파이프라인 (ChromaDB 총 **1,016건**, 합성 데이터 0건)

| 소스 (`source` 태그) | ChromaDB 적재 | 수집 방법 |
|------|------|---------|
| `train_set` (`etl_vector_sync`) | 308건 | train_set.json → ChromaDB 동기화 (MD5 dedup) |
| `syslog_augment_v2` (`scripts/load_syslog_train_v2.py`) | 455건 | syslog 기반 증강 데이터 v2 |
| `syslog_augment_v1` (`scripts/load_syslog_train.py`) | 203건 | syslog 기반 증강 데이터 v1 |
| N/A (source 없음) | 50건 | 기타 (출처 태그 미설정) |

**ETL 전략**: Extract(GitHub 공식 이슈) → 에러 스니펫 regex 추출 → 전처리(노이즈 제거·길이 제한·액션 검증) → Load(ChromaDB 직접 upsert)  
합성 데이터 없음 — 모든 항목이 실제 오픈소스 프로젝트 이슈에서 수집된 원본 에러 메시지

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

### 온라인학습 데이터 유입 정책

L1 캐시(ChromaDB)는 큐레이션된 데이터(GitHub 이슈 크롤링, 카오스 인젝터 시그니처 등) 외에 **런타임 실행 결과로부터도 자동으로 학습**합니다(`src/llm_engine.py::learn_from_feedback`, `src/log_watcher.py`에서 호출). L1 미스로 L2(Groq)/Rule 경로가 명령어를 생성해 **실제 실행에 성공**하면, 그 (에러 로그 → 명령어) 쌍을 `source="online_learning"` 태그와 함께 L1에 upsert합니다 — 다음에 같은 에러가 다시 발생하면 L2를 다시 거치지 않고 즉시 재사용합니다.

**안전장치 — 왜 이게 위험하지 않은가:**
- 학습된 엔트리는 실제 분류된 `ErrorCategory`가 아니라 항상 가짜 카테고리(`Learned_from_LLM`)로 저장됩니다(운영 L2 자체가 애초에 진짜 카테고리를 모르고 자유형식 명령어만 생성하기 때문 — 정보 손실이 아니라 원래도 없던 정보). 이 가짜 카테고리는 `autonomy_state`에 등록된 적이 없어 항상 기본값(`approve_then_execute`)으로 남고, **절대 `auto`로 승급되지 않습니다** — 온라인학습으로 생긴 커맨드는 항상 사람 승인을 거칩니다.
- `OBSERVED_ONLY`/`PROPOSED_ONLY`(READ_ONLY/PROPOSE 레벨, 실제로 실행 안 함)는 학습 대상에서 제외됩니다 — 실행해본 적 없는 커맨드를 "성공한 해결책"으로 학습하지 않습니다.
- 사람이 큐레이션한 데이터(`source`가 `chaos_injector_signature`/GitHub 크롤링 등)는 런타임 피드백으로 **절대 건드리지 않습니다** — 아래 품질관리는 `source="online_learning"` 엔트리에만 적용됩니다.

**품질관리 — 반복 실패 엔트리 자동 제거 (2026-09-08)**: L1 히트가 학습된 엔트리에서 왔고(`AgentResponse.l1_source == "online_learning"`) 그 실행이 성공/실패했는지를 `record_learned_outcome()`이 그 엔트리의 `success_count`/`failure_count`에 되먹입니다. 실패 건수가 `LEARNED_ENTRY_MAX_FAILURES`(기본 2) 이상이고 실패율이 `LEARNED_ENTRY_FAILURE_RATE_THRESHOLD`(기본 0.5) 초과일 때만 엔트리를 삭제합니다 — 우연한 실패 1건으로 바로 지우지 않고, 반복적으로 안 통하는 해결책만 걸러내 다음 히트부터 L2가 새 해결책을 다시 시도하게 합니다.

**감사/백필**: `scripts/audit_online_learning_entries.py`로 현재 몇 건이 쌓여 있는지, provenance 태깅이 없는 구버전 엔트리가 있는지 확인·백필할 수 있습니다(기본은 리포트만, `--backfill`로 실제 반영).

---

## 실험 결과 요약 (`experiments/`)

| 실험 | 결과 |
|------|------|
| **Threshold Sweep** (0.1~1.5) | 최적 threshold=**1.2**, action_F1=**0.982**, L1 히트율 **97.7%** |
| **Baseline Compare** | 키워드 매칭 22.1% → RAG **84.9%** (+62.8%p) |
| **Security Audit** (악성 30개) | **30/30 차단** (100%) |
| **Top-K Sweep** (K=1,2,3,5) | **K=1** 최적 (오버헤드 없음) |
| **Debouncer Sweep** | 모든 윈도우에서 **95%+** 중복 방어 |
| **Learning Curve** (50→1,016건) | 데이터 증가에 따른 단조 성능 향상 확인 |

---

## 빠른 시작

### 원클릭 실행 (Makefile)

```bash
make install   # 패키지 설치 + 환경 초기화 (최초 1회)
make start     # Docker 인프라 + 에이전트 한 번에 기동
make stop      # 전체 종료
make status    # 컨테이너 + 에이전트 상태 확인
```

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

### 수동 환경 설정

```bash
# 1. 가상환경 생성 및 패키지 설치
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt

# 2. 환경변수 설정
cp .env.example .env
# .env 파일에서 GITHUB_TOKEN, SLACK_WEBHOOK_URL 등 설정
```

### 데이터 수집 및 학습

```bash
# 1. GitHub 이슈 크롤링 (GITHUB_TOKEN 필요)
python -m src.etl_github_crawler

# 2. Train/Test 분리 (stratified 80/20)
python scripts/split_dataset.py

# 3. ChromaDB 벡터 동기화
python -m src.etl_vector_sync
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

```bash
cp .env.example .env   # 환경변수 설정

# 기본 스택 (에이전트 + 대시보드 + 승인 서버)
docker compose up -d

# Ollama LLM 포함 (L2 추론 활성화)
docker compose --profile llm up -d

# 서비스 포트
# 대시보드:     http://localhost:8501
# 승인 서버:    http://localhost:8000
# Ollama:       http://localhost:11434
```

| 서비스 | 역할 |
|--------|------|
| `agent` | 로그 감시 메인 에이전트 |
| `dashboard` | Streamlit 실시간 대시보드 |
| `approval-server` | Human-in-the-Loop FastAPI 승인 서버 |
| `ollama` | 로컬 LLM 서버 (선택 — `--profile llm`) |

---

## L2 추론 환경 요구사항

| 방식 | 요구사항 | 비고 |
|------|----------|------|
| **Ollama** | Ollama 설치 + `qwen2.5:0.5b` pull | CPU/GPU 무관, 권장 |
| **ipex_llm** | Intel Arc / Iris Xe GPU | spawn 멀티프로세싱으로 VRAM 격리 |
| **Rule-based** | 없음 | LLM 실패 시 키워드 기반 자동 폴백 |

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
- **테스트셋 분리**: `test_set.json`은 ChromaDB에 절대 포함 금지 (교수님 피드백 반영)
