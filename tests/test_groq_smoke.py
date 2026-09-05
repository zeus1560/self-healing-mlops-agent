"""
Groq L2 백엔드 스모크 테스트 — 실제 네트워크 호출 없이 urllib를 목킹해서 검증한다.

오늘 run_l2_accuracy.py에서 실제로 겪은 두 가지 회귀를 다시 잡기 위한 테스트:
  1. User-Agent 헤더 누락 → 기본 urllib UA가 Cloudflare에 차단(1010)됨
  2. reasoning_effort 파라미터 누락 → qwen3 계열이 <think> 체인에 토큰을 다 쓰고
     max_tokens 내에서 content가 비어버림
GROQ_API_KEY가 없어도(CI 환경) 항상 돌아간다 — 실제 API를 호출하지 않는다.
"""
import json
import unittest
from unittest.mock import patch

import src.llm_engine as llm_engine


class _FakeResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestGroqAvailability(unittest.TestCase):
    def test_unavailable_when_key_empty(self):
        with patch.object(llm_engine, "GROQ_API_KEY", ""):
            self.assertFalse(llm_engine._is_groq_available())

    def test_available_when_key_set(self):
        with patch.object(llm_engine, "GROQ_API_KEY", "gsk_dummy"):
            self.assertTrue(llm_engine._is_groq_available())


class TestGroqRequestShape(unittest.TestCase):
    """실제 API를 호출하지 않고, 만들어지는 요청의 헤더·페이로드만 검증한다."""

    def _call_and_capture(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["headers"] = dict(req.headers)
            captured["payload"] = json.loads(req.data)
            return _FakeResponse({"choices": [{"message": {"content": "systemctl restart nginx"}}]})

        with patch.object(llm_engine, "GROQ_API_KEY", "gsk_dummy"), \
             patch.object(llm_engine.urllib.request, "urlopen", side_effect=fake_urlopen):
            result = llm_engine._call_groq_chat("dummy prompt", max_tokens=24, timeout=5)
        return result, captured

    def test_user_agent_header_present(self):
        """기본 urllib UA는 Cloudflare에 차단되므로 반드시 커스텀 User-Agent가 있어야 한다."""
        _, captured = self._call_and_capture()
        # urllib은 헤더 키를 title-case로 정규화한다 (User-agent)
        ua_keys = [k for k in captured["headers"] if k.lower() == "user-agent"]
        self.assertTrue(ua_keys, "User-Agent 헤더가 없음 — Cloudflare 1010 차단 위험")
        self.assertNotIn("python-urllib", captured["headers"][ua_keys[0]].lower())

    def test_reasoning_effort_none_in_payload(self):
        """qwen3 <think> 체인 방지용 reasoning_effort=none이 페이로드에 있어야 한다."""
        _, captured = self._call_and_capture()
        self.assertEqual(captured["payload"].get("reasoning_effort"), "none")

    def test_authorization_header_uses_api_key(self):
        _, captured = self._call_and_capture()
        auth_keys = [k for k in captured["headers"] if k.lower() == "authorization"]
        self.assertTrue(auth_keys)
        self.assertEqual(captured["headers"][auth_keys[0]], "Bearer gsk_dummy")

    def test_successful_response_returns_content(self):
        result, _ = self._call_and_capture()
        self.assertEqual(result, "systemctl restart nginx")


class TestGroqFallbackChain(unittest.TestCase):
    """_is_groq_available()가 False면 RAGEngine이 Groq를 건너뛰고 폴백 체인으로 넘어간다."""

    def test_is_groq_available_gates_the_fallback_chain(self):
        with patch.object(llm_engine, "GROQ_API_KEY", ""):
            self.assertFalse(llm_engine._is_groq_available())

    def test_run_groq_returns_error_string_on_network_failure(self):
        """네트워크 실패 시에도 예외를 던지지 않고 'ERROR:...' 문자열로 폴백해야 한다."""
        import urllib.error

        def fake_urlopen(req, timeout=None):
            raise urllib.error.URLError("mocked: no network in test")

        with patch.object(llm_engine, "GROQ_API_KEY", "gsk_dummy"), \
             patch.object(llm_engine, "_GROQ_MAX_RETRIES", 1), \
             patch.object(llm_engine.urllib.request, "urlopen", side_effect=fake_urlopen):
            result = llm_engine._run_groq("ERROR: test", "system ctx", timeout=1)
        self.assertTrue(result.startswith("ERROR"))


class TestSelfReflectionReadOnlyBypass(unittest.TestCase):
    """
    2026-09-05 발견: Groq 온도=0 자가 반성 호출도 'systemctl status postgresql' 같은
    완전히 무해한 조회 명령을 호출마다 다른 판정(YES/NO 뒤섞임)으로 거부하는 사례가
    있었음 — 조회성 명령어는 LLM 호출 자체를 건너뛰고 항상 통과해야 한다.
    """

    def test_status_subcommand_is_read_only(self):
        self.assertTrue(llm_engine._is_read_only_command("systemctl status postgresql"))

    def test_restart_subcommand_is_not_read_only(self):
        self.assertFalse(llm_engine._is_read_only_command("systemctl restart postgresql"))

    def test_query_commands_are_read_only(self):
        for cmd in ["df", "free", "ps", "ss", "netstat", "uptime", "echo hi"]:
            self.assertTrue(llm_engine._is_read_only_command(cmd), cmd)

    def test_mutating_commands_are_not_read_only(self):
        for cmd in ["pkill -x nginx", "kill -TERM 123", "systemctl restart nginx"]:
            self.assertFalse(llm_engine._is_read_only_command(cmd), cmd)

    def test_empty_command_is_not_read_only(self):
        self.assertFalse(llm_engine._is_read_only_command(""))

    def test_reflect_skips_llm_call_for_read_only_command(self):
        """읽기 전용 명령어는 urlopen을 아예 호출하지 않고 통과해야 한다."""
        with patch.object(llm_engine, "GROQ_API_KEY", "gsk_dummy"), \
             patch.object(llm_engine.urllib.request, "urlopen") as mock_urlopen:
            safe = llm_engine._reflect_on_command(
                "systemctl status postgresql", "ERROR: timeout", "N/A"
            )
        self.assertTrue(safe)
        mock_urlopen.assert_not_called()

    def test_reflect_still_calls_llm_for_mutating_command(self):
        """부작용 있는 명령어는 그대로 LLM 판정 경로를 탄다 (회귀 방지)."""

        def fake_urlopen(req, timeout=None):
            return _FakeResponse({"choices": [{"message": {"content": "YES"}}]})

        with patch.object(llm_engine, "GROQ_API_KEY", "gsk_dummy"), \
             patch.object(llm_engine.urllib.request, "urlopen", side_effect=fake_urlopen):
            safe = llm_engine._reflect_on_command(
                "systemctl restart postgresql", "ERROR: timeout", "N/A"
            )
        self.assertTrue(safe)


if __name__ == "__main__":
    unittest.main()
