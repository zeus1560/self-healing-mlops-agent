"""
Observability 통합 테스트 스크립트

이 스크립트는 Self-Healing Agent의 관측 가능성(Observability)을 검증합니다.
동일한 에러를 두 번 실행하여:
- 첫 번째: L2_LLM에서 해결 (느림, 추론 필요)
- 두 번째: L1_CACHE에서 해결 (빠름, 벡터DB 캐시 활용)

실행 방법:
  1. python test_observability.py
  2. 첫 번째 실행 완료 후, 다시 python test_observability.py

성능 개선을 명확히 확인할 수 있습니다.
"""

import logging
import time
from src.llm_engine import RAGEngine
from src.executor import ActionExecutor
from src.schemas import ActionType
from src.observability import AgentObserver

logging.basicConfig(level=logging.INFO, format="%(message)s")


def test_observability_integration():
    """Observability 통합 테스트"""

    # 1. 엔진 및 옵저버 초기화
    rag_engine = RAGEngine()
    executor = ActionExecutor()
    observer = AgentObserver()  # 메트릭 수집기 추가

    print("\n" + "=" * 60)
    print("🤖 Self-Healing MLOps Agent - Observability 검증")
    print("=" * 60)

    # 테스트 에러 로그
    target_error_log = (
        "torch.xpu.OutOfMemoryError: XPU out of memory. Tried to allocate 4.00 GiB."
    )
    print(f"\n📋 테스트 대상 에러:\n{target_error_log}\n")

    # ==========================================
    # [시간 측정 시작] 처리 소요 시간(Latency) 로깅용
    # ==========================================
    start_time = time.perf_counter()

    # 2. 에러 분석 (뇌)
    logging.info("🧠 LLM 분석 중...")
    decision = rag_engine.analyze_error(target_error_log)
    logging.info(f"결정 사항:\n{decision.to_json()}")

    # 3. 조치 실행 (손발)
    logging.info("🤚 조치 실행 중...")
    execution_success = executor.execute(decision)

    # 4. 소요 시간 계산
    latency = time.perf_counter() - start_time

    # ==========================================
    # 5. [Observability] 메트릭 적재 (ETL)
    # ==========================================
    # L1 Cache에서 온 것인지, L2 LLM에서 온 것인지 추적
    if "Vector DB" in decision.reasoning:
        resolution_source = "L1_CACHE"
    else:
        resolution_source = "L2_LLM"

    observer.log_event(
        error_log=target_error_log,
        source=resolution_source,
        action_type=decision.action_type.value,
        latency_sec=latency,
        success=execution_success,
    )

    print(f"\n⏱️  처리 완료!")
    print(f"  - 소요 시간: {latency:.4f}초")
    print(f"  - 해결 출처: {resolution_source}")
    print(f"  - 성공 여부: {'✅ 성공' if execution_success else '❌ 실패'}")

    # ==========================================
    # 6. [Phase 4] Feedback Loop (지식 캐싱)
    # ==========================================
    if decision.action_type == ActionType.EXECUTE_LLM_COMMAND and execution_success:
        logging.info(
            "🧠 [Phase 4] LLM의 커맨드가 성공했습니다. Vector DB에 캐싱합니다..."
        )
        rag_engine.learn_from_feedback(target_error_log, decision.reasoning)

    # 7. 현재까지의 누적 성능 리포트 출력
    observer.print_performance_report()


if __name__ == "__main__":
    test_observability_integration()
