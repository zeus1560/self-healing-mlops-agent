"""
schemas.py — 에이전트 전역 데이터 모델 정의.

ErrorCategory : 시스템이 인식하는 에러 유형 열거형.
ActionType    : 에이전트가 실행할 수 있는 조치 열거형.
AgentResponse : RAGEngine → ActionExecutor로 전달되는 정형화된 응답 DTO.
"""
import json
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Optional


class ErrorCategory(str, Enum):
    """에이전트가 분류하는 에러 카테고리."""

    # Memory 계열
    OUT_OF_MEMORY   = "Out_Of_Memory"
    MEMORY_LEAK     = "Memory_Leak"

    # Database 계열
    DB_CONNECTION   = "DB_Connection"
    DB_TIMEOUT      = "DB_Timeout"
    DB_DEADLOCK     = "DB_Deadlock"

    # Network 계열
    NETWORK_TIMEOUT     = "Network_Timeout"
    NETWORK_UNREACHABLE = "Network_Unreachable"

    # 권한/설정 계열
    PERMISSION_DENIED   = "Permission_Denied"
    CONFIGURATION_ERROR = "Configuration_Error"
    AUTH_ERROR          = "Auth_Error"

    # 파일시스템 계열
    PATH_NOT_FOUND = "Path_Not_Found"
    DISK_FULL      = "Disk_Full"

    # 프로세스 계열
    PROCESS_CRASH  = "Process_Crash"
    PORT_CONFLICT  = "Port_Conflict"

    UNKNOWN = "Unknown"


class ActionType(str, Enum):
    """시스템이 허용하는 안전한 조치 목록."""

    RESTART_SERVICE      = "restart_service"
    CLEAR_MEMORY         = "clear_memory"
    KILL_PROCESS         = "kill_process"
    ALERT_ONLY           = "alert_only"
    ESCALATE_TO_HUMAN    = "escalate_to_human"
    EXECUTE_LLM_COMMAND  = "execute_llm_command"
    EXECUTE_RULE_COMMAND = "execute_rule_command"


@dataclass
class AgentResponse:
    """
    RAGEngine → ActionExecutor로 전달되는 정형화된 응답 DTO.

    Attributes:
        error_category:     분류된 에러 카테고리 (ErrorCategory 값 또는 사용자 정의 문자열).
        severity:           에러 심각도 ("LOW" | "MEDIUM" | "HIGH" | "CRITICAL").
        action_type:        실행할 조치 유형.
        target_process:     조치 대상 프로세스/서비스 이름 (선택).
        reasoning:          판단 근거 설명.
        resolution_source:  해결책 출처 ("L1_CACHE" | "L2_LLM" | "RULE").
        command:            실행할 셸 명령어 (L2/RULE 경로에서 명시적으로 설정).
    """

    error_category:    str
    severity:          str
    action_type:       ActionType
    target_process:    Optional[str] = None
    reasoning:         str           = ""
    resolution_source: str           = "L1_CACHE"
    command:           Optional[str] = None

    def to_json(self) -> str:
        """JSON 직렬화. action_type은 Enum 값(str)으로 변환한다."""
        data = asdict(self)
        data["action_type"] = self.action_type.value
        return json.dumps(data, ensure_ascii=False, indent=2)
