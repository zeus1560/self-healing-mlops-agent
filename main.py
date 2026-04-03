import os
import logging
import time
from src.monitor import LogMonitor
from src.llm_engine import RAGEngine
from src.executor import ActionExecutor
from src.schemas import ActionType
from src.utils.profiler import profile_performance, MemoryProfiler
from src.observability import AgentObserver

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)


@profile_performance
def run_agent_pipeline(monitor, llm, executor, observer):
    # 💡 내부에 있던 초기화 3줄을 완전히 삭제했습니다. 파라미터로 받은 객체만 사용합니다.
    error_logs = monitor.get_recent_errors()

    if not error_logs:
        return  # 에러 없으면 콘솔 더럽히지 않고 조용히 패스

    logging.info("🚀 Self-Healing MLOps Agent 파이프라인 가동...")
    for error_log in error_logs:
        logging.info(f"수신된 로그: {error_log}")

        # =========================================================
        # [시간 측정 시작] 처리 소요 시간(Latency) 로깅용
        # =========================================================
        start_time = time.perf_counter()

        # 1. 뇌(LLM/VectorDB)에 에러 분석 지시
        decision = llm.analyze_error(error_log)
        logging.info(f"LLM 판단 결과:\n{decision.to_json()}")

        # 2. 손발(Executor)에 시스템 조치 지시 (실제 실행 및 성공 여부 반환)
        execution_success = executor.execute(decision)

        # 4. 소요 시간 계산
        latency = time.perf_counter() - start_time

        # =========================================================
        # [Observability] L1 vs L2 결정 (reasoning 기반 식별자)
        # =========================================================
        if "Vector DB" in decision.reasoning:
            resolution_source = "L1_CACHE"
        else:
            resolution_source = "L2_LLM"

        # =========================================================
        # [Observability] 메트릭 적재 (ETL)
        # =========================================================
        observer.log_event(
            error_log=error_log,
            source=resolution_source,
            action_type=decision.action_type.value,
            latency_sec=latency,
            success=execution_success,
        )

        # =========================================================
        # [Phase 4: Feedback Loop] 자동 학습
        # 조건 1: Action이 LLM이 새롭게 만들어낸 커맨드였을 것
        # 조건 2: 그 커맨드를 OS에 던졌을 때 에러 없이(Return 0) 성공했을 것
        # =========================================================
        if decision.action_type == ActionType.EXECUTE_LLM_COMMAND and execution_success:
            logging.info(
                "🧠 [Phase 4] LLM의 커맨드가 성공적으로 작동했습니다. 이 지식을 Vector DB에 영구 캐싱합니다."
            )

            # llm_engine.py에 추가했던 메서드 호출
            llm.learn_from_feedback(
                error_log=error_log, successful_command=decision.reasoning
            )
        elif (
            decision.action_type == ActionType.EXECUTE_LLM_COMMAND
            and not execution_success
        ):
            logging.warning(
                "⚠️ [Phase 4] 커맨드 실행 실패! 잘못된 지식이므로 DB에 학습하지 않고 폐기합니다."
            )


if __name__ == "__main__":
    logging.info("==================================================")
    logging.info(" 🛡️ Self-Healing MLOps Agent 백그라운드 감시 시작 🛡️ ")
    logging.info("==================================================")

    profiler = MemoryProfiler()
    profiler.print_status("초기 메모리 상태")

    # 💡 무한 루프 밖에서 딱 한 번만 뇌, 눈, 손, 관측기를 생성합니다! (기억력 영구 유지)
    global_monitor = LogMonitor(log_file_path="data/system_dummy.log")
    global_llm = RAGEngine()
    global_executor = ActionExecutor()
    global_observer = AgentObserver()  # 메트릭 수집기 추가

    try:
        while True:
            # 5초마다 감시 수행
            run_agent_pipeline(
                global_monitor, global_llm, global_executor, global_observer
            )
            time.sleep(5)

    except KeyboardInterrupt:
        print("\n" + "=" * 50)
        logging.warning("🛑 관리자에 의해 에이전트 감시가 종료되었습니다.")
        profiler.print_status("종료 직전 메모리 상태")

        # =========================================================
        # [Observability] 누적 성능 리포트 출력
        # =========================================================
        global_observer.print_performance_report()
