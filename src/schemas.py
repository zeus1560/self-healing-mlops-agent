import json
from enum import Enum
from dataclasses import dataclass, asdict
from typing import Optional


# 시스템이 허용하는 안전한 조치 목록
class ActionType(str, Enum):
    RESTART_SERVICE = "restart_service"
    CLEAR_MEMORY = "clear_memory"
    KILL_PROCESS = "kill_process"
    ALERT_ONLY = "alert_only"
    ESCALATE_TO_HUMAN = "escalate_to_human"


# LLM이 뱉어낼 정형화된 응답 포맷
@dataclass
class AgentResponse:
    error_category: str
    severity: str
    action_type: ActionType
    target_process: Optional[str] = None
    reasoning: str = ""

    def to_json(self):
        data = asdict(self)
        data["action_type"] = self.action_type.value
        return json.dumps(data, ensure_ascii=False, indent=2)
