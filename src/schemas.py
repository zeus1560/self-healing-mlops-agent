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

    # CPU 계열 (2026-09-10 추가 — 카오스 인젝터 "cpu" fault가 실제로 존재하는데
    # ErrorCategory 매핑 자체가 없던 구조적 공백이었음. dashboard/app.py의
    # _CATEGORY_DESC엔 "CPU_Overload" 표시 문구가 이미 있었으나 실제로 연결된
    # 적은 없었음 — 이름을 그대로 재사용해 맞춘다)
    CPU_OVERLOAD    = "CPU_Overload"

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


class AutonomyLevel(str, Enum):
    """카테고리별 Progressive Autonomy 단계."""

    READ_ONLY            = "read_only"            # 분류·로그만, 조치 없음
    PROPOSE              = "propose"               # 조치 제안 알림만, 실행 안 함
    APPROVE_THEN_EXECUTE = "approve_then_execute"  # 승인 후 실행
    AUTO                 = "auto"                  # 즉시 실행


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
        l1_doc_id:          L1_CACHE 히트 시 실제로 매칭된 ChromaDB 문서 ID (그 외 None).
                             온라인학습 엔트리의 실행 결과를 다시 그 문서에 되먹이는 데 쓰인다.
        l1_source:          매칭된 ChromaDB 문서의 "source" 메타데이터
                             (예: "online_learning", "chaos_injector_signature"). L1_CACHE 히트 시에만 의미 있음.
        l1_evidence:        L1_CACHE 히트 시 앙상블 투표에 실제로 참여한 과거 사건들(거리·문서
                             내용 요약)을 사람이 읽을 수 있게 정리한 설명(Explainability, 2026-09-11
                             추가). "왜 이 조치를 골랐는가"에 벡터 검색이 실제로 무엇을 근거로
                             삼았는지 보여준다 — L2_LLM/RULE 경로에는 해당 없어 None.
        l1_nearest_category: L1이 임계값(RAG_THRESHOLD) 미달로 액션 채택은 포기했지만, 그래도
                             가장 가까웠던 과거 문서 하나의 error_category(2026-09-15 추가).
                             L2_LLM/RULE 경로로 빠진 사건은 error_category가
                             "LLM_Inferred"/"Rule_Inferred"/"Unknown" 같은 의미 없는 값으로
                             찍혀 FP/FN 분석의 recall 지표가 아예 적용 불가했던 문제
                             (run_fp_fn_analysis.py, 2026-09-04/13 세션에서 반복 관찰)를
                             참고용으로 메꾸기 위함이다. **주의**: 이건 임계값 미달 추측일
                             뿐이므로 autonomy 게이트(executor.py의 autonomy_store.get_level)나
                             액션 실행 로직 어디에도 관여하지 않는다 — 순수 분석/Explainability용.
        l1_nearest_distance: 위 l1_nearest_category에 대응하는 벡터 거리(작을수록 유사).
                             L1_CACHE 히트(candidates 존재)에서는 항상 None — 이미 threshold를
                             통과해 l1_evidence로 근거가 남으므로 중복 정보다.
        l2_diagnosis:        Groq L2 경로의 멀티에이전트 3단계(진단→제안→검토, 2026-09-15
                             추가) 중 1단계 진단 에이전트(_diagnose_error)가 낸 원인/권장
                             조치 유형/대상 소견. 진단이 실패(네트워크 오류·형식 불일치·
                             GROQ_API_KEY 미설정)하면 기존 방식대로 진단 없이 명령을
                             생성하고 이 필드는 None으로 남는다 — Ollama/ipex_llm/RULE
                             경로도 이 단계를 안 거치므로 항상 None.
        self_reflection_safe: 3단계 검토(self-reflection, _reflect_on_command)가 실제로
                             계산한 (안전 여부) 불리언 그대로(2026-09-15 code-review로
                             추가). 지금까지는 이 값이 reasoning 문자열(예: "⚠️ 자가
                             반성이 위험 판정...")로만 남아서, 외부 분석 스크립트들이
                             매번 "⚠️" 접두어나 특정 부분 문자열을 다시 파싱해 판정을
                             역추론해야 했다 — reasoning 문구가 조금만 바뀌어도(표현
                             수정, 이모지 제거, 다국어화 등) 그 파싱이 조용히 깨지는
                             사고가 이미 두 번 있었다(run_l2_production_path_check.py).
                             이 필드는 그 값을 계산 시점에 그대로 보존해 다시 파싱할
                             필요를 없앤다. L1_CACHE/RULE/에스컬레이션 경로(검토 자체가
                             없음)에서는 None.
    """

    error_category:    str
    severity:          str
    action_type:       ActionType
    target_process:    Optional[str] = None
    reasoning:         str           = ""
    resolution_source: str           = "L1_CACHE"
    command:           Optional[str] = None
    l1_doc_id:         Optional[str] = None
    l1_source:         Optional[str] = None
    l1_evidence:        Optional[str] = None
    l1_nearest_category: Optional[str]   = None
    l1_nearest_distance: Optional[float] = None
    l2_diagnosis:        Optional[str]   = None
    self_reflection_safe: Optional[bool] = None

    def to_json(self) -> str:
        """JSON 직렬화. action_type은 Enum 값(str)으로 변환한다."""
        data = asdict(self)
        data["action_type"] = self.action_type.value
        return json.dumps(data, ensure_ascii=False, indent=2)
