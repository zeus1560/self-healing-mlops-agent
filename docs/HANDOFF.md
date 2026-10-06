# 인계 메모 (2026-10-07 기준)

L2 LLM 모드 전환(§6 B2) 작업 세션(2026-10-04~07)의 인계 메모다. 새 세션은 이 파일을 먼저 읽고
아래 "처음 읽을 파일"을 확인한 뒤 "다음 할 일"부터 이어간다.

VM 접속 정보(GCP 프로젝트·인스턴스·SSH 사용자·저장소 경로)는 이 저장소에 두지 않는다 — Claude
로컬 메모(`project_gcp_deployment.md`, `project_llm_mode_rollout.md`)에 있다.

---

## 1. 현재 상태

### 커밋

| 위치 | 커밋 | 내용 |
|---|---|---|
| origin/main | 문서 커밋(`538bf9b3` 바로 뒤) | §6 B14·B15, 운영 차단 사례·미검증 경로 |
| origin/main 코드 기준 | `538bf9b3` | 넘기기 경보 사유 공란을 카테고리·에러 로그 첫 줄로 채움(B14) |
| **VM** | **`39ba2b6f`** | origin/main보다 뒤 — 코드 차이는 `afae588f`(텔레그램 상태 로그)·`538bf9b3`(경보 사유) |

이번 세션 커밋(오래된 순): `2a5f98f3` 화이트리스트 강화 → `6e2104ed` LLM_PROVIDER(로컬/클라우드
모드) → `e6352294` 진단-라우팅 검토 + auto·검토 NO→승인 → `d0e3cc9c` 검토 실패 fail-closed →
`7e7d74de` Ollama 미연결 시 사전 로딩 건너뜀 + 측정 `--max-429` → `4e44d5ae` 문서·측정 결과 →
`71ce08cd` 로깅 `force=True`·httpx 봇 토큰 URL 차단 → `34f2955f` 승인 타임아웃 시 expired 표시 →
`4d299777`·`39ba2b6f`·`28eecd7f`·`d2363c6b` §6 문서 → `afae588f` 텔레그램 상태 로그.

### VM 설정 (2026-10-05 배포)

- `.env`: `LLM_PROVIDER=groq` 추가(클라우드 모드). 실행 중 프로세스 환경에서 확인됨.
- 자율성 레벨: auto 카테고리 6개(Configuration_Error, Disk_Full, Out_Of_Memory, Path_Not_Found,
  Permission_Denied, Process_Crash). `LLM_Inferred`·`Rule_Inferred`는 기본값(approve_then_execute).
- 승인: 오래된 pending 19건을 expired로 정리(`decided_by=system:cleanup-20261005`). 현재 approved 48 /
  expired 19 / rejected 2.
- journald: `/etc/systemd/journald.conf.d/size.conf`에 `SystemMaxUse=500M`.
- ops-agent: 인스턴스 라벨 `goog-ops-agent-policy` 제거(OS 정책 할당은 남겨 둠, 대상 0대) +
  `google-cloud-ops-agent`, `-opentelemetry-collector`, `-fluent-bit` 3개 `mask`. 패키지는 남김.
  되돌리기: 라벨 재부착 + `unmask` 3개 + `enable --now google-cloud-ops-agent`.
- syslog: syslog만 강제 회전(실제 `/etc/logrotate.d/rsyslog` 옵션 그대로).
- **rsyslog `/dev/console` 문제는 아직 수정 전**(아래 N2).
- Ollama 없음, Groq 키는 측정 PC와 **같은 키(같은 조직)** — 측정이 운영 한도를 함께 쓴다(B11).

### 백업 파일 (VM)

| 파일 | 내용 |
|---|---|
| `<VM 저장소>/data/agent_metrics.db.bak-20261005` | 승인 19건 정리 전 DB(root, 600, 단일 파일, 무결성 ok) |
| `/var/lib/logrotate/status.bak-20261005` | syslog 강제 회전 전 logrotate 상태 |
| `/var/log/self-healing-agent-journal-20261005.txt` | 에이전트 journal 2026-10-03 00:00 이후 505줄(root, 600, 토큰 패턴 0건) |

---

## 2. 하루 뒤 확인 결과 (2026-10-06 15:55 UTC, 재시작 후 34시간)

- **장애 6건 정상 처리**: 카오스 크론(6시간마다)이 cpu 2, db_deadlock 2, permission_denied 1,
  memory_leak 1을 주입. L1 5건은 플레이북 조치가 원래 `ESCALATE_TO_HUMAN`이라 텔레그램 경보만
  (결과 IMPOSSIBLE). 일별 처리 건수는 배포 전과 같은 수준(하루 3~5건).
- **L2 `kill -9` 두 겹 차단**: Groq 진단이 PID를 대상으로 골라 구조화 라우팅 안 함 → 생성 단계가
  `kill -9 128130` → **검토(Groq) NO**("not a soft signal") → **화이트리스트가 `-9` 차단**. 실행 없음. §6 B2에
  "운영 중 실제 차단 사례"로 기록.
