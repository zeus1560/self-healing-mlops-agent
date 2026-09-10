"""
tests/test_systemd_docker_fallback.py — exec_method: systemd의 docker 폴백 경로 테스트.

배경(2026-09-10 VM 실측): target-app은 systemd 유닛이 아니라 docker-compose
컨테이너인데, AUTO로 승급된 Process_Crash 카테고리가 L1 캐시의 target_process
"rsyslog"(실존하지만 target-app과 무관한 서비스)를 그대로 systemctl에 넘겨 조용히
"성공"으로 재시작해버렸다 — AUTO 모드라 사람이 걸러줄 기회도 없었음. target_process가
실제 systemd 유닛이 아니면 servers.yaml의 docker_target_app으로 대체하도록 수정.

conftest.py의 isolate_autonomy_store(autouse)가 매 테스트마다 임시 DB로 격리한다.
subprocess.run은 다른 executor 테스트와 동일하게 patch("subprocess.run")으로 목킹한다.
"""
import unittest
from unittest.mock import MagicMock, patch

from src.executor import ActionExecutor


class TestSystemdUnitExists(unittest.TestCase):
    def setUp(self):
        self.ex = ActionExecutor()  # exec_method 기본값 systemd

    def test_returns_true_when_unit_loaded(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="loaded\n", stderr="")
            self.assertTrue(self.ex._systemd_unit_exists("nginx"))
        mock_run.assert_called_once_with(
            ["systemctl", "show", "nginx", "--property=LoadState", "--value"],
            capture_output=True, text=True, shell=False, timeout=10,
        )

    def test_returns_false_when_not_found(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="not-found\n", stderr="")
            self.assertFalse(self.ex._systemd_unit_exists("rsyslog-typo"))

    def test_returns_false_for_none_without_subprocess_call(self):
        with patch("subprocess.run") as mock_run:
            self.assertFalse(self.ex._systemd_unit_exists(None))
        mock_run.assert_not_called()

    def test_returns_false_on_flag_injection_attempt(self):
        with patch("subprocess.run") as mock_run:
            self.assertFalse(self.ex._systemd_unit_exists("-n suspicious"))
        mock_run.assert_not_called()

    def test_returns_false_on_exception(self):
        with patch("subprocess.run", side_effect=FileNotFoundError("systemctl not found")):
            self.assertFalse(self.ex._systemd_unit_exists("nginx"))


class TestRestartServiceDockerFallback(unittest.TestCase):
    def setUp(self):
        self.ex = ActionExecutor()
        self.ex.exec_method = "systemd"
        self.ex.docker_target_app = "mlops_target_app"

    def test_falls_back_to_docker_when_unit_missing(self):
        with patch.object(self.ex, "_systemd_unit_exists", return_value=False), \
             patch.object(self.ex, "_restart_container_docker", return_value=(True, None)) as mock_docker:
            ok, err = self.ex._restart_service("rsyslog")
        mock_docker.assert_called_once_with("mlops_target_app")
        self.assertTrue(ok)
        self.assertIsNone(err)

    def test_falls_back_to_docker_when_target_is_none(self):
        with patch.object(self.ex, "_systemd_unit_exists", return_value=False), \
             patch.object(self.ex, "_restart_container_docker", return_value=(True, None)) as mock_docker:
            self.ex._restart_service(None)
        mock_docker.assert_called_once_with("mlops_target_app")

    def test_keeps_systemctl_path_when_real_unit(self):
        with patch.object(self.ex, "_systemd_unit_exists", return_value=True), \
             patch.object(self.ex, "_restart_container_docker") as mock_docker, \
             patch("subprocess.run") as mock_run, \
             patch.object(self.ex, "_verify_service_active", return_value=True):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            ok, err = self.ex._restart_service("nginx")
        mock_docker.assert_not_called()
        mock_run.assert_called_once_with(
            ["systemctl", "restart", "nginx"],
            capture_output=True, text=True, shell=False, timeout=30,
        )
        self.assertTrue(ok)

    def test_no_fallback_configured_keeps_old_behavior(self):
        """회귀: docker_target_app 미설정이면 존재 확인 자체를 안 하고 기존 systemctl 경로 그대로."""
        self.ex.docker_target_app = None
        with patch.object(self.ex, "_systemd_unit_exists") as mock_exists, \
             patch("subprocess.run") as mock_run, \
             patch.object(self.ex, "_verify_service_active", return_value=True):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            self.ex._restart_service("whatever")
        mock_exists.assert_not_called()
        mock_run.assert_called_once_with(
            ["systemctl", "restart", "whatever"],
            capture_output=True, text=True, shell=False, timeout=30,
        )


