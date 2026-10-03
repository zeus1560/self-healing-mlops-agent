"""
2026-10-04 화이트리스트 점검(docs/RESEARCH_SUMMARY.md §6) 회귀 테스트.

점검 당시 executor._validate_command는 첫 번째 인자만 검사해서 아래 BYPASS_COMMANDS가
전부 통과했다(실측). 이 파일은 그 우회 명령 전부를 거부 테스트로, 정상 사용 형태를
허용 테스트로 고정한다. pgrep/fuser/proc 조회처럼 호스트 상태에 따라 달라지는 부분은
결정론적으로 만들기 위해 필요한 곳만 mock한다.
"""
import os
import subprocess
import sys
import unittest
from unittest.mock import mock_open, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src import executor as executor_mod  # noqa: E402
from src import llm_engine  # noqa: E402
from src.executor import ActionExecutor  # noqa: E402

# 점검표의 실측 우회 명령(당시 전부 PASS) + 같은 부류의 변형.
BYPASS_COMMANDS = [
    # S1 fuser — 파일 경로/마운트 전체/SIGKILL
    "fuser -k /usr/local/cuda/11.1.0/gpu /dev",
    "fuser -k /var/lib/postgresql/data",
    "fuser -k -m /",
    "fuser -k -9 5000/tcp",
    "fuser -k -KILL 5000/tcp",
    "fuser -k 0/tcp",
    "fuser -k 70000/tcp",
    # S2 kill — 전체 프로세스/프로세스 그룹/옵션 구분자
    "kill -TERM -1",
    "kill -HUP 0",
    "kill -TERM -- -1",
    "kill -TERM 1",
    "kill -TERM 001",
    # S3 pkill — 에이전트 자신/전체 매칭/뒤쪽 시그널 플래그/보호 대상
    "pkill -f python",
    "pkill -f .",
    "pkill -f ''",
    "pkill -x python3",
    "pkill -f sshd",
    "pkill -f -9 nginx",
    "pkill -x -KILL nginx",
    "pkill -f -u root .",
    "pkill -x dockerd",
    # S4 systemctl — 보호 서비스
    "systemctl stop sshd",
    "systemctl stop ssh.service",
    "systemctl restart networking",
    "systemctl stop docker",
    "systemctl restart systemd-logind",
    "systemctl stop containerd",
    "systemctl stop dbus",
    # S5 nginx — 중지/종료/임의 설정
    "nginx -s stop",
    "nginx -s quit",
    "nginx -s reload -c /tmp/x.conf",
    "nginx test",
    "nginx",
    # S6 journalctl — 하한 미달/다른 vacuum 옵션
    "journalctl --vacuum-size 1",
    "journalctl --vacuum-size 100M",
    "journalctl --vacuum-time 1s",
    "journalctl --vacuum-time 6d",
    "journalctl --vacuum-files 1",
    "journalctl --vacuum-size 1G --vacuum-files 1",
]


class TestBypassCommandsBlocked(unittest.TestCase):
    def setUp(self):
        self.ex = ActionExecutor()

    def test_all_audited_bypass_commands_are_blocked(self):
        for cmd in BYPASS_COMMANDS:
            with self.subTest(cmd=cmd):
                _, err = self.ex._validate_command(cmd)
                self.assertIsNotNone(err, f"'{cmd}'가 화이트리스트를 통과함")

    def test_agent_own_service_is_blocked_via_env(self):
        with patch.dict(os.environ, {"AGENT_SERVICE_NAME": "self-healing-agent"}):
            for cmd in ("systemctl stop self-healing-agent", "systemctl restart self-healing-agent.service"):
                _, err = self.ex._validate_command(cmd)
                self.assertIsNotNone(err, cmd)

    def test_extra_protected_processes_from_env(self):
        with patch.dict(os.environ, {"PROTECTED_PROCESSES": "vault, consul"}):
            _, err = self.ex._validate_command("systemctl restart vault")
            self.assertIsNotNone(err)

    def test_kill_protected_process_by_pid_is_blocked(self):
        """pkill sshd만 막고 kill -TERM <sshd PID>를 놓치면 안 된다 — /proc/<PID>/comm 확인."""
        with patch.object(executor_mod, "_read_proc_comm", return_value="sshd"):
            _, err = self.ex._validate_command("kill -TERM 4242")
        self.assertIsNotNone(err)

    def test_kill_agent_self_or_parent_is_blocked(self):
        for pid in (os.getpid(), os.getppid()):
            _, err = self.ex._validate_command(f"kill -TERM {pid}")
            self.assertIsNotNone(err, pid)

    def test_kill_nonexistent_pid_is_blocked(self):
        with patch.object(executor_mod, "_read_proc_comm", return_value=None):
            _, err = self.ex._validate_command("kill -TERM 4242")
        self.assertIsNotNone(err)

    def test_pkill_f_matching_too_many_processes_is_blocked(self):
        with patch.object(executor_mod, "_list_pids", return_value=[101, 102, 103, 104, 105, 106]), \
             patch.object(executor_mod, "_read_proc_comm", return_value="worker"):
            _, err = self.ex._validate_command("pkill -f worker")
        self.assertIsNotNone(err)

    def test_pkill_f_matching_protected_process_is_blocked(self):
        with patch.object(executor_mod, "_list_pids", return_value=[101, 202]), \
             patch.object(executor_mod, "_read_proc_comm", side_effect=["worker", "sshd"]):
            _, err = self.ex._validate_command("pkill -f worker")
        self.assertIsNotNone(err)

    def test_pkill_f_matching_agent_itself_is_blocked(self):
        """실제 pgrep — 'pytest' 패턴은 이 테스트를 돌리는 프로세스 자신에 걸린다."""
        _, err = self.ex._validate_command("pkill -f pytest")
        self.assertIsNotNone(err)

    def test_pkill_f_unverifiable_is_blocked(self):
        with patch.object(executor_mod, "_list_pids", return_value=None):
            _, err = self.ex._validate_command("pkill -f worker")
        self.assertIsNotNone(err)

    def test_fuser_port_used_by_protected_process_is_blocked(self):
        with patch.object(executor_mod, "_list_pids", return_value=[os.getpid()]):
            _, err = self.ex._validate_command("fuser -k 22/tcp")
        self.assertIsNotNone(err)


