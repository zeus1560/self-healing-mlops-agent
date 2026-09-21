"""
tests/test_diagnosis_routing.py

진단 주도 라우팅(_route_from_diagnosis, 2026-09-17 추가) 회귀 테스트.

로드맵 항목: 진단 에이전트(_diagnose_error)가 restart_service/kill_process/
clear_memory를 확신 있게 뽑으면, 자유형식 명령 생성+self-reflection 검토를
건너뛰고 L1과 동일한 구조화 ActionType 경로로 바로 연결한다. 이전엔 진단
결과가 프롬프트 힌트로만 쓰이고 실제 action_type 결정엔 전혀 반영되지
않았다.

핵심 안전장치: executor.py의 _kill_process()/_restart_service()는 정확한
프로세스/서비스 *이름* 일치만 지원하고 PID/포트는 지원하지 않는다 — 이
시스템의 실제 kill 대상은 대부분 PID이므로, target에 숫자가 있으면 구조화
라우팅을 포기하고 기존 자유형식 경로로 폴백해야 한다. 이 폴백이 없으면
"5821"은 존재하지 않는 이름의 프로세스를 찾다 조용히 실패하고, "pid 5821"
(공백 포함)은 이름 검증 정규식에 걸려 SecurityBlock으로 실패한다 — 둘 다
기존 경로라면 정상 처리됐을 요청을 헛되이 태우는 회귀다.
"""
import unittest
from unittest.mock import MagicMock, patch

from src.llm_engine import _route_from_diagnosis
from src.schemas import ActionType


def _diag(action_type: str, target: str, root_cause: str = "worker leak") -> dict:
    return {"root_cause": root_cause, "action_type": action_type, "target": target}


class TestRouteFromDiagnosis(unittest.TestCase):
    def test_restart_service_with_named_target_routes_structurally(self):
        resp = _route_from_diagnosis(
            _diag("restart_service", "nginx"), "요약", nearest_category=None, best_distance=3.5,
        )
        self.assertIsNotNone(resp)
        self.assertEqual(resp.action_type, ActionType.RESTART_SERVICE)
        self.assertEqual(resp.target_process, "nginx")
        self.assertEqual(resp.resolution_source, "L2_LLM")
        self.assertEqual(resp.l2_diagnosis, "요약")

    def test_clear_memory_needs_no_target(self):
        resp = _route_from_diagnosis(
            _diag("clear_memory", "none"), "요약", nearest_category=None, best_distance=3.5,
        )
        self.assertIsNotNone(resp)
        self.assertEqual(resp.action_type, ActionType.CLEAR_MEMORY)
        self.assertIsNone(resp.target_process)

    def test_kill_process_with_bare_pid_falls_back_to_freeform(self):
        """순수 숫자 PID("5821")는 _validate_process_name의 문자 정규식은 통과해버려
        존재하지 않는 이름의 프로세스를 찾다 조용히 실패한다 — 반드시 폴백해야 함."""
        resp = _route_from_diagnosis(
            _diag("kill_process", "5821"), "요약", nearest_category=None, best_distance=3.5,
        )
        self.assertIsNone(resp)

    def test_kill_process_with_pid_prefixed_text_falls_back_to_freeform(self):
        """진단 에이전트가 실제로 흔히 내는 형태("pid 5821")도 폴백해야 한다 — 공백
        포함 문자열은 이름 검증 정규식에 걸려 그대로 보내면 SecurityBlock이 난다."""
        resp = _route_from_diagnosis(
            _diag("kill_process", "pid 5821"), "요약", nearest_category=None, best_distance=3.5,
        )
        self.assertIsNone(resp)

    def test_kill_process_with_named_target_routes_structurally(self):
        resp = _route_from_diagnosis(
            _diag("kill_process", "payment-worker"), "요약", nearest_category=None, best_distance=3.5,
        )
        self.assertIsNotNone(resp)
        self.assertEqual(resp.action_type, ActionType.KILL_PROCESS)
        self.assertEqual(resp.target_process, "payment-worker")

    def test_restart_service_with_port_number_target_falls_back(self):
        """포트 번호도 서비스 이름이 아니므로(숫자 포함) 구조화 라우팅하지 않는다."""
        resp = _route_from_diagnosis(
            _diag("restart_service", "8080"), "요약", nearest_category=None, best_distance=3.5,
        )
        self.assertIsNone(resp)

    def test_restart_service_with_description_target_falls_back(self):
        """공백 섞인 설명문("mlflow tracking server")은 _PROCESS_NAME_RE에 걸려
        SecurityBlock으로 무조건 실패하므로 구조화 라우팅을 포기해야 한다
        (2026-09-22, §3.1/§6에서 16건 중 4건꼴로 재현된 버그)."""
        resp = _route_from_diagnosis(
            _diag("restart_service", "mlflow tracking server"), "요약",
            nearest_category=None, best_distance=3.5,
        )
        self.assertIsNone(resp)

    def test_kill_process_with_file_path_target_falls_back(self):
        """파일 경로("/tmp/tensorboard_logs")를 서비스명으로 착각한 경우도
        _PROCESS_NAME_RE에 걸려 무조건 실패하므로 폴백해야 한다."""
        resp = _route_from_diagnosis(
            _diag("kill_process", "/tmp/tensorboard_logs"), "요약",
            nearest_category=None, best_distance=3.5,
        )
        self.assertIsNone(resp)

    def test_missing_target_falls_back_for_target_requiring_actions(self):
        for action_type in ("restart_service", "kill_process"):
            with self.subTest(action_type=action_type):
                resp = _route_from_diagnosis(
                    _diag(action_type, "none"), "요약", nearest_category=None, best_distance=3.5,
                )
                self.assertIsNone(resp)

    def test_unrecognized_action_type_falls_back(self):
        resp = _route_from_diagnosis(
            _diag("other", "none"), "요약", nearest_category=None, best_distance=3.5,
        )
        self.assertIsNone(resp)

    def test_carries_nearest_category_and_distance_through(self):
        resp = _route_from_diagnosis(
            _diag("clear_memory", "none"), "요약", nearest_category="Memory_Leak", best_distance=2.1,
        )
        self.assertEqual(resp.l1_nearest_category, "Memory_Leak")
        self.assertEqual(resp.l1_nearest_distance, 2.1)