- **승인 요청 0건**(배포 전 7일은 redis 재시작 6건) — 실행형 조치가 나오는 장애가 이번 기간에 안 들어옴.
- 429·검토 실패·진단-라우팅 0건. 로깅 정상(INFO 기록, 토큰 노출 0).
- **경보 사유 공란**: L1 `ESCALATE_TO_HUMAN` 이벤트 83건 중 **64건**이 reasoning 빈 문자열 → 경보의
  "사유:"가 비어 있음(기존 문제, 플레이북 데이터에 근거가 없음).
- **journal**: 디스크 기준 하루 약 **62MB**, 500M 기준 **약 8일** 보관. 24시간 줄 수의 **71%가 rsyslog**,
  k3s 20%, 에이전트 0.2%.
- ops-agent masked 유지, osconfig는 패키지·서비스를 건드리지 않음. 디스크 75%, `/var/log` 1.4G로 유지.

---

## 3. 다음 배포 계획 (2026-10-07 사용자 결정: N1·N2 진행 — 단계마다 실행 전 확인 필수)

| 단계 | 내용 | 예상 결과 |
|---|---|---|
| N1 | VM을 `39ba2b6f` → origin/main 최신(`538bf9b3` 포함)으로 pull(`--ff-only`) 후 `self-healing-agent` 재시작 | journal에 `[Telegram] 상태: 활성 \| 봇 토큰 설정: 예 \| 승인 알림 대상(chat) 설정: 예 \| polling: 예`(값 미노출). 이후 넘기기 경보의 "실패 상세"가 비지 않음 |
| N2 | rsyslog 수정안 1: `/etc/rsyslog.d/90-google.conf` 백업 → **6번 줄 `daemon,kern.* /dev/console` 주석 처리(수정 이유를 파일 안 주석으로 남김)** → `rsyslogd -N1` 설정 검사 → `systemctl restart rsyslog` | 1시간 동안 `suspended` 0회, `/var/log/syslog` 기록 계속 |
| N3 | 24시간 뒤 journal 증가량 재계산 | 하루 약 20MB, 500M 기준 약 25일 예상 |

N2 근거: rsyslog는 `$PrivDropToUser syslog`로 권한을 낮춰 도는데 `/dev/console`은 `root:tty 0620`이라
`syslog` 사용자가 쓸 수 없다 → daemon 메시지(하루 약 1.6만 건)마다 omfile 동작이 중단·재개(하루
약 5만 줄). 이 파일은 `google-compute-engine` 패키지의 conffile이라 수정은 업그레이드 후에도 유지된다.
주석 처리 시 GCE 시리얼 콘솔로 가는 daemon 로그는 사라지지만 커널 메시지는 커널이 직접 콘솔에
출력한다. 수정안 2(`syslog`를 tty 그룹에 추가)는 rsyslog 권한 하강 시 보조 그룹 유지 여부 검증이 필요.

---

## 4. 남은 작업

1. **장애 주입 검증**(사용자 결정: `db_connection` 2회, 사용자가 텔레그램을 볼 수 있을 때) — 1회차는
   무응답으로 5분 뒤 expired 표시 확인, 2회차는 사용자가 텔레그램에서 승인해 실제 실행·결과 기록 확인.
   주입 전 명령·예상 결과(승인 요청 내용, 실행될 조치) 제시, 회차마다 `pending_approvals` 상태 변화·
   `metrics` 기록·텔레그램 발송 로그 보고. 진단-라우팅 검토·fail-closed는 일부러 일으키지 않음(운영 미검증으로 문서화).
2. **Groq 클라우드 모드 재측정** — 사용자가 **다른 계정(조직)의 측정 전용 키**를 준비하면 진행.
   키는 채팅에 붙여넣지 않고 측정 PC의 `~/.config/groq-measure.env`(권한 600)에 두고
   `set -a; . ~/.config/groq-measure.env; set +a`로만 로드. 측정 전 남은 TPD 확인(작은 요청으로 TPD 429
   여부), 로컬 Ollama는 끈 상태(VM과 같은 조건), `--interval 9 --max-429 5`, 3회. 같은 기준으로 분류 후
   README·RESEARCH_SUMMARY §3.4의 클라우드 칸("측정 중")을 채운다.
3. **운영에서 아직 검증되지 않은 경로**: 승인 흐름과 expired 표시, 진단-라우팅 검토, fail-closed
   (auto + 검토 NO·실패 → 사람 승인). 모두 테스트로는 고정돼 있다.
4. 다음 배포 N1~N3.

---

## 5. §6 백로그 (B1~B16) — 상세는 `docs/RESEARCH_SUMMARY.md` §6

