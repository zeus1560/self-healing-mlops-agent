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

import urllib.error  # noqa: E402

from src import autonomy_store, llm_engine, llm_mode  # noqa: E402
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

    def _free_form(self, safe, error=False, reasoning="r"):
        return AgentResponse(
            error_category=CATEGORY, severity="HIGH", action_type=ActionType.EXECUTE_LLM_COMMAND,
            reasoning=reasoning, resolution_source="L1_CACHE", command="systemctl restart worker",
            self_reflection_safe=safe, self_reflection_error=error,
        )

    def _structured(self, safe, error=False):
        return AgentResponse(
            error_category=CATEGORY, severity="HIGH", action_type=ActionType.RESTART_SERVICE,
            reasoning="r", resolution_source="L2_LLM", target_process="worker",
            self_reflection_safe=safe, self_reflection_error=error,
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


class TestReviewFailureFailsClosed(_Base):
    """검토 시도 후 실패(Groq·Ollama 모두) — auto에서도 승인 대기, 설계상 생략과 구분."""

    def test_auto_review_failed_free_form_waits_for_approval(self):
        self._set(AutonomyLevel.AUTO)
        approval, run, _ = self._run(self._free_form(None, error=True))
        approval.assert_called_once()
        run.assert_not_called()

    def test_auto_review_failed_structured_waits_for_approval(self):
        self._set(AutonomyLevel.AUTO)
        approval, _, restart = self._run(self._structured(None, error=True))
        approval.assert_called_once()
        restart.assert_not_called()

    def test_auto_l1_seed_restart_skipped_by_design_executes(self):
        """L1 시드 RESTART_SERVICE는 설계상 검토 생략(safe=True, error=False) — auto면 실행."""
        self._set(AutonomyLevel.AUTO)
        resp = AgentResponse(
            error_category=CATEGORY, severity="HIGH", action_type=ActionType.RESTART_SERVICE,
            reasoning="l1", resolution_source="L1_CACHE", target_process="worker",
        )
        resp = llm_engine._reflect_on_l1_hit(resp, "worker hung")
        self.assertFalse(resp.self_reflection_error)
        approval, _, restart = self._run(resp)
        approval.assert_not_called()
        restart.assert_called_once()

    def test_approve_level_review_failed_shows_warning_on_approval_screen(self):
        self._set(AutonomyLevel.APPROVE_THEN_EXECUTE)
        warning = llm_engine._review_failed_warning("Groq 제안", "자가 반성 검증 요청 실패(...)", "cmd")
        approval, _, _ = self._run(self._free_form(None, error=True, reasoning=warning))
        approval.assert_called_once()
        self.assertIn("자가 반성 검토 실패", approval.call_args.kwargs["explanation"])


class TestReviewFailureIsReportedNotConservativePass(unittest.TestCase):
    """_reflect_on_command가 Groq·Ollama 모두 실패하면 True(보수적 통과)가 아니라 None을 낸다."""

    def _fail_all(self):
        return patch.object(llm_engine.urllib.request, "urlopen",
                            side_effect=urllib.error.URLError("rate limited / down (test)"))

    def test_reflect_returns_none_when_groq_and_ollama_fail(self):
        with patch.object(llm_engine, "_is_groq_available", return_value=True), \
             patch.object(llm_engine.time, "sleep"), self._fail_all():
            safe, rationale = llm_engine._reflect_on_command("kill -TERM 4821", "ERROR", "ctx")
        self.assertIsNone(safe)
        # run_bias_injection_test.py의 폴백 표식이 유지돼야 한다
        self.assertIn("자가 반성 검증 요청 실패", rationale)

    def test_free_form_response_marks_review_error(self):
        with patch.object(llm_engine, "_reflect_on_command", return_value=(None, "자가 반성 검증 요청 실패: x")):
            resp = llm_engine._make_llm_response("kill -TERM 4821", "ERROR", "ctx", "Groq")
        self.assertTrue(resp.self_reflection_error)
        self.assertIsNone(resp.self_reflection_safe)
        self.assertIn("자가 반성 검토 실패", resp.reasoning)

    def test_structured_response_marks_review_error(self):
        resp = AgentResponse(
            error_category="LLM_Inferred", severity="HIGH", action_type=ActionType.KILL_PROCESS,
            reasoning="routed", resolution_source="L2_LLM", target_process="worker",
        )
        with patch.object(llm_engine, "_reflect_on_command", return_value=(None, "자가 반성 검증 요청 실패: x")):
            resp = llm_engine._apply_self_reflection(resp, "ERROR", "ctx", "진단 에이전트 구조화 라우팅")
        self.assertTrue(resp.self_reflection_error)
        self.assertIn("자가 반성 검토 실패", resp.reasoning)

    def test_ollama_fallback_success_is_not_a_failure(self):
        """Groq 실패 → Ollama 폴백이 판정하면 그 판정을 그대로 쓴다(실패 아님)."""
        import json as _json

        class _Resp:
            def __init__(self, body): self._b = _json.dumps(body).encode()
            def read(self): return self._b
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake(req, *a, **k):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "groq" in url:
                raise urllib.error.URLError("429")
            return _Resp({"response": "YES: matches"})

        with patch.object(llm_engine, "_is_groq_available", return_value=True), \
             patch.object(llm_engine.time, "sleep"), \
             patch.object(llm_engine.urllib.request, "urlopen", side_effect=fake):
            safe, _ = llm_engine._reflect_on_command("kill -TERM 4821", "ERROR", "ctx")
        self.assertTrue(safe)


if __name__ == "__main__":
    unittest.main()
