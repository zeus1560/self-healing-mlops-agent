"""
[테스트 스크립트] Phase 4: 자동 학습 시뮬레이션 (LLM 없이 직접 테스트)
===================================================================
시나리오:
  1. 미지의 에러를 시뮬레이션
  2. LLM 커맨드를 직접 주입
  3. Executor가 커맨드 실행
  4. 성공 시 RAGEngine에 자동 학습
  5. 동일 에러 재발생 시 캐시에서 즉시 반환
"""

import logging
import time
from src.llm_engine import RAGEngine
from src.executor import ActionExecutor
from src.schemas import AgentResponse, ActionType

logging.basicConfig(level=logging.INFO, format="%(message)s")


def test_phase4_simulation():
    """Phase 4 완전 루프 시뮬레이션 (LLM 없이)"""

    print("\n" + "=" * 70)
    print("🤖 Phase 4 자동 학습 시뮬레이션 테스트 (LLM 제외)")
    print("=" * 70)

    # 1. 엔진 및 실행기 초기화
    rag_engine = RAGEngine()
    executor = ActionExecutor()

    # 2. 테스트용 새로운 에러
    target_error_log = (
        "CRITICAL: PostgreSQL connection pool exhausted. All 20 connections in use."
    )
    simulated_command = (
        "echo 'Database recovery initiated' && exit 0"  # Windows에서 실행 가능한 명령어
    )

    print("\n" + "-" * 70)
    print("[Step 1] 에러 발생")
    print("-" * 70)
    print(f"에러: {target_error_log}\n")

    # 3. LLM이 판단한 결과를 시뮬레이션 (LLM 없이 직접 주입)
    print("[Step 2] LLM 커맨드 생성 (시뮬레이션)")
    simulated_decision = AgentResponse(
        error_category="Database_Error",
        severity="CRITICAL",
        action_type=ActionType.EXECUTE_LLM_COMMAND,
        reasoning=simulated_command,  # LLM이 생성했다고 가정한 커맨드
    )

    print(f"  - Action: {simulated_decision.action_type.name}")
    print(f"  - 생성된 커맨드: {simulated_decision.reasoning}\n")

    # 4. 손발(Executor)에 시스템 조치 지시
    print("[Step 3] ActionExecutor에 조치 실행 요청...")
    execution_success = executor.execute(simulated_decision)
    print(f"  - 실행 결과: {'✅ SUCCESS' if execution_success else '❌ FAILED'}\n")

    # =========================================================
    # 5. [Phase 4: Feedback Loop] 자동 학습
    # =========================================================
    print("[Step 4] Phase 4 피드백 루프 - 자동 학습 처리")
    print("-" * 70)

    if (
        simulated_decision.action_type == ActionType.EXECUTE_LLM_COMMAND
        and execution_success
    ):
        print("✅ 조건 만족: LLM 커맨드가 성공적으로 작동!")
        print("   → Vector DB에 이 지식을 영구 캐싱합니다.\n")

        rag_engine.learn_from_feedback(
            error_log=target_error_log, successful_command=simulated_decision.reasoning
        )

        learning_success = True
    elif (
        simulated_decision.action_type == ActionType.EXECUTE_LLM_COMMAND
        and not execution_success
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

        print("두 번째 호출: RAGEngine.analyze_error() 실행")
        cached_decision = rag_engine.analyze_error(target_error_log)

        print(f"\n두 번째 호출 결과:")
        print(f"  - Action: {cached_decision.action_type.name}")
        print(f"  - 반환된 커맨드: {cached_decision.reasoning}")
        print(
            f"  - 원본 커맨드와 동일: {'✅ YES' if cached_decision.reasoning == simulated_decision.reasoning else '❌ NO'}\n"
        )

        if cached_decision.reasoning == simulated_decision.reasoning:
            print("=" * 70)
            print("✅✅✅ Phase 4 완전 성공! ✅✅✅")
            print("-" * 70)
            print("1차 사이클:")
            print("   ✅ LLM이 새로운 커맨드 생성")
            print("   ✅ Executor가 커맨드 실행 성공")
            print("   ✅ RAGEngine이 Vector DB에 학습 저장")
            print("\n2차 사이클:")
            print("   ✅ 캐시에서 즉시 동일한 커맨드 반환")
            print("   ✅ LLM 추론 없이 빛의 속도로 응답!")
            print("=" * 70)
            return True
        else:
            print("=" * 70)
            print("❌ Phase 4 실패: 커맨드 불일치")
            print(f"   Expected: {simulated_decision.reasoning}")
            print(f"   Got:      {cached_decision.reasoning}")
            print("=" * 70)
            return False
    else:
        print("⚠️ 학습이 실패했으므로 재테스트를 건너뜁니다.")
        return False


if __name__ == "__main__":
    success = test_phase4_simulation()
    if success:
        print("\n🎉 모든 테스트를 통과했습니다!")
    else:
        print("\n⚠️ 테스트에 실패했습니다.")
