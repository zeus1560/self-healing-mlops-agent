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


class TestApprovalTokenNotLogged(unittest.TestCase):
    """§6 B18: 승인 URL의 토큰이 로그에 그대로 남으면 journal 열람자가 승인할 수 있었다."""

    def test_pending_url_token_masked_in_log_but_sent_to_chatops(self):
        token = "vlXvLuSECRETPARTabcdefghijklmnopqrstuvwxyz0123"
        with patch("src.executor.approval_store") as mock_store, \
             patch("src.executor.get_chatops_client", return_value=None), \
             patch("src.executor.SlackChatOps") as MockSlack, \
             patch("sys.stdin") as mock_stdin, \
             patch("src.executor.time.sleep"), \
             self.assertLogs(level="DEBUG") as logs:
            mock_stdin.isatty.return_value = False
            mock_store.create_request.return_value = token
            mock_store.get_status.return_value = "approved"
            ActionExecutor()._await_approval("restart_service(redis)", "redis refused")
        joined = "\n".join(logs.output)
        self.assertNotIn("SECRETPART", joined)
        self.assertIn("/pending/vlXvLu…(가림)", joined)
        # 승인 버튼(콜백)은 토큰이 있어야 동작하므로 ChatOps로는 그대로 간다.
        _, kwargs = MockSlack.return_value.send_approval_request.call_args
        self.assertIn(token, kwargs["reason"])


class TestEmptyExplanationFallback(unittest.TestCase):
    """§6 B18: 근거가 비면 텔레그램이 토큰 URL을 "설명"에 대신 보여 줬다(2026-10-07)."""

    def test_executor_fills_empty_explanation_like_escalation(self):
        from src import autonomy_store
        from src.schemas import ActionType, AgentResponse, AutonomyLevel
        autonomy_store.set_level("DB_Connection", AutonomyLevel.APPROVE_THEN_EXECUTE, "tester")
        d = AgentResponse(error_category="DB_Connection", severity="HIGH",
                          action_type=ActionType.RESTART_SERVICE, target_process="redis",
                          reasoning="", resolution_source="L1_CACHE")
        ex = ActionExecutor()
        with patch.object(ex, "_await_approval", return_value="rejected") as mock_await:
            ex.execute(d, original_error_log="redis://u:pw1234@10.0.0.5:6379 refused\nline2")
        explanation = mock_await.call_args.kwargs["explanation"]
        self.assertEqual(explanation, "[DB_Connection] redis://<CREDS>@<IP>:6379 refused")

    def test_telegram_never_shows_reason_url(self):
        import asyncio
        from unittest.mock import AsyncMock
        from src.telegram_bot import TelegramChatOps
        client = TelegramChatOps.__new__(TelegramChatOps)
        client.enabled, client.chat_id, client._loop = True, "1", None
        send = AsyncMock(return_value=None)
        client.bot = type("B", (), {"send_message": send})()
        client.send_approval_request("ERR", "restart_service(redis)",
                                     "🔐 명령어 확인 및 승인: http://x:8000/pending/SECRETTOK", "")
        text = send.call_args.kwargs["text"]
        self.assertNotIn("SECRETTOK", text)
        self.assertIn("(근거 없음)", text)
        # 버튼(callback_data)에는 토큰이 그대로 있어야 승인이 동작한다.
        markup = send.call_args.kwargs["reply_markup"]
        self.assertIn("approve|SECRETTOK", str(markup))


if __name__ == "__main__":
    unittest.main()