class TestKillProcessDockerFallback(unittest.TestCase):
    def setUp(self):
        self.ex = ActionExecutor()
        self.ex.exec_method = "systemd"
        self.ex.docker_target_app = "mlops_target_app"

    def test_falls_back_to_docker_when_unit_missing(self):
        with patch.object(self.ex, "_systemd_unit_exists", return_value=False), \
             patch.object(self.ex, "_kill_container_docker", return_value=(True, None)) as mock_docker:
            ok, err = self.ex._kill_process("pod")
        mock_docker.assert_called_once_with("mlops_target_app")
        self.assertTrue(ok)

    def test_keeps_pkill_path_when_real_unit_process(self):
        with patch.object(self.ex, "_systemd_unit_exists", return_value=True), \
             patch.object(self.ex, "_kill_container_docker") as mock_docker, \
             patch("subprocess.run") as mock_run, \
             patch.object(self.ex, "_verify_process_dead", return_value=True):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            ok, err = self.ex._kill_process("nginx")
        mock_docker.assert_not_called()
        mock_run.assert_called_once_with(
            ["pkill", "-x", "nginx"],
            capture_output=True, text=True, shell=False, timeout=10,
        )
        self.assertTrue(ok)


class TestRestartContainerDocker(unittest.TestCase):
    def setUp(self):
        self.ex = ActionExecutor()

    def test_success_path(self):
        with patch("subprocess.run") as mock_run, \
             patch.object(self.ex, "_verify_container_running", return_value=True) as mock_verify:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            ok, err = self.ex._restart_container_docker("mlops_target_app")
        mock_run.assert_called_once_with(
            ["docker", "restart", "mlops_target_app"],
            capture_output=True, text=True, shell=False, timeout=30,
        )
        mock_verify.assert_called_once_with("mlops_target_app")
        self.assertTrue(ok)
        self.assertIsNone(err)

    def test_command_failure(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="No such container")
            ok, err = self.ex._restart_container_docker("ghost")
        self.assertFalse(ok)
        self.assertIn("No such container", err)

    def test_missing_docker_binary_handled_gracefully(self):
        with patch("subprocess.run", side_effect=FileNotFoundError("docker not found")):
            ok, err = self.ex._restart_container_docker("mlops_target_app")
        self.assertFalse(ok)
        self.assertIsInstance(err, str)


class TestKillContainerDocker(unittest.TestCase):
    def setUp(self):
        self.ex = ActionExecutor()

    def test_success_path_no_rollback(self):
        """kill 계열은 pkill/_kill_pod_k8s와 동일하게 롤백이 없다."""
        with patch("subprocess.run") as mock_run, \
             patch.object(self.ex, "_verify_container_running", return_value=True) as mock_verify:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            ok, err = self.ex._kill_container_docker("mlops_target_app")
        mock_run.assert_called_once_with(
            ["docker", "kill", "mlops_target_app"],
            capture_output=True, text=True, shell=False, timeout=15,
        )
        mock_verify.assert_called_once_with("mlops_target_app")
        self.assertTrue(ok)

    def test_command_failure(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="No such container")
            ok, err = self.ex._kill_container_docker("ghost")
        self.assertFalse(ok)
        self.assertIn("No such container", err)


if __name__ == "__main__":
    unittest.main()
