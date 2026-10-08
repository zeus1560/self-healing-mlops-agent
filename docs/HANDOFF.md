# 인계 메모 (2026-10-07 기준, 2차 갱신)

L2 LLM 모드 전환(§6 B2)과 운영 검증 세션(2026-10-04~07)의 인계 메모다. 새 세션은 이 파일을 먼저 읽고
아래 "처음 읽을 파일"을 확인한 뒤 "다음 할 일"부터 이어간다.

VM 접속 정보(GCP 프로젝트·인스턴스·SSH 사용자·저장소 경로)는 이 저장소에 두지 않는다 — Claude
로컬 메모(`project_gcp_deployment.md`, `project_llm_mode_rollout.md`)에 있다. 시각은 따로 적지 않으면 UTC.

---

## 0. 10/08 실제 상태 (최종 갱신 2026-10-08 06:55 UTC = KST 15:55)

| 항목 | 상태 | 근거 |
|---|---|---|
| 배포 `f34d3da8`(승인 URL 토큰 로그 가림) | **완료·운영 확인** | D1(10/07 18:10:30, VM HEAD `6e58a0d7`) · 통제 주입에서 가림 줄 1·원문 토큰 URL 0 |
| 배포 `5486d6f7`(`set_decision` 만료 시각 확인) | **완료** · 새 경로(비정상 종료 뒤 pending 행)는 운영 미검증 | VM 격리 테스트 10/10 · 통제 주입의 만료 후 클릭 거부는 기존 경로(status=expired)로 확인 |
| 배포 `3edc2c83`(승인 근거 대체·텔레그램 토큰 URL 미표시) | **완료** · 본문 URL 0(입력값 기준) | 통제 주입 rowid 73의 본문 입력값 3개에서 `http`·`/pending/` 0 — 사용자 육안 확인은 못 함 |
| N3(journal 24시간 증가량) | **완료(기록만, 미적용)** | §2 "N3 측정" — 사후 24h 약 25~45MB, 보관 약 11~20일 |
| rsyslog 1시간·24시간 효과 | **완료 — 효과 있음(닫음)** | §2 "rsyslog 결과" — 사후 suspended 0 |
| 완화책 A(B19: Process_Crash auto → 승인) | **적용** · 컷오프 2026-10-08 06:36:16.970 UTC | `autonomy_state` 재조회 approve_then_execute, `shadow_events` id 7 demoted |
| 통제 주입(10/08 D1 검증) | **완료 — 예상대로** | 아래 "통제 주입(10/08 D1 검증)" |

**D1 배포(2026-10-07 18:10:30 UTC)** — §0 표는 2026-10-08 06:55 UTC 기준으로 다시 썼다(배포 전 판은 git 이력 `8d40d9b5` 이전).
- 사전 확인: HEAD `4505c043`, 추적 파일 변경 0, pending 0 → root로 `git pull --ff-only` → HEAD **`6e58a0d7`** →
  `systemctl restart self-healing-agent` → 120초 뒤 active·NRestarts 0, 텔레그램 상태 줄 1, Traceback 0·ERROR 0·
  `api.telegram.org/bot` 0·토큰 패턴 0 → 롤백 조건 해당 없음. 원본: scratchpad `d1_deploy_20261007.txt`.
- 배포 전 8000 포트 확인(읽기 전용): tcp:8000 허용 규칙은 `allow-approval-server`(대상 태그 `approval-server`)
  하나뿐이고 인스턴스 태그는 비어 있음, 로컬 PC에서 외부 `:8000/health` 응답 없음(같은 시각 `:22`는 열림 —
  경로 정상), VM은 여전히 `0.0.0.0:8000`·`[::]:8000` 리스닝(방화벽에만 의존). → **외부 차단 확인.**
  **`mlops_approval` 컨테이너는 옛 코드**(코드가 이미지에 포함, `data/`만 마운트) — 웹 경로 `set_decision`
  만료 확인 없음. **재빌드는 후속(B18과 같이 처리).**
