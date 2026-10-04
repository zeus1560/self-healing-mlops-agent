"""
승인 대기 타임아웃·종료 시 행을 expired로 표시한다 (2026-10-05).

예전엔 실행기가 타임아웃으로 대기를 끝내도 pending_approvals 행이 status='pending'으로 영원히
남았고, 실행기 대기(300초)가 토큰 유효시간(10분)보다 짧아 그 사이에 들어온 승인은 기록만 되고
실행되지 않았다. DB 경로는 conftest의 isolate_approval_store가 임시 파일로 바꿔 준다.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src import approval_store, executor as executor_mod  # noqa: E402
from src.executor import ActionExecutor  # noqa: E402


class TestAwaitApprovalMarksExpired(unittest.TestCase):
    def setUp(self):
        approval_store.init_table()
        self.ex = ActionExecutor()
        self.tokens = []
        orig = approval_store.create_request

        def capture(*a, **k):
            t = orig(*a, **k)
            self.tokens.append(t)
            return t

        self.patches = [
            patch.object(approval_store, "create_request", side_effect=capture),
            patch.object(executor_mod, "_APPROVAL_TIMEOUT_SEC", 0.05),
            patch.object(executor_mod, "_APPROVAL_POLL_INTERVAL", 0.01),
            patch.object(executor_mod, "get_chatops_client", return_value=MagicMock()),
            patch.dict(os.environ, {"AUTO_APPROVE": "false"}),
            patch.object(executor_mod.sys.stdin, "isatty", return_value=False),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def _status(self):
        return approval_store.get_request(self.tokens[-1])

    def test_timeout_marks_row_expired(self):
        self.assertEqual(self.ex._await_approval("systemctl restart nginx", "ERR"), "timeout")
        row = self._status()
        self.assertEqual(row["status"], "expired")
        self.assertEqual(row["decided_by"], "system:timeout")

    def test_late_approval_after_timeout_is_refused(self):
        self.ex._await_approval("systemctl restart nginx", "ERR")
        self.assertFalse(approval_store.set_decision(self.tokens[-1], "approved", "telegram:1"))
        self.assertEqual(self._status()["status"], "expired")

    def test_shutdown_marks_row_expired(self):
        with patch.object(executor_mod, "_is_shutting_down", return_value=True):
            self.assertEqual(self.ex._await_approval("systemctl restart nginx", "ERR"), "shutdown")
        self.assertEqual(self._status()["decided_by"], "system:shutdown")

    def test_approved_in_time_is_not_overwritten(self):
        def approve_on_poll(token):
            approval_store.set_decision(token, "approved", "telegram:1")
            return "approved"

        with patch.object(approval_store, "get_status", side_effect=approve_on_poll):
            self.assertEqual(self.ex._await_approval("systemctl restart nginx", "ERR"), "approved")
        self.assertEqual(self._status()["status"], "approved")


class TestMarkExpired(unittest.TestCase):
    def setUp(self):
        approval_store.init_table()

    def test_only_pending_rows_change(self):
        t1 = approval_store.create_request("cmd", "log", "r")
        t2 = approval_store.create_request("cmd", "log", "r")
        approval_store.set_decision(t2, "rejected", "web:1")
        self.assertTrue(approval_store.mark_expired(t1))
        self.assertFalse(approval_store.mark_expired(t2))
        self.assertEqual(approval_store.get_request(t1)["status"], "expired")
        self.assertEqual(approval_store.get_request(t2)["status"], "rejected")

    def test_row_is_kept_not_deleted(self):
        t = approval_store.create_request("cmd", "log", "r")
        approval_store.mark_expired(t)
        self.assertIsNotNone(approval_store.get_request(t))


if __name__ == "__main__":
    unittest.main()
