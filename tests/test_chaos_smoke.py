"""
카오스 엔지니어링 target-app 스모크 테스트.

실제 장애(OOM/CPU/디스크풀/프로세스킬)를 주입하는 /inject/* 엔드포인트는
파괴적이라 여기서 호출하지 않는다 — 앱이 정상적으로 임포트되고, 기대하는
라우트가 전부 등록돼 있고, /health가 정상 응답하는지만 확인하는 구조적 스모크
테스트다.

반면 permission_denied/path_not_found/config_error(로컬 파일 연산)와
db_connection/network_timeout(실제 소켓 연결 시도, 아무 것도 파괴하지 않음),
memory_leak(90MB만 쓰는 백그라운드 프로세스, cgroup 한도의 극히 일부)는
리소스를 소모하거나 프로세스를 죽이지 않아 파괴적이지 않으므로, 실제로 호출해
evidence 로그에 진짜 예외/증거가 기록되는지까지 검증한다.

auth_error는 비파괴적이지만 여기선 구조적 테스트(라우트 등록·POST 전용)만
한다 — /internal/protected를 자기 자신(127.0.0.1:9000)에게 실제로 호출하는
구조라, FastAPI TestClient는 실제 포트를 바인딩하지 않아 이 자기참조 호출이
ConnectionError로 실패한다(HTTPError로 잡히지 않아 500이 남). 실제 동작은
로컬에서 uvicorn을 실제로 띄워 수동 검증했다(2026-09-08).
"""
import os
import sys
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
            "/inject/permission_denied",
            "/inject/path_not_found",
            "/inject/config_error",
            "/inject/db_connection",
            "/inject/network_timeout",
            "/inject/auth_error",
            "/inject/memory_leak",
            "/stop",
        ):
            self.assertIn(expected, paths, f"{expected} 라우트가 없음")

    def test_health_endpoint_ok(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json().get("status"), "ok")

    def test_inject_routes_are_post_only(self):
        """실수로 GET에도 반응해서 헬스체크나 크롤러가 장애를 유발하지 않는지 확인."""
        for path in (
            "/inject/oom",
            "/inject/cpu",
            "/inject/diskfull",
            "/inject/process_crash",
            "/inject/permission_denied",
            "/inject/path_not_found",
            "/inject/config_error",
            "/inject/db_connection",
            "/inject/network_timeout",
            "/inject/auth_error",
            "/inject/memory_leak",
        ):
            route = next(r for r in self.app.routes if r.path == path)
            self.assertEqual(set(route.methods) - {"HEAD"}, {"POST"}, f"{path}가 POST 전용이 아님")


class TestNonDestructiveInjectors(unittest.TestCase):
    """OS 리소스를 소모하지 않는 5종은 실제로 호출해 진짜 예외 발생을 검증한다."""

    @classmethod
    def setUpClass(cls):
        cls.app = target_app_module.app
        cls.client = TestClient(cls.app)
        # target-app/main.py와 동일한 경로 계산식 — main.py는 importlib shim을 통해
        # 'deploy.target_app_main'이라는 별도 모듈로 로드되므로 shim(target_app_module)엔
        # EVIDENCE_LOG가 노출되지 않는다.
        cls.evidence_log = "/app/data/realtime_system.log" if os.path.isdir("/app/data") else "./data/realtime_system.log"

    def _tail_evidence_log(self, n_lines=5):
        with open(self.evidence_log, "r", encoding="utf-8") as f:
            return f.readlines()[-n_lines:]

    @unittest.skipUnless(sys.platform.startswith("linux"), "exec 권한 비트 의미론은 Linux 컨테이너 배포 환경 전용")
    def test_permission_denied_raises_real_permission_error(self):
        resp = self.client.post("/inject/permission_denied")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json().get("injected"), "permission_denied")
        tail = "".join(self._tail_evidence_log())
        self.assertIn("PermissionError", tail)
        self.assertNotIn("expected PermissionError", tail, "실행 비트 제거가 실제로 EACCES를 유발하지 못함")

    def test_path_not_found_raises_real_file_not_found_error(self):
        resp = self.client.post("/inject/path_not_found")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json().get("injected"), "path_not_found")
        tail = "".join(self._tail_evidence_log())
        self.assertIn("FileNotFoundError", tail)

    def test_config_error_raises_real_json_decode_error(self):
        resp = self.client.post("/inject/config_error")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json().get("injected"), "config_error")
        tail = "".join(self._tail_evidence_log())
        self.assertIn("JSONDecodeError", tail)

    def test_db_connection_raises_real_redis_connection_error(self):
        """127.0.0.1:6379엔 아무 것도 리스닝하지 않아 실제 ConnectionError가 난다."""
        resp = self.client.post("/inject/db_connection")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json().get("injected"), "db_connection")
        tail = "".join(self._tail_evidence_log())
        self.assertIn("redis.exceptions.ConnectionError", tail)
        self.assertNotIn("expected redis.exceptions.ConnectionError", tail, "127.0.0.1:6379에 뭔가 실제로 응답함 — 격리 확인 필요")

    def test_network_timeout_raises_real_operational_error(self):
        """192.0.2.1(RFC 5737 TEST-NET-1)은 응답하는 호스트가 없어 실제로 접속이 타임아웃된다."""
        resp = self.client.post("/inject/network_timeout")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json().get("injected"), "network_timeout")
        tail = "".join(self._tail_evidence_log())
        self.assertIn("psycopg2.OperationalError", tail)

    def test_memory_leak_raises_real_gradual_growth(self):
        """
        90MB(15MB*6단계, cgroup 512m 한도의 극히 일부)만 쓰는 백그라운드 프로세스를
        실제로 띄워 RSS를 psutil로 측정 — 완료까지 최대 ~10초 걸리는 유일한
        '비파괴적' 인젝터(다른 4종은 즉시 끝남).
        """
        resp = self.client.post("/inject/memory_leak")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json().get("injected"), "memory_leak")
        tail = "".join(self._tail_evidence_log())
        self.assertIn("MemoryLeak", tail)
        self.assertNotIn("too few samples", tail, "RSS 샘플링이 실제로 추세를 못 잡음 — 환경 이슈 가능성")

    def test_injectors_clean_up_after_themselves(self):
        """장애 주입용으로 만든 임시 파일이 뒤에 남지 않는지 확인 (컨테이너 /app 오염 방지)."""
        if sys.platform.startswith("linux"):
            self.client.post("/inject/permission_denied")
        self.client.post("/inject/config_error")
        data_dir = "/app/data" if os.path.isdir("/app/data") else "./data"
        names = ("app_config.json",) if not sys.platform.startswith("linux") else ("locked_reload.sh", "app_config.json")
        for name in names:
            self.assertFalse(
                os.path.exists(os.path.join(data_dir, name)),
                f"{name}이 정리되지 않고 남아있음",
            )


if __name__ == "__main__":
    unittest.main()
