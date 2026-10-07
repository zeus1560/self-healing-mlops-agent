# 인계 메모 (2026-10-07 기준, 2차 갱신)

L2 LLM 모드 전환(§6 B2)과 운영 검증 세션(2026-10-04~07)의 인계 메모다. 새 세션은 이 파일을 먼저 읽고
아래 "처음 읽을 파일"을 확인한 뒤 "다음 할 일"부터 이어간다.

VM 접속 정보(GCP 프로젝트·인스턴스·SSH 사용자·저장소 경로)는 이 저장소에 두지 않는다 — Claude
로컬 메모(`project_gcp_deployment.md`, `project_llm_mode_rollout.md`)에 있다. 시각은 따로 적지 않으면 UTC.

---

## 0. 10/08 실제 상태 (2026-10-07 17:5x UTC = KST 10/08 02:5x 확인)

| 항목 | 상태 | 근거 |
|---|---|---|
| 배포 `f34d3da8`(승인 URL 토큰 로그 가림) | **미완료** | VM HEAD `4505c043`(reflog 마지막 pull 10/06 16:26), 에이전트 10/06 16:26부터 계속 실행 |
| 배포 `5486d6f7`(`set_decision` 만료 시각 확인) | **미완료** | 위와 같음 |
| 배포 `3edc2c83`(승인 근거 대체·텔레그램 토큰 URL 미표시) | **미완료** | 위와 같음 |
| N3(journal 24시간 증가량 재계산) | **미완료** | 측정 기록 없음 — 아래 §2 "N3 측정"에 이번에 기록 |
| rsyslog 1시간·24시간 효과 확인 | **미완료** | 10/06에 걸어 둔 백그라운드 확인이 세션 종료로 실행되지 않음(결과 파일 없음) — §2 판정 기준으로 이번에 측정 |

10/06 마지막 보고에서 "rsyslog 1시간 재확인만 남았다"고 했지만 그건 그날 작업 범위 기준이었고, 실제로는
배포 3건과 N3도 미완료 상태로 남아 있었다(배포는 10/07로 미룬 상태, N3는 시점 미도래).

참고: 로컬 저장소는 2026-10-07에 `revision` 브랜치(로컬 전용, `revision/` 디렉터리만 변경)로 체크아웃돼
있다. 운영 문서는 main에 커밋한다(`git worktree`로 main을 별도 폴더에 열어 작업).

---

## 1. 현재 상태

### 커밋

| 위치 | 커밋 | 내용 |
|---|---|---|
| origin/main | 이 HANDOFF 커밋(`3edc2c83` 바로 뒤) | — |
| **VM** | **`4505c043`** (2026-10-06 16:26 배포) | 경보 사유 대체(`538bf9b3`)·가림 규칙 보강(`69d92109`)까지 반영 |
| **다음 배포 대상(코드)** | `f34d3da8` → `5486d6f7` → `3edc2c83` | 아래 §3 |

이번 세션(10/07) 커밋(오래된 순): `538bf9b3` 넘기기 경보 사유 대체 → `ef62ade0` §6 B14·B15 문서 →
`69d92109` pii_masker 보강(봇 토큰·gsk_·Bearer·JWT·URL 계정) → `4505c043` B16 문서(직전 커밋 메시지의
"B16"은 스크립트 실패로 실제론 빠져 있었고 이 커밋이 그 문서) → `f34d3da8` 승인 URL 토큰 로그 가림 + B12
재계산·B18 → `5486d6f7` `set_decision` 만료 시각 확인 + B17 → `3edc2c83` 승인 근거 대체 + 텔레그램 토큰 URL 미표시.

### VM 설정 (누적)

- `.env`: `LLM_PROVIDER=groq`(클라우드 모드), `AUTO_APPROVE=false`, `CHAOS_ENABLED=true`,
  `SLACK_WEBHOOK_URL` 미설정, `APPROVAL_BASE_URL=http://<공인IP>:8000`.
- 자율성 레벨: auto 6개(Configuration_Error, Disk_Full, Out_Of_Memory, Path_Not_Found, Permission_Denied,
  Process_Crash). 그 외(DB_Connection 포함)는 approve_then_execute.
- 승인 테이블: approved 49 / expired 20 / rejected 2(10/06 주입 검증 2건 포함).
- journald `SystemMaxUse=500M`. ops-agent 라벨 제거 + 3개 서비스 mask(되돌리기는 §6 B13).
- **rsyslog(N2, 10/06 16:27)**: `/etc/rsyslog.d/90-google.conf` 6번 줄 `daemon,kern.* /dev/console`
  주석 처리(이유는 파일 안 주석). 백업 `/var/backups/90-google.conf.bak-20261007`. 되돌리기: 백업 복원 후
  `systemctl restart rsyslog`. 1시간 재확인 결과는 §2.
