"""
ActionExecutor — 에이전트 결정을 안전하게 실행하는 실행기.

보안 설계 (4중 방어):
  1. shlex.split(): 토큰 파싱 단계에서 인용 부호 트릭 차단.
  2. _SHELL_METACHAR 필터: 파이프·리다이렉트·서브쉘 등 체이닝 메타문자 전량 차단.
  3. _MAX_CMD_TOKENS: 토큰 수 상한으로 과도하게 긴 명령어(잠재적 체이닝) 차단.
  4. BANNED_TOKENS + ALLOWED_COMMANDS: 블랙리스트·화이트리스트 이중 필터.
     - systemctl 3번째 토큰(서비스 이름)은 _PROCESS_NAME_RE 로 추가 검증.
  5. shell=False 원칙: subprocess.run 은 항상 리스트 형태 토큰을 사용.
  6. _validate_process_name(): 프로세스·서비스 이름에 플래그(-로 시작) 또는
     비허용 문자가 포함된 경우 즉시 거부 — Flag Injection 방지.
  7. pkill -x: 정확한 프로세스 이름 완전 일치만 허용, 부분 매칭 방지.

Human-in-the-Loop:
  모든 LLM 생성 명령은 Slack 승인 후 실행.

종료 신호 연동:
  set_shutdown_event()로 외부에서 threading.Event를 주입하면,
  승인 대기 루프가 종료 신호를 감지하고 즉시 취소한다.
"""
import gc
import logging
import os
import re
import shlex
import subprocess
import sys
import time
import traceback
import threading
from typing import Optional

import requests

from src import approval_store
from src.schemas import ActionType, AgentResponse
from src.slack_bot import SlackChatOps
from src.telegram_bot import get_chatops_client

# ── 환경 변수 설정 ────────────────────────────────────────────────────────────
_APPROVAL_POLL_INTERVAL = int(os.getenv("APPROVAL_POLL_INTERVAL_SEC", "5"))
_APPROVAL_TIMEOUT_SEC   = int(os.getenv("APPROVAL_TIMEOUT_SEC", "300"))

# 단일 LLM 명령어의 최대 허용 토큰 수.
# 이 값을 초과하면 명령 체이닝 시도로 간주해 차단한다.
# 예: "systemctl restart nginx" = 3토큰, "journalctl --vacuum-size" = 2토큰
_MAX_CMD_TOKENS = int(os.getenv("MAX_CMD_TOKENS", "6"))

# ── 보안 상수 ─────────────────────────────────────────────────────────────────
# 쉘 메타문자: 파이프·리다이렉트·서브쉘·글로빙 등 모든 체이닝 수단을 포함.
# shlex.split 이후에도 토큰 내 메타문자 잔존 여부를 재확인한다.
_SHELL_METACHAR = frozenset('|><;&`$(){}*?!\\~')

# 프로세스·서비스 이름 허용 패턴:
# 영문자·숫자·밑줄·하이픈·점만 허용. 플래그(-로 시작) 및 경로 구분자 차단.
_PROCESS_NAME_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_\-.]*$')

# ── 종료 신호 ─────────────────────────────────────────────────────────────────
# log_watcher.start_watching()에서 set_shutdown_event()로 주입된다.
_shutdown_event: Optional[threading.Event] = None


def set_shutdown_event(event: threading.Event) -> None:
    """외부 종료 이벤트를 주입한다. log_watcher.start_watching()에서 호출."""
    global _shutdown_event
    _shutdown_event = event


def _is_shutting_down() -> bool:
    return bool(_shutdown_event and _shutdown_event.is_set())


