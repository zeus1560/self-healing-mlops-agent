import gc
import hashlib
import logging
import os
import re
import shlex
import subprocess
import time
import traceback
from typing import Optional

import chromadb
import requests
from chromadb.config import Settings

from src.schemas import ActionType, AgentResponse
from src.slack_bot import SlackChatOps

_SHELL_METACHAR = frozenset('|><;&`$(){}*?!\\~')

# 명령어 실패 후 시스템을 안전 상태로 되돌리기 위한 롤백 맵.
# systemctl은 서비스명을 원본 명령어에서 추출해 동적으로 생성한다.
_ROLLBACK_MAP: dict[str, str | None] = {
    "systemctl":  "__stop_service__",   # systemctl stop <service> 동적 생성
    "nginx":      "systemctl stop nginx",
    "ulimit":     "ulimit -n 1024",     # 커널 기본값 복원
    "fuser":      None,                 # 이미 포트 해제됨
    "pkill":      None,                 # 프로세스 종료는 되돌릴 수 없음
    "kill":       None,
    "free":       None,
    "df":         None,
    "ss":         None,
    "netstat":    None,
    "uptime":     None,
    "ps":         None,
    "echo":       None,
    "journalctl": None,
}


class ActionExecutor:
    def __init__(self, slack_webhook_url: Optional[str] = None):
        logging.info("[ActionExecutor] 시스템 제어 및 보안 모듈 로드 완료. 대기 중...")
        self.slack_webhook_url = slack_webhook_url

        try:
            persist_directory = os.path.join(os.getcwd(), "data", "chroma_db")
            self.chroma_client = chromadb.PersistentClient(
                path=persist_directory,
                settings=Settings(anonymized_telemetry=False),
            )
            self.l1_collection = self.chroma_client.get_or_create_collection(
                name="error_playbook_vectors"
            )
            logging.info("[ActionExecutor] L1 Cache DB 연결 완료.")
        except Exception:
            logging.error(f"[ActionExecutor] L1 Cache DB 연결 실패:\n{traceback.format_exc()}")
            self.l1_collection = None

        # 허용 명령어 화이트리스트. 빈 set = 어떤 인자도 허용.
        # python/perl/node 등 인터프리터는 의도적으로 제외 (arbitrary code exec 위험).
        self.ALLOWED_COMMANDS = {
            "pkill":      set(),
            "kill":       set(),
            "systemctl":  {"restart", "status", "stop", "start"},
            "echo":       set(),
            "ulimit":     set(),
            "nginx":      {"-s", "reload", "test"},
            "free":       set(),
            "df":         set(),
            "ss":         set(),
            "netstat":    set(),
            "uptime":     set(),
            "ps":         set(),
            "fuser":      {"-k"},
            "journalctl": {"--vacuum-size", "--vacuum-time"},
        }

        # 화이트리스트를 통과했더라도 최상위 명령어로 허용하지 않을 블랙리스트.
        # ALLOWED_COMMANDS와 교집합이 없어야 한다.
        self.BANNED_TOKENS = {
            "rm", "mkfs", "dd", "chmod", "chown", "shutdown", "reboot",
            "wget", "curl", "nc",
            "bash", "sh", "dash", "zsh", "fish",
            "python", "python3", "python2", "perl", "ruby", "node", "php", "lua",
        }

    def execute(self, decision: AgentResponse, original_error_log: str = "") -> dict:
        logging.info("===> [ActionExecutor] 시스템 조치 실행 시작 <===")
        logging.info(f"결정된 액션: {decision.action_type.name}")

        if decision.action_type == ActionType.CLEAR_MEMORY:
            ok = self._clear_memory()
            result = {
                "success": ok,
                "result_category": "SUCCESS" if ok else "FAILURE",
                "error_type": None,
                "error_detail": None,
            }
        elif decision.action_type == ActionType.RESTART_SERVICE:
            ok = self._restart_service(decision.target_process)
            result = {
                "success": ok,
                "result_category": "SUCCESS" if ok else "FAILURE",
                "error_type": None,
                "error_detail": None,
            }
        elif decision.action_type == ActionType.ESCALATE_TO_HUMAN:
            self._escalate_to_human(decision.reasoning)
            result = {
                "success": True,
                "result_category": "IMPOSSIBLE",
                "error_type": "EscalatedToHuman",
                "error_detail": decision.reasoning[:300],
            }
        elif decision.action_type == ActionType.EXECUTE_LLM_COMMAND:
            result = self._execute_llm_command(decision.reasoning, original_error_log)
        else:
            logging.warning(f"수행 불가 액션: {decision.action_type}")
            result = {
                "success": False,
                "result_category": "IMPOSSIBLE",
                "error_type": "UnknownActionType",
                "error_detail": str(decision.action_type),
            }

        logging.info(
            f"===> [ActionExecutor] 완료 | 결과:{result['result_category']}"
            + (f" | {result['error_type']}" if result["error_type"] else "")
            + " <===\n"
        )
        return result

    def _try_rollback(self, failed_command: str) -> None:
        """실패한 명령어에 대응하는 롤백 명령어가 있으면 실행한다."""
        try:
            tokens = shlex.split(failed_command)
        except (ValueError, IndexError):
            return

        base_cmd = tokens[0] if tokens else ""
        rollback = _ROLLBACK_MAP.get(base_cmd)

        if rollback is None:
            return

        # systemctl restart/start nginx → systemctl stop nginx 동적 생성
        if rollback == "__stop_service__":
            service = tokens[-1] if len(tokens) >= 3 else None
            if not service or service == base_cmd:
                return
            rollback = f"systemctl stop {service}"

        logging.warning(f"  [롤백] '{failed_command}' 실패 → 롤백 실행: {rollback}")
        try:
            proc = subprocess.run(
                shlex.split(rollback),
                capture_output=True, text=True, shell=False, timeout=10,
            )
            if proc.returncode == 0:
                logging.info(f"  [롤백 성공] {proc.stdout.strip() or '완료'}")
            else:
                logging.error(f"  [롤백 실패] {proc.stderr.strip()}")
        except Exception:
            logging.error(f"  [롤백 오류]\n{traceback.format_exc()}")

    def _validate_command(self, command: str) -> tuple[list[str], dict | None]:
        """
        shlex 파싱 → 메타문자 → 블랙리스트 → 화이트리스트 순으로 검증.
        통과 시 (tokens, None), 차단 시 ([], error_dict) 반환.
        """
        def _block(reason: str, detail: str) -> tuple[list, dict]:
            logging.error(f"  [Security Block] {reason}: {detail}")
            return [], {"success": False, "result_category": "FAILURE",
                        "error_type": "SecurityBlock", "error_detail": detail}

        try:
            tokens = shlex.split(command)
        except ValueError as e:
            logging.error(f"  [Security Block] 셸 파싱 실패: {e}")
            return [], {"success": False, "result_category": "FAILURE",
                        "error_type": "ShellParseError", "error_detail": str(e)}

        if not tokens:
            return [], {"success": False, "result_category": "FAILURE",
                        "error_type": "EmptyCommand", "error_detail": "빈 토큰 목록"}

        # 모든 토큰에서 쉘 메타문자 전수 검사
        for token in tokens:
            if any(ch in _SHELL_METACHAR for ch in token):
                return _block("메타문자 감지", f"토큰 {token!r} 에 쉘 메타문자 포함")

        base_cmd = tokens[0]

        # 블랙리스트: 인터프리터·파괴적 명령어 차단
        if base_cmd in self.BANNED_TOKENS:
            return _block("블랙리스트 명령어", base_cmd)

        # 화이트리스트: 명시적으로 허용된 명령어만 통과
        if base_cmd not in self.ALLOWED_COMMANDS:
            return _block("허용되지 않은 명령어", base_cmd)

        return tokens, None

    def _execute_llm_command(self, reasoning: str, error_log: str) -> dict:
        command = re.sub(r"^\[.*?\]\s*", "", reasoning).strip()

        if not command:
            logging.warning("  [실행거부] 실행할 명령어가 없습니다.")
            return {"success": False, "result_category": "FAILURE",
                    "error_type": "EmptyCommand", "error_detail": "reasoning에서 커맨드 파싱 실패"}

        tokens, err = self._validate_command(command)
        if err:
            return err

        auto_approve = os.getenv("AUTO_APPROVE", "false").lower() == "true"

        if auto_approve:
            logging.info(f"  [AUTO_APPROVE] 자동 승인 모드. 커맨드: {command}")
        else:
            chatops = SlackChatOps()
            chatops.send_approval_request(
                error_log="실시간 감지된 시스템 장애", command=command, reason=reasoning
            )
            print("\n" + "=" * 50)
            print("[Human-in-the-Loop] 슬랙으로 승인 요청이 발송되었습니다.")
            print(f"실행 대기 중인 명령어: {command}")
            approval = input("이 명령어를 실행하시겠습니까? (y/n): ").strip().lower()
            if approval != "y":
                logging.warning("[관리자 거절] 조치가 취소되었습니다.")
                return {"success": False, "result_category": "FAILURE",
                        "error_type": "HumanRejected", "error_detail": "관리자가 실행을 거절했습니다."}
            print("=" * 50 + "\n")

        logging.info(f"  [조치 승인됨] 커맨드 실행: {command}")
        try:
            proc = subprocess.run(
                tokens,
                capture_output=True,
                text=True,
                shell=False,
                timeout=15,
            )
            if proc.returncode == 0:
                logging.info(f"  [실행성공] 결과: {proc.stdout.strip()}")
                self._save_to_l1_cache(error_log, command)
                return {"success": True, "result_category": "SUCCESS",
                        "error_type": None, "error_detail": None}
            detail = proc.stderr.strip() or f"returncode={proc.returncode}"
            logging.error(f"  [실행실패] {detail}")
            self._try_rollback(command)
            return {"success": False, "result_category": "FAILURE",
                    "error_type": "CalledProcessError", "error_detail": detail}

        except subprocess.TimeoutExpired:
            logging.error(f"  [타임아웃]\n{traceback.format_exc()}")
            return {"success": False, "result_category": "FAILURE",
                    "error_type": "TimeoutExpired", "error_detail": f"15초 초과: {command}"}

        except PermissionError:
            logging.error(f"  [권한 거부]\n{traceback.format_exc()}")
            return {"success": False, "result_category": "IMPOSSIBLE",
                    "error_type": "PermissionError", "error_detail": traceback.format_exc()}

        except MemoryError:
            logging.error(f"  [메모리 부족]\n{traceback.format_exc()}")
            return {"success": False, "result_category": "IMPOSSIBLE",
                    "error_type": "MemoryError", "error_detail": traceback.format_exc()}

        except Exception as e:
            logging.error(f"  [실행실패]\n{traceback.format_exc()}")
            return {"success": False, "result_category": "FAILURE",
                    "error_type": type(e).__name__, "error_detail": traceback.format_exc()}

    def _save_to_l1_cache(self, error_log: str, successful_command: str) -> None:
        if not self.l1_collection:
            return
        if not error_log:
            logging.warning("  원본 에러 로그가 없어 L1 학습을 건너뜁니다.")
            return
        try:
            error_hash = hashlib.md5(error_log.encode("utf-8")).hexdigest()
            self.l1_collection.upsert(
                documents=[error_log],
                metadatas=[{
                    "error_category": "Learned_from_LLM",
                    "action_type": "EXECUTE_LLM_COMMAND",
                    "reasoning": successful_command,
                    "target_process": "unknown",
                    "learned_at": int(time.time()),
                }],
                ids=[f"learned_{error_hash}"],
            )
            logging.info(f"  [FeedbackLoop] L1 Cache 적재 완료 (ID: {error_hash[:8]})")
        except Exception:
            logging.error(f"  [FeedbackLoop] L1 Cache 업데이트 실패:\n{traceback.format_exc()}")

    def _clear_memory(self) -> bool:
        logging.warning("[조치] 시스템 메모리 최적화 시작...")
        collected = gc.collect()
        logging.info(f"  OS RAM 확보 완료 (수거: {collected}개)")
        try:
            import torch
            if hasattr(torch, "xpu") and torch.xpu.is_available():
                torch.xpu.empty_cache()
                logging.info("  Intel XPU VRAM 캐시 초기화 완료.")
            elif torch.cuda.is_available():
                torch.cuda.empty_cache()
                logging.info("  NVIDIA GPU VRAM 캐시 초기화 완료.")
        except ImportError:
            pass
        except Exception:
            logging.error(f"  VRAM 초기화 중 오류:\n{traceback.format_exc()}")
        logging.warning("메모리 최적화 완료")
        return True

    def _restart_service(self, target: str) -> bool:
        target_name = target if target else "Unknown_Service"
        logging.warning(f"[조치] '{target_name}' 서비스 재시작 중...")
        try:
            proc = subprocess.run(
                ["systemctl", "restart", target_name],
                capture_output=True,
                text=True,
                shell=False,
                timeout=30,
            )
            if proc.returncode == 0:
                logging.info(f"'{target_name}' 재시작 성공")
                return True
            detail = proc.stderr.strip() or f"returncode={proc.returncode}"
            logging.error(f"'{target_name}' 재시작 실패: {detail}")
            self._try_rollback(f"systemctl restart {target_name}")
            return False
        except subprocess.TimeoutExpired:
            logging.error(f"'{target_name}' 재시작 타임아웃 (30s 초과)")
            self._try_rollback(f"systemctl restart {target_name}")
            return False
        except Exception:
            logging.error(f"'{target_name}' 재시작 오류:\n{traceback.format_exc()}")
            return False

    def _escalate_to_human(self, reasoning: str) -> None:
        logging.error(f"[경보 발송] 관리자 개입 필요. 사유: {reasoning}")
        self._send_slack_alert(
            f"*[CRITICAL] 관리자 개입 필요*\n*사유:* {reasoning}", "CRITICAL"
        )

    def _send_slack_alert(self, message: str, severity: str = "INFO") -> None:
        if not self.slack_webhook_url:
            return
        color_map = {
            "INFO": "#36a64f",
            "WARNING": "#ffcc00",
            "ERROR": "#ff9900",
            "CRITICAL": "#ff0000",
        }
        payload = {
            "attachments": [{
                "color": color_map.get(severity, "#cccccc"),
                "text": message,
                "mrkdwn_in": ["text"],
            }]
        }
        try:
            requests.post(self.slack_webhook_url, json=payload, timeout=2)
        except Exception:
            logging.error(f"[Slack] 알람 전송 실패:\n{traceback.format_exc()}")
