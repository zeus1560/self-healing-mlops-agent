# Post-boot Verification Checklist

이 문서는 Oracle Cloud 인스턴스 부팅 직후 운영자가 순차적으로 실행하여 서비스가 정상 동작하는지 확인하는 체크리스트입니다.

> 중요: 절대 비밀 키나 토큰을 이 파일에 하드코딩하지 마십시오. .env 파일은 안전하게 관리하세요.

---

## 사전 필수 확인 (항상 먼저)
- [ ] 데몬/백그라운드 프로세스의 리소스(좀비 프로세스 등) 관리를 검토하세요. 장기 GPU 작업은 별도 리소스 해제가 필요할 수 있습니다.
- [ ] 호스트 기반 실행(시스템d) + Docker Compose 조합은 가볍고 관리 가능한 구조입니다.
- [ ] 실행 파이프라인은 shlex 기반 토큰화, 쉘 메타문자 차단, shell=False, timeout 등으로 기본적인 명령 인젝션을 방지합니다.

---

## 1) cloud-init 완료 확인
- 명령:
  - sudo cloud-init status --wait
  - sudo tail -n 200 /var/log/cloud-init.log
- 기대 결과: cloud-init 상태가 `done`. 치명적 오류 없음.

## 2) 방화벽(iptables) 규칙 확인
- 명령:
  - sudo iptables -L -n -v
  - sudo iptables -S
- 확인 포인트:
  - SSH(22)가 ADMIN_CIDR로 제한되어 있는지
  - 포트 8000, 8501, 9000이 ADMIN_CIDR만 허용되고 외부는 DROP 되어 있는지

## 3) Docker 및 docker-compose 상태
- 명령:
  - docker --version
  - docker compose version
  - sudo systemctl status docker --no-pager
- 기대 결과: Docker active (running), Docker Compose 사용 가능

## 4) 코드베이스 및 브랜치 확인
- 명령:
  - ls -la /root/agent
  - git -C /root/agent rev-parse --abbrev-ref HEAD
  - git -C /root/agent log -1 --pretty=oneline
- 확인: `/root/agent`에 코드가 있고 feature/oracle-deploy 브랜치(또는 원하는 브랜치)가 체크아웃되어 있는지

## 5) .env 파일 확인
- 명령:
  - ls -l /root/agent/.env
  - sudo sed -n '1,200p' /root/agent/.env   # 토큰 등 민감정보는 출력 주의
- 확인: 파일 존재, 권한 600 권장, 필수 변수 포함(GROQ_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID 등)

## 6) Docker Compose 서비스 상태 확인
- 명령:
  - cd /root/agent && docker compose ps
  - docker compose logs approval-server --tail 100
  - docker compose logs target-app --tail 100
- 기대 결과: approval-server, target-app 컨테이너가 `running` 상태이며 로그에 치명적 오류 없음

## 7) target-app 헬스체크
- 명령:
  - curl -sS http://127.0.0.1:9000/health
- 기대 결과: {"status":"ok"}

## 8) 호스트 에이전트(systemd) 상태
- 명령:
  - sudo systemctl status self-healing-agent --no-pager
  - sudo journalctl -u self-healing-agent -n 200 --no-hostname --no-pager
- 확인: 서비스가 active(running), 초기화 로그(예: "self-healing agent started. watching logs...") 존재

## 9) 로그 감지 및 승인 파이프라인 동작 테스트
- 시나리오:
  1) 장애 주입: curl -s -X POST http://127.0.0.1:9000/inject/oom
  2) 에이전트 로그 확인 및 DB 검사:
     - sudo journalctl -u self-healing-agent -f
     - sqlite3 /root/agent/data/agent_metrics.db "SELECT token, command, status, created_at FROM pending_approvals ORDER BY created_at DESC LIMIT 5;"
- 기대 결과: pending_approvals에 새 레코드 생성, approval-server/Telegram 알림 로그 확인

## 10) approval-server API 확인
- 명령:
  - curl -sS http://127.0.0.1:8000/health  # 엔드포인트 존재 시
  - docker compose logs approval-server --tail 50
- 기대 결과: approval-server 정상 동작

## 11) DB 스키마 / 접근성 확인
- 명령:
  - sqlite3 /root/agent/data/agent_metrics.db ".tables"
  - sqlite3 /root/agent/data/agent_metrics.db "SELECT count(*) FROM pending_approvals;"
- 기대 결과: DB 파일 존재, 테이블 확인, 레코드 조회 가능

## 12) 알림(텔레그램/슬랙) 테스트
- 방법:
  - TELEGRAM_BOT_TOKEN과 TELEGRAM_CHAT_ID가 설정되어 있다면, inject 시 Telegram 메시지 수신 여부 확인
  - Slack fallback도 설정되어 있으면 Slack 메시지 수신 확인
- 주의: 외부 네트워크 및 방화벽 규칙이 허용되어야 함

## 13) 재시작 복구 테스트
- 명령:
  - sudo systemctl restart self-healing-agent
  - cd /root/agent && docker compose restart approval-server target-app
  - sudo systemctl status self-healing-agent; docker compose ps
- 기대 결과: 서비스 정상 복구, DB/로그 유지

## 14) 메트릭 CSV 내보내기 (검증용)
- 명령:
  - cd /root/agent && .venv/bin/python scripts/export_metrics.py --days 90 --out /root/agent/exports/metrics_90d.csv
- 기대 결과: CSV 파일 생성 (/root/agent/exports/metrics_90d.csv)

## 15) 보안 및 검증 항목
- 확인 항목:
  - .env가 Git에 커밋되지 않았는지 확인 (git status)
  - .env 권한: chmod 600
  - netfilter-persistent 규칙이 재부팅 후에도 유지되는지 확인 (권장: 재부팅 테스트)

---

## 문제 발생 시 빠른 진단 요령
- cloud-init 오류: /var/log/cloud-init.log, /var/log/cloud-init-output.log
- Docker 데몬 오류: sudo systemctl status docker; journalctl -u docker
- 컨테이너 CrashLoop: docker compose logs <service> --tail 200
- 포트/방화벽 문제: sudo iptables -L -n -v 및 OCI 보안 리스트 확인
- 권한 문제: .env 파일 소유 및 권한 확인

---

작성자: Self-Healing MLOps Agent (자동 생성)
