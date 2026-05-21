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
    ├── debouncer.py        # LRU 기반 중복 에러 쿨다운 (MD5 해시)
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
  error_category 기준 Stratified 80/20 split (seed=42)
  → data/train_set.json  (350건, ChromaDB 적재용)
  → data/test_set.json   (86건, 평가 전용 — ChromaDB 절대 미포함)
       │
       ▼
[etl_vector_sync.py]
  log_text MD5 해시 → ChromaDB upsert (중복 방지)
  train_set.json 우선, 없으면 etl_backup.json 폴백
```

### 훈련 데이터 파이프라인 (ChromaDB 총 **1,275건**, 합성 데이터 0건)

| 소스 | ChromaDB 적재 | 수집 방법 |
|------|------|---------|
| ETL GitHub Issues + Demo (`etl_vector_sync`) | 436건 | GitHub API 크롤링 + 데모 시나리오 (MD5 dedup 후) |
| `scripts/loghub_pipeline.py --keyword-only` | 226건 | Loghub 공개 연구 데이터셋 (키워드 분류) |
| `scripts/etl_github_to_chroma.py` | 613건 | **공식 레포 30개 쿼리** — pytorch/tensorflow/elasticsearch/celery/gunicorn/sqlalchemy/psycopg2/redis-py/aiohttp/grpc/vault/paramiko/ansible/kubernetes/helm 등 |

**ETL 전략**: Extract(GitHub 공식 이슈) → 에러 스니펫 regex 추출 → 전처리(노이즈 제거·길이 제한·액션 검증) → Load(ChromaDB 직접 upsert)  
합성 데이터 없음 — 모든 항목이 실제 오픈소스 프로젝트 이슈에서 수집된 원본 에러 메시지

**데이터 전처리 파이프라인**:
- 노이즈 필터링: URL·티켓 링크·30자 미만·에러 키워드 없는 텍스트 제거 (-286건)
- 텍스트 길이 제한: 임베딩 모델(all-MiniLM-L6-v2) max 512자로 상한 적용
- 액션 일관성: 카테고리별 올바른 action_type 전수 검증 및 수정 (-155건 오류 수정)
- MD5 해시 중복 제거: 동일 텍스트 upsert 시 자동 덮어쓰기

---

## 보안 아키텍처

`executor.py`의 `_validate_command()`는 3단계 방어를 순서대로 적용합니다.

| 단계 | 검사 | 차단 예시 |
|------|------|-----------|
| 1. shlex 파싱 | 따옴표·이스케이프 올바른 토큰화 | 잘못된 따옴표 구조 |
| 2. 메타문자 전수 검사 | `\|><;&\`$(){}*?!\\~` | `systemctl restart nginx; rm -rf /` |
| 3. BANNED_TOKENS | 인터프리터·파괴적 명령 차단 | `python3`, `bash`, `rm`, `curl` |
| 4. ALLOWED_COMMANDS | 명시적 화이트리스트만 통과 | 목록 외 모든 명령어 |

**Human-in-the-Loop**: 보안 필터 통과 후 Slack 승인 요청 발송 → `y/n` 대기  
`AUTO_APPROVE=true` 환경변수로 실험/테스트 모드 자동 승인 전환

---

## 실험 결과 요약 (`experiments/`)

| 실험 | 결과 |
|------|------|
| **Threshold Sweep** (0.1~1.5) | 최적 threshold=**1.2**, action_F1=**0.982**, L1 히트율 **97.7%** |
| **Baseline Compare** | 키워드 매칭 22.1% → RAG **84.9%** (+62.8%p) |
| **Security Audit** (악성 30개) | **30/30 차단** (100%) |
| **Top-K Sweep** (K=1,2,3,5) | **K=1** 최적 (오버헤드 없음) |
| **Debouncer Sweep** | 모든 윈도우에서 **95%+** 중복 방어 |
| **Learning Curve** (50→350건) | 데이터 증가에 따른 단조 성능 향상 확인 |

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
