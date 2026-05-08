import subprocess
import logging

def _run_safe_command(cmd: str) -> str:
    """읽기 전용 상태 확인 명령어를 안전하게 실행하고 결과를 반환합니다."""
    try:
        # shell=False 로 유지하되, 파이프(|) 등은 지원하지 않으므로 단순 명령만 실행
        result = subprocess.run(
            cmd.split(), 
            capture_output=True, 
            text=True, 
            timeout=3
        )
        # LLM 컨텍스트가 너무 길어지지 않게 최대 500자로 자름
        return result.stdout.strip()[:500] 
    except Exception as e:
        return f"명령어 실행 실패 ({cmd}): {e}"

def gather_system_context(error_log: str) -> str:
    """
    에러 로그의 키워드를 분석하여, LLM이 판단하기 좋은 
    현재 시스템의 진짜 상태(Context)를 수집합니다.
    """
    context = []
    error_lower = error_log.lower()

    # 1. 메모리 관련 에러 (OOM 등)
    if "oom" in error_lower or "memory" in error_lower:
        context.append("[System Memory Status]")
        context.append(_run_safe_command("free -m"))
    
    # 2. 디스크 관련 에러
    elif "space" in error_lower or "disk" in error_lower or "no space" in error_lower:
        context.append("[System Disk Usage]")
        context.append(_run_safe_command("df -h"))
        
    # 3. 네트워크/포트 관련 에러
    elif "timeout" in error_lower or "connection" in error_lower or "port" in error_lower or "refused" in error_lower:
        context.append("[System Network Ports (ss)]")
        # ss 커맨드는 리눅스 기본 내장 네트워크 확인 도구
        context.append(_run_safe_command("ss -tuln"))

    # 4. 키워드 매칭이 안 되면 기본 부하 상태만 전달
    if not context:
        context.append("[System Uptime & Load Average]")
        context.append(_run_safe_command("uptime"))

    return "\n".join(context)