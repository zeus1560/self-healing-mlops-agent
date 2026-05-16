import logging
import time
from src.llm_engine import RAGEngine
from src.executor import ActionExecutor
from src.observability import AgentObserver
from src.schemas import ActionType


def simulate_error_handling(error_log: str, engine, executor, observer, step_name: str):
    logging.info("\n" + "=" * 60)
    logging.info(f"[{step_name}] 에러 발생: {error_log}")
    logging.info("=" * 60)

    start_time = time.perf_counter()

    # 1. 인지 및 추론 (L1 검색 or L2 추론)
    decision = engine.analyze_error(error_log)

    # 2. 보안 검증 및 실행
    result = executor.execute(decision, original_error_log=error_log)

    latency = time.perf_counter() - start_time
    source  = decision.resolution_source

    # 3. 메트릭 적재
    observer.log_event(
        error_log=error_log,
        source=source,
        action_type=decision.action_type.value,
        latency_sec=latency,
        success=result["success"],
        result_category=result.get("result_category", "SUCCESS"),
        error_type=result.get("error_type"),
        error_detail=result.get("error_detail"),
    )

    # 4. 피드백 루프 — log_watcher.py와 동일 조건
    if result["success"] and source in ("L2_LLM", "RULE") and decision.command:
        engine.learn_from_feedback(error_log, decision.command)


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

    logging.info("\n⏳ 10초 대기 후 동일 에러 재발생 시뮬레이션...\n")
    time.sleep(10)  # 2초에서 10초로 변경 (OS 커널의 VRAM GC 시간 확보)

    # [Test 2] 방금 학습한 에러 -> Vector DB에서 0.1초 컷
    simulate_error_handling(
        test_error, engine, executor, observer, "2차 시도: Vector DB Cache Hit"
    )

    # [최종 확인] 성능 리포트 출력
    observer.print_performance_report()
