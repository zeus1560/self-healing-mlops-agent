"""
tests/test_autonomy.py — Progressive Autonomy 저장소·게이트 테스트.

conftest.py의 isolate_autonomy_store(autouse)가 매 테스트마다 임시 DB로 격리하므로
여기서는 별도 setUp/tearDown 없이 바로 autonomy_store를 사용한다.
"""
import os
import unittest
from unittest.mock import MagicMock, patch

from src import autonomy_store
from src.executor import ActionExecutor
from src.schemas import ActionType, AgentResponse, AutonomyLevel


class TestAutonomyStore(unittest.TestCase):
    def test_default_level_when_unset(self):
        self.assertEqual(
            autonomy_store.get_level("Process_Crash"),
            autonomy_store.DEFAULT_AUTONOMY_LEVEL,
        )

    def test_set_and_get_roundtrip(self):
        autonomy_store.set_level("Process_Crash", AutonomyLevel.AUTO, "tester")
        self.assertEqual(autonomy_store.get_level("Process_Crash"), AutonomyLevel.AUTO)

    def test_set_level_appears_in_list_all(self):
        autonomy_store.set_level("Process_Crash", AutonomyLevel.AUTO, "tester", note="test promo")
        rows = autonomy_store.list_all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["category"], "Process_Crash")
        self.assertEqual(rows[0]["level"], "auto")

    def test_start_and_get_shadow_target(self):
        autonomy_store.start_shadow("Out_Of_Memory", AutonomyLevel.AUTO, "tester")
        self.assertEqual(autonomy_store.get_shadow_target("Out_Of_Memory"), AutonomyLevel.AUTO)
        # shadow는 검토 중 표시일 뿐 승급이 아니다 — 실제 레벨은 기본값 그대로.
        self.assertEqual(
            autonomy_store.get_level("Out_Of_Memory"), autonomy_store.DEFAULT_AUTONOMY_LEVEL
        )

    def test_no_shadow_target_by_default(self):
        self.assertIsNone(autonomy_store.get_shadow_target("Disk_Full"))

    def test_set_level_clears_shadow_target(self):
        autonomy_store.start_shadow("Process_Crash", AutonomyLevel.AUTO, "tester")
        autonomy_store.set_level("Process_Crash", AutonomyLevel.AUTO, "tester")
        self.assertIsNone(autonomy_store.get_shadow_target("Process_Crash"))


def _decision(action_type, category="Process_Crash", command=None, target_process=None):
    return AgentResponse(
        error_category=category,
        severity="HIGH",
        action_type=action_type,
        target_process=target_process,
        reasoning="test reasoning",
        resolution_source="L1_CACHE",
        command=command,
    )


