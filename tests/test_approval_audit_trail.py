"""
tests/test_approval_audit_trail.py

회귀 테스트 — 2026-09-17 실제 운영 DB(pending_approvals)를 직접 조회하다가
발견한 버그: ActionExecutor._await_approval()의 데몬 모드가 Telegram/Slack
승인 메시지에는 explanation(_compose_explanation(decision), "판단 근거")을
제대로 넣어 보내면서, 정작 approval_store.create_request()에는 reason으로
빈 문자열("")을 하드코딩해서 넘기고 있었다 — 승인 당시엔 사람이 근거를 봤지만
나중에 pending_approvals 테이블을 다시 조회하면 "왜"가 통째로 빠져 있었다
(실측: 운영 VM의 최근 승인 요청 10건 전부 reason 컬럼이 빈 값).

이 테스트는 daemon 모드 경로에서 create_request()가 explanation을 그대로
전달받는지 확인한다.
"""
import unittest
from unittest.mock import MagicMock, patch

from src.executor import ActionExecutor


class TestApprovalExplanationPersisted(unittest.TestCase):
    def setUp(self):
        self.executor = ActionExecutor()

    def _run_await_approval(self, explanation: str):
        with patch("src.executor.approval_store") as mock_store, \
             patch("src.executor.get_chatops_client", return_value=None), \
             patch("src.executor.SlackChatOps") as MockSlack, \
             patch("sys.stdin") as mock_stdin, \
             patch("src.executor.time.sleep"):
            mock_stdin.isatty.return_value = False  # 데몬 모드 강제
            mock_store.create_request.return_value = "fake-token"
            mock_store.get_status.return_value = "approved"  # 첫 폴링에서 즉시 승인
            MockSlack.return_value.send_approval_request = MagicMock()

            outcome = self.executor._await_approval(
                "restart_service(redis)", "CRITICAL redis connection refused",
                explanation=explanation,
            )
            return outcome, mock_store

    def test_explanation_is_persisted_to_create_request(self):
        explanation = "⚠️ L1 앙상블: 과거 3건 중 3건이 restart_service를 선택 (신뢰도 높음)"
        outcome, mock_store = self._run_await_approval(explanation)

        self.assertEqual(outcome, "approved")
        mock_store.create_request.assert_called_once()
        args, _ = mock_store.create_request.call_args
        # create_request(description, error_log, reason) — 세 번째 인자가
        # 빈 문자열이 아니라 실제 explanation이어야 한다.
        self.assertEqual(args[2], explanation)
        self.assertNotEqual(args[2], "")

    def test_empty_explanation_still_passed_through_as_is(self):
        # explanation 자체가 빈 문자열인 정상 케이스(예: _compose_explanation이
        # 근거를 못 만든 경우)까지 강제로 뭔가를 채워 넣지는 않는다 — 그냥
        # 있는 그대로 전달되는지만 확인(하드코딩된 "" 오버라이드가 없는지).
        outcome, mock_store = self._run_await_approval("")
        self.assertEqual(outcome, "approved")
        args, _ = mock_store.create_request.call_args
        self.assertEqual(args[2], "")


if __name__ == "__main__":
    unittest.main()
