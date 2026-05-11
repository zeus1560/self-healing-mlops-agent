import logging
import os
import shlex
import subprocess
import traceback

# LLM 컨텍스트가 너무 길어지지 않도록 명령어 출력을 최대 N자로 자름
_MAX_CMD_OUTPUT = int(os.getenv("DIAG_MAX_OUTPUT_CHARS", "500"))


def _run_safe_command(cmd: str) -> str:
    """읽기 전용 상태 확인 명령어를 안전하게 실행하고 결과를 반환한다."""
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
        logging.error(f"[SystemDiag] 명령어 실행 실패 ({cmd}):\n{traceback.format_exc()}")
        return ""


def gather_system_context(error_log: str) -> str:
    """
    에러 로그의 키워드를 분석하여 LLM 판단에 필요한 시스템 상태를 수집한다.
    복수의 키워드가 일치할 경우 관련 섹션을 모두 수집한다.
    """
    context = []
    lower   = error_log.lower()

    if any(kw in lower for kw in ("oom", "memory")):
        context.append("[System Memory Status]")
        context.append(_run_safe_command("free -m"))

    if any(kw in lower for kw in ("space", "disk", "no space")):
        context.append("[System Disk Usage]")
        context.append(_run_safe_command("df -h"))

    if any(kw in lower for kw in ("timeout", "connection", "port", "refused")):
        context.append("[System Network Ports]")
        context.append(_run_safe_command("ss -tuln"))

    if not context:
        context.append("[System Uptime & Load Average]")
        context.append(_run_safe_command("uptime"))

    return "\n".join(context)