- **8000 포트 외부 차단(10/06 약 16:50)**: 네트워크 태그 `approval-server` 제거(방화벽 규칙
  `allow-approval-server`는 남음, 대상 0대). 되돌리기:
  `gcloud compute instances add-tags self-healing-agent --zone us-central1-c --tags=approval-server`.
- 저장소 소유권: `.git`·작업 트리 일부가 root 소유 → **git은 root로**(B16).
- Ollama 없음, Groq 키는 측정 PC와 같은 키(B11).

### 백업 파일 (VM)

| 파일 | 내용 |
|---|---|
| `<VM 저장소>/data/agent_metrics.db.bak-20261005` | 승인 19건 정리 전 DB |
| `/var/lib/logrotate/status.bak-20261005` | syslog 강제 회전 전 logrotate 상태 |
| `/var/log/self-healing-agent-journal-20261005.txt` | 에이전트 journal 2026-10-03 이후 505줄(봇 토큰 0건, 승인 토큰 URL 여부는 미확인 — B18) |
| `/var/backups/90-google.conf.bak-20261007` | rsyslog 수정 전 `90-google.conf`(root 644) |

---

## 2. 2026-10-06 (UTC) 작업 결과

### 배포
- **N1**: `39ba2b6f` → `4505c043`(root로 `pull --ff-only`, B16). 텔레그램 상태 줄 정상, 재시작 후 Traceback 0·
  ERROR 0·`api.telegram.org/bot` 0줄. (첫 시도는 사전 확인 ".git 안 root 소유 0개"가 어긋나 merge 전 중단 →
  지난 배포도 root로 pull했음을 reflog로 확인 후 root로 진행.)
- **N2**: rsyslog 수정 → `rsyslogd -N1` 통과 → 재시작 90초 동안 `suspended` 0회(수정 전 시간당 약 1,100회),
  `/var/log/syslog` 기록 계속. 1시간·24시간 효과는 아래 기준으로 판정.

#### rsyslog 수정 효과 판정 기준 (결과를 보기 전에 작성 — 2026-10-07 17:5x UTC)

기준 시점: rsyslog 재시작 **2026-10-06 16:27:10 UTC**(N2). 구간은 모두 이 시점 기준의 고정 구간이라
journal이 남아 있는 동안(약 8일) 언제 재도 같은 값이 나온다.

- **무엇을 셀지**
  - 지표 A(주 지표): `journalctl -u rsyslog` 중 `suspended`가 들어간 줄 수 — `/dev/console` omfile 동작
    중단·재개 메시지.
  - 지표 B(유발 이벤트): 같은 구간의 journal 중 syslog facility daemon(3)·kern(0) 메시지 수
    (`journalctl SYSLOG_FACILITY=3 + SYSLOG_FACILITY=0`) — 수정 전이었다면 `daemon,kern.* /dev/console`
    규칙으로 콘솔에 가려 했을 메시지.
  - 보조 C: 구간 journal 전체 줄 수와 그중 rsyslog 유닛 비율, `/var/log/syslog`가 구간 안에 계속 기록됐는지.
- **구간**: 1시간 — 사후 10/06 16:27:10~17:27:10, 사전 15:27:10~16:27:10. 24시간(R24) — 사후 10/06 16:27:10~
  10/07 16:27:10, 사전 10/05 16:27:10~10/06 16:27:10. 사전 값은 같은 명령으로 같은 길이의 구간에서 다시 잰다.
- **이미 측정한 비교값(참고, 정의가 조금 다름)**: 1시간 **1,092회** — 10/06 N2 사전 확인
  (`journalctl -u rsyslog --since '-1h' | grep -c suspended`, 16:2x 실행이라 구간 끝이 재시작 시각과 정확히
  같지 않음). 시간당 약 1,100회·하루 약 5만 줄 — §6 B13(10/05~06 확인). daemon 메시지 하루 약 1.6만 건,
  24시간 journal 줄 수 중 rsyslog 71% — 10/06 하루 뒤 확인(§2 이전 판, `project_llm_mode_rollout.md`).
  지표 B의 사전 값은 측정한 적 없음 — 이번에 함께 잰다.
