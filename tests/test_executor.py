import os
import logging
from src.schemas import AgentResponse, ActionType
# 방금 만든 파일 이름이 executor.py라고 가정 (다르다면 맞춰서 수정)
from src.executor import ActionExecutor 

# 로그가 화면에 보이도록 기본 설정
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# 1. Slack Webhook URL 세팅 (실제 너의 URL로 변경해!)
# 실제 환경에서는 os.environ.get("SLACK_WEBHOOK_URL") 로 가져오는 것이 안전해.
TEST_WEBHOOK_URL = "https://hooks.slack.com/services/T00000000/B00000000/XXXXXXXXXXXXXXXXXXXXXXXX"

# 2. 실행기 인스턴스 생성
executor = ActionExecutor(slack_webhook_url=TEST_WEBHOOK_URL)

# 3. 테스트용 가짜 LLM 응답 생성 (안전한 화이트리스트 커맨드 테스트)
test_decision = AgentResponse(
    error_category="Test",
    severity="INFO",
    action_type=ActionType.EXECUTE_LLM_COMMAND,
    target_process=None,
    reasoning="[LLM 추론 (L2)] echo 'Hello MLOps, Slack Alert Test!'"
)

# 4. 실행 및 결과 확인
print("\n========== 테스트 시작 ==========")
result = executor.execute(test_decision)
print(f"========== 테스트 종료 (성공 여부: {result}) ==========\n")