# ── 롤백 맵 ──────────────────────────────────────────────────────────────────
# 명령어 실패 후 시스템을 안전 상태로 되돌리기 위한 롤백 맵.
# None: 롤백 불필요(읽기 전용 또는 단순 프로세스 종료).
_ROLLBACK_MAP: dict[str, str | None] = {
    "systemctl":  "__stop_service__",
    "nginx":      "systemctl stop nginx",
    "ulimit":     "ulimit -n 1024",
    "fuser":      None,
    "pkill":      None,
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


def _result(ok: bool, fail_type: str, detail: str | None = None) -> dict:
    """표준 실행 결과 딕셔너리 생성 헬퍼. 성공 시 error_type=None."""
    return {
        "success":         ok,
        "result_category": "SUCCESS" if ok else "FAILURE",
        "error_type":      fail_type if not ok else None,
        "error_detail":    detail,
    }


def _validate_process_name(name: str) -> str | None:
    """
    프로세스/서비스 이름의 안전성을 검증한다.

    거부 조건:
      - 비어있거나 None
      - '-'로 시작 (플래그 인젝션: 'pkill -9' 등)
      - 허용 패턴(_PROCESS_NAME_RE) 불일치

    Returns:
        안전하면 name, 위험하면 None.
    """
    if not name:
        return None
    if name.startswith("-"):
        logging.error(f"  [Security Block] 프로세스 이름이 플래그로 시작: {name!r}")
        return None
    if not _PROCESS_NAME_RE.match(name):
        logging.error(f"  [Security Block] 프로세스 이름에 비허용 문자 포함: {name!r}")
        return None
    return name


class ActionExecutor:
    """
    에이전트 결정(AgentResponse)을 실제 시스템 조치로 변환·실행한다.

    모든 LLM 생성 명령어는 _validate_command()의 4중 보안 필터를 통과한 뒤
    Human-in-the-Loop(Slack 승인 또는 대화형 확인) 과정을 거쳐 실행된다.
    """

    def __init__(self, slack_webhook_url: Optional[str] = None):
        logging.info("[ActionExecutor] 시스템 제어 및 보안 모듈 로드 완료. 대기 중...")
        self.slack_webhook_url = slack_webhook_url

        # 허용 명령어 화이트리스트.
        # 빈 set   = 인자 제한 없음 (df, ps 등 읽기 전용 명령어에만 사용).
        # 비어있지 않은 set = 첫 번째 인자를 집합 내 값으로만 제한.
        #
        # [보안] pkill·kill은 신호 플래그 인젝션(-9, -SIGKILL 등)을 방지하기 위해
        #        명시적 허용 집합으로 제한. 빈 set을 사용하면 'kill -9 1' 같은
        #        파괴적 명령이 통과하는 취약점이 발생하므로 반드시 비어있지 않은
        #        집합을 사용해야 한다.
        # [보안] python/perl/node 등 인터프리터는 의도적으로 제외.
        #        (arbitrary code execution 위험)
        self.ALLOWED_COMMANDS: dict[str, set[str]] = {
            "pkill":      {"-f", "-x"},              # 패턴 매칭 플래그만 허용
            "kill":       {"-TERM", "-HUP"},          # 소프트 신호만 허용
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

        self.BANNED_TOKENS: frozenset[str] = frozenset({
            "rm", "mkfs", "dd", "chmod", "chown", "shutdown", "reboot",
            "wget", "curl", "nc",
            "bash", "sh", "dash", "zsh", "fish",
            "python", "python3", "python2", "perl", "ruby", "node", "php", "lua",
        })

    # ── 공개 인터페이스 ─────────────────────────────────────────────────────────
    def execute(self, decision: AgentResponse, original_error_log: str = "") -> dict:
        """AgentResponse를 받아 적절한 시스템 조치를 실행하고 결과 딕셔너리를 반환한다."""
        logging.info("===> [ActionExecutor] 시스템 조치 실행 시작 <===")
        logging.info(f"결정된 액션: {decision.action_type.name}")

        if decision.action_type == ActionType.CLEAR_MEMORY:
            ok, err = self._clear_memory()
            result  = _result(ok, "MemoryClearFailed", err)

        elif decision.action_type == ActionType.RESTART_SERVICE:
            ok, err = self._restart_service(decision.target_process)
            result  = _result(ok, "ServiceRestartFailed", err)

        elif decision.action_type == ActionType.ESCALATE_TO_HUMAN:
            self._escalate_to_human(decision.reasoning)
            result = {
                "success":         True,
                "result_category": "IMPOSSIBLE",
                "error_type":      "EscalatedToHuman",
                "error_detail":    decision.reasoning[:300],
            }

        elif decision.action_type == ActionType.KILL_PROCESS:
            ok, err = self._kill_process(decision.target_process)
            result  = _result(ok, "ProcessKillFailed", err)

        elif decision.action_type == ActionType.ALERT_ONLY:
            logging.warning(
                f"[ALERT_ONLY] 조치 없음, 관찰만 기록: {decision.reasoning[:200]}"
            )
            result = _result(True, "")

        elif decision.action_type in (ActionType.EXECUTE_LLM_COMMAND,
                                      ActionType.EXECUTE_RULE_COMMAND):
            result = self._execute_llm_command(decision.command or "", original_error_log)

        else:
            logging.warning(f"수행 불가 액션: {decision.action_type}")
            result = _result(False, "UnknownActionType", str(decision.action_type))
            result["result_category"] = "IMPOSSIBLE"

        logging.info(
            f"===> [ActionExecutor] 완료 | 결과:{result['result_category']}"
            + (f" | {result['error_type']}" if result["error_type"] else "")
            + " <===\n"
        )
        return result

    # ── 보안 검증 ────────────────────────────────────────────────────────────
    def _validate_command(self, command: str) -> tuple[list[str], dict | None]:
        """
        4단계 보안 파이프라인으로 LLM 생성 명령어를 검증한다.

        검증 순서:
          1. shlex 파싱 → 인용 부호 트릭, 멀티라인 인젝션 차단
          2. 토큰 수 상한 → 과도하게 긴 명령어(잠재적 체이닝) 차단
          3. 메타문자 → 파이프·리다이렉트·서브쉘 등 모든 체이닝 차단
          4. 경로 포함 여부 → 절대/상대 경로로 화이트리스트 우회 차단
          5. 블랙리스트 → 파괴적·임의 실행 가능 명령어 차단
          6. 화이트리스트 → 허용 목록 외 명령어 전량 차단
          7. systemctl 서비스 이름 검증 → 경로 트래버설·플래그 인젝션 차단

        Returns:
            통과: (token_list, None)
            차단: ([], error_dict)
        """
        def _block(reason: str, detail: str) -> tuple[list, dict]:
            logging.error(f"  [Security Block] {reason}: {detail}")
            return [], {"success": False, "result_category": "FAILURE",
                        "error_type": "SecurityBlock", "error_detail": detail}

        # 1. shlex 파싱
        try:
            tokens = shlex.split(command)
        except ValueError as e:
            logging.error(f"  [Security Block] 셸 파싱 실패: {e}")
            return [], {"success": False, "result_category": "FAILURE",
                        "error_type": "ShellParseError", "error_detail": str(e)}

        if not tokens:
            return [], {"success": False, "result_category": "FAILURE",
                        "error_type": "EmptyCommand", "error_detail": "빈 토큰 목록"}

        # 2. 토큰 수 상한 — 허용 범위를 벗어나는 복잡한 명령은 잠재적 공격 시그니처
        if len(tokens) > _MAX_CMD_TOKENS:
            return _block(
                "토큰 수 초과",
                f"토큰 {len(tokens)}개 (최대 허용: {_MAX_CMD_TOKENS}개): {tokens}",
            )

        # 3. 쉘 메타문자 검사
        for token in tokens:
            if any(ch in _SHELL_METACHAR for ch in token):
                return _block("메타문자 감지", f"토큰 {token!r} 에 쉘 메타문자 포함")

        base_cmd = tokens[0]

        # 4. 경로 포함 명령어 — 화이트리스트 우회 방지
        if "/" in base_cmd or "\\" in base_cmd:
            return _block("경로 포함 명령어", f"경로 구분자가 포함된 명령어: {base_cmd!r}")

        # 5. 블랙리스트
        if base_cmd in self.BANNED_TOKENS:
            return _block("블랙리스트 명령어", base_cmd)

        # 6. 화이트리스트
        if base_cmd not in self.ALLOWED_COMMANDS:
            return _block("허용되지 않은 명령어", base_cmd)

        allowed_args = self.ALLOWED_COMMANDS[base_cmd]
        if allowed_args and len(tokens) >= 2:
            first_arg = tokens[1]
            if first_arg not in allowed_args:
                return _block(
                    "허용되지 않은 인자",
                    f"'{base_cmd}' 의 인자 {first_arg!r} 미허용. 허용: {sorted(allowed_args)}",
                )

        # 7. systemctl 서비스 이름 추가 검증.
        #    "systemctl restart ../etc/shadow" 같은 경로 트래버설 및
        #    "systemctl restart -f" 같은 플래그 인젝션을 명시적으로 차단한다.
        if base_cmd == "systemctl" and len(tokens) >= 3:
            svc = tokens[2]
            if not _PROCESS_NAME_RE.match(svc):
                return _block(
                    "서비스 이름 검증 실패",
                    f"systemctl 서비스 이름 {svc!r} 에 비허용 문자 포함",
                )

        return tokens, None

    # ── LLM 명령어 실행 ─────────────────────────────────────────────────
    def _execute_llm_command(self, command: str, error_log: str) -> dict:
        """
        검증·승인 파이프라인을 통해 LLM 생성 명령어를 실행한다.

        실행 모드:
          AUTO_APPROVE=true  → 검증 후 즉시 실행 (CI/테스트 환경)
          대화형 터미널      → stdin 승인 프롬프트
          데몬 모드          → Slack 승인 대기 (최대 _APPROVAL_TIMEOUT_SEC)
        """
        if not command:
            logging.warning("  [실행거부] 실행할 명령어가 없습니다.")
            return {"success": False, "result_category": "FAILURE",
                    "error_type": "EmptyCommand",
                    "error_detail": "AgentResponse.command가 비어있음"}

        tokens, err = self._validate_command(command)
        if err:
            return err

        auto_approve = os.getenv("AUTO_APPROVE", "false").lower() == "true"
        try:
            is_interactive = sys.stdin.isatty()
        except Exception:
            is_interactive = False

        if auto_approve:
            logging.info(f"  [AUTO_APPROVE] 자동 승인 모드. 커맨드: {command}")

        elif is_interactive:
            print("\n" + "=" * 50)
            print("[Human-in-the-Loop] 실행 대기 중인 명령어:", command)
            approval = input("이 명령어를 실행하시겠습니까? (y/n): ").strip().lower()
            if approval != "y":
                logging.warning("[관리자 거절] 조치가 취소되었습니다.")
                return {"success": False, "result_category": "FAILURE",
                        "error_type": "HumanRejected",
                        "error_detail": "관리자가 실행을 거절했습니다."}
            print("=" * 50 + "\n")

        else:
            # 데몬 모드 — Slack 승인 대기
            approval_store.init_table()
            token       = approval_store.create_request(command, error_log, "")
            base_url    = os.getenv("APPROVAL_BASE_URL", "http://localhost:8080")
            pending_url = f"{base_url}/pending/{token}"
            logging.warning(
                f"  [데몬 모드] 승인 대기 중 ({_APPROVAL_TIMEOUT_SEC}s): {command}\n"
                f"  확인 및 승인: {pending_url}"
            )
            try:
                chatops = get_chatops_client() or SlackChatOps()
                chatops.send_approval_request(
                    error_log=error_log,
                    command=command,
                    reason=f"🔐 명령어 확인 및 승인: {pending_url}",
                )
            except Exception:
                logging.error(f"  [ChatOps] 승인 요청 발송 실패:\n{traceback.format_exc()}")

            deadline = time.time() + _APPROVAL_TIMEOUT_SEC
            while time.time() < deadline:
                # SIGTERM 수신 시 승인 대기를 즉시 취소해 깨끗하게 종료한다.
                if _is_shutting_down():
                    logging.warning("  [종료 신호] 승인 대기 중 에이전트 종료 감지. 실행 취소.")
                    return _result(False, "ShutdownDuringApproval",
                                   "에이전트 종료 신호로 승인 대기 취소")
                time.sleep(_APPROVAL_POLL_INTERVAL)
                status = approval_store.get_status(token)
                if status == "approved":
                    logging.info("  [승인됨] Slack 승인 확인. 실행 진행.")
                    break
                if status == "rejected":
                    logging.warning("  [거절됨] Slack 거절 확인. 실행 취소.")
                    return {"success": False, "result_category": "FAILURE",
                            "error_type": "HumanRejected",
                            "error_detail": "Slack에서 관리자가 실행을 거절했습니다."}
            else:
                logging.warning(f"  [타임아웃] {_APPROVAL_TIMEOUT_SEC}s 내 응답 없음. 실행 취소.")
                return {"success": False, "result_category": "IMPOSSIBLE",
                        "error_type": "ApprovalTimeout",
                        "error_detail": f"{_APPROVAL_TIMEOUT_SEC}s 대기 후 응답 없음: {command}"}

        logging.info(f"  [조치 승인됨] 커맨드 실행: {command}")
        try:
            proc = subprocess.run(
                tokens,
                capture_output=True, text=True, shell=False, timeout=15,
            )
            if proc.returncode == 0:
                logging.info(f"  [실행성공] 결과: {proc.stdout.strip()}")
                return _result(True, "")
            detail = proc.stderr.strip() or f"returncode={proc.returncode}"
            logging.error(f"  [실행실패] {detail}")
            self._try_rollback(command)
            return _result(False, "CalledProcessError", detail)

        except subprocess.TimeoutExpired:
            logging.error(f"  [타임아웃]\n{traceback.format_exc()}")
            return _result(False, "TimeoutExpired", f"15초 초과: {command}")

        except PermissionError:
            logging.error(f"  [권한 거부]\n{traceback.format_exc()}")
            result = _result(False, "PermissionError", traceback.format_exc())
            result["result_category"] = "IMPOSSIBLE"
            return result

        except MemoryError:
            logging.error(f"  [메모리 부족]\n{traceback.format_exc()}")
            result = _result(False, "MemoryError", traceback.format_exc())
            result["result_category"] = "IMPOSSIBLE"
            return result

        except Exception as e:
            logging.error(f"  [실행실패]\n{traceback.format_exc()}")
            return _result(False, type(e).__name__, traceback.format_exc())

    # ── 롤백 ────────────────────────────────────────────────────────────
    def _try_rollback(self, failed_command: str) -> None:
        """실패한 명령어에 대응하는 롤백 명령어가 있으면 검증 후 실행한다."""
        try:
            tokens = shlex.split(failed_command)
        except (ValueError, IndexError):
            return

        base_cmd = tokens[0] if tokens else ""
        rollback = _ROLLBACK_MAP.get(base_cmd)
        if rollback is None:
            return

        if rollback == "__stop_service__":
            service = tokens[-1] if len(tokens) >= 3 else None
            if not service or service == base_cmd:
                return
            # 롤백 대상 서비스 이름도 _validate_process_name 으로 재검증
            if not _validate_process_name(service):
                logging.warning(f"  [롤백 차단] 서비스 이름 검증 실패: {service!r}")
                return
            rollback = f"systemctl stop {service}"

        rollback_tokens, err = self._validate_command(rollback)
        if err:
            logging.warning(f"  [롤백 차단] 롤백 명령어 보안 검증 실패: {rollback!r}")
            return

        logging.warning(f"  [롤백] '{failed_command}' 실패 → 롤백 실행: {rollback}")
        try:
            proc = subprocess.run(
                rollback_tokens, capture_output=True, text=True, shell=False, timeout=10,
            )
            if proc.returncode == 0:
                logging.info(f"  [롤백 성공] {proc.stdout.strip() or '완료'}")
            else:
                logging.error(f"  [롤백 실패] {proc.stderr.strip()}")
        except Exception:
            logging.error(f"  [롤백 오류]\n{traceback.format_exc()}")

    # ── 개별 조치 구현 ───────────────────────────────────────────────────
    def _clear_memory(self) -> tuple[bool, str | None]:
        """
        gc.collect()로 Python 힙을 정리하고, 가용 시 GPU VRAM 캐시를 초기화한다.

        Intel UMA 환경에서 VRAM과 RAM을 공유하므로, VRAM 캐시 해제가
        시스템 전체 가용 메모리 증가에 직접 기여한다.
        """
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
            logging.debug("  torch 미설치 — VRAM 초기화 생략.")
        except Exception:
            logging.error(f"  VRAM 초기화 중 오류:\n{traceback.format_exc()}")
        logging.warning("메모리 최적화 완료")
        return True, None

    def _verify_process_dead(self, target_name: str, wait_sec: float = 1.0) -> bool:
        """pkill 후 프로세스가 실제로 종료됐는지 pgrep -x 로 확인한다."""
        time.sleep(wait_sec)
        try:
            proc = subprocess.run(
                ["pgrep", "-x", target_name],
                capture_output=True, text=True, shell=False, timeout=5,
            )
            if proc.returncode != 0:
                logging.info(f"  [복구 검증 ✓] '{target_name}' 프로세스 종료 확인됨.")
                return True
            pids = proc.stdout.strip()
            logging.warning(f"  [복구 검증 ✗] '{target_name}' 여전히 실행 중 (PID: {pids}).")
            return False
        except Exception:
            logging.error(f"  [복구 검증 오류]\n{traceback.format_exc()}")
            return False

    def _verify_service_active(self, service_name: str, wait_sec: float = 2.0) -> bool:
        """systemctl restart 후 서비스가 실제로 active 상태인지 확인한다."""
        time.sleep(wait_sec)
        try:
            proc = subprocess.run(
                ["systemctl", "is-active", service_name],
                capture_output=True, text=True, shell=False, timeout=10,
            )
            status = proc.stdout.strip()
            if status == "active":
                logging.info(f"  [복구 검증 ✓] '{service_name}' 서비스 active 확인됨.")
                return True
            logging.warning(f"  [복구 검증 ✗] '{service_name}' 상태: {status}.")
            return False
        except Exception:
            logging.error(f"  [복구 검증 오류]\n{traceback.format_exc()}")
            return False

    def _kill_process(self, target: str | None) -> tuple[bool, str | None]:
        """
        지정 프로세스를 pkill -x 로 종료한다.

        보안:
          - _validate_process_name()으로 플래그 인젝션(-9 등) 및 비허용 문자를 차단.
          - '-x' 플래그: 이름이 정확히 일치하는 프로세스만 종료 (부분 매칭 방지).
            예) target="nginx" 일 때 "nginx-helper" 같은 다른 프로세스를 종료하지 않는다.
        """
        safe_name = _validate_process_name(target or "")
        if safe_name is None:
            msg = f"[Security Block] 프로세스 이름 검증 실패: {target!r}"
            logging.error(f"[조치] {msg}")
            return False, msg

        logging.warning(f"[조치] '{safe_name}' 프로세스 종료 시도...")
        try:
            proc = subprocess.run(
                ["pkill", "-x", safe_name],   # -x: 완전 일치만 허용
                capture_output=True, text=True, shell=False, timeout=10,
            )
            if proc.returncode == 0:
                logging.info(f"  '{safe_name}' 종료 신호 전송. 복구 검증 중...")
                ok = self._verify_process_dead(safe_name)
                return ok, (None if ok else f"'{safe_name}' 종료 후 프로세스가 여전히 실행 중")
            msg = f"'{safe_name}' 매칭 프로세스 없음 (이미 종료됐을 수 있음)"
            logging.warning(f"  {msg}.")
            return False, msg
        except subprocess.TimeoutExpired:
            msg = f"'{safe_name}' 종료 타임아웃 (10s 초과)"
            logging.error(f"  {msg}.")
            return False, msg
        except Exception:
            msg = traceback.format_exc()
            logging.error(f"  '{safe_name}' 종료 오류:\n{msg}")
            return False, msg

    def _restart_service(self, target: str | None) -> tuple[bool, str | None]:
        """
        systemctl restart 로 서비스를 재시작한다.

        보안: _validate_process_name()으로 플래그 인젝션 및 경로 트래버설을 차단한다.
        """
        safe_name = _validate_process_name(target or "")
        if safe_name is None:
            msg = f"[Security Block] 서비스 이름 검증 실패: {target!r}"
            logging.error(f"[조치] {msg}")
            return False, msg

        logging.warning(f"[조치] '{safe_name}' 서비스 재시작 중...")
        try:
            proc = subprocess.run(
                ["systemctl", "restart", safe_name],
                capture_output=True, text=True, shell=False, timeout=30,
            )
            if proc.returncode == 0:
                logging.info(f"  '{safe_name}' 재시작 신호 전송. 복구 검증 중...")
                ok = self._verify_service_active(safe_name)
                return ok, (None if ok else f"'{safe_name}' 재시작 후 active 상태 미확인")
            detail = proc.stderr.strip() or f"returncode={proc.returncode}"
            logging.error(f"'{safe_name}' 재시작 실패: {detail}")
            self._try_rollback(f"systemctl restart {safe_name}")
            return False, detail
        except subprocess.TimeoutExpired:
            msg = f"'{safe_name}' 재시작 타임아웃 (30s 초과)"
            logging.error(msg)
            self._try_rollback(f"systemctl restart {safe_name}")
            return False, msg
        except Exception:
            msg = traceback.format_exc()
            logging.error(f"'{safe_name}' 재시작 오류:\n{msg}")
            return False, msg

    def _escalate_to_human(self, reasoning: str) -> None:
        # Slack 알림은 AgentObserver.log_event()에서 일원화해서 발송.
        logging.error(f"[에스컬레이션] 관리자 개입 필요. 사유: {reasoning}")

    def _send_slack_alert(self, message: str, severity: str = "INFO") -> None:
        if not self.slack_webhook_url:
            return
        color_map = {
            "INFO":     "#36a64f",
            "WARNING":  "#ffcc00",
            "ERROR":    "#ff9900",
            "CRITICAL": "#ff0000",
        }
        payload = {
            "attachments": [{
                "color":     color_map.get(severity, "#cccccc"),
                "text":      message,
                "mrkdwn_in": ["text"],
            }]
        }
        try:
            requests.post(self.slack_webhook_url, json=payload, timeout=2)
        except Exception:
            logging.error(f"[Slack] 알람 전송 실패:\n{traceback.format_exc()}")
