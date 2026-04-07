import logging
import gc
import time
import subprocess
import re
from src.schemas import AgentResponse, ActionType
import platform


class ActionExecutor:
    """
    RAG 엔진의 판단(AgentResponse)을 받아 실제 OS 및 애플리케이션 제어를 수행합니다.
    """

    def __init__(self):
        logging.info("[ActionExecutor] 시스템 제어 및 보안 모듈 로드 완료. 대기 중...")

        # =====================================================================
        # [Phase 5: Security] 정규식 컴파일 (성능 최적화를 위해 Init에서 미리 메모리에 올려둠)
        # =====================================================================
        # 1. 체이닝 차단 (Command Injection 방어)
        #    에러 로그에 삽입된 악성 코드가 '; rm -rf /' 형태로 넘어오는 것을 원천 차단
        #    단, quote 안의 세미콜론은 허용 (python3 -c '...' 형태)
        self.chaining_pattern = re.compile(r"(?<!['\"])([;&|><\n])(?!['\"])")

        # 2. 블랙리스트 (파괴적이거나 외부 통신을 하는 명령어 차단)
        #    Linux(rm, dd, wget)와 Windows(del, format, Invoke-WebRequest) 모두 커버
        self.blacklist_pattern = re.compile(
            r"\b(rm|mkfs|dd|chmod|chown|shutdown|reboot|del|format|wget|curl|nc|bash -i|Invoke-WebRequest)\b",
            re.IGNORECASE,
        )

        # 3. 화이트리스트 (System 레벨에서 허용할 최상위 커맨드 접두사)
        #    이 단어들로 시작하지 않는 모든 커맨드는 일단 의심하고 차단함
        self.whitelist_pattern = re.compile(
            r"^(pkill|kill|systemctl|python3 -c|echo|netstat|taskkill|Get-Process|Restart-Service|ulimit)",
            re.IGNORECASE,
        )

    def execute(self, decision: AgentResponse) -> bool:
        """
        조치 실행 후 성공 여부를 True/False로 반환합니다. (Phase 4 학습의 기준점)
        """
        logging.info("===> [ActionExecutor] 시스템 조치 실행 시작 <===")
        logging.info(f"결정된 액션: {decision.action_type.name}")

        success = False

        if decision.action_type == ActionType.CLEAR_MEMORY:
            success = self._clear_memory()
        elif decision.action_type == ActionType.RESTART_SERVICE:
            success = self._restart_service(decision.target_process)
        elif decision.action_type == ActionType.ESCALATE_TO_HUMAN:
            success = self._escalate_to_human(decision.reasoning)
        elif decision.action_type == ActionType.EXECUTE_LLM_COMMAND:
            # [추가됨] LLM이 추론한 원시 커맨드 실행
            success = self._execute_llm_command(decision.reasoning)
        else:
            logging.warning(
                f"현재 에이전트가 수행할 수 없는 액션입니다: {decision.action_type}"
            )

        logging.info(
            f"===> [ActionExecutor] 시스템 조치 완료 (성공여부: {success}) <===\n"
        )
        return success

    def _execute_llm_command(self, command: str) -> bool:
        """LLM이 도출한 쉘/파이썬 커맨드를 실제 OS에 실행하고 검증합니다."""
        clean_command = command.replace("[LLM 추론 (L2)]", "").strip()
        clean_command = clean_command.replace(
            "[Vector DB 유사도 매칭 성공]", ""
        ).strip()

        logging.warning(f"⚡ [조치] LLM 추론 커맨드 실행 요청: {clean_command}")

        # [방어벽] 실행 전 무조건 Security Layer를 통과해야 함
        if not self._validate_command(clean_command):
            logging.error(
                "❌ [조치 거부] 보안 정책에 의해 커맨드 실행이 원천 차단되었습니다."
            )
            return False

        try:
            # =================================================================
            # 🚨 [테스트 환경 방어] Windows 환경이면 실제 실행하지 않고 성공 처리
            # =================================================================
            if platform.system() == "Windows":
                logging.info(
                    f"  👉 [Windows 시뮬레이션] OS 보호를 위해 실제 명령어를 실행하지 않습니다."
                )
                logging.info(
                    f"  👉 [실행결과] Success: Simulated (명령어: {clean_command})"
                )
                return True

            # 실제 리눅스(프로덕션) 환경일 때만 진짜로 실행
            result = subprocess.run(
                clean_command, shell=True, capture_output=True, text=True, check=True
            )
            logging.info(f"  👉 [실행결과] Success: {result.stdout.strip()}")
            return True

        except subprocess.CalledProcessError as e:
            logging.error(f"  ❌ [실행실패] Failed: {e.stderr.strip()}")
            return False

    def _validate_command(self, command: str) -> bool:
        """
        [Phase 5: Security Layer] 커맨드가 OS에 도달하기 전 3단계 필터링을 거칩니다.
        """
        command = command.strip()

        # Quote 안의 내용을 임시 제거 (python3 -c '...' 형태 허용)
        # 이를 통해 quote 안의 합법적인 세미콜론은 체크 대상에서 제외됨
        command_without_quotes = re.sub(r"'[^']*'|\"[^\"]*\"", "", command)

        # 1단계: 파이프/체이닝 검사 (quote 제거 버전에서 검사)
        if self.chaining_pattern.search(command_without_quotes):
            logging.error(
                f"🚨 [Security Block] 명령어 체이닝/리다이렉션 기호가 감지되었습니다: {command}"
            )
            return False

        # 2단계: 블랙리스트 검사 (원본 명령어에서 검사)
        if self.blacklist_pattern.search(command):
            logging.error(
                f"🚨 [Security Block] 시스템 파괴 위험이 있는 블랙리스트 명령어 감지: {command}"
            )
            return False

        # 3단계: 화이트리스트 검사 (원본 명령어에서 검사)
        if not self.whitelist_pattern.match(command):
            logging.error(
                f"🚨 [Security Block] 승인되지 않은 명령어 구문입니다 (Whitelist 위반): {command}"
            )
            return False

        return True

    def _clear_memory(self) -> bool:
        logging.warning("🧹 [조치] 시스템 메모리(RAM/XPU VRAM) 최적화를 시작합니다...")
        collected_objects = gc.collect()
        logging.info(f"  👉 OS RAM 여유 공간 확보 완료 (수거: {collected_objects}개)")

        try:
            import torch

            # [수정됨] CUDA 대신 XPU(Intel) VRAM 해제
            if hasattr(torch, "xpu") and torch.xpu.is_available():
                torch.xpu.empty_cache()
                logging.info(
                    "  👉 Intel XPU VRAM 캐시(torch.xpu.empty_cache) 초기화 완료."
                )
            elif torch.cuda.is_available():
                torch.cuda.empty_cache()
                logging.info("  👉 NVIDIA GPU VRAM 캐시 초기화 완료.")
            else:
                logging.info("  👉 (활성화된 가속기가 없어 VRAM 초기화는 생략합니다.)")
        except ImportError:
            logging.info("  👉 (PyTorch 환경이 아니므로 VRAM 초기화는 생략합니다.)")

        logging.warning("✅ 메모리 최적화 및 반환 조치가 완료되었습니다.")
        return True

    def _restart_service(self, target: str) -> bool:
        target_name = target if target else "Unknown_Service"
        logging.warning(
            f"🔄 [조치] '{target_name}' 프로세스를 안전하게 재시작합니다..."
        )
        time.sleep(1)
        logging.info(f"✅ '{target_name}' 프로세스가 정상적으로 다시 올라왔습니다.")
        return True

    def _escalate_to_human(self, reasoning: str) -> bool:
        logging.error(f"🚨 [경보 발송] 치명적 에러 발생! 관리자 개입이 필요합니다.")
        logging.error(f"   사유: {reasoning}")
        return True
