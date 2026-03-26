import logging
import gc
import time
from src.schemas import AgentResponse, ActionType


class ActionExecutor:
    """
    RAG 엔진의 판단(AgentResponse)을 받아 실제 OS 및 애플리케이션 제어를 수행합니다.
    """

    def __init__(self):
        logging.info("[ActionExecutor] 시스템 제어 모듈 로드 완료. 대기 중...")

    def execute(self, decision: AgentResponse):
        logging.info("===> [ActionExecutor] 시스템 조치 실행 시작 <===")
        logging.info(f"결정된 액션: {decision.action_type.name}")

        if decision.action_type == ActionType.CLEAR_MEMORY:
            self._clear_memory()
        elif decision.action_type == ActionType.RESTART_SERVICE:
            self._restart_service(decision.target_process)
        elif decision.action_type == ActionType.ESCALATE_TO_HUMAN:
            self._escalate_to_human(decision.reasoning)
        else:
            logging.warning(
                f"현재 에이전트가 수행할 수 없는 액션입니다: {decision.action_type}"
            )

        logging.info("===> [ActionExecutor] 시스템 조치 완료 <===\n")

    def _clear_memory(self):
        logging.warning("🧹 [조치] 시스템 메모리(RAM/VRAM) 최적화를 시작합니다...")

        # 1. 파이썬 OS RAM 가비지 컬렉팅 (사용 안 하는 메모리 찌꺼기 강제 수거)
        collected_objects = gc.collect()
        logging.info(
            f"  👉 OS RAM 여유 공간 확보 완료 (수거된 쓰레기 객체 수: {collected_objects}개)"
        )

        # 2. 실무용 방어 코드: PyTorch VRAM 캐시 초기화
        # PyTorch가 설치되어 있고 GPU를 쓰고 있다면 텐서 캐시를 날려버립니다.
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                logging.info("  👉 GPU VRAM 캐시(torch.cuda.empty_cache) 초기화 완료.")
            else:
                logging.info("  👉 (GPU가 활성화되지 않아 VRAM 초기화는 생략합니다.)")
        except ImportError:
            logging.info("  👉 (PyTorch 환경이 아니므로 VRAM 초기화는 생략합니다.)")

        logging.warning("✅ 메모리 최적화 및 반환 조치가 완료되었습니다.")

    def _restart_service(self, target: str):
        target_name = target if target else "Unknown_Service"
        logging.warning(
            f"🔄 [조치] '{target_name}' 프로세스를 안전하게 재시작합니다..."
        )
        # 실제로는 subprocess.run(["systemctl", "restart", target_name]) 등이 들어갑니다.
        time.sleep(1)  # 재시작 대기 시간 시뮬레이션
        logging.info(f"✅ '{target_name}' 프로세스가 정상적으로 다시 올라왔습니다.")

    def _escalate_to_human(self, reasoning: str):
        logging.error(f"🚨 [경보 발송] 치명적 에러 발생! 관리자 개입이 필요합니다.")
        logging.error(f"   사유: {reasoning}")
        # 실제로는 slack_sdk 등을 이용해 웹훅으로 메시지를 쏩니다.
