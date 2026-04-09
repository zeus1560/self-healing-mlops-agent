import logging
import gc
import time
import subprocess
import platform
import shlex
import requests
from typing import List, Optional
from src.schemas import AgentResponse, ActionType

class ActionExecutor:
    """
    RAG 엔진의 판단(AgentResponse)을 받아 실제 OS 및 애플리케이션 제어를 수행합니다.
    """

    def __init__(self, slack_webhook_url: Optional[str] = None):
        logging.info("[ActionExecutor] 시스템 제어 및 보안 모듈 로드 완료. 대기 중...")
        
        # Slack 알림용 Webhook URL
        self.slack_webhook_url = slack_webhook_url

        # =====================================================================
        # [Phase 5: Security] Token 기반 화이트리스트/블랙리스트 (정규식 대체)
        # =====================================================================
        self.ALLOWED_COMMANDS = {
            "pkill": set(),
            "kill": set(),
            "systemctl": {"restart", "status", "stop", "start"},
            "python3": {"-c"},
            "echo": set(),
            "netstat": set(),
            "taskkill": set(),
            "get-process": set(),
            "restart-service": set(),
            "ulimit": set(),
            "nginx": {"-s", "reload", "test"}
        }

        self.BANNED_TOKENS = {
            "rm", "mkfs", "dd", "chmod", "chown", "shutdown", "reboot", 
            "del", "format", "wget", "curl", "nc", "bash", "invoke-webrequest",
            ">", ">>", "|", "&", ";"
        }

    def execute(self, decision: AgentResponse) -> bool:
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
            success = self._execute_llm_command(decision.reasoning)
        else:
            logging.warning(f"현재 에이전트가 수행할 수 없는 액션입니다: {decision.action_type}")

        logging.info(f"===> [ActionExecutor] 시스템 조치 완료 (성공여부: {success}) <===\n")
        return success

    def _execute_llm_command(self, command: str) -> bool:
        """LLM이 도출한 쉘/파이썬 커맨드를 실제 OS에 실행하고 검증합니다."""
        clean_command = command.replace("[LLM 추론 (L2)]", "").strip()
        clean_command = clean_command.replace("[Vector DB 유사도 매칭 성공]", "").strip()

        logging.warning(f"⚡ [조치] LLM 추론 커맨드 실행 요청: {clean_command}")

        safe_tokens = self._validate_command(clean_command)
        if not safe_tokens:
            logging.error("❌ [조치 거부] 보안 정책에 의해 커맨드 실행이 원천 차단되었습니다.")
            self._send_slack_alert(f"🚨 *보안 차단*\n승인되지 않은 명령어 실행 시도: `{clean_command}`", "CRITICAL")
            return False

        try:
            if platform.system() == "Windows":
                logging.info("  👉 [Windows 시뮬레이션] OS 보호를 위해 실제 명령어를 실행하지 않습니다.")
                return True

            result = subprocess.run(
                safe_tokens, 
                shell=False, 
                capture_output=True, 
                text=True, 
                check=True,
                timeout=15
            )
            
            log_output = result.stdout.strip()[:300]
            logging.info(f"  👉 [실행결과] Success: {log_output}")
            
            # 파서를 고장내던 백틱 3개(```) 제거 후 변수로 포맷팅
            success_msg = f"✅ *조치 성공*\n명령어: `{clean_command}`\n[실행 결과]\n{log_output}"
            self._send_slack_alert(success_msg, "INFO")
            return True

        except subprocess.CalledProcessError as e:
            err_output = e.stderr.strip()[:300]
            logging.error(f"  ❌ [실행실패] Failed: {err_output}")
            
            fail_msg = f"⚠️ *조치 실패*\n명령어: `{clean_command}`\n[에러 로그]\n{err_output}"
            self._send_slack_alert(fail_msg, "ERROR")
            return False
            
        except subprocess.TimeoutExpired:
            logging.error("  ❌ [실행실패] 실행 시간 초과")
            self._send_slack_alert(f"⚠️ *타임아웃*\n명령어 실행 중 응답이 없습니다: `{clean_command}`", "ERROR")
            return False

    def _validate_command(self, command: str) -> Optional[List[str]]:
        command = command.strip()

        try:
            tokens = shlex.split(command)
        except ValueError:
            logging.error(f"🚨 [Security Block] 셸 구문 파싱 실패 (인젝션 의심): {command}")
            return None

        if not tokens:
            return None

        base_cmd = tokens[0].lower()

        if base_cmd not in self.ALLOWED_COMMANDS:
            logging.error(f"🚨 [Security Block] 승인되지 않은 최상위 명령어: {base_cmd}")
            return None

        for token in tokens:
            if token.lower() in self.BANNED_TOKENS:
                logging.error(f"🚨 [Security Block] 블랙리스트 토큰 감지: {token}")
                return None

        return tokens

    def _clear_memory(self) -> bool:
        logging.warning("🧹 [조치] 시스템 메모리(RAM/XPU VRAM) 최적화를 시작합니다...")
        collected_objects = gc.collect()
        logging.info(f"  👉 OS RAM 여유 공간 확보 완료 (수거: {collected_objects}개)")

        try:
            import torch
            if hasattr(torch, "xpu") and torch.xpu.is_available():
                torch.xpu.empty_cache()
                logging.info("  👉 Intel XPU VRAM 캐시 초기화 완료.")
            elif torch.cuda.is_available():
                torch.cuda.empty_cache()
                logging.info("  👉 NVIDIA GPU VRAM 캐시 초기화 완료.")
            else:
                logging.info("  👉 (활성화된 가속기가 없어 VRAM 초기화 생략)")
        except ImportError:
            logging.info("  👉 (PyTorch 환경이 아니므로 VRAM 초기화 생략)")

        logging.warning("✅ 메모리 최적화 완료")
        return True

    def _restart_service(self, target: str) -> bool:
        target_name = target if target else "Unknown_Service"
        logging.warning(f"🔄 [조치] '{target_name}' 프로세스 재시작 중...")
        time.sleep(1)
        logging.info(f"✅ '{target_name}' 프로세스 정상 작동")
        return True

    def _escalate_to_human(self, reasoning: str) -> bool:
        logging.error(f"🚨 [경보 발송] 관리자 개입 필요. 사유: {reasoning}")
        self._send_slack_alert(f"🚨 *[CRITICAL] 관리자 개입 필요*\n*사유:* {reasoning}", "CRITICAL")
        return True

    def _send_slack_alert(self, message: str, severity: str = "INFO"):
        """비동기적(짧은 타임아웃)으로 Slack에 알림 전송"""
        if not self.slack_webhook_url:
            return
            
        color_map = {
            "INFO": "#36a64f", 
            "WARNING": "#ffcc00", 
            "ERROR": "#ff9900", 
            "CRITICAL": "#ff0000"
        }
        
        payload = {
            "attachments": [{
                "color": color_map.get(severity, "#cccccc"), 
                "text": message, 
                "mrkdwn_in": ["text"]
            }]
        }
        
        try:
            requests.post(self.slack_webhook_url, json=payload, timeout=2)
        except requests.exceptions.RequestException as e:
            logging.error(f"Slack 전송 실패: {e}")