- **판정**
  - **효과 있음**: 사후 A = 0 **그리고** 사후 B ≥ 1 **그리고** `/var/log/syslog` 기록 계속.
  - **효과 없음**: 사후 B ≥ 1이고 사후 A가 사전 A의 50% 이상.
  - **판정 보류**: 사후 B = 0(**유발 이벤트 없음**) / 0 < 사후 A < 사전 A의 50%(부분 감소 — 남은 원인 조사) /
    사전 A = 0이라 비교 불가 / 구간 일부가 순환 삭제됐거나 데이터가 불완전.

**rsyslog 결과**: 미측정(2026-10-07 17:5x UTC 기준). 10/06에 걸어 둔 백그라운드 확인은 세션 종료로 실행되지
않았다.

### 장애 주입 검증(`db_connection` 2회) — §6 B2
- **1회차(16:32, 무응답)**: L1_CACHE → 승인 요청 `restart_service(redis)` 수신 → 300초 뒤 행 **expired**
  (`system:timeout`), metrics `IMPOSSIBLE/ApprovalTimeout`, 실패 경보 수신. 이어서 **만료 후 승인 클릭 → "이미
  처리되었거나 만료되었습니다", 행 expired 유지, 조치 없음** — 만료 후 승인 거부를 운영에서 확인.
- **2회차(16:46, 사용자 승인)**: 약 55초 뒤 승인 → 실행기가 **`docker restart mlops_target_app`** 실행 →
  Running 확인 → metrics `SUCCESS`(64.3초). 승인→실행→기록 경로 확인. 단 승인 화면 명령과 실제 동작이 다름(B17).
  수동 `docker restart`는 RestartCount를 0으로 초기화(재시작 확인은 StartedAt 기준).
- 승인 대기 로그에 웹 승인 URL이 토큰째 남는 것 발견 → B18(심각도 중간) → 로그 가림 코드 + 8000 포트 차단.

### 재계산·점검
- **B12**: 늦은 승인(DB approved, 실제 미실행) 36건 반영 시 9월 이후 시간 안 응답 9/51 → **타임아웃 약 82%**.
  낮 4/29·밤 5/22 — 야간 영향 없음, 운영자 1명이 6시간마다 오는 경보에 5분 안에 반응하기 어려운 것이 주원인
  (표본 적음).
- **B18 노출 면**: 8000만 외부 개방이었음(지금 차단). 대시보드 8501·target-app 9000은 외부 규칙 없음. 승인
  컨테이너 웹 접근 기록 0줄.

---

## 3. 다음 배포 (2026-10-07 예정 — 단계마다 명령·예상 결과를 보여 주고 확인 후 실행)

| 순서 | 내용 | 예상 결과 |
|---|---|---|
| D1 | VM `4505c043` → origin/main 최신(root로 `git pull --ff-only`, 사전 확인: HEAD `4505c043`·추적 파일 변경 0) → `self-healing-agent` 재시작 | 텔레그램 상태 줄 정상, Traceback·ERROR 0, `api.telegram.org/bot` 0줄 |
| D1 반영 코드 | `f34d3da8` 승인 URL 토큰 로그 가림 / `5486d6f7` `set_decision` 만료 시각 확인 / `3edc2c83` 승인 근거 대체·텔레그램 토큰 URL 미표시 | 다음 승인 요청부터 journal에 `/pending/<앞 6자>…(가림)` |
| N3 | N2(10/06 16:27) 24시간 뒤 journal 증가량 재계산 | 하루 약 20MB, 500M 기준 약 25일 예상 |
| R24 | rsyslog 수정 24시간 효과: `suspended` 건수, journal 중 rsyslog 비율(수정 전 71%) | `suspended` 0, rsyslog 비율 대폭 감소 |

---

## 4. 남은 작업

1. **다음 배포 D1 → N3 → R24**(§3).
2. **운영 미검증 경로**(모두 테스트로는 고정): 진단-라우팅 검토(`e6352294`), 검토 실패 fail-closed
   (`d0e3cc9c`, auto + 검토 NO·실패 → 사람 승인), **비정상 종료 후 만료 승인 거부**(`5486d6f7`, 배포 전).
   일부러 일으키지 않기로 함 — 실제로 발생하면 기록.
3. **Groq 클라우드 모드 재측정** — 다른 계정(조직)의 측정 전용 키가 준비되면. 키는 측정 PC의
   `~/.config/groq-measure.env`(600)에 두고 `set -a; . file; set +a`로만 로드. 남은 TPD 확인, 로컬 Ollama 끈
   상태, `--interval 9 --max-429 5`, 3회 → 분류 → README·RESEARCH_SUMMARY §3.4 클라우드 칸.
