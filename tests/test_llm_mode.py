"""
L2 LLM 모드(LLM_PROVIDER) 회귀 테스트 — 2026-10-04, docs/RESEARCH_SUMMARY.md §6 B2.

로컬 모드(ollama, 기본값)는 어떤 경우에도 Groq로 요청을 보내지 않아야 하고, 클라우드
모드(groq)는 명시적으로 켰을 때만 쓴다. 네트워크는 전부 urllib.request.urlopen mock으로
대체하고, 실제로 나가려던 요청의 URL을 기록해 Groq 호스트가 한 번도 없는지 확인한다.
"""
import json
import logging
import os
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src import autonomy_store, llm_engine, llm_mode  # noqa: E402
from src.executor import ActionExecutor  # noqa: E402
from src.schemas import ActionType, AgentResponse, AutonomyLevel  # noqa: E402

GROQ_HOST = "api.groq.com"


class _FakeResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _url_of(req) -> str:
    return req.full_url if isinstance(req, urllib.request.Request) else str(req)


class _RecordingNetwork:
    """urlopen 대체 — 요청 URL을 기록하고, ollama_up이면 Ollama만 정상 응답한다."""

    def __init__(self, ollama_up: bool, ollama_command: str = "kill -TERM 4821"):
        self.urls: list[str] = []
        self.ollama_up = ollama_up
        self.ollama_command = ollama_command

    def __call__(self, req, *args, **kwargs):
        url = _url_of(req)
        self.urls.append(url)
        if self.ollama_up and url.startswith(llm_engine.OLLAMA_BASE_URL):
            if url.endswith("/api/tags"):
                return _FakeResponse({"models": []})
            body = json.loads(req.data)
            if "safety reviewer" in body.get("prompt", ""):
                return _FakeResponse({"response": "YES: matches error"})
            return _FakeResponse({"response": self.ollama_command})
        raise urllib.error.URLError("connection refused (test)")

    def groq_calls(self) -> list[str]:
        return [u for u in self.urls if GROQ_HOST in u]


class TestResolveProvider(unittest.TestCase):
    def test_values(self):
        cases = {"": "ollama", "ollama": "ollama", " OLLAMA ": "ollama",
                 "groq": "groq", "Groq": "groq", "gorq": "ollama", "openai": "ollama"}
        for raw, expected in cases.items():
            self.assertEqual(llm_mode.resolve_llm_provider(raw), expected, raw)


class TestStartupModeMessages(unittest.TestCase):
    def _msgs(self, raw, key_set):
        provider = llm_mode.resolve_llm_provider(raw)
        return llm_engine._llm_mode_messages(raw, provider, key_set)

    def test_key_without_provider_warns_and_stays_local(self):
        msgs = self._msgs("", True)
        warnings = [m for lvl, m in msgs if lvl == logging.WARNING]
        self.assertEqual(len(warnings), 1)
        self.assertIn("LLM_PROVIDER가 설정되지 않아 로컬 모드로 동작합니다", warnings[0])
        self.assertIn("LLM_PROVIDER=groq", warnings[0])
        self.assertIn("mode=local", msgs[-1][1])

    def test_invalid_value_logs_error_and_falls_back_to_local(self):
        msgs = self._msgs("gorq", True)
        self.assertTrue(any(lvl == logging.ERROR and "gorq" in m for lvl, m in msgs))
        self.assertIn("mode=local", msgs[-1][1])

    def test_groq_without_key_logs_error(self):
        msgs = self._msgs("groq", False)
        self.assertTrue(any(lvl == logging.ERROR and "GROQ_API_KEY가 없습니다" in m for lvl, m in msgs))

    def test_explicit_local_no_warning(self):
        msgs = self._msgs("ollama", True)
        self.assertEqual([lvl for lvl, _ in msgs], [logging.INFO])

    def test_key_value_never_logged(self):
        secret = "gsk_TOPSECRET_should_not_appear"
        with patch.object(llm_engine, "GROQ_API_KEY", secret), \
             patch.object(llm_mode, "LLM_PROVIDER_RAW", "groq"), \
             patch.object(llm_mode, "LLM_PROVIDER", "groq"), \
             self.assertLogs(level="INFO") as cm:
            llm_engine.log_llm_mode()
        self.assertNotIn(secret, "\n".join(cm.output))
        self.assertIn("mode=cloud", "\n".join(cm.output))