- 만료 확인 VM 테스트: `git archive 6e58a0d7` → `/tmp`, zeus3826(uid 1001), `.env` 없음·환경변수 HOME/PATH만,
  VM Python 3.10.12 → `tests/test_approval_expiry.py` **10 passed**(신규 `TestSetDecisionChecksExpiry` 4개 포함).
  `/tmp` 폴더 삭제. 원본: scratchpad `d1_expiry_test_vm_20261007.txt`.
- 남은 확인: 통제 주입 1회(토큰 가림·만료 후 승인 거부의 운영 동작) — 사용자 확인 후 실행.

**통제 주입(10/08 D1 검증) — 2026-10-08 06:45:00 UTC (KST 15:45)** · **통계 제외 대상**
- 식별: `pending_approvals` **rowid 73**(토큰 앞 6자 `Yzezm1`, `restart_service(redis)`), `metrics` **id 1889**
  (`L1_CACHE/DB_Connection/RESTART_SERVICE/IMPOSSIBLE/ApprovalTimeout`), 서킷브레이커 서명 **`677b153c`**.
  → 승인 무응답 통계(B12)와 IMPOSSIBLE 집계(B15)에서 이 행들을 뺄 것. 10/06 1·2회차 주입(승인 행 16:32·16:46 생성분,
  metrics 16:37·16:47 행)도 같은 성격의 통제 주입이다.
- 방식: 스크립트 전문을 먼저 `~/ops-records/20261008/d1verify_20261008T0645.sh`(sha `a1cf05528e83`)에 저장 → VM
  `/root/ops-records/`에 같은 파일 업로드 → `setsid nohup`으로 실행(원격 PID·PGID 635745, `/run/d1verify.pid`),
  출력은 VM `/root/ops-records/d1verify-20261008T0645.log`. 종료 후 pidfile 삭제·프로세스 0 확인.
- 사전 확인(06:44:00): HEAD `6e58a0d7`, active, NRestarts 0, pending 0, 서킷브레이커 CLOSED 아닌 행 0, Process_Crash
  approve_then_execute, DB_Connection 기본값(approve_then_execute), target-app StartedAt 기준값 06:00:03 → 통과.
- 결과: 06:45:05 승인 요청 생성(텔레그램 수신은 사용자 확인) → 06:50:05 **expired**(`system:timeout`) → 실패 경보 발송
  로그 1. 확인 4항목: **가림 줄 1**(`/pending/Yzezm1…(가림)`), **원문 토큰 URL 0**(DB `reason`에도 `/pending/` 0),
  **5분 뒤 expired**, **만료 뒤 승인 클릭 거부**(사용자 화면 "이미 처리되었거나 만료되었습니다"·버튼 제거, rowid 73
  status·decided_at·decided_by 불변, metrics 1889 뒤 0행, target-app StartedAt 06:00:03 불변). 클릭 처리는 journal에
  기록을 남기지 않는다(06:50:40 이후 0줄 — 콜백 경로에 로그 없음). 서킷브레이커 `677b153c`: T+40초 행 없음
  (결과가 나와야 기록) → 타임아웃 후 CLOSED·1.
- **승인 요청 본문의 URL 여부**: 사용자 육안 확인은 못 함. 대신 (1) `6e58a0d7`의 `send_approval_request` 코드 —
  본문은 `감지된 에러`(error_log 앞 300자)·`실행 예정 명령어`·`설명`(explanation 앞 800자, 비면 "(근거 없음)")이고
  `reason`(토큰 URL)은 본문에 쓰지 않으며, 버튼은 URL이 아닌 `callback_data`(`approve|<토큰>`)다. (2) 발송 로그에는
  본문이 남지 않으므로 rowid 73에 저장된 같은 입력값(`error_log`·`command`·`reason`=explanation)에서 `http`·`/pending/`
  개수를 셈 → 셋 다 0. 즉 "코드와 입력값 기준 URL 없음"이고 실제 수신 화면은 확인되지 않았다.