class TestLegitimateCommandsAllowed(unittest.TestCase):
    def setUp(self):
        self.ex = ActionExecutor()

    def assertAllowed(self, cmd):
        _, err = self.ex._validate_command(cmd)
        self.assertIsNone(err, f"'{cmd}'가 거부됨: {err}")

    def test_static_allowed_forms(self):
        for cmd in [
            "nginx -s reload", "nginx -t",
            "journalctl", "journalctl --vacuum-size 1G", "journalctl --vacuum-size 500M",
            "journalctl --vacuum-time 7d", "journalctl --vacuum-time 2weeks",
            "systemctl restart nginx", "systemctl restart postgresql",
            "systemctl restart rsyslog",   # 재시작이 정상 조치(L1 시드 플레이북) — 보호 대상 아님
            "systemctl status sshd", "systemctl status self-healing-agent",
            "pkill -x gunicorn",
            "free -h", "df -h", "ss -s", "ps aux",
        ]:
            with self.subTest(cmd=cmd):
                self.assertAllowed(cmd)

    def test_fuser_port_forms_allowed(self):
        with patch.object(executor_mod, "_list_pids", return_value=[]):
            for cmd in ("fuser -k 5000/tcp", "fuser -k 8080/tcp", "fuser -k 53/udp"):
                with self.subTest(cmd=cmd):
                    self.assertAllowed(cmd)

    def test_pkill_f_narrow_pattern_allowed(self):
        """실제 pgrep — 아무 프로세스에도 안 걸리는 좁은 패턴은 허용."""
        self.assertAllowed("pkill -f zz-no-such-process-7f3a")

    def test_kill_real_unprotected_process_allowed(self):
        proc = subprocess.Popen(["sleep", "30"])
        try:
            self.assertAllowed(f"kill -TERM {proc.pid}")
            self.assertAllowed(f"kill -HUP {proc.pid}")
        finally:
            proc.kill()
            proc.wait()


class TestProtectionHelpers(unittest.TestCase):
    def test_agent_service_autodetected_from_cgroup(self):
        cgroup = "0::/system.slice/self-healing-agent.service\n"
        with patch.dict(os.environ, {"AGENT_SERVICE_NAME": ""}), \
             patch("builtins.open", mock_open(read_data=cgroup)):
            self.assertEqual(executor_mod._agent_service_name(), "self-healing-agent")

    def test_agent_service_env_overrides_cgroup(self):
        with patch.dict(os.environ, {"AGENT_SERVICE_NAME": "my-agent.service"}):
            self.assertEqual(executor_mod._agent_service_name(), "my-agent")

    def test_python_variants_are_protected(self):
        for name in ("python", "python3", "python3.10"):
            self.assertTrue(executor_mod._is_protected_name(name), name)