| 항목 | 한 줄 요약 |
|---|---|
| B1 | prod VM DB의 PII·민감값 스캔 — 미실행 |
| B2 | Groq 제3자 전송 — 로컬/클라우드 모드로 코드 대응·VM 배포 완료, 마스킹(B4)만 남음 |
| B3 | `decided_by` PII·승인 테이블 무기한 보관 + 만료 요청이 pending으로 남던 문제(코드 수정·19건 정리 완료) |
| B4 | 클라우드 모드 전송 전 마스킹(다음 우선순위) — ps 프로세스 이름, ss 내부 IP·포트, df 마운트 경로 |
| B5 | 로컬 프롬프트 예시 → placeholder(생성·검토 프롬프트 모두) — 예시를 줄이면 베끼는 대상만 바뀜 |
| B6 | 로컬 모드 L2 기본 끄기 검토 — 대상 일치율 0.5b 3.3%·3b 1.3%, 관련성 검증 사실상 없음 |
| B7 | systemctl 서비스 허용 목록(서버별) — 지금은 보호 목록만 |
| B8 | "too many open files" 실제 조치 설계 — `ulimit`은 subprocess로 효과 없음 |
| B9 | 사람 승인 없이 L1에 쌓일 수 있는 경로(온라인학습 + auto/AUTO_APPROVE) — 참고 |
| B10 | LLM이 에러 로그 대신 호스트 ps에서 대상 선택(llama-server, MainThread 사례) |
| B11 | 측정·운영 Groq 키 분리, 무료 tier 일일 한도(200k)는 운영 혼자서도 소진 가능 |
| B12 | 승인 타임아웃 약 28%(69건 중 19건), 실제 대기 5분, 텔레그램 "발송" 로그는 전달 미보장 |
| B13 | VM 로그 소음 — ops-agent 권한 오류(비활성화로 해결), rsyslog `/dev/console`(N2로 대응 예정) |
| B14 | 넘기기 경보 사유 공란(83건 중 64건) — 경보 대체 사유 코드 수정(`538bf9b3`), 플레이북 근거 채우기 남음, B12 연관 가능성 |
| B15 | 넘기기가 IMPOSSIBLE로 기록돼 통계 왜곡(`success`·`result_category` 기준이 반대 방향) — `ESCALATED` 분리 검토 |
| B16 | VM 저장소 소유권 혼재 — pull이 root로 실행돼 `.git`(809개)·작업 트리 일부가 root 소유. 배포는 root로 pull, `data/` 처리와 함께 별도 정리 필요 |

---

## 6. 작업 원칙 (이 세션에서 합의)

- **VM 상태 변경**(pull, 재시작, `.env`, DB, 서비스, GCP 리소스)은 실행 전에 명령과 예상 결과를 보여
  주고 사용자 확인을 받는다. 읽기 전용 확인은 바로 진행해도 된다.
- **멈춤 조건**: 예상과 다른 결과가 나오면 다음 단계로 넘어가지 않고 멈춘 뒤 보고한다. 롤백도 확인 후.
- **키·토큰 미출력**: 값 대신 개수·설정 여부·해시 앞자리만. 측정 로그(Groq 조직 ID·측정 PC 시스템
  컨텍스트 포함)는 커밋하지 않는다(`.gitignore`의 `logs/`).
- **측정 결과는 덮어쓰지 않는다**: 무효·이전 결과는 별도 폴더로 옮겨 보존한다.
- **순환 중인 로그의 보존은 확인과 같은 시점에 바로 실행**한다(D3.5에서 확인과 실행 사이에 기록이
  추가로 삭제됨).
- 테스트는 VM과 같은 Python 3.10 + chromadb 0.5.0 환경에서 돌린다(로컬 Python 3.14는 numpy 2.x로
  chromadb import 실패 — README "테스트 실행"). VM에서 테스트할 때는 운영 폴더 밖 `/tmp`에
  `git archive`로 풀고, root가 아닌 사용자로, `.env` 없이 실행한다.
- VM 저장소 git 명령은 **root로** 실행한다(B16: `.git`·작업 트리 일부가 root 소유, zeus3826으로는 pull 불가).
- VM 명령은 `gcloud compute ssh ... --command 'sudo bash -s' < script.sh` 패턴(SSH 사용자는 VM 저장소
  디렉터리에 직접 들어갈 수 없어 `sudo bash -s` 필요).

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

1. (2026-10-07 결정) 사유 공란 수정 커밋 → push → N1·N2 배포 → 확인 → 약속한 시간에 장애 주입 검증 2회 →
   N3(24시간 뒤 journal 재계산). 단계마다 명령·예상 결과를 보여 주고 확인 후 실행.
3. 측정 전용 키가 준비되면 Groq 재측정 → 분류 → 문서의 클라우드 칸 채우기.

---

## 9. 새 세션이 처음 읽을 파일

1. `docs/HANDOFF.md` (이 파일)
2. `docs/RESEARCH_SUMMARY.md` §6 (B1~B16), §3.4 (모드별 대상 일치율)
3. `README.md` — "L2 LLM 모드", "외부로 나가는 데이터", "보안 아키텍처", "테스트 실행"
4. Claude 로컬 메모 `project_llm_mode_rollout.md`, `project_gcp_deployment.md` (VM 접속·상태)
5. 코드: `src/llm_mode.py`, `src/llm_engine.py`(`_is_groq_available`, `_apply_self_reflection`,
   `_reflect_on_command`), `src/executor.py`(`_validate_command`, `_effective_level`, `_await_approval`),
   `src/autonomy_store.py`, `src/approval_store.py`(`mark_expired`), `src/log_watcher.py`
   (`configure_logging`, `log_chatops_status`)
