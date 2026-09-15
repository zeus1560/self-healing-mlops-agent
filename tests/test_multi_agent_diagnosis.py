"""
tests/test_multi_agent_diagnosis.py

멀티에이전트 3단계(진단→제안→검토, 2026-09-15 추가) 회귀 테스트.

기존엔 Groq L2 경로가 "원인 파악"과 "명령 생성"을 한 프롬프트에서 동시에
했다 — 2026-09-15 faithfulness 실측(run_faithfulness_test.py)에서 프롬프트가
여러 판단을 뒤섞을 때 엉뚱한 결론(화이트리스트에 있는 fuser를 "없다"고 오판)이
나오는 걸 발견한 뒤, 같은 위험을 명령 생성 단계에서도 줄이기 위해 1단계
진단(_diagnose_error)을 명령 생성(_run_groq) 앞에 분리했다. 3단계 검토는
기존 self-reflection(_reflect_on_command) 그대로 재사용 — 여기서는 다시
검증하지 않는다(tests/test_groq_smoke.py 참고).

핵심 설계 원칙: 진단이 실패해도(네트워크 오류, 형식 불일치, GROQ_API_KEY 미설정)
기존 방식(진단 없이 바로 생성)으로 안전하게 폴백해야 한다 — 새 실패 모드를
추가하지 않는다는 게 이 기능의 전제 조건이라, 그 폴백 경로를 특히 꼼꼼히 검증한다.
"""
import unittest
from unittest.mock import MagicMock, patch


class TestParseDiagnosis(unittest.TestCase):
    def test_parses_well_formed_response(self):
        from src.llm_engine import _parse_diagnosis

        raw = "ROOT_CAUSE: leaky worker consuming memory | ACTION_TYPE: kill_process | TARGET: pid 5821"
        result = _parse_diagnosis(raw)
        self.assertEqual(result["root_cause"], "leaky worker consuming memory")
        self.assertEqual(result["action_type"], "kill_process")
        self.assertEqual(result["target"], "pid 5821")

    def test_lowercases_action_type(self):
        from src.llm_engine import _parse_diagnosis

        result = _parse_diagnosis("ROOT_CAUSE: x | ACTION_TYPE: RESTART_SERVICE | TARGET: nginx")
        self.assertEqual(result["action_type"], "restart_service")

    def test_case_insensitive_labels(self):
        from src.llm_engine import _parse_diagnosis

        result = _parse_diagnosis("root_cause: x | action_type: restart_service | target: none")
        self.assertIsNotNone(result)
        self.assertEqual(result["target"], "none")

    def test_returns_none_for_malformed_response(self):
        from src.llm_engine import _parse_diagnosis

        self.assertIsNone(_parse_diagnosis("I think this is probably fine to restart."))

    def test_returns_none_for_missing_field(self):
        from src.llm_engine import _parse_diagnosis

        self.assertIsNone(_parse_diagnosis("ROOT_CAUSE: x | TARGET: none"))


class TestDiagnoseError(unittest.TestCase):
    def test_returns_none_when_groq_unavailable(self):
        from src.llm_engine import _diagnose_error

        with patch("src.llm_engine._is_groq_available", return_value=False):
            self.assertIsNone(_diagnose_error("some error", "some context"))

    def test_returns_none_on_groq_call_failure(self):
        from src.llm_engine import _diagnose_error

        with patch("src.llm_engine._is_groq_available", return_value=True), \
             patch("src.llm_engine._call_groq_chat", return_value="ERROR: timeout"):
            self.assertIsNone(_diagnose_error("some error", "some context"))

    def test_returns_none_on_unparseable_response(self):
        from src.llm_engine import _diagnose_error

        with patch("src.llm_engine._is_groq_available", return_value=True), \
             patch("src.llm_engine._call_groq_chat", return_value="not the expected format"):
            self.assertIsNone(_diagnose_error("some error", "some context"))

    def test_returns_parsed_diagnosis_on_success(self):
        from src.llm_engine import _diagnose_error

        raw = "ROOT_CAUSE: worker leak | ACTION_TYPE: kill_process | TARGET: pid 5821"
        with patch("src.llm_engine._is_groq_available", return_value=True), \
             patch("src.llm_engine._call_groq_chat", return_value=raw):
            result = _diagnose_error("some error", "some context")
        self.assertEqual(result["action_type"], "kill_process")


