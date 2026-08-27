"""
카오스 엔지니어링 target-app 스모크 테스트.

실제 장애(OOM/CPU/디스크풀/프로세스킬)를 주입하는 /inject/* 엔드포인트는
파괴적이라 여기서 호출하지 않는다 — 앱이 정상적으로 임포트되고, 기대하는
라우트가 전부 등록돼 있고, /health가 정상 응답하는지만 확인하는 구조적 스모크
테스트다.
"""
import unittest

from fastapi.testclient import TestClient

# docker-compose가 실제로 쓰는 임포트 경로와 동일하게 shim을 통해 가져온다
# (uvicorn deploy.target_app:app)
import deploy.target_app as target_app_module


class TestTargetAppRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = target_app_module.app
        cls.client = TestClient(cls.app)

    def test_expected_inject_routes_registered(self):
        paths = {route.path for route in self.app.routes}
        for expected in (
            "/health",
            "/inject/oom",
            "/inject/cpu",
            "/inject/diskfull",
            "/inject/process_crash",
            "/stop",
        ):
            self.assertIn(expected, paths, f"{expected} 라우트가 없음")

    def test_health_endpoint_ok(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json().get("status"), "ok")

    def test_inject_routes_are_post_only(self):
        """실수로 GET에도 반응해서 헬스체크나 크롤러가 장애를 유발하지 않는지 확인."""
        for path in ("/inject/oom", "/inject/cpu", "/inject/diskfull", "/inject/process_crash"):
            route = next(r for r in self.app.routes if r.path == path)
            self.assertEqual(set(route.methods) - {"HEAD"}, {"POST"}, f"{path}가 POST 전용이 아님")


if __name__ == "__main__":
    unittest.main()
