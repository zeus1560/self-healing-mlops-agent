.PHONY: help start stop restart demo logs status install

AGENT_SERVICE := self-healing-agent
VENV          := .venv/bin/python

help:
	@echo ""
	@echo "  Self-Healing MLOps Agent"
	@echo ""
	@echo "  make install   패키지 설치 + 환경 초기화"
	@echo "  make start     인프라(Docker) + 에이전트 기동"
	@echo "  make stop      전체 종료"
	@echo "  make restart   에이전트만 재시작"
	@echo "  make demo      장애 주입 전체 데모 시나리오 실행"
	@echo "  make logs      에이전트 실시간 로그 출력"
	@echo "  make status    컨테이너 + 에이전트 상태 확인"
	@echo ""

install:
	bash install.sh

start:
	docker compose up -d
	@echo "⏳ 컨테이너 기동 대기 (5초)..."
	@sleep 5
	sudo systemctl start $(AGENT_SERVICE)
	@echo "✅ 에이전트 기동 완료"
	@echo "   대시보드  → http://localhost:8501"
	@echo "   승인 서버 → http://localhost:8000"

stop:
	-sudo systemctl stop $(AGENT_SERVICE)
	docker compose down
	@echo "✅ 전체 종료 완료"

restart:
	sudo systemctl restart $(AGENT_SERVICE)
	@echo "✅ 에이전트 재시작 완료"

demo:
	$(VENV) demo/inject_failure.py --scenario full

logs:
	sudo journalctl -u $(AGENT_SERVICE) -f

status:
	@echo "── Docker 컨테이너 ──────────────────────────"
	@docker compose ps
	@echo ""
	@echo "── 에이전트 서비스 ──────────────────────────"
	@sudo systemctl status $(AGENT_SERVICE) --no-pager -l || true