class TestL2SlowTrackDiagnosisThreading(unittest.TestCase):
    """RAGEngine._l2_slow_track()이 진단 성공/실패 각각에서 올바르게 동작하는지 확인."""

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

    def test_successful_diagnosis_populates_l2_diagnosis(self):
        engine = self._make_engine()
        best_meta = {"error_category": "Memory_Leak"}

        with patch("src.llm_engine._is_groq_available", return_value=True), \
             patch("src.llm_engine._diagnose_error", return_value={
                 "root_cause": "worker leak", "action_type": "kill_process", "target": "pid 5821",
             }), \
             patch("src.llm_engine._run_groq", return_value="kill -TERM 5821") as mock_run_groq, \
             patch("src.llm_engine._reflect_on_command", return_value=(True, "안전함")):
            resp = engine._l2_slow_track("some novel error", best_meta, 3.5)

        self.assertEqual(resp.resolution_source, "L2_LLM")
        self.assertIsNotNone(resp.l2_diagnosis)
        self.assertIn("worker leak", resp.l2_diagnosis)
        self.assertIn("kill_process", resp.l2_diagnosis)
        # 진단이 보강한 컨텍스트가 실제로 명령 생성 호출에 전달됐는지 확인
        enriched_context_arg = mock_run_groq.call_args[0][1]
        self.assertIn("진단 에이전트 소견", enriched_context_arg)
        self.assertIn("worker leak", enriched_context_arg)

    def test_reflection_stage_never_sees_diagnosis_enriched_context(self):
        """2026-09-15 code-review 발견·수정: 검토(self-reflection)는 진단 에이전트의
        결론이 섞이지 않은 원본 system_context만 받아야 한다 — 그래야 진단이
        틀렸을 때 검토가 그 틀린 결론을 그대로 재확인하며 뭉개지 않는다."""
        engine = self._make_engine()
        best_meta = {"error_category": "Memory_Leak"}

        with patch("src.llm_engine._is_groq_available", return_value=True), \
             patch("src.llm_engine.gather_system_context", return_value="원본 컨텍스트"), \
             patch("src.llm_engine._diagnose_error", return_value={
                 "root_cause": "worker leak", "action_type": "kill_process", "target": "pid 5821",
             }), \
             patch("src.llm_engine._run_groq", return_value="kill -TERM 5821"), \
             patch("src.llm_engine._reflect_on_command", return_value=(True, "안전함")) as mock_reflect:
            engine._l2_slow_track("some novel error", best_meta, 3.5)

        reflection_context_arg = mock_reflect.call_args[0][2]
        self.assertEqual(reflection_context_arg, "원본 컨텍스트")
        self.assertNotIn("진단 에이전트 소견", reflection_context_arg)

    def test_failed_diagnosis_falls_back_to_original_flow(self):
        """진단 실패 시 l2_diagnosis=None이고, 기존 system_context 그대로 생성에 쓰여야 한다."""
        engine = self._make_engine()
        best_meta = {"error_category": "Memory_Leak"}

        with patch("src.llm_engine._is_groq_available", return_value=True), \
             patch("src.llm_engine._diagnose_error", return_value=None), \
             patch("src.llm_engine._run_groq", return_value="kill -TERM 5821") as mock_run_groq, \
             patch("src.llm_engine._reflect_on_command", return_value=(True, "안전함")), \
             patch("src.llm_engine.gather_system_context", return_value="원본 컨텍스트"):
            resp = engine._l2_slow_track("some novel error", best_meta, 3.5)

        self.assertIsNone(resp.l2_diagnosis)
        mock_run_groq.assert_called_once_with("some novel error", "원본 컨텍스트")

    def test_groq_unavailable_never_calls_diagnosis(self):
        """GROQ_API_KEY 미설정이면 진단 단계 자체를 시도하지 않고 바로 Ollama로 폴백해야 한다."""
        engine = self._make_engine()
        best_meta = {"error_category": "Memory_Leak"}

        with patch("src.llm_engine._is_groq_available", return_value=False), \
             patch("src.llm_engine._diagnose_error") as mock_diagnose, \
             patch("src.llm_engine._is_ollama_available", return_value=False), \
             patch("src.llm_engine.run_ipex_engine", return_value="ERROR"), \
             patch("src.llm_engine._rule_based_fallback", return_value=None), \
             patch("src.llm_engine.gather_system_context", return_value="ctx"):
            engine._l2_slow_track("some novel error", best_meta, 3.5)

        mock_diagnose.assert_not_called()

    def test_rule_and_ollama_paths_never_populate_l2_diagnosis(self):
        """진단 단계는 Groq 경로에만 있다 — RULE 경로는 항상 l2_diagnosis=None이어야 한다."""
        engine = self._make_engine()
        best_meta = {"error_category": "Memory_Leak"}

        with patch("src.llm_engine._is_groq_available", return_value=False), \
             patch("src.llm_engine._is_ollama_available", return_value=False), \
             patch("src.llm_engine.run_ipex_engine", return_value="ERROR"), \
             patch("src.llm_engine._rule_based_fallback", return_value="systemctl restart nginx"), \
             patch("src.llm_engine.gather_system_context", return_value="ctx"):
            resp = engine._l2_slow_track("some novel error", best_meta, 3.5)

        self.assertEqual(resp.resolution_source, "RULE")
        self.assertIsNone(resp.l2_diagnosis)


if __name__ == "__main__":
    unittest.main()