class TestActionExecutorAutonomyGate(unittest.TestCase):
    def setUp(self):
        self.ex = ActionExecutor()

    def test_init_creates_autonomy_table_even_if_nobody_called_init_table(self):
        # 회귀 테스트: execute()가 autonomy_state 테이블 존재를 가정하고 바로 조회하면
        # 초기화 안 된 새 DB에서 크래시한다(실제로 tests/test_executor.py 수집 중 발견됨).
        # ActionExecutor()가 스스로 init_table()을 호출해야 한다.
        import tempfile
        fresh_db = tempfile.mktemp(suffix=".db")
        orig = autonomy_store._DB_PATH
        autonomy_store._DB_PATH = fresh_db
        try:
            ActionExecutor()  # init_table() 없이 바로 생성
            # 크래시하지 않고 조회 가능해야 한다.
            self.assertEqual(
                autonomy_store.get_level("Anything"), autonomy_store.DEFAULT_AUTONOMY_LEVEL
            )
        finally:
            autonomy_store._DB_PATH = orig

    def test_read_only_blocks_execution(self):
        autonomy_store.set_level("Process_Crash", AutonomyLevel.READ_ONLY, "tester")
        with patch.object(self.ex, "_kill_process") as mock_kill:
            result = self.ex.execute(_decision(ActionType.KILL_PROCESS, target_process="worker"))
        mock_kill.assert_not_called()
        self.assertTrue(result["success"])
        self.assertEqual(result["result_category"], "OBSERVED_ONLY")

    def test_propose_sends_notification_and_blocks_execution(self):
        autonomy_store.set_level("Process_Crash", AutonomyLevel.PROPOSE, "tester")
        with patch.object(self.ex, "_restart_service") as mock_restart, \
             patch("src.executor.SlackChatOps") as mock_slack_cls:
            mock_slack_cls.return_value.send_notification.return_value = True
            result = self.ex.execute(_decision(ActionType.RESTART_SERVICE, target_process="nginx"))
        mock_restart.assert_not_called()
        mock_slack_cls.return_value.send_notification.assert_called_once()
        self.assertEqual(result["result_category"], "PROPOSED_ONLY")

    def test_approve_then_execute_runs_after_auto_approve_env(self):
        autonomy_store.set_level("Process_Crash", AutonomyLevel.APPROVE_THEN_EXECUTE, "tester")
        with patch.dict(os.environ, {"AUTO_APPROVE": "true"}), \
             patch.object(self.ex, "_restart_service", return_value=(True, None)) as mock_restart:
            result = self.ex.execute(_decision(ActionType.RESTART_SERVICE, target_process="nginx"))
        mock_restart.assert_called_once()
        self.assertTrue(result["success"])

    def test_approve_then_execute_blocks_without_approval(self):
        autonomy_store.set_level("Process_Crash", AutonomyLevel.APPROVE_THEN_EXECUTE, "tester")
        with patch.object(self.ex, "_await_approval", return_value="rejected") as mock_await, \
             patch.object(self.ex, "_restart_service") as mock_restart:
            result = self.ex.execute(_decision(ActionType.RESTART_SERVICE, target_process="nginx"))
        mock_await.assert_called_once()
        mock_restart.assert_not_called()
        self.assertFalse(result["success"])
        self.assertEqual(result["error_type"], "HumanRejected")

    def test_auto_runs_immediately_like_today(self):
        autonomy_store.set_level("Process_Crash", AutonomyLevel.AUTO, "tester")
        with patch.object(self.ex, "_restart_service", return_value=(True, None)) as mock_restart:
            result = self.ex.execute(_decision(ActionType.RESTART_SERVICE, target_process="nginx"))
        mock_restart.assert_called_once()
        self.assertTrue(result["success"])

    def test_escalate_to_human_bypasses_gate_even_at_read_only(self):
        autonomy_store.set_level("Process_Crash", AutonomyLevel.READ_ONLY, "tester")
        result = self.ex.execute(_decision(ActionType.ESCALATE_TO_HUMAN))
        self.assertEqual(result["error_type"], "EscalatedToHuman")

    def test_llm_command_at_auto_skips_approval(self):
        autonomy_store.set_level("Process_Crash", AutonomyLevel.AUTO, "tester")
        with patch.object(self.ex, "_await_approval") as mock_await, \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            result = self.ex.execute(_decision(ActionType.EXECUTE_LLM_COMMAND, command="free -m"))
        mock_await.assert_not_called()
        self.assertTrue(result["success"])

    def test_llm_command_at_approve_then_execute_awaits_approval(self):
        autonomy_store.set_level("Process_Crash", AutonomyLevel.APPROVE_THEN_EXECUTE, "tester")
        with patch.object(self.ex, "_await_approval", return_value="approved") as mock_await, \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            result = self.ex.execute(_decision(ActionType.EXECUTE_LLM_COMMAND, command="free -m"))
        mock_await.assert_called_once()
        self.assertTrue(result["success"])

    def test_llm_command_approval_description_surfaces_self_reflection_warning(self):
        """
        2026-09-05: 자가 반성이 거부한 명령어는 더 이상 강제 에스컬레이션되지 않고
        정상 승인 게이트를 타는 대신(llm_engine._make_llm_response), 그 사유를
        승인 화면에서 사람이 볼 수 있어야 한다.
        """
        autonomy_store.set_level("Process_Crash", AutonomyLevel.APPROVE_THEN_EXECUTE, "tester")
        decision = AgentResponse(
            error_category="Process_Crash", severity="HIGH",
            action_type=ActionType.EXECUTE_LLM_COMMAND,
            command="systemctl restart postgresql",
            reasoning="⚠️ 자가 반성이 위험 판정(Groq 제안) — 승인 시 주의: systemctl restart postgresql",
            resolution_source="L2_LLM",
        )
        with patch.object(self.ex, "_await_approval", return_value="approved") as mock_await, \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            self.ex.execute(decision)
        description = mock_await.call_args[0][0]
        self.assertIn("systemctl restart postgresql", description)
        self.assertIn("자가 반성", description)

    def test_llm_command_approval_description_plain_when_no_self_reflection_flag(self):
        autonomy_store.set_level("Process_Crash", AutonomyLevel.APPROVE_THEN_EXECUTE, "tester")
        decision = _decision(ActionType.EXECUTE_LLM_COMMAND, command="free -m")  # reasoning="test reasoning"
        with patch.object(self.ex, "_await_approval", return_value="approved") as mock_await, \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            self.ex.execute(decision)
        description = mock_await.call_args[0][0]
        self.assertEqual(description, "free -m")


