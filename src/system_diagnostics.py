"""
system_diagnostics — LLM 판단에 필요한 시스템 상태를 수집한다.

에러 로그의 키워드를 분석하여 관련 진단 명령어만 선택적으로 실행한다.
모든 명령어는 읽기 전용이며, shell=False + timeout=3s로 안전하게 실행된다.

LLM 컨텍스트 크기 제한:
  각 명령어 출력은 _MAX_CMD_OUTPUT 바이트로 잘려 프롬프트 오버플로를 방지한다.
"""
import logging
import os
import shlex
import subprocess
import traceback

# LLM 컨텍스트가 너무 길어지지 않도록 명령어 출력을 최대 N자로 자름
_MAX_CMD_OUTPUT = int(os.getenv("DIAG_MAX_OUTPUT_CHARS", "500"))

# 에러 키워드 → (섹션 레이블, 진단 명령어) 매핑
_DIAG_RULES: list[tuple[tuple[str, ...], str, str]] = [
    (("oom", "memory"),                    "[System Memory Status]",   "free -m"),
    (("space", "disk", "no space"),        "[System Disk Usage]",      "df -h"),
    (("timeout", "connection", "port",
      "refused"),                          "[System Network Ports]",   "ss -tuln"),
]
_DEFAULT_DIAG = ("[System Uptime & Load Average]", "uptime")


def _run_safe_command(cmd: str) -> str:
    """
    읽기 전용 상태 확인 명령어를 안전하게 실행하고 결과를 반환한다.

    shell=False + shlex.split으로 인젝션을 원천 차단한다.
    타임아웃(3s) 초과 또는 오류 시 빈 문자열을 반환한다.
    """
    try:
        result = subprocess.run(
            shlex.split(cmd),
            capture_output=True,
            text=True,
            shell=False,
            timeout=3,
        )
        return result.stdout.strip()[:_MAX_CMD_OUTPUT]
    except Exception:
        logging.error(
            f"[SystemDiag] 명령어 실행 실패 ({cmd}):\n{traceback.format_exc()}"
        )
        return ""


def gather_system_context(error_log: str) -> str:
    """
    에러 로그의 키워드를 분석하여 LLM 판단에 필요한 시스템 상태를 수집한다.

    복수의 키워드가 일치할 경우 관련 섹션을 모두 수집한다.
    어떤 키워드도 매칭되지 않으면 기본 진단(uptime)을 수행한다.

    Args:
        error_log: 분석할 에러 로그 문자열.

    Returns:
        LLM 프롬프트에 삽입할 시스템 상태 문자열.
    """
    context: list[str] = []
    lower   = error_log.lower()

    for keywords, label, cmd in _DIAG_RULES:
        if any(kw in lower for kw in keywords):
            context.append(label)
            context.append(_run_safe_command(cmd))

    if not context:
        label, cmd = _DEFAULT_DIAG
        context.append(label)
        context.append(_run_safe_command(cmd))

    return "\n".join(context)