class TestLocalModeNeverCallsGroq(unittest.TestCase):
    def _run(self, net, log="ERROR: worker 4821 hung on port 9000"):
        with patch.object(llm_engine, "GROQ_API_KEY", "gsk_dummy"), \
             patch.object(llm_mode, "LLM_PROVIDER", "ollama"), \
             patch.object(llm_engine.urllib.request, "urlopen", side_effect=net), \
             patch.object(llm_engine, "gather_system_context", return_value="ctx"), \
             patch.object(llm_engine, "_is_ipex_installed", return_value=False), \
             patch.object(llm_engine.time, "sleep"):
            return llm_engine.RAGEngine._l2_slow_track(None, log, {}, best_distance=999.0)

    def test_key_set_but_local_mode_uses_only_ollama(self):
        net = _RecordingNetwork(ollama_up=True)
        resp = self._run(net)
        self.assertEqual(net.groq_calls(), [])
        self.assertTrue(any(u.startswith(llm_engine.OLLAMA_BASE_URL) for u in net.urls))
        self.assertEqual(resp.action_type, ActionType.EXECUTE_LLM_COMMAND)

    def test_self_reflection_in_local_mode_never_calls_groq(self):
        net = _RecordingNetwork(ollama_up=True)
        with patch.object(llm_engine, "GROQ_API_KEY", "gsk_dummy"), \
             patch.object(llm_mode, "LLM_PROVIDER", "ollama"), \
             patch.object(llm_engine.urllib.request, "urlopen", side_effect=net):
            llm_engine._reflect_on_command("kill -TERM 4821", "ERROR: hung", "ctx")
        self.assertEqual(net.groq_calls(), [])
        self.assertEqual(len(net.urls), 1)

    def test_ollama_down_falls_to_rule_or_human_without_groq(self):
        net = _RecordingNetwork(ollama_up=False)
        with self.assertLogs(level="WARNING") as cm:
            rule_resp = self._run(net, log="CUDA out of memory while loading model")
        self.assertEqual(net.groq_calls(), [])
        self.assertEqual(rule_resp.resolution_source, "RULE")
        self.assertIn("[로컬 모드] Ollama 연결 불가", "\n".join(cm.output))

        net2 = _RecordingNetwork(ollama_up=False)
        esc_resp = self._run(net2, log="weird unknown failure xyz")
        self.assertEqual(net2.groq_calls(), [])
        self.assertEqual(esc_resp.action_type, ActionType.ESCALATE_TO_HUMAN)


class TestCloudModeGroqDownNoOllama(unittest.TestCase):
    """VM 환경 시나리오: 클라우드 모드인데 Groq 실패 + Ollama 없음 + ipex 미설치."""

    def _run(self, log):
        net = _RecordingNetwork(ollama_up=False)
        sleeps: list[float] = []
        t0 = time.perf_counter()
        with patch.object(llm_engine, "GROQ_API_KEY", "gsk_dummy"), \
             patch.object(llm_mode, "LLM_PROVIDER", "groq"), \
             patch.object(llm_engine.urllib.request, "urlopen", side_effect=net), \
             patch.object(llm_engine, "gather_system_context", return_value="ctx"), \
             patch.object(llm_engine.time, "sleep", side_effect=sleeps.append):
            resp = llm_engine.RAGEngine._l2_slow_track(None, log, {}, best_distance=999.0)
        return resp, net, sleeps, time.perf_counter() - t0

    def test_falls_through_to_rule(self):
        resp, net, sleeps, elapsed = self._run("CUDA out of memory while loading model")
        self.assertEqual(resp.resolution_source, "RULE")
        self.assertEqual(resp.command, "free -h")
        self.assertTrue(net.groq_calls(), "클라우드 모드면 Groq를 먼저 시도해야 함")
        # 재시도 대기 총합: 진단 2s + 생성 2s(_GROQ_RETRY_BASE**1, 재시도 1회씩).
        # 네트워크 타임아웃(진단 15s/생성 30s × 시도 수)은 실제 장애 시 여기에 더해진다.
        self.assertLessEqual(sum(sleeps), 10)
        self.assertLess(elapsed, 5)

    def test_falls_through_to_human_when_no_rule(self):
        resp, _, sleeps, elapsed = self._run("weird unknown failure xyz")
        self.assertEqual(resp.action_type, ActionType.ESCALATE_TO_HUMAN)
        self.assertLessEqual(sum(sleeps), 10)
        self.assertLess(elapsed, 5)


class TestOllamaWarmupSkip(unittest.TestCase):
    """Ollama에 연결할 수 없으면 사전 로딩을 건너뛴다(트레이스 없음). 2026-10-05."""

    class _SyncThread:
        def __init__(self, target, **kw): self._t = target
        def start(self): self._t()

    def _warmup(self, provider):
        net = _RecordingNetwork(ollama_up=False)
        with patch.object(llm_mode, "LLM_PROVIDER", provider), \
             patch.object(llm_engine.threading, "Thread", self._SyncThread), \
             patch.object(llm_engine.urllib.request, "urlopen", side_effect=net), \
             self.assertLogs(level="INFO") as cm:
            llm_engine._ollama_warmup()
        return net, "\n".join(cm.output)

    def test_cloud_mode_skips_quietly(self):
        net, logs = self._warmup("groq")
        self.assertFalse(any(u.endswith("/api/generate") for u in net.urls))
        self.assertIn("사전 로딩 건너뜀", logs)
        self.assertNotIn("Traceback", logs)
        self.assertNotIn("WARNING", logs)

    def test_local_mode_warns_without_traceback(self):
        net, logs = self._warmup("ollama")
        self.assertFalse(any(u.endswith("/api/generate") for u in net.urls))
        self.assertIn("[로컬 모드] Ollama 연결 불가", logs)
        self.assertNotIn("Traceback", logs)


