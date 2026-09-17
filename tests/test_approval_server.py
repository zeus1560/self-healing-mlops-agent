"""
tests/test_approval_server.py

Human-in-the-Loop 승인의 마지막 관문(src/approval_server.py)에 대한 테스트 —
지금까지 이 파일은 테스트가 0건이었다(2026-09-17 코드 신뢰도 점검에서 발견).

같이 검증하는 것: 이번에 GET→POST 전환(링크 미리보기 크롤러/백신 스캐너가
자동 GET으로 상태를 바꿔버릴 수 있던 문제) + 승인자 식별자(decided_by) 저장이
실제로 동작하는지.

isolate_approval_store 픽스처(conftest.py, autouse)가 매 테스트마다 격리된
임시 DB를 자동으로 붙여준다.
"""
import unittest

from fastapi.testclient import TestClient

import src.approval_server as approval_server
from src import approval_store


class TestApprovalServer(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(approval_server.app)

    def _new_token(self, command="systemctl restart nginx", reason="테스트 근거"):
        return approval_store.create_request(command, "ERROR nginx down", reason)

    # ── 1. 정상 승인 ──────────────────────────────────────────────────────
    def test_post_approve_changes_status_and_records_identity(self):
        token = self._new_token()

        resp = self.client.post(f"/approve/{token}")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(approval_store.get_status(token), "approved")
        req = approval_store.get_request(token)
        self.assertEqual(req["status"], "approved")
        # TestClient의 기본 클라이언트 주소 — "web:" 접두어로 웹 경로임을 표시.
        self.assertTrue(req["decided_by"].startswith("web:"))

    # ── 2. 정상 거절 ──────────────────────────────────────────────────────
    def test_post_reject_changes_status(self):
        token = self._new_token()

        resp = self.client.post(f"/reject/{token}")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(approval_store.get_status(token), "rejected")

    # ── 3. 만료된 토큰 ────────────────────────────────────────────────────
    def test_expired_token_rejected(self):
        token = self._new_token()
        # expires_at을 과거로 직접 돌려 만료 상태를 재현한다.
        conn = approval_store._conn()
        try:
            conn.execute(
                "UPDATE pending_approvals SET expires_at = '2020-01-01T00:00:00+00:00' "
                "WHERE token = ?", (token,),
            )
            conn.commit()
        finally:
            conn.close()

        resp = self.client.post(f"/approve/{token}")

        self.assertEqual(resp.status_code, 410)  # _EXPIRED_HTML
        self.assertEqual(approval_store.get_status(token), "expired")

    # ── 4. 이미 처리된 토큰 재요청 ────────────────────────────────────────
    def test_already_decided_token_is_idempotent(self):
        token = self._new_token()
        first = self.client.post(f"/approve/{token}")
        self.assertEqual(first.status_code, 200)

        second = self.client.post(f"/reject/{token}")  # 이미 approved인데 거절 재시도

        self.assertEqual(second.status_code, 409)  # _GONE_HTML
        # 두 번째 요청이 상태를 덮어쓰지 않았는지 확인 — 여전히 approved여야 한다.
        self.assertEqual(approval_store.get_status(token), "approved")

    # ── 5. GET 요청으로 승인/거절 시도 (구버전 취약점 회귀 테스트) ───────────
    def test_get_request_to_approve_is_rejected(self):
        token = self._new_token()

        resp = self.client.get(f"/approve/{token}")

        # POST 전용 라우트에 GET을 보내면 FastAPI/Starlette가 405를 반환한다 —
        # 링크 미리보기 크롤러가 GET으로 이 URL을 fetch해도 상태가 안 바뀐다.
        self.assertEqual(resp.status_code, 405)
        self.assertEqual(approval_store.get_status(token), "pending")

    def test_get_request_to_reject_is_rejected(self):
        token = self._new_token()

        resp = self.client.get(f"/reject/{token}")

        self.assertEqual(resp.status_code, 405)
        self.assertEqual(approval_store.get_status(token), "pending")

    # ── 부가: pending 확인 페이지는 여전히 GET(읽기 전용)이어야 한다 ─────────
    def test_pending_page_is_still_get_and_renders_forms(self):
        token = self._new_token(command="pkill -f leaky_worker")

        resp = self.client.get(f"/pending/{token}")

        self.assertEqual(resp.status_code, 200)
        self.assertIn("pkill -f leaky_worker", resp.text)
        # <a href> 링크가 아니라 <form method="post">로 렌더링되는지 확인.
        self.assertIn('<form method="post"', resp.text)
        self.assertNotIn(f'href="http://localhost:8080/approve/{token}"', resp.text)


if __name__ == "__main__":
    unittest.main()