class TestLogWatcherLearningGuard(unittest.TestCase):
    """
    OBSERVED_ONLY/PROPOSED_ONLY(실행 안 함, success=True)가 learn_from_feedback()으로
    "성공한 해결책"처럼 벡터 DB에 잘못 학습되지 않는지 확인한다.
    """

    def _make_handler(self):
        from src.log_watcher import LogTailHandler
        from src.utils.debouncer import LogDebouncer

        mock_engine   = MagicMock()
        mock_executor = MagicMock()
        mock_observer = MagicMock()
        mock_breaker  = MagicMock()
        mock_breaker.can_proceed.return_value = True

        handler = LogTailHandler(
            "/tmp/nonexistent_test_log_for_autonomy_tests.log",
            LogDebouncer(cooldown_seconds=0),
            executor=mock_executor,
            observer_agent=mock_observer,
            engine=mock_engine,
            circuit_breaker=mock_breaker,
        )
        return handler, mock_engine, mock_executor

    @staticmethod
    def _decision():
        return AgentResponse(
            error_category="Process_Crash",
            severity="HIGH",
            action_type=ActionType.EXECUTE_LLM_COMMAND,
            reasoning="test",
            resolution_source="L2_LLM",
            command="free -m",
        )

    def test_observed_only_does_not_trigger_learning(self):
        handler, mock_engine, mock_executor = self._make_handler()
        mock_engine.analyze_error.return_value = self._decision()
        mock_executor.execute.return_value = {
            "success": True, "result_category": "OBSERVED_ONLY",
            "error_type": None, "error_detail": "",
        }
        handler.trigger_agent_pipeline("ERROR something crashed")
        mock_engine.learn_from_feedback.assert_not_called()

    def test_proposed_only_does_not_trigger_learning(self):
        handler, mock_engine, mock_executor = self._make_handler()
        mock_engine.analyze_error.return_value = self._decision()
        mock_executor.execute.return_value = {
            "success": True, "result_category": "PROPOSED_ONLY",
            "error_type": None, "error_detail": "",
        }
        handler.trigger_agent_pipeline("ERROR something crashed")
        mock_engine.learn_from_feedback.assert_not_called()

    def test_normal_success_still_triggers_learning(self):
        handler, mock_engine, mock_executor = self._make_handler()
        mock_engine.analyze_error.return_value = self._decision()
        mock_executor.execute.return_value = {
            "success": True, "result_category": "SUCCESS",
            "error_type": None, "error_detail": "",
        }
        handler.trigger_agent_pipeline("ERROR something crashed")
        mock_engine.learn_from_feedback.assert_called_once()


if __name__ == "__main__":
    unittest.main()
