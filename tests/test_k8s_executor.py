"""
tests/test_k8s_executor.py — exec_method: k8s 실행 경로 테스트 (Phase 3 k8s 연동).

conftest.py의 isolate_autonomy_store(autouse)가 매 테스트마다 임시 DB로 격리한다.
subprocess.run은 test_autonomy.py와 동일하게 patch("subprocess.run")으로 목킹한다
(src/executor.py가 `import subprocess` 후 subprocess.run을 호출하므로, 전역 patch가
모듈 레벨 참조에도 그대로 적용됨).

exec_method는 config/servers.yaml → TARGET_SERVER 환경변수로 선택되지만, 여기서는
실제 설정 파일에 손대지 않고 ActionExecutor 인스턴스의 exec_method/k8s_namespace
속성을 테스트에서 직접 덮어써서 k8s 경로만 격리해서 검증한다.
"""
import unittest
from unittest.mock import MagicMock, patch

from src.executor import ActionExecutor


class TestExecutorDefaultsToSystemd(unittest.TestCase):
    """회귀 테스트: TARGET_SERVER 미설정 시 기존(systemd) 동작이 그대로여야 한다."""

    def test_default_exec_method_is_systemd(self):
        ex = ActionExecutor()
        self.assertEqual(ex.exec_method, "systemd")

    def test_default_k8s_namespace_is_default(self):
        ex = ActionExecutor()
        self.assertEqual(ex.k8s_namespace, "default")

    def test_default_docker_target_app_matches_gcp_primary_config(self):
        """gcp-primary(config/servers.yaml)의 docker_target_app이 그대로 읽혀야 한다."""
        ex = ActionExecutor()
        self.assertEqual(ex.docker_target_app, "mlops_target_app")


class TestRestartDeploymentK8s(unittest.TestCase):
    def setUp(self):
        self.ex = ActionExecutor()
        self.ex.exec_method = "k8s"
        self.ex.k8s_namespace = "default"

    def test_restart_service_dispatches_to_k8s_when_exec_method_is_k8s(self):
        with patch.object(self.ex, "_restart_deployment_k8s", return_value=(True, None)) as mock_k8s:
            ok, err = self.ex._restart_service("target-app")
        mock_k8s.assert_called_once_with("target-app")
        self.assertTrue(ok)
        self.assertIsNone(err)

    def test_restart_deployment_k8s_runs_rollout_restart_then_verifies(self):
        with patch("subprocess.run") as mock_run, \
             patch.object(self.ex, "_verify_deployment_ready", return_value=True) as mock_verify:
            mock_run.return_value = MagicMock(returncode=0, stdout="deployment.apps/target-app restarted", stderr="")
            ok, err = self.ex._restart_deployment_k8s("target-app")

        mock_run.assert_called_once_with(
            ["kubectl", "rollout", "restart", "deployment/target-app", "-n", "default"],
            capture_output=True, text=True, shell=False, timeout=30,
        )
        mock_verify.assert_called_once_with("target-app")
        self.assertTrue(ok)
        self.assertIsNone(err)

    def test_restart_deployment_k8s_rolls_back_when_not_ready(self):
        with patch("subprocess.run") as mock_run, \
             patch.object(self.ex, "_verify_deployment_ready", return_value=False):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            ok, err = self.ex._restart_deployment_k8s("target-app")

        # 첫 호출: rollout restart, 두 번째 호출: rollout undo (검증 실패 시 롤백)
        self.assertEqual(mock_run.call_count, 2)
        undo_call = mock_run.call_args_list[1]
        self.assertEqual(
            undo_call.args[0],
            ["kubectl", "rollout", "undo", "deployment/target-app", "-n", "default"],
        )
        self.assertFalse(ok)
        self.assertIn("롤아웃 미완료", err)

    def test_restart_deployment_k8s_reports_command_failure(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="deployments.apps \"nope\" not found")
            ok, err = self.ex._restart_deployment_k8s("nope")

        self.assertFalse(ok)
        self.assertIn("not found", err)


