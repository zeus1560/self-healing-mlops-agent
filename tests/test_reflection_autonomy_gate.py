"""
auto 레벨 + self-reflection NO → 자동 실행하지 않고 사람 승인으로 (2026-10-04).

승인 레벨(approve_then_execute)에서는 9/05 결정(검토 NO여도 차단하지 않고 경고만)을 유지하고,
auto 레벨에서만 검토 NO를 승인 대기로 내린다 — auto엔 승인 화면이 없어 경고가 의미 없기 때문.
자유형식(EXECUTE_LLM_COMMAND) 경로와 구조화(RESTART_SERVICE 등) 경로 모두 고정한다.
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src import autonomy_store, llm_mode  # noqa: E402
from src.executor import ActionExecutor  # noqa: E402
from src.schemas import ActionType, AgentResponse, AutonomyLevel  # noqa: E402

CATEGORY = "Process_Crash"   # VM에서 실제로 auto인 카테고리 중 하나(2026-10-04 확인)


class _Base(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._orig = autonomy_store._DB_PATH
        autonomy_store._DB_PATH = self.db
        autonomy_store.init_table()
        self.ex = ActionExecutor()

    def tearDown(self):
        autonomy_store._DB_PATH = self._orig
        os.remove(self.db)

    def _set(self, level):
        with patch.object(llm_mode, "LLM_PROVIDER", "groq"):
            autonomy_store.set_level(CATEGORY, level, "tester")

    def _free_form(self, safe):
        return AgentResponse(
            error_category=CATEGORY, severity="HIGH", action_type=ActionType.EXECUTE_LLM_COMMAND,
            reasoning="r", resolution_source="L1_CACHE", command="systemctl restart worker",
            self_reflection_safe=safe,
        )

    def _structured(self, safe):
        return AgentResponse(
            error_category=CATEGORY, severity="HIGH", action_type=ActionType.RESTART_SERVICE,
            reasoning="r", resolution_source="L2_LLM", target_process="worker",
            self_reflection_safe=safe,
        )

    def _run(self, decision):
        with patch.object(self.ex, "_await_approval", return_value="rejected") as mock_approval, \
             patch("src.executor.subprocess.run") as mock_run, \
             patch.object(self.ex, "_restart_service", return_value=(True, None)) as mock_restart:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = ""
            self.ex.execute(decision, "ERROR: worker hung")
        return mock_approval, mock_run, mock_restart


class TestAutoWithReviewNo(_Base):
    def test_free_form_auto_review_no_waits_for_approval(self):
        self._set(AutonomyLevel.AUTO)
        approval, run, _ = self._run(self._free_form(False))
        approval.assert_called_once()
        run.assert_not_called()

    def test_structured_auto_review_no_waits_for_approval(self):
        self._set(AutonomyLevel.AUTO)
        approval, _, restart = self._run(self._structured(False))
        approval.assert_called_once()
        restart.assert_not_called()


class TestAutoWithReviewYes(_Base):
    def test_free_form_auto_review_yes_executes(self):
        self._set(AutonomyLevel.AUTO)
        approval, run, _ = self._run(self._free_form(True))
        approval.assert_not_called()
        run.assert_called_once()

    def test_structured_auto_review_yes_executes(self):
        self._set(AutonomyLevel.AUTO)
        approval, _, restart = self._run(self._structured(True))
        approval.assert_not_called()
        restart.assert_called_once()

    def test_not_reviewed_none_keeps_auto(self):
        """검토를 안 거친 응답(예: L1 RESTART_SERVICE 이전 데이터, CLEAR_MEMORY)은 영향 없음."""
        self._set(AutonomyLevel.AUTO)
        approval, _, restart = self._run(self._structured(None))
        approval.assert_not_called()
        restart.assert_called_once()


class TestApproveLevelUnchanged(_Base):
    """승인 레벨: 검토 결과와 무관하게 승인 대기(9/05 결정 — NO여도 차단 없이 경고만)."""

    def test_review_no_still_goes_to_approval_not_blocked(self):
        self._set(AutonomyLevel.APPROVE_THEN_EXECUTE)
        for decision in (self._free_form(False), self._structured(False)):
            approval, _, _ = self._run(decision)
            approval.assert_called_once()

    def test_review_yes_still_goes_to_approval(self):
        self._set(AutonomyLevel.APPROVE_THEN_EXECUTE)
        approval, _, _ = self._run(self._free_form(True))
        approval.assert_called_once()


if __name__ == "__main__":
    unittest.main()
