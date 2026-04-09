import os
import time
import logging
from src.monitor import LogMonitor
from src.observability import AgentObserver

# 로그가 화면에 잘 보이도록 설정
logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")


def test_1_debouncer():
    print("\n" + "=" * 50)
    print(" 🛠️ 테스트 1: LogDebouncer (중복 에러 필터링) 검증")
    print("=" * 50)

    test_log_path = "data/test_dummy.log"
    if os.path.exists(test_log_path):
        os.remove(test_log_path)

    # 모니터 초기화 (5분 쿨타임)
    monitor = LogMonitor(log_file_path=test_log_path)

    # [상황 부여] 똑같은 OOM 에러가 0.1초 만에 3번 연속으로 쏟아졌다고 가정
    logging.info("👉 동일한 에러 3줄을 로그 파일에 씁니다...")
    with open(test_log_path, "a", encoding="utf-8") as f:
        f.write("[ERROR] CUDA Out of Memory in device 0\n")
        f.write("[ERROR] CUDA Out of Memory in device 0\n")
        f.write("[ERROR] CUDA Out of Memory in device 0\n")

    # 모니터가 몇 개를 읽어오는지 확인
    errors = monitor.get_recent_errors()

    logging.info(f"👉 파일에 쓰인 에러 수: 3개")
    logging.info(f"👉 모니터가 파이프라인으로 넘긴 에러 수: {len(errors)}개")

    if len(errors) == 1:
        logging.info("✅ [PASS] Debouncer가 완벽하게 작동하여 중복을 차단했습니다!")
    else:
        logging.error("❌ [FAIL] Debouncer가 작동하지 않았습니다.")


def test_2_slack_webhook():
    print("\n" + "=" * 50)
    print(" 🛠️ 테스트 2: 하이브리드 리포팅 (Slack 알람 분기) 검증")
    print("=" * 50)

    # 1. 가짜 웹훅 URL을 넣어 관측기 생성 (실제 웹훅 주소가 있다면 교체해도 됩니다)
    dummy_webhook = "http://localhost:9999/dummy-webhook"
    observer = AgentObserver(
        db_path="data/test_metrics.db", slack_webhook_url=dummy_webhook
    )

    logging.info(
        "👉 [상황 A] 에이전트가 조치에 '성공'한 경우 (Slack 알람이 울리면 안 됨!)"
    )
    observer.log_event(
        error_log="[ERROR] Nginx Connection Refused",
        source="L1_CACHE",
        action_type="RESTART_SERVICE",
        latency_sec=0.15,
        success=True,  # 💡 성공!
    )
    logging.info("   -> (위 로그에 'Slack Alert 전송됨' 메시지가 없어야 정상입니다)")

    time.sleep(1)

    logging.info(
        "\n👉 [상황 B] 에이전트가 조치에 '실패'한 경우 (Slack 알람 발송 시도!)"
    )
    observer.log_event(
        error_log="[ERROR] Kernel Panic - Unable to sync",
        source="L2_LLM",
        action_type="EXECUTE_LLM_COMMAND",
        latency_sec=4.2,
        success=False,  # 💡 실패!
    )
    # 가짜 URL이므로 전송 실패(ConnectionRefusedError) 로그가 찍히면 통신 로직이 정상 작동한 것입니다.


if __name__ == "__main__":
    test_1_debouncer()
    test_2_slack_webhook()
    print("\n" + "=" * 50)
    print(" 🎉 모든 검증 완료! (에러 로그를 확인하세요)")
    print("=" * 50)
