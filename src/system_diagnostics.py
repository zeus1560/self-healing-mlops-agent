"""
system_diagnostics — LLM 판단에 필요한 시스템 상태를 수집한다.

에러 로그의 키워드를 분석하여 관련 진단 명령어만 선택적으로 실행한다.

보안:
  - _ALLOWED_DIAG_CMDS: 명시적 허용 목록. 하드코딩된 명령어라도 allowlist 검증을
    추가해 향후 실수로 임의 명령어가 실행되는 것을 원천 차단한다.
  - 모든 명령어는 리스트 형태로 미리 토크나이즈돼 런타임 shlex.split 비용을 제거한다.
  - shell=False + timeout=3s로 인젝션 및 행 걸림을 방지한다.

LLM 컨텍스트 크기 제한:
  각 명령어 출력은 _MAX_CMD_OUTPUT 바이트로 잘려 프롬프트 오버플로를 방지한다.
"""
import logging
import os
import subprocess
import traceback

# LLM 컨텍스트가 너무 길어지지 않도록 명령어 출력을 최대 N자로 자름
_MAX_CMD_OUTPUT = int(os.getenv("DIAG_MAX_OUTPUT_CHARS", "500"))

# 진단 명령어 실행 허용 목록.
# _DIAG_RULES 에 등록된 명령어는 반드시 이 집합 안에 있어야 실행된다.
# 향후 규칙 추가 시 이 목록도 함께 갱신해야 한다.
_ALLOWED_DIAG_CMDS: frozenset[str] = frozenset({"free", "df", "ss", "uptime"})

# 에러 키워드 → (섹션 레이블, 사전 토크나이즈된 진단 명령어) 매핑.
# 문자열 대신 list[str]을 사용해 런타임 shlex.split 반복 호출을 제거하고,
# 명령어 구조를 소스 레벨에서 명확하게 표현한다.
_DIAG_RULES: list[tuple[tuple[str, ...], str, list[str]]] = [
    (
        ("oom", "memory"),
        "[System Memory Status]",
        ["free", "-m"],
    ),
    (
        ("space", "disk", "no space"),
        "[System Disk Usage]",
        ["df", "-h"],
    ),
    (
        ("timeout", "connection", "port", "refused"),
        "[System Network Ports]",
        ["ss", "-tuln"],
    ),
]
_DEFAULT_DIAG: tuple[str, list[str]] = ("[System Uptime & Load Average]", ["uptime"])


def _run_safe_command(tokens: list[str]) -> str:
    """
    사전 토크나이즈된 읽기 전용 명령어를 안전하게 실행하고 결과를 반환한다.

    _ALLOWED_DIAG_CMDS allowlist 검증을 먼저 수행해 허용되지 않은 명령어를 차단한다.
    shell=False로 인젝션을 원천 차단하고, 타임아웃(3s) 초과 시 빈 문자열을 반환한다.

    Args:
        tokens: 실행할 명령어 토큰 리스트 (예: ["free", "-m"]).

    Returns:
        stdout 출력 문자열 (최대 _MAX_CMD_OUTPUT 자). 오류 시 빈 문자열.
    """
    if not tokens or tokens[0] not in _ALLOWED_DIAG_CMDS:
        logging.error(
            f"[SystemDiag] 허용되지 않은 진단 명령어 차단: {tokens!r} "
            f"(허용: {sorted(_ALLOWED_DIAG_CMDS)})"
        )
        return ""
    try:
        result = subprocess.run(
            tokens,
            capture_output=True,
            text=True,
            shell=False,
            timeout=3,
        )
        return result.stdout.strip()[:_MAX_CMD_OUTPUT]
    except Exception:
        logging.error(
            f"[SystemDiag] 명령어 실행 실패 ({tokens}):\n{traceback.format_exc()}"
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

    for keywords, label, tokens in _DIAG_RULES:
        if any(kw in lower for kw in keywords):
            context.append(label)
            context.append(_run_safe_command(tokens))

    if not context:
        label, tokens = _DEFAULT_DIAG
        context.append(label)
        context.append(_run_safe_command(tokens))

    return "\n".join(context)