4. §6 백로그 중 결정 대기: B15(`ESCALATED` 분리), B17(승인 전 실제 동작 확정), B12 대응(대기 시간·늦은 승인
   재확인 후 실행), B18 후속(0.0.0.0 → 127.0.0.1 바인딩, 보존 파일 토큰 확인), B16(소유권 정리).

---

## 5. §6 백로그 (B1~B18) — 상세는 `docs/RESEARCH_SUMMARY.md` §6

| 항목 | 한 줄 요약 |
|---|---|
| B1 | prod VM DB의 PII·민감값 스캔 — 미실행 |
| B2 | Groq 제3자 전송 — 로컬/클라우드 모드로 코드 대응·VM 배포 완료, 마스킹(B4)만 남음. 운영 차단 사례·주입 검증 기록 |
| B3 | `decided_by` PII·승인 테이블 무기한 보관 + 만료 요청이 pending으로 남던 문제(코드 수정·19건 정리 완료) |
| B4 | 클라우드 모드 전송 전 마스킹(다음 우선순위) — ps 프로세스 이름, ss 내부 IP·포트, df 마운트 경로 |
| B5 | 로컬 프롬프트 예시 → placeholder(생성·검토 프롬프트 모두) — 예시를 줄이면 베끼는 대상만 바뀜 |
| B6 | 로컬 모드 L2 기본 끄기 검토 — 대상 일치율 0.5b 3.3%·3b 1.3%, 관련성 검증 사실상 없음 |
| B7 | systemctl 서비스 허용 목록(서버별) — 지금은 보호 목록만 |
| B8 | "too many open files" 실제 조치 설계 — `ulimit`은 subprocess로 효과 없음 |
| B9 | 사람 승인 없이 L1에 쌓일 수 있는 경로(온라인학습 + auto/AUTO_APPROVE) — 참고 |
| B10 | LLM이 에러 로그 대신 호스트 ps에서 대상 선택(llama-server, MainThread 사례) |
| B11 | 측정·운영 Groq 키 분리, 무료 tier 일일 한도(200k)는 운영 혼자서도 소진 가능 |
| B12 | 승인 타임아웃 — 늦은 승인 반영 시 9월 이후 **약 82%**, 야간 영향 없음. 대응: 대기 시간 조정 / 늦은 승인 시 재확인 후 실행 |
| B13 | VM 로그 소음 — ops-agent(비활성화), rsyslog `/dev/console`(N2로 수정, 24시간 효과 확인 대기) |
| B14 | 넘기기 경보 사유 공란(83건 중 64건) — 대체 사유 코드(`538bf9b3`, 배포됨), 플레이북 근거 채우기 남음 |
| B15 | 넘기기가 IMPOSSIBLE로 기록돼 통계 왜곡 — `ESCALATED` 분리 검토 |
| B16 | VM 저장소 소유권 혼재 — git은 root로, `data/` 처리와 함께 별도 정리 |
| B17 | 승인 화면 `restart_service(redis)` ≠ 실제 `docker restart mlops_target_app` — 승인 전 실제 동작 확정·표시 |
| B18 | 승인 URL 토큰 로그 노출(중간) — 로그 가림·만료 확인·근거 대체 코드(배포 대기), 8000 외부 차단 완료, 127.0.0.1 바인딩 검토 |

---

## 6. 작업 원칙 (이 세션에서 합의)

- **VM 상태 변경**(pull, 재시작, `.env`, DB, 서비스, GCP 리소스)은 실행 전에 명령과 예상 결과를 보여
  주고 사용자 확인을 받는다. 읽기 전용 확인은 바로 진행해도 된다.
- **멈춤 조건**: 예상과 다른 결과가 나오면 다음 단계로 넘어가지 않고 멈춘 뒤 보고한다. 롤백도 확인 후
  (사용자가 "예상과 다르면 되돌리고 멈춤"을 미리 허락한 단계는 예외).
- **키·토큰 미출력**: 값 대신 개수·설정 여부·해시 앞자리만. 승인 토큰·`decided_by`도 가려서 보고. 측정
  로그는 커밋하지 않는다(`.gitignore`의 `logs/`).
