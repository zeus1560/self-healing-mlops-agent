import logging
import time
from src.llm_engine import RAGEngine
from src.executor import ActionExecutor
from src.observability import AgentObserver
from src.schemas import ActionType


def simulate_error_handling(error_log: str, engine, executor, observer, step_name: str):
    print(f"\n" + "=" * 60)
    print(f"🔥 [{step_name}] 에러 발생: {error_log}")
    print("=" * 60)

    start_time = time.perf_counter()

    # 1. 인지 및 추론 (L1 검색 or L2 추론)
    decision = engine.analyze_error(error_log)

    # 2. 보안 검증 및 실행
    execution_success = executor.execute(decision)

    latency = time.perf_counter() - start_time

    # 3. 메트릭 적재 (ETL)
    resolution_source = (
        "L1_CACHE" if "Vector DB 유사도 매칭 성공" in decision.reasoning else "L2_LLM"
    )

    observer.log_event(
        error_log=error_log,
        source=resolution_source,
        action_type=decision.action_type.value,
        latency_sec=latency,
        success=execution_success,
    )

    # 4. 피드백 루프 (성공적인 LLM 조치만 학습)
    if (
        resolution_source == "L2_LLM"
        and decision.action_type == ActionType.EXECUTE_LLM_COMMAND
        and execution_success
    ):
        engine.learn_from_feedback(error_log, decision.reasoning)


if __name__ == "__main__":
    # 로그 포맷 깔끔하게 설정
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    # 모듈 초기화
    engine = RAGEngine()
    executor = ActionExecutor()
    observer = AgentObserver()

    # 테스트용 가짜 에러 로그
    test_error = "CRITICAL: memory leak detected in worker process 8080."

    # [Test 1] 처음 겪는 에러 -> LLM 구동 및 학습
    simulate_error_handling(
        test_error, engine, executor, observer, "1차 시도: LLM Fallback"
    )

    print("\n⏳ 10초 대기 후 동일 에러 재발생 시뮬레이션 (XPU VRAM 반환 대기)...\n")
    time.sleep(10)  # 2초에서 10초로 변경 (OS 커널의 VRAM GC 시간 확보)

    # [Test 2] 방금 학습한 에러 -> Vector DB에서 0.1초 컷
    simulate_error_handling(
        test_error, engine, executor, observer, "2차 시도: Vector DB Cache Hit"
    )

    # [최종 확인] 성능 리포트 출력
    observer.print_performance_report()