class _TempAutonomyDB(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._orig = autonomy_store._DB_PATH
        autonomy_store._DB_PATH = self.db
        autonomy_store.init_table()

    def tearDown(self):
        autonomy_store._DB_PATH = self._orig
        os.remove(self.db)


class TestLocalModeBlocksAutoPromotion(_TempAutonomyDB):
    def test_set_level_auto_rejected_in_local_mode(self):
        with patch.object(llm_mode, "LLM_PROVIDER", "ollama"):
            with self.assertRaises(autonomy_store.LocalModeAutoNotAllowed):
                autonomy_store.set_level("LLM_Inferred", AutonomyLevel.AUTO, "tester")
            self.assertEqual(autonomy_store.get_level("LLM_Inferred"), AutonomyLevel.APPROVE_THEN_EXECUTE)

    def test_shadow_to_auto_rejected_in_local_mode(self):
        with patch.object(llm_mode, "LLM_PROVIDER", "ollama"):
            with self.assertRaises(autonomy_store.LocalModeAutoNotAllowed):
                autonomy_store.start_shadow("LLM_Inferred", AutonomyLevel.AUTO, "tester")

    def test_other_categories_still_promotable_in_local_mode(self):
        with patch.object(llm_mode, "LLM_PROVIDER", "ollama"):
            autonomy_store.set_level("Process_Crash", AutonomyLevel.AUTO, "tester")
            self.assertEqual(autonomy_store.get_level("Process_Crash"), AutonomyLevel.AUTO)

    def test_cloud_mode_allows_auto(self):
        with patch.object(llm_mode, "LLM_PROVIDER", "groq"):
            autonomy_store.set_level("LLM_Inferred", AutonomyLevel.AUTO, "tester")
            self.assertEqual(autonomy_store.get_level("LLM_Inferred"), AutonomyLevel.AUTO)

    def test_stored_auto_is_capped_after_switching_to_local(self):
        with patch.object(llm_mode, "LLM_PROVIDER", "groq"):
            autonomy_store.set_level("LLM_Inferred", AutonomyLevel.AUTO, "tester")
        with patch.object(llm_mode, "LLM_PROVIDER", "ollama"), self.assertLogs(level="WARNING"):
            self.assertEqual(autonomy_store.get_level("LLM_Inferred"), AutonomyLevel.APPROVE_THEN_EXECUTE)

    def test_default_auto_env_is_capped_in_local_mode(self):
        with patch.object(llm_mode, "LLM_PROVIDER", "ollama"), \
             patch.object(autonomy_store, "DEFAULT_AUTONOMY_LEVEL", AutonomyLevel.AUTO), \
             self.assertLogs(level="WARNING"):
            self.assertEqual(autonomy_store.get_level("LLM_Inferred"), AutonomyLevel.APPROVE_THEN_EXECUTE)


class TestLLMInferredRequiresApproval(_TempAutonomyDB):
    """기본 자율성 레벨(approve_then_execute)에서 LLM_Inferred 명령은 사람 승인을 거친다."""

    def _decision(self):
        return AgentResponse(
            error_category="LLM_Inferred", severity="CRITICAL",
            action_type=ActionType.EXECUTE_LLM_COMMAND,
            reasoning="test", resolution_source="L2_LLM",
            command="systemctl restart nginx",
        )

    def _execute(self, provider, default_level):
        ex = ActionExecutor()
        with patch.object(llm_mode, "LLM_PROVIDER", provider), \
             patch.object(autonomy_store, "DEFAULT_AUTONOMY_LEVEL", default_level), \
             patch.object(ex, "_await_approval", return_value="rejected") as mock_approval, \
             patch("src.executor.subprocess.run") as mock_run:
            ex._execute_llm_command(self._decision(), "ERROR: nginx down")
        return mock_approval, mock_run

    def test_default_level_requires_approval(self):
        for provider in ("ollama", "groq"):
            mock_approval, mock_run = self._execute(provider, AutonomyLevel.APPROVE_THEN_EXECUTE)
            mock_approval.assert_called_once()
            mock_run.assert_not_called()

    def test_local_mode_requires_approval_even_if_default_is_auto(self):
        mock_approval, mock_run = self._execute("ollama", AutonomyLevel.AUTO)
        mock_approval.assert_called_once()
        mock_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
