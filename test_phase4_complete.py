"""
[테스트 스크립트] Phase 4: 자동 학습 완전 파이프라인 검증
===================================================================
시나리오:
  1. 미지의 에러 발생
  2. LLM이 커맨드 생성
  3. Executor가 커맨드 실행
  4. 성공 시 RAGEngine에 자동 학습
  5. 동일 에러 재발생 시 캐시에서 즉시 반환
"""

import logging
import time
from src.llm_engine import RAGEngine
from src.executor import ActionExecutor
from src.schemas import ActionType

logging.basicConfig(level=logging.INFO, format="%(message)s")


def test_phase4_loop():
    """Phase 4 완전 루프 테스트"""

    print("\n" + "=" * 70)
    print("🤖 Phase 4 자동 학습 완전 파이프라인 테스트")
    print("=" * 70)

    # 1. 엔진 및 실행기 초기화
    rag_engine = RAGEngine()
    executor = ActionExecutor()

    # 2. 테스트용 새로운 에러 (DB에 없는 것)
    target_error_log = (
        "CRITICAL: Redis cache connection timeout. Unable to connect to 127.0.0.1:6379"
    )
    simulated_command = "redis-cli ping && redis-cli FLUSHALL"

    print("\n" + "-" * 70)
    print("[Step 1] 미지의 에러 발생")
    print("-" * 70)
    print(f"에러: {target_error_log}\n")

    # 3. 뇌(LLM/VectorDB)에 에러 분석 지시
    print("[Step 2] RAGEngine에 에러 분석 요청...")
    decision = rag_engine.analyze_error(target_error_log)

    print(f"\n결정 정보:")
    print(f"  - Action: {decision.action_type.name}")
    print(f"  - Reasoning: {decision.reasoning}\n")

    # 4. 손발(Executor)에 시스템 조치 지시
    print("[Step 3] ActionExecutor에 조치 실행 요청...")
    if decision.action_type == ActionType.EXECUTE_LLM_COMMAND:
        print(f"  - 실행할 커맨드: {decision.reasoning}")

    execution_success = executor.execute(decision)
    print(f"\n  - 실행 결과: {'✅ SUCCESS' if execution_success else '❌ FAILED'}\n")

    # =========================================================
    # 5. [Phase 4: Feedback Loop] 자동 학습
    # =========================================================
    print("[Step 4] Phase 4 피드백 루프 - 자동 학습 처리")
    print("-" * 70)

    if decision.action_type == ActionType.EXECUTE_LLM_COMMAND and execution_success:
        print("✅ 조건 만족: LLM 커맨드가 성공적으로 작동!")
        print("   → Vector DB에 이 지식을 영구 캐싱합니다.\n")

        rag_engine.learn_from_feedback(
            error_log=target_error_log, successful_command=decision.reasoning
        )

        learning_success = True
    elif (
        decision.action_type == ActionType.EXECUTE_LLM_COMMAND and not execution_success
    ):
        print("❌ 조건 미충족: 커맨드 실행 실패")
        print("   → DB에 학습하지 않고 폐기합니다.\n")
        learning_success = False
    else:
        print("⚠️ 조건 미충족: Action이 LLM_COMMAND가 아님")
        print("   → 학습 대상이 아닙니다.\n")
        learning_success = False

    # =========================================================
    # 6. 학습 확인 테스트 (동일 에러 재발생 시뮬레이션)
    # =========================================================
    if learning_success:
        print("[Step 5] 학습 확인 - 동일 에러 재발생")
        print("-" * 70)
        print("1초 대기 후 동일한 에러로 재테스트...\n")
        time.sleep(1)

        # 두 번째 호출: 새로운 커맨드 생성 없이 캐시에서 즉시 반환되어야 함
        cached_decision = rag_engine.analyze_error(target_error_log)

        print(f"두 번째 호출 결과:")
        print(f"  - Action: {cached_decision.action_type.name}")
        print(f"  - 반환된 커맨드: {cached_decision.reasoning}")
        print(
            f"  - 원본 커맨드와 동일: {'✅ YES' if cached_decision.reasoning == decision.reasoning else '❌ NO'}\n"
        )

        if cached_decision.reasoning == decision.reasoning:
            print("=" * 70)
            print("✅ Phase 4 완전 성공!")
            print("   1차: LLM이 새로운 커맨드 생성 후 실행 성공!")
            print("   2차: 캐시에서 즉시 동일한 커맨드 반환!")
            print("=" * 70)
        else:
            print("=" * 70)
            print("❌ Phase 4 실패: 커맨드 불일치")
            print(f"   Expected: {decision.reasoning}")
            print(f"   Got:      {cached_decision.reasoning}")
            print("=" * 70)
    else:
        print("⚠️ 학습이 실패했으므로 재테스트를 건너뜁니다.")


if __name__ == "__main__":
    test_phase4_loop()
    print("\n✅ 테스트 완료!")