- 원본: `~/ops-records/20261008/`(`d1verify_*`, `post_click_check.txt`), VM `/root/ops-records/d1verify-20261008T0645.log`.

**2026-10-08 06:36 UTC — 완화책 A 적용(B19): Process_Crash auto → approve_then_execute**
- 적용 전 확인: 9/4 auto는 **수동** 승급(zeus3826 shell 기록에 `sudo .venv/bin/python -m scripts.set_autonomy_level
  <카테고리> auto --note "9/4 게이트 배포, 기존 카오스 검증 이력 근거로 유지"` 6건, `shadow_events` promoted 6건
  08:45:22~51). 자동 승급 코드 경로 없음(`set_level`/`start_shadow` 호출은 `scripts/set_autonomy_level.py`뿐,
  `src/autonomy_store.py` 설계 주석 "승급/강등은 코드에서 자동으로 일어나지 않는다") → 다시 auto로 올라가지 않음.
- 명령(VM, root, 저장소 폴더): `.venv/bin/python -m scripts.set_autonomy_level Process_Crash approve_then_execute
  --note "B19: 플레이북 대상 rsyslog 불일치, 10/08"` → `'Process_Crash' → approve_then_execute 로 변경 완료 (변경자: root)`.
- 결과: `autonomy_state` Process_Crash = `approve_then_execute`(updated_at `2026-10-08T06:36:16.970424+00:00`,
  root), `shadow_events` id 7 `auto → approve_then_execute demoted`. 다른 5개 카테고리는 auto 유지. 레벨은 이벤트마다
  DB에서 읽어(`get_level`) 서비스 재시작 없이 즉시 반영.
- **컷오프: 2026-10-08 06:36:16.970 UTC** — 이후 Process_Crash는 승인 요청(`restart_service(rsyslog)`)으로 가고
  응답이 없으면 IMPOSSIBLE/ApprovalTimeout으로 기록된다. 이전 SUCCESS와 직접 비교하지 말 것.
- 되돌리기: 같은 명령을 `auto`로. 원본: `~/ops-records/20261008/mitigation_A_apply.txt`, `autonomy_precheck_*.txt`.

**2026-10-08 05:55~06:05 UTC — 통제 주입 준비 중 확인·취소 기록**
- **18:00 자연 요청의 원문 토큰 URL 1줄**: 10/07 18:00 카오스(network_timeout)가 승인 요청(`pending_approvals`
  rowid 72, `restart_service(postgres_pool)`)을 만들었고 18:05에 expired(`system:timeout`, metrics id 1886
  `ApprovalTimeout`). D1(18:10:30) 전이라 옛 코드로 돌아 **승인 대기 로그에 웹 승인 URL이 토큰째 1줄** 남았다
  (10/07 18:00~18:10:30 구간 원문 토큰 URL 1줄). 해당 토큰은 만료. **journal은 지우지 않고 보존.** D1 이후
  10/08 06:01까지 원문 토큰 URL 0줄, 가림 줄 0줄(배포 뒤 자연 승인 요청이 아직 0건).
- **06:15 통제 주입 취소 경위**: T=06:15:00 UTC로 정해 사전 확인(06:14)·주입·확인을 한 스크립트로 백그라운드
  실행했으나, 사전 확인에 "주입 뒤 OPEN이 되고 12:00 전에 자동 복귀하지 않으면 중단" 조건이 없어 사용자 지시로
  06:00:37에 로컬 작업을 중단. **로컬 중단 뒤에도 VM 쪽 원격 스크립트가 고아 프로세스로 남아**(`sudo bash -s`
  →`bash -s`→`sleep 1008`, 부모 init) 그대로 두면 06:15에 주입할 상태였다 → 그 3개 프로세스만 종료, 종료 확인,
  05:59 이후 주입 0건 확인. 주입은 일어나지 않았다. (사전 확인이 돌았더라도 06:00 카오스로 target-app StartedAt이
  바뀌어 StartedAt 조건에서 멈췄을 것.)
