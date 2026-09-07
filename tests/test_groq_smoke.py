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
from src.schemas import ActionType


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
        """대상(PID) 지정 위험이 남는 명령어는 그대로 LLM 판정 경로를 탄다 (회귀 방지)."""

        def fake_urlopen(req, timeout=None):
            return _FakeResponse({"choices": [{"message": {"content": "YES"}}]})

        with patch.object(llm_engine, "GROQ_API_KEY", "gsk_dummy"), \
             patch.object(llm_engine.urllib.request, "urlopen", side_effect=fake_urlopen) as mock_urlopen:
            safe = llm_engine._reflect_on_command(
                "kill -TERM 4821", "ERROR: timeout", "N/A"
            )
        self.assertTrue(safe)
        mock_urlopen.assert_called_once()


class TestSelfReflectionSelfDestructiveKillBlock(unittest.TestCase):
    """
    2026-09-07: 2026-09-04 FP/FN 분석에서 관찰된 'kill -9 1' 제안의 근본 원인 —
    executor.py 화이트리스트는 -9(SIGKILL)는 막아주지만 -TERM/-HUP(허용된
    소프트 신호)로 PID 1(컨테이너 자기 자신/실서버 init)을 지정하면 여전히
    통과한다. LLM 판정에만 맡기면 systemctl restart와 같은 뒤섞임이 재현되므로,
    "PID 1"이라는 사실 자체로 이미 답이 나와있는 이 경우는 LLM 호출 없이 항상 거부한다.
    """

    def test_kill_targeting_pid_1_is_self_destructive(self):
        for cmd in ["kill -TERM 1", "kill -HUP 1", "kill -TERM 1 4821"]:
            self.assertTrue(llm_engine._is_self_destructive_kill(cmd), cmd)

    def test_kill_targeting_other_pid_is_not_self_destructive(self):
        for cmd in ["kill -TERM 4821", "kill -HUP 10234"]:
            self.assertFalse(llm_engine._is_self_destructive_kill(cmd), cmd)

    def test_non_kill_commands_are_never_self_destructive(self):
        for cmd in ["pkill -f zombie_worker", "systemctl restart nginx", ""]:
            self.assertFalse(llm_engine._is_self_destructive_kill(cmd), cmd)

    def test_reflect_rejects_pid_1_without_llm_call(self):
        with patch.object(llm_engine, "GROQ_API_KEY", "gsk_dummy"), \
             patch.object(llm_engine.urllib.request, "urlopen") as mock_urlopen:
            safe = llm_engine._reflect_on_command(
                "kill -TERM 1", "ERROR: process unresponsive", "N/A"
            )
        self.assertFalse(safe)
        mock_urlopen.assert_not_called()


class TestSelfReflectionBoundedStateChangeBypass(unittest.TestCase):
    """
    2026-09-07: \'systemctl restart postgresql\' 노이즈(동일 명령 온도=0에도 YES/NO
    뒤섞임, 2026-09-05 발견)의 근본 원인은 executor.py 화이트리스트가 이미 인자/
    서비스 이름까지 좁게 검증하는 상태변경 명령어까지 매번 LLM한테 재판정을
    맡겼기 때문이다. systemctl(restart/start/stop)/nginx(-s reload·test)/
    journalctl(--vacuum-*)/ulimit는 LLM 호출 없이 결정론적으로 통과시키고,
    대상 지정 위험이 남는 kill/pkill/fuser는 계속 LLM 판정을 거치게 한다.
    """

    def test_systemctl_restart_start_stop_are_bounded(self):
        for cmd in ["systemctl restart postgresql", "systemctl start nginx", "systemctl stop worker"]:
            self.assertTrue(llm_engine._is_bounded_state_change_command(cmd), cmd)

    def test_nginx_reload_and_test_are_bounded(self):
        for cmd in ["nginx -s reload", "nginx test"]:
            self.assertTrue(llm_engine._is_bounded_state_change_command(cmd), cmd)

    def test_journalctl_and_ulimit_are_bounded(self):
        for cmd in ["journalctl --vacuum-size=100M", "ulimit -n 4096"]:
            self.assertTrue(llm_engine._is_bounded_state_change_command(cmd), cmd)

    def test_nginx_invalid_arg_is_not_bounded(self):
        """화이트리스트에 없는 인자 조합은 구조적으로 안전하다고 볼 근거가 없다."""
        self.assertFalse(llm_engine._is_bounded_state_change_command("nginx restart"))

    def test_kill_pkill_fuser_are_not_bounded(self):
        for cmd in ["kill -TERM 4821", "pkill -f zombie_worker", "fuser -k /var/lock/db.lock"]:
            self.assertFalse(llm_engine._is_bounded_state_change_command(cmd), cmd)

    def test_empty_command_is_not_bounded(self):
        self.assertFalse(llm_engine._is_bounded_state_change_command(""))

    def test_reflect_skips_llm_call_for_bounded_state_change_command(self):
        with patch.object(llm_engine, "GROQ_API_KEY", "gsk_dummy"), \
             patch.object(llm_engine.urllib.request, "urlopen") as mock_urlopen:
            safe = llm_engine._reflect_on_command(
                "systemctl restart postgresql", "ERROR: timeout", "N/A"
            )
        self.assertTrue(safe)
        mock_urlopen.assert_not_called()


class TestSelfReflectionNoForcedEscalation(unittest.TestCase):
    """
    2026-09-05 결정: 자가 반성이 명령어를 거부해도 더 이상 강제로
    ESCALATE_TO_HUMAN을 반환하지 않는다 — 이전 동작은 이미 auto로 승급된
    카테고리까지 매번 우회시켜 "승급은 사람만 결정한다"는 Progressive
    Autonomy 원칙과 충돌했다. 대신 EXECUTE_LLM_COMMAND를 그대로 반환해
    executor.py의 autonomy 게이트가 정상적으로 처리하게 하고, 거부 사유만
    reasoning에 남긴다.
    """

    def test_rejected_command_still_returns_execute_action_not_escalation(self):
        with patch.object(llm_engine, "_reflect_on_command", return_value=False):
            response = llm_engine._make_llm_response(
                "systemctl restart postgresql", "ERROR: timeout", "N/A", "Groq"
            )
        self.assertEqual(response.action_type, ActionType.EXECUTE_LLM_COMMAND)
        self.assertEqual(response.command, "systemctl restart postgresql")
        self.assertIn("자가 반성", response.reasoning)

    def test_approved_command_has_plain_reasoning(self):
        with patch.object(llm_engine, "_reflect_on_command", return_value=True):
            response = llm_engine._make_llm_response(
                "systemctl restart postgresql", "ERROR: timeout", "N/A", "Groq"
            )
        self.assertEqual(response.action_type, ActionType.EXECUTE_LLM_COMMAND)
        self.assertNotIn("자가 반성", response.reasoning)


if __name__ == "__main__":
    unittest.main()
