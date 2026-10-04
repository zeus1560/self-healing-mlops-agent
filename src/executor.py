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

from src import approval_store, autonomy_store, server_config
from src.schemas import ActionType, AgentResponse, AutonomyLevel
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

# ── 보호 대상 (2026-10-04 화이트리스트 점검, RESEARCH_SUMMARY §6) ────────────────
# 종료·중지·재시작하면 원격 접속, 로깅, 컨테이너 런타임, 에이전트 자신이 죽는 대상.
# pkill/kill/fuser/systemctl 검증과 L1 구조화 액션(restart_service/kill_process —
# _validate_process_name)이 공통으로 쓴다. rsyslog는 재시작이 정상 조치(L1 시드 플레이북)라
# 넣지 않는다. 점검 당시 "pkill -f sshd",
# "systemctl stop sshd", "kill -TERM -1" 등이 전부 화이트리스트를 통과했다.
# 에이전트 자신의 서비스 이름은 하드코딩하지 않는다 — AGENT_SERVICE_NAME 또는
# /proc/self/cgroup 자동 감지(_agent_service_name). 추가 보호 대상은
# PROTECTED_PROCESSES(쉼표 구분)로 받는다.
_BASE_PROTECTED_NAMES = frozenset({
    "systemd", "init", "sshd", "ssh", "networking", "NetworkManager",
    "systemd-networkd", "systemd-resolved", "systemd-journald", "systemd-logind",
    "dbus", "dbus-daemon", "docker", "dockerd", "containerd",
    "python", "python3",
})
# pkill -f 패턴 하나가 이보다 많은 프로세스에 걸리면 범위가 너무 넓다고 보고 거부한다.
_PKILL_MAX_MATCHES = 5
_FUSER_PORT_RE = re.compile(r'^(\d{1,5})/(tcp|udp)$')
# journalctl vacuum 하한 — 이보다 작게 지우면 감사 기록·장애 로그가 사라진다.
_JOURNAL_MIN_VACUUM_BYTES = 500 * 1024 ** 2
_JOURNAL_MIN_VACUUM_SEC   = 7 * 86400
_SIZE_RE = re.compile(r'^(\d+(?:\.\d+)?)([KMGTkmgt]?)$')
_TIME_RE = re.compile(r'^(\d+)([A-Za-z]*)$')
_TIME_UNIT_SEC = {
    "": 1, "s": 1, "sec": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "week": 604800, "weeks": 604800,
    "M": 2629800, "month": 2629800, "months": 2629800,
    "y": 31557600, "year": 31557600, "years": 31557600,
}


def _agent_service_name() -> str | None:
    """에이전트 자신의 systemd 서비스 이름 — AGENT_SERVICE_NAME, 없으면 cgroup에서 감지."""
    env = os.getenv("AGENT_SERVICE_NAME", "").strip()
    if env:
        return env.removesuffix(".service")
    try:
        with open("/proc/self/cgroup", encoding="utf-8") as f:
            for line in f:
                m = re.search(r"/([^/]+)\.service$", line.strip())
                if m:
                    return m.group(1)
    except OSError:
        pass
    return None


def _is_protected_name(name: str) -> bool:
    """프로세스/서비스 이름이 보호 대상인지 판별한다(.service 접미사 무시)."""
    name = name.removesuffix(".service")
    extra = {n.strip() for n in os.getenv("PROTECTED_PROCESSES", "").split(",") if n.strip()}
    agent = _agent_service_name()
    return (
        name in _BASE_PROTECTED_NAMES or name in extra
        or (agent is not None and name == agent)
        or name.startswith("systemd-") or name.startswith("python")
    )