- **서킷브레이커 확인(`src/circuit_breaker.py`, VM DB)**: 서명 = 에러 로그 첫 줄 앞 100자(소문자·공백 정리)의 MD5.
  같은 서명에서 성공 1회면 CLOSED·카운터 0. 실패 3회째 OPEN → 그 서명의 파이프라인 전체(RAG·조치·승인)를 건너뜀,
  `opened_at` 30분 뒤 **다음 같은 서명 이벤트에서** HALF_OPEN(시험 1회, 성공 CLOSED / 실패 OPEN). 카오스 증거 줄은
  마이크로초 타임스탬프로 시작해 **주입마다 서명이 새로 생긴다** — VM 188행, 연속 실패 최대 2, CLOSED 아닌 행 0
  (06:01 기준). **정정**: 10/06에 "다음 db_connection 주입이 타임아웃되면 2/3"이라고 했던 설명은 틀렸다 — 1회차
  줄(`71a8e16b`, CLOSED·1)은 그 줄에만 해당하고, 2회차는 별도 서명(`22a10dab`)이었다(→ B20).
- **06:00 rsyslog 자동 재시작**: 10/08 06:00 카오스 process_crash(target-app, pid 1) → L1_CACHE(거리 0.1352, 후보
  3/3 `restart_service`) → Process_Crash는 auto라 승인 없이 **`systemctl restart rsyslog`** 실행 → SUCCESS(metrics
  id 1888). target-app은 컨테이너 재시작 정책으로 06:00:03 자체 복구(RestartCount 1). rsyslog는 정상(active,
  `suspended` 0, 10/06 수정 유지, syslog 기록 계속). 보관 journal(10/04~)에서 첫 사례. 원인 분석은 B19.
- 원본 출력: scratchpad `pre_injection_check_20261008T0555.txt`, `cb_check_20261008T0556.txt`,
  `post_0600_chaos_check_20261008T0601.txt`(사본 `~/ops-records/20261008/`). 주의: 10/07 이전 scratchpad 원본
  (rsyslog·N3·D1·VM 테스트 출력, 미커밋 문서 patch)은 `/tmp` 정리로 **사라졌다** — 수치는 이 문서에 기록돼 있고
  patch 내용은 `57623862`에 그대로 들어가 있다.

**갱신(2026-10-07 18:0x UTC)**: rsyslog 1h·24h 측정 완료 — 둘 다 "효과 있음". N3 측정·기록 완료(설정 변경 없음).
상세는 §2 "rsyslog 결과"·"N3 측정". 배포 3건은 여전히 미완료(D1 계획 대기).

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

**rsyslog 결과 (측정 2026-10-07 17:55 UTC, 위 기준 그대로 적용)** — 읽기 전용, journal 보관 시작 10/04 08:57이라
모든 구간이 보관 범위 안. 집계는 journal 항목 단위(`-o json` 한 줄 = 한 항목). 원본 출력은 로컬 scratchpad
`measure_rsyslog_n3_20261007T18xx.txt`(저장소에 넣지 않음).

| 구간 | A suspended | B daemon / kern / 계 | C journal 항목 | C rsyslog 유닛 | C syslog 줄 (최대 간격) |
|---|---|---|---|---|---|
| 1h 사전 10/06 15:27:10~16:27:10 | 1,101 | 919 / 0 / 919 | 3,270 | 2,105 (64.4%) | 3,027 (15초) |
| 1h 사후 10/06 16:27:10~17:27:10 | **0** | 1,108 / 13 / 1,121 | 1,590 | 5 (0.3%) | 1,131 (15초) |
| 24h 사전 10/05 16:27:10~10/06 16:27:10 | 26,245 | 16,216 / 0 / 16,216 | 70,929 | 50,204 (70.8%) | 66,461 (16초) |
| 24h 사후 10/06 16:27:10~10/07 16:27:10 | **0** | 16,390 / 116 / 16,506 | 19,652 | 5 (0.0%) | 16,552 (16초) |