class TestKillPodK8s(unittest.TestCase):
    def setUp(self):
        self.ex = ActionExecutor()
        self.ex.exec_method = "k8s"
        self.ex.k8s_namespace = "default"

    def test_kill_process_dispatches_to_k8s_when_exec_method_is_k8s(self):
        with patch.object(self.ex, "_kill_pod_k8s", return_value=(True, None)) as mock_k8s:
            ok, err = self.ex._kill_process("target-app")
        mock_k8s.assert_called_once_with("target-app")
        self.assertTrue(ok)
        self.assertIsNone(err)

    def test_kill_pod_k8s_deletes_by_label_then_verifies_replacement_running(self):
        with patch("subprocess.run") as mock_run, \
             patch.object(self.ex, "_verify_pod_running", return_value=True) as mock_verify:
            mock_run.return_value = MagicMock(returncode=0, stdout="pod \"target-app-abc\" deleted", stderr="")
            ok, err = self.ex._kill_pod_k8s("target-app")

        mock_run.assert_called_once_with(
            ["kubectl", "delete", "pod", "-l", "app=target-app", "-n", "default"],
            capture_output=True, text=True, shell=False, timeout=15,
        )
        mock_verify.assert_called_once_with("target-app")
        self.assertTrue(ok)
        self.assertIsNone(err)

    def test_kill_pod_k8s_no_rollback_on_verify_failure(self):
        """pkill과 동일하게 kill 계열은 롤백이 없다 — 검증 실패해도 추가 kubectl 호출 없음."""
        with patch("subprocess.run") as mock_run, \
             patch.object(self.ex, "_verify_pod_running", return_value=False):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            ok, err = self.ex._kill_pod_k8s("target-app")

        mock_run.assert_called_once()
        self.assertFalse(ok)
        self.assertIn("Running 미확인", err)

    def test_kill_pod_k8s_reports_command_failure(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="No resources found")
            ok, err = self.ex._kill_pod_k8s("ghost")

        self.assertFalse(ok)
        self.assertIn("No resources found", err)


class TestProcessNameValidationAppliesBeforeK8sDispatch(unittest.TestCase):
    """이름 검증(_validate_process_name)은 exec_method와 무관하게 항상 먼저 적용돼야 한다."""

    def setUp(self):
        self.ex = ActionExecutor()
        self.ex.exec_method = "k8s"

    def test_restart_service_blocks_flag_injection_before_k8s_dispatch(self):
        with patch.object(self.ex, "_restart_deployment_k8s") as mock_k8s:
            ok, err = self.ex._restart_service("-n kube-system")
        mock_k8s.assert_not_called()
        self.assertFalse(ok)
        self.assertIn("Security Block", err)

    def test_kill_process_blocks_flag_injection_before_k8s_dispatch(self):
        with patch.object(self.ex, "_kill_pod_k8s") as mock_k8s:
            ok, err = self.ex._kill_process("--all")
        mock_k8s.assert_not_called()
        self.assertFalse(ok)
        self.assertIn("Security Block", err)


class TestArbitraryKubectlStillBlockedForLlmCommands(unittest.TestCase):
    """
    범위 결정 회귀 테스트: exec_method: k8s가 도입돼도 EXECUTE_LLM_COMMAND의 자유형
    kubectl 명령은 여전히 막혀야 한다 (계획 문서에 명시된 의도적 범위 제외).

    ALLOWED_COMMANDS가 "명령어당 첫 번째 인자만 검증"하는 구조라 `kubectl rollout
    restart` 같은 다중 서브커맨드를 안전하게 표현할 수 없음 — 누군가 이 화이트리스트에
    아무 생각 없이 "kubectl"만 추가하면 이 안전장치가 깨진다는 걸 여기서 고정해둔다.
    """

    def setUp(self):
        self.ex = ActionExecutor()
        self.ex.exec_method = "k8s"

    def test_llm_generated_kubectl_command_is_whitelist_blocked(self):
        tokens, err = self.ex._validate_command("kubectl delete deployment target-app -n default")
        self.assertEqual(tokens, [])
        self.assertIsNotNone(err)
        self.assertEqual(err["error_type"], "SecurityBlock")