class TestL2SlowTrackStructuredRoutingWiring(unittest.TestCase):
    """_l2_slow_track이 구조화 라우팅을 실제로 타면 제안(_run_groq)/검토
    (_reflect_on_command)를 아예 호출하지 않아야 한다 — L1과 동일하게 두
    단계를 건너뛰는 게 이 기능의 핵심이므로, 헬퍼 함수 단위 테스트만으론
    부족하고 실제 배선까지 확인해야 한다."""

    def _make_engine(self):
        patcher_client = patch("src.llm_engine._get_chroma_client")
        patcher_warmup = patch("src.llm_engine._ollama_warmup")
        mock_client = patcher_client.start()
        self.addCleanup(patcher_client.stop)
        patcher_warmup.start()
        self.addCleanup(patcher_warmup.stop)
        mock_client.return_value.get_collection.return_value = MagicMock()

        from src.llm_engine import RAGEngine
        return RAGEngine()

    def test_confident_structured_diagnosis_skips_propose_and_review(self):
        engine = self._make_engine()
        best_meta = {"error_category": "Process_Crash"}

        with patch("src.llm_engine._is_groq_available", return_value=True), \
             patch("src.llm_engine._diagnose_error", return_value={
                 "root_cause": "payment-worker hung", "action_type": "kill_process",
                 "target": "payment-worker",
             }), \
             patch("src.llm_engine._run_groq") as mock_run_groq, \
             patch("src.llm_engine._reflect_on_command") as mock_reflect:
            resp = engine._l2_slow_track("some novel error", best_meta, 3.5)

        mock_run_groq.assert_not_called()
        mock_reflect.assert_not_called()
        self.assertEqual(resp.action_type, ActionType.KILL_PROCESS)
        self.assertEqual(resp.target_process, "payment-worker")
        self.assertEqual(resp.resolution_source, "L2_LLM")

    def test_pid_target_diagnosis_still_goes_through_propose_and_review(self):
        """PID 대상은 기존 동작 그대로 — 라우팅 신설이 이 경로를 건드리면 안 된다."""
        engine = self._make_engine()
        best_meta = {"error_category": "Memory_Leak"}

        with patch("src.llm_engine._is_groq_available", return_value=True), \
             patch("src.llm_engine._diagnose_error", return_value={
                 "root_cause": "worker leak", "action_type": "kill_process", "target": "pid 5821",
             }), \
             patch("src.llm_engine._run_groq", return_value="kill -TERM 5821") as mock_run_groq, \
             patch("src.llm_engine._reflect_on_command", return_value=(True, "안전함")) as mock_reflect:
            resp = engine._l2_slow_track("some novel error", best_meta, 3.5)

        mock_run_groq.assert_called_once()
        mock_reflect.assert_called_once()
        self.assertEqual(resp.action_type, ActionType.EXECUTE_LLM_COMMAND)
        self.assertEqual(resp.command, "kill -TERM 5821")


if __name__ == "__main__":
    unittest.main()