- **판정: 1시간 — 효과 있음, 24시간 — 효과 있음.** 사후 A = 0, 사후 B ≥ 1(유발 이벤트 1,121건·16,506건 —
  수정 전이었다면 콘솔로 가려 했을 daemon/kern 메시지가 실제로 발생), `/var/log/syslog` 기록 계속(최대 간격
  15~16초).
- 사후 rsyslog 유닛 5건은 재시작 메시지. journal 항목은 24시간 기준 70,929 → 19,652(약 72% 감소).
- 설명되지 않은 관찰(판정에는 영향 없음 — B는 daemon만으로도 ≥ 1): 사전 구간에는 kern(0) 항목이 0건, 사후에는
  13건·116건. 원인 미확인.

**N3 측정 (2026-10-07 18:0x UTC) — 기록만, 설정 변경 없음** — 원본: scratchpad `measure_n3_retry_20261007T18xx.txt`,
`measure_n3_entryweight_20261007T18xx.txt`(첫 시도는 헤더 시각 파싱 실패로 0MB가 나와 무효).
- 방법: `/var/log/journal`의 파일 9개(합계 535.3MB, `--disk-usage` 510.5M)마다 헤더의 Head/Tail realtime 시각과
  파일 크기를 읽어, 구간과 겹치는 시간 비율로 크기를 나눠 더함(크기 비례). 기록 중인 system 파일(50.3MB, 10/06
  09:56~)은 수정 전 6.5시간을 포함해 시간 비례가 사후를 과대평가하므로, 그 파일만 구간별 `-o export` 바이트
  비율(사전 20.0MB : 사후 24h 21.7MB : 이후 1.5MB → 사후 50.2%)로 나눈 보정값을 함께 기록.
- **원래 수치**: 사전 24시간 **57.8MB**(크기 비례). 사후 24시간 **45.4MB**(크기 비례, 상한 쪽 — user 파일 8MB 선할당 포함) /
  **약 25.3MB**(보정: system 파일 export 바이트 비율, user 파일은 항목 132건뿐이라 제외).
- **보관 일수(SystemMaxUse=500M 기준, 계산만)**: 사전 약 8.7일 → 사후 약 11일(45.4MB) ~ 약 20일(25.3MB).
  예상치(하루 약 20MB, 약 25일)보다는 짧은 쪽. **적용 여부: 미적용**(설정 변경 없음).

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

1. **운영 미검증 경로**(모두 테스트로는 고정): 진단-라우팅 검토(`e6352294`), 검토 실패 fail-closed(`d0e3cc9c`, auto +
   검토 NO·실패 → 사람 승인), **비정상 종료 후 만료 승인 거부**(`5486d6f7` 새 경로 — 배포·VM 테스트는 했지만 운영에서
   그 상황이 일어난 적 없음), 웹 승인 경로의 만료 확인(`mlops_approval` 컨테이너는 옛 코드, 8000 외부 차단 상태).
   일부러 일으키지 않기로 함 — 실제로 발생하면 기록.
2. **Groq 클라우드 모드 재측정** — 다른 계정(조직)의 측정 전용 키가 준비되면. 키는 측정 PC의
   `~/.config/groq-measure.env`(600)에 두고 `set -a; . file; set +a`로만 로드. 남은 TPD 확인, 로컬 Ollama 끈
   상태, `--interval 9 --max-429 5`, 3회 → 분류 → README·RESEARCH_SUMMARY §3.4 클라우드 칸.