def _read_proc_comm(pid: int) -> str | None:
    """/proc/<pid>/comm(프로세스 이름)을 읽는다. 없거나 못 읽으면 None."""
    try:
        with open(f"/proc/{pid}/comm", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def _protected_pid_reason(pid: int) -> str | None:
    """PID가 종료하면 안 되는 대상이면 그 이유를, 아니면 None을 반환한다."""
    if pid in (os.getpid(), os.getppid()):
        return f"PID {pid}는 에이전트 자신(또는 부모 프로세스)"
    comm = _read_proc_comm(pid)
    if comm is not None and _is_protected_name(comm):
        return f"PID {pid}({comm})는 보호 대상 프로세스"
    return None


def _list_pids(cmd: list[str]) -> list[int] | None:
    """pgrep/fuser 출력에서 PID 목록을 뽑는다. 매칭 없음은 [], 확인 불가는 None."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, shell=False, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode not in (0, 1):
        return None
    return [int(t) for t in re.findall(r"\d+", proc.stdout)]


def _parse_journal_size(value: str) -> int | None:
    m = _SIZE_RE.match(value)
    if not m:
        return None
    power = " KMGT".index(m.group(2).upper() or " ")
    return int(float(m.group(1)) * 1024 ** power)


def _parse_journal_time(value: str) -> int | None:
    m = _TIME_RE.match(value)
    if not m or m.group(2) not in _TIME_UNIT_SEC:
        return None
    return int(m.group(1)) * _TIME_UNIT_SEC[m.group(2)]

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


def _describe_action(decision: AgentResponse) -> str:
    """
    승인 요청 메시지에 표시할 사람이 읽을 수 있는 조치 설명을 만든다.

    LLM/Rule 경로는 이미 구체적인 셸 명령어(decision.command)가 있으므로 그대로 쓰고,
    L1 캐시가 반환하는 구조화된 액션(RESTART_SERVICE 등)은 target_process를 붙여
    "restart_service(nginx)" 형태로 표시한다.
    """
    if decision.command:
        return decision.command
    if decision.target_process:
        return f"{decision.action_type.value}({decision.target_process})"
    return decision.action_type.value


def _compose_explanation(decision: AgentResponse) -> str:
    """
    승인 요청 메시지의 "설명" 섹션에 넣을 판단 근거를 만든다(Explainability,
    2026-09-11 추가). reasoning(L2 self-reflection 판정 등)과 l1_evidence(L1 앙상블
    투표가 실제로 근거 삼은 과거 사건들, src/llm_engine.py::_format_evidence)를
    둘 다 있으면 합쳐서, 승인/거절하는 사람이 "왜 이 조치를 제안했는지"를 화면
    하나에서 검증할 수 있게 한다.
    """
    parts = [p for p in (decision.reasoning, decision.l1_evidence) if p]
    return "\n\n".join(parts)


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
    # 2026-10-04: 자유형식 명령과 같은 보호 목록을 구조화 액션(restart_service/
    # kill_process, L1 플레이북·L2 진단-라우팅)에도 적용한다 — L1 구조화 액션은
    # self-reflection을 생략하므로 이 검사가 대상에 대한 마지막 확인이다.
    if _is_protected_name(name):
        logging.error(f"  [Security Block] 보호 대상 프로세스/서비스: {name!r}")
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
        autonomy_store.init_table()

        # TARGET_SERVER 미설정 시 config/servers.yaml의 첫 서버(gcp-primary, exec_method:
        # systemd)로 귀결 — 기존 배포(VM) 동작은 완전히 무변경.
        server = server_config.get_server(os.getenv("TARGET_SERVER"))
        self.exec_method: str = server.get("exec_method", "systemd")
        self.k8s_namespace: str = server.get("k8s_namespace", "default")
        self.k8s_target_app: str | None = server.get("k8s_target_app")
        self.docker_target_app: str | None = server.get("docker_target_app")

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
            "nginx":      {"-s", "-t"},                # 실제 허용 형태는 아래 6.6 참고
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

        result = self._dispatch(decision, original_error_log)

        logging.info(
            f"===> [ActionExecutor] 완료 | 결과:{result['result_category']}"
            + (f" | {result['error_type']}" if result["error_type"] else "")
            + " <===\n"
        )
        return result

    # ── Progressive Autonomy 게이트 + 액션 분기 ─────────────────────────────────
    def _dispatch(self, decision: AgentResponse, original_error_log: str) -> dict:
        """
        카테고리별 Autonomy 레벨을 확인한 뒤 실제 액션 분기로 넘긴다.

        ESCALATE_TO_HUMAN/ALERT_ONLY는 이미 그 자체로 "조치 없음/사람에게 위임"이므로
        게이트 대상에서 제외한다.
        """
        _ungated = (ActionType.ESCALATE_TO_HUMAN, ActionType.ALERT_ONLY)
        if decision.action_type not in _ungated:
            level = self._effective_level(decision)

            if level == AutonomyLevel.READ_ONLY:
                return self._observed_only(decision)

            if level == AutonomyLevel.PROPOSE:
                return self._propose_only(decision, original_error_log)

            if (level == AutonomyLevel.APPROVE_THEN_EXECUTE
                    and decision.action_type not in (ActionType.EXECUTE_LLM_COMMAND,
                                                      ActionType.EXECUTE_RULE_COMMAND)):
                outcome = self._await_approval(
                    _describe_action(decision), original_error_log,
                    explanation=_compose_explanation(decision),
                )
                if outcome != "approved":
                    return self._approval_failure_result(outcome)
            # level == AUTO, 또는 APPROVE_THEN_EXECUTE + LLM/Rule 커맨드.
            # 승인 게이트는 여기서 처리하지 않음 — 실제 검증은 _execute_llm_command() 참조
            # (명령어 자체가 여기선 아직 없어 command 인자로 _await_approval을 못 부름).
            # → 그대로 통과해서 실제 액션 분기로 진행

        if decision.action_type == ActionType.CLEAR_MEMORY:
            ok, err = self._clear_memory()
            return _result(ok, "MemoryClearFailed", err)

        elif decision.action_type == ActionType.RESTART_SERVICE:
            ok, err = self._restart_service(decision.target_process)
            return _result(ok, "ServiceRestartFailed", err)

        elif decision.action_type == ActionType.ESCALATE_TO_HUMAN:
            self._escalate_to_human(decision.reasoning)
            return {
                "success":         True,
                "result_category": "IMPOSSIBLE",
                "error_type":      "EscalatedToHuman",
                "error_detail":    decision.reasoning[:300],
            }

        elif decision.action_type == ActionType.KILL_PROCESS:
            ok, err = self._kill_process(decision.target_process)
            return _result(ok, "ProcessKillFailed", err)

        elif decision.action_type == ActionType.ALERT_ONLY:
            logging.warning(
                f"[ALERT_ONLY] 조치 없음, 관찰만 기록: {decision.reasoning[:200]}"
            )
            return _result(True, "")

        elif decision.action_type in (ActionType.EXECUTE_LLM_COMMAND,
                                      ActionType.EXECUTE_RULE_COMMAND):
            return self._execute_llm_command(decision, original_error_log)

        else:
            logging.warning(f"수행 불가 액션: {decision.action_type}")
            result = _result(False, "UnknownActionType", str(decision.action_type))
            result["result_category"] = "IMPOSSIBLE"
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

        # 6.4. kill로 PID 1(컨테이너 자기 자신/init) 지정 차단.
        #    2026-09-10 adversarial testing 확장 중 발견: src/llm_engine.py의
        #    _is_self_destructive_kill()이 "executor.py 화이트리스트가 최종
        #    방어선"이라고 문서화해뒀는데, 실제로는 "kill -TERM 1"/"kill -HUP 1"이
        #    여기 화이트리스트만으로는 그대로 통과했음(-9는 이미 막혀있었지만
        #    허용된 -TERM/-HUP로는 못 막았음). self-reflection의 판정은 자문
        #    성격(2026-09-05 결정, 노이즈가 커서 강제 차단 안 함)이라 이 계층이
        #    실제 최종 방어선이 되도록 동일 로직을 여기에도 둔다.
        #    문자열 "1" 정확 일치만으로는 "001"/"+1"처럼 실제 kill(1) 유틸리티가
        #    똑같이 PID 1로 파싱하는 변형을 놓친다(직접 실측으로 확인) — 정수로
        #    파싱해 값 자체를 비교한다.
        #
        #    2026-10-04 화이트리스트 점검: 첫 인자만 보던 탓에 "kill -TERM -1"(모든
        #    프로세스)/"kill -HUP 0"(자기 프로세스 그룹)/"kill -TERM -- -1"이 통과했다.
        #    "kill -TERM|-HUP <PID>" 3토큰, PID는 2 이상의 정수만 허용하고, 실행 직전
        #    /proc/<PID>/comm으로 보호 대상(sshd 등)·에이전트 자신이면 거부한다.
        #    프로세스를 확인할 수 없으면(이미 없음 등) 보수적으로 거부한다.
        if base_cmd == "kill":
            if len(tokens) != 3 or not tokens[2].isdigit():
                return _block("kill 형식 이상", f"'kill -TERM|-HUP <PID>' 형식만 허용: {tokens}")
            pid = int(tokens[2])
            if pid <= 1:
                return _block("PID 1 이하 대상 kill 차단", f"자기 자신/init 대상 지정: {tokens}")
            reason = _protected_pid_reason(pid)
            if reason:
                return _block("보호 대상 kill 차단", reason)
            if _read_proc_comm(pid) is None:
                return _block("kill 대상 확인 불가", f"PID {pid} 프로세스를 확인할 수 없음")

        # 6.4.1 pkill — 2026-10-04 점검: "pkill -f python"(에이전트 자신), "pkill -f ."
        #    (전체 매칭), "pkill -f -9 nginx"(뒤쪽 SIGKILL 플래그)가 통과했다.
        #    "pkill -x|-f <이름>" 3토큰만 허용하고 이름은 _PROCESS_NAME_RE + 보호 목록으로
        #    검사한다. -f는 정규식이 명령줄 전체에 걸리므로 실행 직전 pgrep -f로 실제
        #    매칭 프로세스를 확인해 보호 대상·에이전트 자신이 끼거나 너무 많으면 거부한다.
        if base_cmd == "pkill":
            if len(tokens) != 3:
                return _block("pkill 형식 이상", f"'pkill -x|-f <이름>' 3토큰만 허용: {tokens}")
            pattern = tokens[2]
            if not _PROCESS_NAME_RE.match(pattern):
                return _block("pkill 대상 검증 실패", f"대상 {pattern!r} 에 비허용 문자 포함")
            if _is_protected_name(pattern):
                return _block("보호 대상 pkill 차단", f"{pattern!r}는 보호 대상")
            if tokens[1] == "-f":
                pids = _list_pids(["pgrep", "-f", pattern])
                if pids is None:
                    return _block("pkill 대상 확인 불가", f"pgrep -f {pattern!r} 실행 실패")
                if len(pids) > _PKILL_MAX_MATCHES:
                    return _block(
                        "pkill 매칭 범위 과다",
                        f"pgrep -f {pattern!r} 매칭 {len(pids)}개 (최대 {_PKILL_MAX_MATCHES}개)",
                    )
                for p in pids:
                    reason = _protected_pid_reason(p)
                    if reason:
                        return _block("보호 대상 pkill 차단", reason)

        # 6.4.2 fuser — 2026-10-04 점검: 인자 제한이 없어 "fuser -k /var/lib/postgresql/data"
        #    (파일을 연 프로세스 전부), "fuser -k -m /"(루트 파일시스템 사용 프로세스 전부),
        #    "fuser -k -9 5000/tcp"가 통과했다. "fuser -k <1~65535>/tcp|udp" 3토큰만
        #    허용하고, 그 포트를 쓰는 프로세스에 보호 대상(예: 22/tcp의 sshd)이 있으면 거부한다.
        if base_cmd == "fuser":
            m = _FUSER_PORT_RE.match(tokens[2]) if len(tokens) == 3 else None
            if tokens[1:2] != ["-k"] or m is None or not 1 <= int(m.group(1)) <= 65535:
                return _block("fuser 형식 이상", f"'fuser -k <포트>/tcp|udp' 형식만 허용: {tokens}")
            pids = _list_pids(["fuser", tokens[2]])
            if pids is None:
                return _block("fuser 대상 확인 불가", f"fuser {tokens[2]} 실행 실패")
            for p in pids:
                reason = _protected_pid_reason(p)
                if reason:
                    return _block("보호 대상 fuser 차단", reason)

        # 6.4.3 nginx — 2026-10-04 점검: "nginx -s stop/quit", "nginx -s reload -c <임의 설정>"이
        #    통과했다. 설정 리로드와 설정 검사 두 형태만 허용한다.
        if base_cmd == "nginx" and tokens not in (["nginx", "-s", "reload"], ["nginx", "-t"]):
            return _block("nginx 형식 이상", f"'nginx -s reload' / 'nginx -t'만 허용: {tokens}")

        # 6.4.4 journalctl vacuum — 2026-10-04 점검: "journalctl --vacuum-size 1"이 통과해
        #    시스템 로그(감사 기록 포함)를 통째로 지울 수 있었다. 인자 없는 조회 또는
        #    "--vacuum-size <500M 이상>" / "--vacuum-time <7d 이상>" 3토큰만 허용한다
        #    (--vacuum-files 등 다른 옵션은 6번 첫 인자 검사에서 이미 거부됨).
        if base_cmd == "journalctl" and len(tokens) > 1:
            if len(tokens) != 3:
                return _block("journalctl 형식 이상", f"'journalctl --vacuum-size|--vacuum-time <값>'만 허용: {tokens}")
            if tokens[1] == "--vacuum-size":
                size = _parse_journal_size(tokens[2])
                if size is None or size < _JOURNAL_MIN_VACUUM_BYTES:
                    return _block("journalctl vacuum 하한 미달", f"--vacuum-size {tokens[2]!r} (최소 500M)")
            else:
                sec = _parse_journal_time(tokens[2])
                if sec is None or sec < _JOURNAL_MIN_VACUUM_SEC:
                    return _block("journalctl vacuum 하한 미달", f"--vacuum-time {tokens[2]!r} (최소 7d)")

        # 6.5. ss -K/--kill 명시적 차단.
        #    2026-09-10 adversarial testing 확장 중 발견: `ss`는 읽기 전용 진단
        #    도구로 보고 인자 제한 없음(빈 set)으로 등록했었는데, 실제로는
        #    -K/--kill(매칭되는 소켓을 강제로 닫음)이라는 파괴적 플래그가 있어
        #    "ss -K dst 0.0.0.0/0" 같은 명령이 그대로 통과했음(실측 확인). 다른
        #    화이트리스트 인자 검증처럼 첫 토큰만 보면 뒤쪽 위치의 -K는 못 잡으므로
        #    전체 토큰을 스캔한다.
        if base_cmd == "ss" and any(t in ("-K", "--kill") for t in tokens[1:]):
            return _block("ss 파괴적 플래그 차단", f"-K/--kill 플래그 감지: {tokens}")

        # 7. systemctl 서비스 이름 추가 검증.
        #    "systemctl restart ../etc/shadow" 같은 경로 트래버설 및
        #    "systemctl restart -f" 같은 플래그 인젝션을 명시적으로 차단한다.
        #
        #    2026-09-10 adversarial testing 확장 중 발견: 기존엔 len(tokens) >= 3
        #    만 보고 tokens[2](서비스 이름)만 검증해서, "systemctl restart nginx
        #    --now --force EXTRA" 같은 토큰 4개 이상짜리가 뒤쪽 토큰은 전혀
        #    검증되지 않은 채 그대로 통과해 subprocess.run에 넘어갔음(실측 확인,
        #    err=None). 이 시스템의 정당한 사용 형태는 항상 "systemctl <verb>
        #    <service>" 3토큰뿐이라 그 외엔 무조건 차단한다.
        if base_cmd == "systemctl":
            if len(tokens) != 3:
                return _block(
                    "systemctl 토큰 개수 이상",
                    f"'systemctl <verb> <service>' 3토큰만 허용, 실제 {len(tokens)}개: {tokens}",
                )
            svc = tokens[2]
            if not _PROCESS_NAME_RE.match(svc):
                return _block(
                    "서비스 이름 검증 실패",
                    f"systemctl 서비스 이름 {svc!r} 에 비허용 문자 포함",
                )
            # 2026-10-04 점검: "systemctl stop sshd" / "systemctl stop <에이전트 자신>"이
            # 통과했다. 보호 대상 서비스는 status 외 동작을 거부한다(서비스 허용 목록은
            # §6 후속 과제).
            if tokens[1] != "status" and _is_protected_name(svc):
                return _block("보호 대상 서비스 차단", f"systemctl {tokens[1]} {svc!r} — 보호 대상")

        return tokens, None

    # ── Progressive Autonomy 헬퍼 ────────────────────────────────────────
    def _effective_level(self, decision: AgentResponse) -> AutonomyLevel:
        """카테고리 레벨에 self-reflection 결과를 반영한 실제 적용 레벨.

        2026-10-04 결정: auto 레벨인데 self-reflection이 NO(self_reflection_safe=False)면
        자동 실행하지 않고 approve_then_execute로 내린다 — auto엔 승인 화면이 없어 경고가
        아무 역할을 못 하기 때문. 승인 레벨에서는 9/05 결정(NO여도 차단하지 않고 경고만)을
        그대로 유지한다. 검토를 시도했지만 실패한 경우(self_reflection_error, 2026-10-04)도
        NO와 똑같이 승인으로 내린다 — 장애가 몰려 레이트리밋이 걸릴 때 검토 없이 auto로
        실행되던 구멍. 설계상 검토를 생략한 응답(error=False, safe=True/None)은 영향 없음.
        자유형식·구조화 경로 공통.
        """
        level = autonomy_store.get_level(decision.error_category)
        if level == AutonomyLevel.AUTO and (
            decision.self_reflection_safe is False or decision.self_reflection_error
        ):
            reason = "검토 실패" if decision.self_reflection_error else "위험 판정"
            logging.warning(
                f"[Autonomy] '{decision.error_category}'는 auto지만 자가 반성 {reason} — "
                f"자동 실행하지 않고 사람 승인으로 전환합니다."
            )
            return AutonomyLevel.APPROVE_THEN_EXECUTE
        return level


    def _observed_only(self, decision: AgentResponse) -> dict:
        """READ_ONLY 레벨: 조치를 실행하지 않고 관찰 기록만 남긴다."""
        logging.info(
            f"[READ_ONLY] '{decision.error_category}' 카테고리 — 조치 미실행, 관찰만 기록: "
            f"{_describe_action(decision)}"
        )
        return {
            "success":         True,
            "result_category": "OBSERVED_ONLY",
            "error_type":      None,
            "error_detail":    decision.reasoning[:300],
        }

    def _propose_only(self, decision: AgentResponse, error_log: str) -> dict:
        """PROPOSE 레벨: 조치를 실행하지 않고 제안 알림만 보낸다."""
        description = _describe_action(decision)
        logging.info(f"[PROPOSE] '{decision.error_category}' 카테고리 — 제안만 발송: {description}")
        try:
            (get_chatops_client() or SlackChatOps()).send_notification(
                title="🔎 [Self-Healing Agent] 제안된 조치 (미실행)",
                message=(
                    f"*카테고리*: {decision.error_category}\n"
                    f"*제안된 조치*: `{description}`\n"
                    f"*근거*: {decision.reasoning[:300]}\n\n"
                    f"이 카테고리는 아직 '제안' 단계라 자동/승인 실행되지 않습니다."
                ),
            )
        except Exception:
            logging.error(f"  [알림] 제안 발송 실패:\n{traceback.format_exc()}")
        return {
            "success":         True,
            "result_category": "PROPOSED_ONLY",
            "error_type":      None,
            "error_detail":    decision.reasoning[:300],
        }

    def _approval_failure_result(self, outcome: str) -> dict:
        """_await_approval()의 비승인 결과를 표준 결과 딕셔너리로 변환한다."""
        if outcome == "shutdown":
            return _result(False, "ShutdownDuringApproval", "에이전트 종료 신호로 승인 대기 취소")
        if outcome == "timeout":
            return {"success": False, "result_category": "IMPOSSIBLE",
                    "error_type": "ApprovalTimeout",
                    "error_detail": f"{_APPROVAL_TIMEOUT_SEC}s 대기 후 응답 없음"}
        return {"success": False, "result_category": "FAILURE",
                "error_type": "HumanRejected",
                "error_detail": "관리자가 실행을 거절했습니다."}

    def _await_approval(
        self, description: str, error_log: str, explanation: str = ""
    ) -> str:
        """
        Human-in-the-Loop 승인을 기다린다. LLM 커맨드·구조화된 액션 양쪽에서 공유한다.

        실행 모드:
          AUTO_APPROVE=true  → 즉시 승인 (CI/테스트 환경)
          대화형 터미널      → stdin 승인 프롬프트
          데몬 모드          → Slack 승인 대기 (최대 _APPROVAL_TIMEOUT_SEC)

        explanation: 승인 화면에 보여줄 판단 근거(_compose_explanation 참고,
            Explainability 2026-09-11 추가) — 2026-09-10까지는 이 정보가 승인
            메시지에 전혀 안 들어가고 있었음(reason 파라미터가 토큰 URL 전용이라
            "설명" 섹션에 URL만 보였음, 실제 근거는 명령어 실행 후 로그를 봐야만
            확인 가능했음) — 이제 사람이 승인/거절하는 그 화면에서 바로 보인다.

        Returns: "approved" | "rejected" | "timeout" | "shutdown"
        """
        auto_approve = os.getenv("AUTO_APPROVE", "false").lower() == "true"
        try:
            is_interactive = sys.stdin.isatty()
        except Exception:
            is_interactive = False

        if auto_approve:
            logging.info(f"  [AUTO_APPROVE] 자동 승인 모드. 조치: {description}")
            return "approved"

        if is_interactive:
            print("\n" + "=" * 50)
            print("[Human-in-the-Loop] 실행 대기 중인 조치:", description)
            if explanation:
                print("근거:", explanation)
            approval = input("이 조치를 실행하시겠습니까? (y/n): ").strip().lower()
            print("=" * 50 + "\n")
            if approval != "y":
                logging.warning("[관리자 거절] 조치가 취소되었습니다.")
                return "rejected"
            return "approved"

        # 데몬 모드 — Slack 승인 대기
        approval_store.init_table()
        # explanation(_compose_explanation 결과)을 Telegram/Slack 메시지엔 그대로
        # 넣으면서 DB엔 빈 문자열("")로 저장하던 버그 — 승인 당시엔 사람이 근거를
        # 봤지만 나중에 pending_approvals를 다시 조회하면 "왜"가 사라져 있었다
        # (2026-09-17 실측으로 발견: 운영 DB의 reason 컬럼이 전부 빈 값이었음).
        token       = approval_store.create_request(description, error_log, explanation)
        base_url    = os.getenv("APPROVAL_BASE_URL", "http://localhost:8080")
        pending_url = f"{base_url}/pending/{token}"
        logging.warning(
            f"  [데몬 모드] 승인 대기 중 ({_APPROVAL_TIMEOUT_SEC}s): {description}\n"
            f"  확인 및 승인: {pending_url}"
        )
        try:
            chatops = get_chatops_client() or SlackChatOps()
            chatops.send_approval_request(
                error_log=error_log,
                command=description,
                reason=f"🔐 명령어 확인 및 승인: {pending_url}",
                explanation=explanation,
            )
        except Exception:
            logging.error(f"  [ChatOps] 승인 요청 발송 실패:\n{traceback.format_exc()}")

        deadline = time.time() + _APPROVAL_TIMEOUT_SEC
        while time.time() < deadline:
            # SIGTERM 수신 시 승인 대기를 즉시 취소해 깨끗하게 종료한다.
            if _is_shutting_down():
                logging.warning("  [종료 신호] 승인 대기 중 에이전트 종료 감지. 실행 취소.")
                approval_store.mark_expired(token, "shutdown")
                return "shutdown"
            time.sleep(_APPROVAL_POLL_INTERVAL)
            status = approval_store.get_status(token)
            if status == "approved":
                logging.info("  [승인됨] 승인 확인. 실행 진행.")
                return "approved"
            if status == "rejected":
                logging.warning("  [거절됨] 거절 확인. 실행 취소.")
                return "rejected"
        logging.warning(f"  [타임아웃] {_APPROVAL_TIMEOUT_SEC}s 내 응답 없음. 실행 취소.")
        # 행을 expired로 표시 — 이후 늦게 들어온 승인은 거부된다(토큰 유효 10분 > 대기 5분).
        approval_store.mark_expired(token, "timeout")
        return "timeout"

    # ── LLM 명령어 실행 ─────────────────────────────────────────────────
    def _execute_llm_command(self, decision: AgentResponse, error_log: str) -> dict:
        """
        검증·승인 파이프라인을 통해 LLM 생성 명령어를 실행한다.

        카테고리가 AUTO까지 승급된 경우에만 승인 없이 즉시 실행하고,
        그 외(APPROVE_THEN_EXECUTE)에는 항상 _await_approval()을 거친다.
        """
        command = decision.command or ""
        if not command:
            logging.warning("  [실행거부] 실행할 명령어가 없습니다.")
            return {"success": False, "result_category": "FAILURE",
                    "error_type": "EmptyCommand",
                    "error_detail": "AgentResponse.command가 비어있음"}

        tokens, err = self._validate_command(command)
        if err:
            return err

        level = self._effective_level(decision)
        if level != AutonomyLevel.AUTO:
            # reasoning(자가 반성 판정 등)은 이제 description(명령어)에 억지로 끼워
            # 넣지 않고 explanation으로 따로 전달한다 — "설명" 섹션에 제대로 보임
            # (2026-09-11, 예전엔 reason 파라미터가 승인 링크 URL 전용이라 이 정보가
            # 승인 화면 어디에도 안 보이고 있었음).
            outcome = self._await_approval(
                command, error_log, explanation=_compose_explanation(decision)
            )
            if outcome != "approved":
                return self._approval_failure_result(outcome)

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
        if self.exec_method == "k8s":
            target = self._resolve_k8s_target_name(target)
        elif (self.exec_method == "systemd" and self.docker_target_app
                and not self._systemd_unit_exists(target)):
            return self._kill_container_docker(self.docker_target_app)

        safe_name = _validate_process_name(target or "")
        if safe_name is None:
            msg = f"[Security Block] 프로세스 이름 검증 실패: {target!r}"
            logging.error(f"[조치] {msg}")
            return False, msg

        if self.exec_method == "k8s":
            return self._kill_pod_k8s(safe_name)

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
        if self.exec_method == "k8s":
            target = self._resolve_k8s_target_name(target)
        elif (self.exec_method == "systemd" and self.docker_target_app
                and not self._systemd_unit_exists(target)):
            return self._restart_container_docker(self.docker_target_app)

        safe_name = _validate_process_name(target or "")
        if safe_name is None:
            msg = f"[Security Block] 서비스 이름 검증 실패: {target!r}"
            logging.error(f"[조치] {msg}")
            return False, msg

        if self.exec_method == "k8s":
            return self._restart_deployment_k8s(safe_name)

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

    # ── systemd → docker 폴백 (exec_method: systemd 전용) ────────────────────
    # 2026-09-10 VM 실측으로 발견: target-app은 systemd 유닛이 아니라 docker-compose
    # 컨테이너라 target_process가 실제 systemd 유닛이 아니면 systemctl은 절대
    # target-app을 고칠 수 없다(예: L1 캐시가 준 "rsyslog" — 실존하는 무관한
    # 서비스를 조용히 "성공"으로 재시작함, AUTO 승급된 6개 카테고리엔 사람이 걸러줄
    # 기회도 없어 더 위험). target_process가 실제 systemd 유닛이면 기존 경로 그대로,
    # 아니면 servers.yaml의 docker_target_app(docker-compose의 실제 컨테이너, k8s의
    # k8s_target_app과 동일한 역할)로 대체한다.

    def _systemd_unit_exists(self, name: str | None) -> bool:
        """이름이 실제 존재하는(LoadState=loaded) systemd 유닛인지 확인한다."""
        safe = _validate_process_name(name or "")
        if safe is None:
            return False
        try:
            proc = subprocess.run(
                ["systemctl", "show", safe, "--property=LoadState", "--value"],
                capture_output=True, text=True, shell=False, timeout=10,
            )
            return proc.returncode == 0 and proc.stdout.strip() == "loaded"
        except Exception:
            logging.error(f"  [systemd 유닛 확인 오류]\n{traceback.format_exc()}")
            return False

    def _verify_container_running(self, name: str, wait_sec: float = 2.0) -> bool:
        """kill/restart 후 docker 컨테이너가 실제로 Running 상태인지 확인한다."""
        time.sleep(wait_sec)
        try:
            proc = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Running}}", name],
                capture_output=True, text=True, shell=False, timeout=10,
            )
            if proc.returncode == 0 and proc.stdout.strip() == "true":
                logging.info(f"  [복구 검증 ✓] 컨테이너 '{name}' Running 확인됨.")
                return True
            logging.warning(f"  [복구 검증 ✗] 컨테이너 '{name}' Running 아님: {proc.stdout.strip()}")
            return False
        except Exception:
            logging.error(f"  [복구 검증 오류]\n{traceback.format_exc()}")
            return False

    def _restart_container_docker(self, name: str) -> tuple[bool, str | None]:
        """docker restart로 컨테이너를 재시작한다 (systemd 유닛이 아닌 target_process의 대체 경로)."""
        logging.warning(f"[조치] 컨테이너 '{name}' 재시작 중 (docker restart)...")
        try:
            proc = subprocess.run(
                ["docker", "restart", name],
                capture_output=True, text=True, shell=False, timeout=30,
            )
            if proc.returncode == 0:
                ok = self._verify_container_running(name)
                return ok, (None if ok else f"컨테이너 '{name}' 재시작 후 Running 미확인")
            detail = proc.stderr.strip() or f"returncode={proc.returncode}"
            logging.error(f"컨테이너 '{name}' 재시작 실패: {detail}")
            return False, detail
        except subprocess.TimeoutExpired:
            msg = f"컨테이너 '{name}' 재시작 타임아웃 (30s 초과)"
            logging.error(msg)
            return False, msg
        except Exception:
            msg = traceback.format_exc()
            logging.error(f"컨테이너 '{name}' 재시작 오류:\n{msg}")
            return False, msg

    def _kill_container_docker(self, name: str) -> tuple[bool, str | None]:
        """
        docker kill로 컨테이너를 종료한다.

        docker-compose의 restart: unless-stopped 정책이 자동 재기동시켜주므로
        (k8s Deployment의 파드 재생성과 동일한 원리) kill 자체엔 롤백이 필요
        없다 — pkill/_kill_pod_k8s와 동일 패턴.
        """
        logging.warning(f"[조치] 컨테이너 '{name}' 종료 시도 (docker kill)...")
        try:
            proc = subprocess.run(
                ["docker", "kill", name],
                capture_output=True, text=True, shell=False, timeout=15,
            )
            if proc.returncode == 0:
                ok = self._verify_container_running(name)
                return ok, (None if ok else f"컨테이너 '{name}' 종료 후 재기동 미확인")
            msg = f"컨테이너 '{name}' 종료 실패: {proc.stderr.strip()}"
            logging.warning(f"  {msg}.")
            return False, msg
        except subprocess.TimeoutExpired:
            msg = f"컨테이너 '{name}' 종료 타임아웃 (15s 초과)"
            logging.error(f"  {msg}.")
            return False, msg
        except Exception:
            msg = traceback.format_exc()
            logging.error(f"  컨테이너 '{name}' 종료 오류:\n{msg}")
            return False, msg

    # ── k8s 실행 경로 (exec_method: k8s) ─────────────────────────────────────
    # RESTART_SERVICE/KILL_PROCESS 두 구조화 액션만 지원. LLM이 자유형식으로
    # 생성하는 kubectl 명령(EXECUTE_LLM_COMMAND)은 이번 라운드 범위 밖 —
    # 화이트리스트(ALLOWED_COMMANDS)가 "명령어당 첫 번째 인자만 검증"하는 구조라
    # `kubectl rollout restart` 같은 다중 서브커맨드에 안 맞아 별도 재설계가 필요함.

    def _resolve_k8s_target_name(self, target: str | None) -> str | None:
        """
        target_process가 실제 존재하는 k8s Deployment 이름인지 확인하고, 아니면
        이 서버의 유일한 관리 대상(servers.yaml의 k8s_target_app)으로 대체한다.

        배경(2026-09-10 로컬 minikube 실측): L1 캐시의 target_process는 systemd
        시절 설계라 k8s 리소스명과 항상 일치하지 않음 — Memory_Leak 카테고리는
        None, Process_Crash 카테고리는 범용 placeholder("pod")로 나와 그대로 쓰면
        보안 검증에 막히거나(None) 존재하지 않는 리소스를 대상으로 kubectl이
        실패한다("pod"). LLM/L1이 준 값을 맹목적으로 신뢰하지 않고 실제 존재
        여부를 확인한 뒤에만 그대로 쓴다 — 존재 확인 없이 신뢰하는 게 이번에
        실제로 문제를 일으켰던 지점이라 검증을 생략하지 않는다.
        """
        if not self.k8s_target_app:
            return target  # 폴백 미설정 — 기존 동작(None이면 아래서 보안 차단) 유지

        candidate = target or self.k8s_target_app
        if candidate == self.k8s_target_app:
            return candidate  # 이미 폴백 값 — 재확인 불필요

        try:
            proc = subprocess.run(
                ["kubectl", "get", "deployment", candidate, "-n", self.k8s_namespace],
                capture_output=True, text=True, shell=False, timeout=10,
            )
            if proc.returncode == 0:
                return candidate
        except Exception:
            logging.error(f"  [k8s 타겟 확인 오류]\n{traceback.format_exc()}")

        logging.warning(
            f"  [k8s 타겟 폴백] '{candidate}'는 존재하지 않는 Deployment — "
            f"'{self.k8s_target_app}'로 대체"
        )
        return self.k8s_target_app

    def _verify_deployment_ready(self, name: str, wait_sec: float = 1.0) -> bool:
        """kubectl rollout restart 후 Deployment가 실제로 롤아웃 완료됐는지 확인한다."""
        time.sleep(wait_sec)
        try:
            proc = subprocess.run(
                ["kubectl", "rollout", "status", f"deployment/{name}",
                 "-n", self.k8s_namespace, "--timeout=30s"],
                capture_output=True, text=True, shell=False, timeout=35,
            )
            if proc.returncode == 0:
                logging.info(f"  [복구 검증 ✓] Deployment '{name}' 롤아웃 완료 확인됨.")
                return True
            logging.warning(f"  [복구 검증 ✗] Deployment '{name}' 롤아웃 미완료: {proc.stdout.strip()}")
            return False
        except Exception:
            logging.error(f"  [복구 검증 오류]\n{traceback.format_exc()}")
            return False

    def _restart_deployment_k8s(self, name: str) -> tuple[bool, str | None]:
        """kubectl rollout restart로 Deployment를 재시작한다 (systemctl restart의 k8s 대응)."""
        logging.warning(f"[조치] Deployment '{name}' 롤아웃 재시작 중 (namespace={self.k8s_namespace})...")
        try:
            proc = subprocess.run(
                ["kubectl", "rollout", "restart", f"deployment/{name}", "-n", self.k8s_namespace],
                capture_output=True, text=True, shell=False, timeout=30,
            )
            if proc.returncode == 0:
                logging.info(f"  Deployment '{name}' 롤아웃 재시작 신호 전송. 복구 검증 중...")
                ok = self._verify_deployment_ready(name)
                if ok:
                    return True, None
                subprocess.run(
                    ["kubectl", "rollout", "undo", f"deployment/{name}", "-n", self.k8s_namespace],
                    capture_output=True, text=True, shell=False, timeout=30,
                )
                return False, f"Deployment '{name}' 재시작 후 롤아웃 미완료, undo 시도함"
            detail = proc.stderr.strip() or f"returncode={proc.returncode}"
            logging.error(f"Deployment '{name}' 롤아웃 재시작 실패: {detail}")
            return False, detail
        except subprocess.TimeoutExpired:
            msg = f"Deployment '{name}' 롤아웃 재시작 타임아웃 (30s 초과)"
            logging.error(msg)
            return False, msg
        except Exception:
            msg = traceback.format_exc()
            logging.error(f"Deployment '{name}' 롤아웃 재시작 오류:\n{msg}")
            return False, msg

    def _verify_pod_running(self, name: str, wait_sec: float = 2.0) -> bool:
        """pod 삭제 후 라벨 셀렉터로 새 파드가 Running 상태인지 확인한다."""
        time.sleep(wait_sec)
        try:
            proc = subprocess.run(
                ["kubectl", "get", "pods", "-l", f"app={name}", "-n", self.k8s_namespace,
                 "--field-selector=status.phase=Running",
                 "-o", "jsonpath={.items[*].metadata.name}"],
                capture_output=True, text=True, shell=False, timeout=10,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                logging.info(f"  [복구 검증 ✓] '{name}' 새 파드 Running 확인됨: {proc.stdout.strip()}")
                return True
            logging.warning(f"  [복구 검증 ✗] '{name}' 라벨의 Running 파드 없음.")
            return False
        except Exception:
            logging.error(f"  [복구 검증 오류]\n{traceback.format_exc()}")
            return False

    def _kill_pod_k8s(self, name: str) -> tuple[bool, str | None]:
        """
        라벨 셀렉터(app={name})로 파드를 삭제한다 (pkill의 k8s 대응).

        Deployment가 컨트롤러라 삭제된 파드는 자동 재생성됨 — 이게 곧
        "복구 검증"의 의미: 롤백은 불필요(pkill과 동일하게 _ROLLBACK_MAP 없음).
        """
        logging.warning(f"[조치] '{name}' 라벨 파드 삭제 시도 (namespace={self.k8s_namespace})...")
        try:
            proc = subprocess.run(
                ["kubectl", "delete", "pod", "-l", f"app={name}", "-n", self.k8s_namespace],
                capture_output=True, text=True, shell=False, timeout=15,
            )
            if proc.returncode == 0:
                logging.info(f"  '{name}' 파드 삭제 신호 전송. 복구 검증 중...")
                ok = self._verify_pod_running(name)
                return ok, (None if ok else f"'{name}' 삭제 후 새 파드 Running 미확인")
            msg = f"'{name}' 라벨 매칭 파드 없음 또는 삭제 실패: {proc.stderr.strip()}"
            logging.warning(f"  {msg}.")
            return False, msg
        except subprocess.TimeoutExpired:
            msg = f"'{name}' 파드 삭제 타임아웃 (15s 초과)"
            logging.error(f"  {msg}.")
            return False, msg
        except Exception:
            msg = traceback.format_exc()
            logging.error(f"  '{name}' 파드 삭제 오류:\n{msg}")
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
