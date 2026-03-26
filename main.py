import os
import logging
import time
from src.monitor import LogMonitor
from src.llm_engine import RAGEngine
from src.executor import ActionExecutor
from src.utils.profiler import profile_performance, MemoryProfiler

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)


@profile_performance
def run_agent_pipeline(monitor, llm, executor):
    # 💡 내부에 있던 초기화 3줄을 완전히 삭제했습니다. 파라미터로 받은 객체만 사용합니다.
    error_logs = monitor.get_recent_errors()

    if not error_logs:
        return  # 에러 없으면 콘솔 더럽히지 않고 조용히 패스

    logging.info("🚀 Self-Healing MLOps Agent 파이프라인 가동...")
    for log in error_logs:
        logging.info(f"수신된 로그: {log}")
        decision = llm.analyze_error(log)
        logging.info(f"LLM 판단 결과:\n{decision.to_json()}")
        executor.execute(decision)


if __name__ == "__main__":
    logging.info("==================================================")
    logging.info(" 🛡️ Self-Healing MLOps Agent 백그라운드 감시 시작 🛡️ ")
    logging.info("==================================================")

    profiler = MemoryProfiler()
    profiler.print_status("초기 메모리 상태")

    # 💡 무한 루프 밖에서 딱 한 번만 뇌, 눈, 손을 생성합니다! (기억력 영구 유지)
    global_monitor = LogMonitor(log_file_path="data/system_dummy.log")
    global_llm = RAGEngine()
    global_executor = ActionExecutor()

    try:
        while True:
            # 5초마다 감시 수행
            run_agent_pipeline(global_monitor, global_llm, global_executor)
            time.sleep(5)

    except KeyboardInterrupt:
        print("\n" + "=" * 50)
        logging.warning("🛑 관리자에 의해 에이전트 감시가 종료되었습니다.")
        profiler.print_status("종료 직전 메모리 상태")