3. **B19 근본 대응**: 플레이북 대상 수정(`scripts/add_chaos_injector_signatures.py` `ACTION_MAP`과 ChromaDB 문서 —
   Process_Crash→rsyslog, DB_Connection→redis, Network_Timeout→postgres_pool) 또는 auto 실행 전 대상 일치 확인(완화책 C).
   Disk_Full(호스트 journal vacuum)·OOM(에이전트 내부 gc)도 대상 불일치 — 이번엔 바꾸지 않음. 완화책 A는 근본 대응 뒤 재검토.
4. **B20**: 서킷브레이커 서명 정규화(타임스탬프·PID·숫자 제거) — 지금은 반복 실패 차단이 사실상 동작하지 않음.
5. §6 결정 대기: B15(`ESCALATED` 분리 — 통제 주입 행 제외 규칙 포함), B17(승인 전 실제 동작 확정), B12 대응(대기 시간·
   늦은 승인 재확인 후 실행), B18 후속(0.0.0.0 → 127.0.0.1 바인딩, `mlops_approval` 재빌드, 보존 파일 토큰 확인),
   B16(소유권 정리).

---

## 5. §6 백로그 (B1~B20) — 상세는 `docs/RESEARCH_SUMMARY.md` §6

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
| B19 | auto 조치 대상 ≠ 장애 대상 — 원인 `ACTION_MAP` 카테고리 단위 매핑(`5d767518`), Process_Crash 문서 104/133 대상 rsyslog, auto 조치 33건 중 target-app 작용 0건(모두 SUCCESS). 완화책 A 적용(06:36:16), 근본 대응 남음 |
| B20 | 서킷브레이커 서명에 타임스탬프가 들어가 카운터가 쌓이지 않음 — 설계상 반복 실패 차단이 동작하지 않음 |

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
- **시각 지정 원격 작업을 멈출 때는 VM 쪽 프로세스까지 확인해 정리한다** — 로컬 백그라운드 작업을 멈춰도
  `gcloud compute ssh ... 'sudo bash -s'`로 보낸 원격 스크립트는 고아로 남아 계속 돈다(10/08 06:15 주입 취소 때 확인).
  원격 스크립트는 시작 시 PID를 기록하고, 중단 시 그 PID 트리를 종료한 뒤 `ps`로 사라졌는지 확인.
- 측정 원본은 scratchpad와 함께 `~/ops-records/<날짜>/`에도 남긴다(`/tmp` 정리로 원본이 사라진 적 있음).

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

1. 12:00 UTC 등 다음 카오스에서 Process_Crash가 승인 요청으로 가는지(완화책 A) 읽기 전용으로 확인 — 첫 사례 기록.
2. B19 근본 대응 설계(§4-3) → 사용자 결정 → 코드·데이터 수정은 단계마다 확인 후.
3. B20 서명 정규화 설계·테스트.
4. 측정 전용 키가 준비되면 Groq 재측정(§4-2).
5. 운영 미검증 항목(§4-1)은 실제 발생 시 기록. §4-5 결정 대기 항목을 사용자와 정리.

---

## 9. 새 세션이 처음 읽을 파일

1. `docs/HANDOFF.md` (이 파일)
2. `docs/RESEARCH_SUMMARY.md` §6 (B1~B18, 특히 B2 주입 검증·B12·B17·B18), §3.4 (모드별 대상 일치율)
3. `README.md` — "L2 LLM 모드", "외부로 나가는 데이터", "보안 아키텍처", "테스트 실행"
4. Claude 로컬 메모 `project_llm_mode_rollout.md`, `project_gcp_deployment.md` (VM 접속·상태)
5. 코드: `src/executor.py`(`_escalation_reason`, `_compose_explanation`, `_await_approval`, `_restart_service`),
   `src/approval_store.py`(`set_decision`, `_is_past`, `mark_expired`), `src/telegram_bot.py`
   (`send_approval_request`, `_handle_callback`), `src/utils/pii_masker.py`, `src/llm_engine.py`, `src/log_watcher.py`