class TestStructuredActionsUseProtectedList(unittest.TestCase):
    """2-2: L1 구조화 액션(restart_service/kill_process)의 대상에도 같은 보호 목록을 적용한다."""

    def test_protected_targets_rejected(self):
        for name in ("sshd", "docker", "systemd-logind", "python3", "containerd"):
            self.assertIsNone(executor_mod._validate_process_name(name), name)

    def test_playbook_targets_allowed(self):
        for name in ("rsyslog", "nginx", "redis", "postgres_pool", "gunicorn"):
            self.assertEqual(executor_mod._validate_process_name(name), name)

    def test_agent_service_rejected(self):
        with patch.dict(os.environ, {"AGENT_SERVICE_NAME": "self-healing-agent"}):
            self.assertIsNone(executor_mod._validate_process_name("self-healing-agent"))

    def test_restart_service_on_protected_target_never_runs_subprocess(self):
        ex = ActionExecutor()
        ex.exec_method = "systemd"
        ex.docker_target_app = None
        with patch("src.executor.subprocess.run") as mock_run:
            ok, err = ex._restart_service("sshd")
        self.assertFalse(ok)
        mock_run.assert_not_called()

    def test_kill_process_on_protected_target_never_runs_subprocess(self):
        ex = ActionExecutor()
        ex.exec_method = "systemd"
        ex.docker_target_app = None
        with patch("src.executor.subprocess.run") as mock_run:
            ok, err = ex._kill_process("sshd")
        self.assertFalse(ok)
        mock_run.assert_not_called()


class TestL1RestartServiceSkipsLLMReview(unittest.TestCase):
    """2-1: L1 구조화 RESTART_SERVICE는 LLM 검토 생략, 자유형식 systemctl은 검토."""

    def _l1_response(self, **kw):
        from src.schemas import ActionType, AgentResponse
        base = dict(error_category="Process_Crash", severity="HIGH", reasoning="l1",
                    resolution_source="L1_CACHE")
        base.update(kw)
        kw_action = base.pop("action_type")
        return AgentResponse(action_type=kw_action, **base)

    def test_structured_restart_service_no_llm_call(self):
        from src.schemas import ActionType
        resp = self._l1_response(action_type=ActionType.RESTART_SERVICE, target_process="rsyslog")
        with patch.object(llm_engine.urllib.request, "urlopen") as mock_urlopen, \
             patch.object(llm_engine, "gather_system_context", return_value="ctx"):
            out = llm_engine._reflect_on_l1_hit(resp, "rsyslogd stopped")
        mock_urlopen.assert_not_called()
        self.assertTrue(out.self_reflection_safe)

    def test_free_form_systemctl_from_l1_is_reviewed(self):
        """온라인학습 엔트리처럼 LLM이 만든 자유형식 systemctl은 L1 히트여도 검토를 거친다."""
        from src.schemas import ActionType
        resp = self._l1_response(action_type=ActionType.EXECUTE_LLM_COMMAND,
                                 command="systemctl restart worker")
        with patch.object(llm_engine, "_is_groq_available", return_value=False), \
             patch.object(llm_engine.urllib.request, "urlopen") as mock_urlopen, \
             patch.object(llm_engine, "gather_system_context", return_value="ctx"):
            llm_engine._reflect_on_l1_hit(resp, "worker hung")
        mock_urlopen.assert_called_once()

    def test_structured_kill_process_still_reviewed(self):
        from src.schemas import ActionType
        resp = self._l1_response(action_type=ActionType.KILL_PROCESS, target_process="worker")
        with patch.object(llm_engine, "_is_groq_available", return_value=False), \
             patch.object(llm_engine.urllib.request, "urlopen") as mock_urlopen, \
             patch.object(llm_engine, "gather_system_context", return_value="ctx"):
            llm_engine._reflect_on_l1_hit(resp, "worker hung")
        mock_urlopen.assert_called_once()


class TestReflectionBypassUsesShlex(unittest.TestCase):
    """L3: LLM 판정 생략 판정도 화이트리스트와 같은 shlex 규칙을 쓴다."""

    def test_unparseable_command_is_never_bypassed(self):
        for cmd in ("df -h '", 'nginx -s "reload'):
            self.assertFalse(llm_engine._is_read_only_command(cmd), cmd)
            self.assertFalse(llm_engine._is_bounded_state_change_command(cmd), cmd)

    def test_quoted_tokens_parsed_like_whitelist(self):
        self.assertTrue(llm_engine._is_bounded_state_change_command("nginx '-s' reload"))
        self.assertTrue(llm_engine._is_self_destructive_kill("kill -TERM '1'"))

    def test_systemctl_restart_goes_through_llm_review(self):
        with patch.object(llm_engine, "_is_groq_available", return_value=False), \
             patch.object(llm_engine.urllib.request, "urlopen") as mock_urlopen:
            llm_engine._reflect_on_command("systemctl restart postgresql", "ERROR: timeout", "N/A")
        mock_urlopen.assert_called_once()


class TestPromptAndRulesNoLongerSuggestBroadCommands(unittest.TestCase):
    def test_prompt_examples_drop_pkill_python_and_ulimit(self):
        prompt = llm_engine._build_prompt("some error", "ctx")
        self.assertNotIn("pkill -f python", prompt)
        self.assertNotIn("ulimit", prompt)

    def test_rules_use_read_only_commands(self):
        self.assertEqual(llm_engine._rule_based_fallback("CUDA out of memory")[0], "free -h")
        self.assertEqual(llm_engine._rule_based_fallback("Too many open files")[0], "ss -s")


if __name__ == "__main__":
    unittest.main()