class TestK8sExecutorSurvivesMissingKubectlBinary(unittest.TestCase):
    """kubectl 바이너리 자체가 없는 환경(FileNotFoundError)에서도 크래시하지 않아야 한다."""

    def setUp(self):
        self.ex = ActionExecutor()
        self.ex.exec_method = "k8s"
        self.ex.k8s_namespace = "default"

    def test_restart_deployment_k8s_handles_missing_kubectl_gracefully(self):
        with patch("subprocess.run", side_effect=FileNotFoundError("kubectl not found")):
            ok, err = self.ex._restart_deployment_k8s("target-app")
        self.assertFalse(ok)
        self.assertIsInstance(err, str)

    def test_kill_pod_k8s_handles_missing_kubectl_gracefully(self):
        with patch("subprocess.run", side_effect=FileNotFoundError("kubectl not found")):
            ok, err = self.ex._kill_pod_k8s("target-app")
        self.assertFalse(ok)
        self.assertIsInstance(err, str)


class TestResolveK8sTargetName(unittest.TestCase):
    """
    _resolve_k8s_target_name() 회귀 테스트 (2026-09-10 추가).

    배경: 로컬 minikube 실측에서 L1 캐시의 target_process가 Memory_Leak은 None,
    Process_Crash는 범용 placeholder("pod")로 나와 실제 k8s 리소스명과 안 맞는 걸
    발견 — servers.yaml의 k8s_target_app으로 폴백하되, "존재 확인 없이 신뢰하지
    않는다"가 핵심이라 존재 여부 확인 분기를 정확히 검증한다.
    """

    def setUp(self):
        self.ex = ActionExecutor()
        self.ex.exec_method = "k8s"
        self.ex.k8s_namespace = "default"
        self.ex.k8s_target_app = "target-app"

    def test_none_target_falls_back_without_kubectl_call(self):
        with patch("subprocess.run") as mock_run:
            resolved = self.ex._resolve_k8s_target_name(None)
        mock_run.assert_not_called()
        self.assertEqual(resolved, "target-app")

    def test_placeholder_target_falls_back_when_deployment_missing(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="not found")
            resolved = self.ex._resolve_k8s_target_name("pod")
        mock_run.assert_called_once_with(
            ["kubectl", "get", "deployment", "pod", "-n", "default"],
            capture_output=True, text=True, shell=False, timeout=10,
        )
        self.assertEqual(resolved, "target-app")

    def test_real_target_kept_when_deployment_exists(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="target-app", stderr="")
            resolved = self.ex._resolve_k8s_target_name("target-app-v2")
        mock_run.assert_called_once()
        self.assertEqual(resolved, "target-app-v2")

    def test_already_fallback_value_skips_existence_check(self):
        with patch("subprocess.run") as mock_run:
            resolved = self.ex._resolve_k8s_target_name("target-app")
        mock_run.assert_not_called()
        self.assertEqual(resolved, "target-app")

    def test_no_fallback_configured_returns_target_unchanged(self):
        self.ex.k8s_target_app = None
        with patch("subprocess.run") as mock_run:
            resolved = self.ex._resolve_k8s_target_name(None)
        mock_run.assert_not_called()
        self.assertIsNone(resolved)

    def test_kubectl_error_treated_as_not_found(self):
        with patch("subprocess.run", side_effect=FileNotFoundError("kubectl not found")):
            resolved = self.ex._resolve_k8s_target_name("pod")
        self.assertEqual(resolved, "target-app")

    def test_restart_service_resolves_before_validating(self):
        with patch.object(self.ex, "_resolve_k8s_target_name", return_value="target-app") as mock_resolve, \
             patch.object(self.ex, "_restart_deployment_k8s", return_value=(True, None)):
            self.ex._restart_service(None)
        mock_resolve.assert_called_once_with(None)

    def test_kill_process_resolves_before_validating(self):
        with patch.object(self.ex, "_resolve_k8s_target_name", return_value="target-app") as mock_resolve, \
             patch.object(self.ex, "_kill_pod_k8s", return_value=(True, None)):
            self.ex._kill_process("pod")
        mock_resolve.assert_called_once_with("pod")

    def test_systemd_path_never_calls_resolver(self):
        """회귀: exec_method가 systemd일 땐 resolver 자체를 호출하지 않아야 한다."""
        self.ex.exec_method = "systemd"
        with patch.object(self.ex, "_resolve_k8s_target_name") as mock_resolve, \
             patch.object(self.ex, "_systemd_unit_exists", return_value=True), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="active", stderr="")
            self.ex._restart_service("nginx")
        mock_resolve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