- **측정 결과는 덮어쓰지 않는다**: 무효·이전 결과는 별도 폴더로 옮겨 보존한다.
- **순환 중인 로그의 보존은 확인과 같은 시점에 바로 실행**한다.
- 테스트는 VM과 같은 Python 3.10 + chromadb 0.5.0 환경에서 돌린다(로컬: `~/ollama-bench/py310`). VM에서
  테스트할 때는 운영 폴더 밖 `/tmp`에 `git archive`로 풀고, root가 아닌 사용자로, `.env` 없이 실행한다.
- VM 저장소 git 명령은 **root로** 실행한다(B16).
- VM 명령은 `gcloud compute ssh ... --command 'sudo bash -s' < script.sh` 패턴. **주의**: 스크립트가 stdin으로
  가므로 stdin을 읽는 명령이 섞이면 뒷부분이 깨질 수 있다(10/06 "파일 없음" 오탐) — 결과가 이상하면 재확인.
- 로컬 작업 경로: `/root/agent`는 D: 드라이브 저장소를 가리키는 심링크 모음이라 git이 "삭제"로 보인다 —
  git·편집은 `/mnt/d/Projects/self-healing-mlops-agent`에서. egg-info 변경은 커밋하지 않는다.
- 셸에서 여러 단계를 이을 때 `set -e`로 중간 실패 시 멈추게 한다(`69d92109` 메시지 불일치 교훈).

---

## 7. 측정·분류 기준과 결과 파일

- 측정 스크립트: `experiments/run_l2_production_path_check.py` — 신규 에러 50건
  (`experiments/run_l2_accuracy.py`의 `NOVEL_ERRORS`), 운영과 같은 L2 경로. `LLM_PROVIDER`로 백엔드 선택,
  실제 HTTP 목적지별 요청 수·429·폴백·검토 실패를 결과 JSON에 기록, `--run N`, `--interval`, `--max-429`.
- 지표: **대상 일치율**(주 지표) = 통과분을 대상 일치 / 조회 / 에러와 무관 / 예시 복사로 분류했을 때
  "에러 로그가 지목한 대상을 겨냥한 조치"의 비율(실제 해결 여부는 미검증). 파이프라인 통과율은 괄호로.
  판정 1인, 기준·판정 내역·논란 표시: `experiments/results/l2_pass_classification_20261004.json`.
- 결과 파일(`experiments/results/`):

| 경로 | 내용 |
|---|---|
| `l2_production_path_check_*_ollama-qwen2.5-{0.5b,3b}_run{1,2,3}.*` | 로컬 모드 확정 수치(`6e2104ed`): 0.5b 3.3%(16.0%), 3b 1.3%(54.7%) |
| `pre_hardening_20261004/` | 화이트리스트 강화·프롬프트 축소 이전 로컬 측정(직접 비교 불가) |
| `groq_ollama_mixed_e6352294/` | 무효 — 측정 PC Ollama 폴백 혼입(참고값 대상 일치 26.0%) |
| `groq_tpd_exhausted_d0e3cc9c/` | 무효 — Groq 일일 토큰 한도 소진(조직 ID 가림) |
| `l2_production_path_check_summary.json` | 이전 공식 Groq 수치(파이프라인 통과율 34%, 2026-09-21 코드) |

---

## 8. 다음 할 일 (순서)

1. **D1 배포**(§3) — 사전 확인·명령·예상 결과를 보여 주고 확인 후 실행.
2. **N3**: 2026-10-07 16:30 UTC(KST 10/08 01:30) 이후 journal 24시간 증가량 재계산.
3. **R24**: rsyslog 수정 24시간 효과(`suspended` 건수, journal 중 rsyslog 비율).
4. 이후 §4의 결정 대기 항목을 사용자와 정리. 측정 전용 키가 준비되면 Groq 재측정.

---

## 9. 새 세션이 처음 읽을 파일

1. `docs/HANDOFF.md` (이 파일)
2. `docs/RESEARCH_SUMMARY.md` §6 (B1~B18, 특히 B2 주입 검증·B12·B17·B18), §3.4 (모드별 대상 일치율)
3. `README.md` — "L2 LLM 모드", "외부로 나가는 데이터", "보안 아키텍처", "테스트 실행"
4. Claude 로컬 메모 `project_llm_mode_rollout.md`, `project_gcp_deployment.md` (VM 접속·상태)
5. 코드: `src/executor.py`(`_escalation_reason`, `_compose_explanation`, `_await_approval`, `_restart_service`),
   `src/approval_store.py`(`set_decision`, `_is_past`, `mark_expired`), `src/telegram_bot.py`
   (`send_approval_request`, `_handle_callback`), `src/utils/pii_masker.py`, `src/llm_engine.py`, `src/log_watcher.py`
