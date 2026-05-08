import json
from enum import Enum
from dataclasses import dataclass, asdict
from typing import Optional


class ErrorCategory(str, Enum):
    # Memory 계열
    OUT_OF_MEMORY     = "Out_Of_Memory"
    MEMORY_LEAK       = "Memory_Leak"

    # Database 계열
    DB_CONNECTION     = "DB_Connection"
    DB_TIMEOUT        = "DB_Timeout"
    DB_DEADLOCK       = "DB_Deadlock"

    # Network 계열
    NETWORK_TIMEOUT   = "Network_Timeout"
    NETWORK_UNREACHABLE = "Network_Unreachable"

    # 권한/설정 계열
    PERMISSION_DENIED = "Permission_Denied"
    CONFIGURATION_ERROR = "Configuration_Error"
    AUTH_ERROR        = "Auth_Error"

    # 파일시스템 계열
    PATH_NOT_FOUND    = "Path_Not_Found"
    DISK_FULL         = "Disk_Full"

    # 프로세스 계열
    PROCESS_CRASH     = "Process_Crash"
    PORT_CONFLICT     = "Port_Conflict"

    UNKNOWN           = "Unknown"


# 시스템이 허용하는 안전한 조치 목록
class ActionType(str, Enum):
    RESTART_SERVICE = "restart_service"
    CLEAR_MEMORY = "clear_memory"
    KILL_PROCESS = "kill_process"
    ALERT_ONLY = "alert_only"
    ESCALATE_TO_HUMAN = "escalate_to_human"
    EXECUTE_LLM_COMMAND = "execute_llm_command"


# LLM이 뱉어낼 정형화된 응답 포맷
@dataclass
class AgentResponse:
    error_category: str
    severity: str
    action_type: ActionType
    target_process: Optional[str] = None
    reasoning: str = ""
    resolution_source: str = "L1_CACHE"  # "L1_CACHE" | "L2_LLM" | "RULE"
    command: Optional[str] = None        # 실행할 셸 명령어 (L2/RULE 시 명시적으로 설정)

    def to_json(self):
        data = asdict(self)
        data["action_type"] = self.action_type.value
        return json.dumps(data, ensure_ascii=False, indent=2)
