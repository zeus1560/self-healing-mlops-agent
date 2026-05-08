import os
import time
from src.schemas import AgentResponse, ActionType
from src.executor import ActionExecutor
from src.observability import AgentObserver

# 환경변수에서 Slack Webhook URL을 가져옵니다. (없으면 직접 하드코딩해서 테스트 가능)
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")

def run_e2e_test():
    print("🚀 [E2E Test] Self-Healing 파이프라인 통합 테스트 시작...\n")

    # 1. 모듈 초기화 (우리가 방금 보안을 강화한 Executor와 메트릭 관리를 위한 Observer)
    executor = ActionExecutor(slack_webhook_url=SLACK_WEBHOOK_URL)
    observer = AgentObserver(db_path="./data/agent_metrics.db", slack_webhook_url=SLACK_WEBHOOK_URL)

    # 2. 가상의 에러 상황 발생 (실무 Nginx OOM 상황 가정)
    fake_error_log = "[ERROR] 2026-04-10 19:45:00 - Nginx worker process 1024 exited on signal 9 (OOM)"
    print(f"🚨 가상 에러 감지: {fake_error_log}")
    time.sleep(1)

    # 3. LLM/Vector DB 추론 결과 모의 (Mock)
    # 실제로는 ChromaDB 검색 -> LLM 추론이 일어나겠지만, 여기선 파이프라인 테스트를 위해 결과만 흉내냄
    decision = AgentResponse(
        error_category="OOM_ERROR",
        severity="HIGH",
        action_type=ActionType.EXECUTE_LLM_COMMAND,
        target_process="nginx",
        reasoning="[LLM 추론 (L2)] echo 'Nginx OOM handled and restarted safely'"
        )
    print(f"🧠 LLM 추론 완료: 조치 방법 -> {decision.reasoning}\n")
    time.sleep(1)

    # 4. ActionExecutor로 커널 레벨 조치 실행 (Security Layer 통과 테스트)
    start_time = time.time()
    success = executor.execute(decision)
    latency = time.time() - start_time

    # 5. Observer를 통해 SQLite DB에 메트릭 적재
    observer.log_event(
        error_log=fake_error_log,
        source="L2_LLM", # 만약 VectorDB에서 바로 찾았다면 'L1_CACHE'로 들어감
        action_type=decision.action_type.name,
        latency_sec=latency,
        success=success
    )

    print("\n✅ [E2E Test] 파이프라인 실행 완료! 대시보드를 확인하세요.")

if __name__ == "__main__":
    if not SLACK_WEBHOOK_URL:
        print("⚠️ SLACK_WEBHOOK_URL 환경변수가 설정되지 않아 슬랙 알람은 스킵됩니다.")
    run_e2e_test()