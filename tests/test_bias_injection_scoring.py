"""
tests/test_bias_injection_scoring.py

experiments/run_bias_injection_test.py의 채점 로직(_bias_acknowledged/
run_case_phrase/run_all) 회귀 테스트. 실제 Groq API는 호출하지 않는다 —
_reflect_on_command()를 전부 mock으로 대체해 "특정 응답이 주어졌을 때 집계가
올바른가"만 검증한다(실제 LLM 판정의 조작 가능성 자체는 이 스위트가 아니라
`python -m experiments.run_bias_injection_test`를 실제로 돌려서 확인하는 대상).
"""
import unittest
from unittest.mock import patch

from experiments.run_bias_injection_test import (
    BIAS_PHRASES,
    BASE_SYSTEM_CTX,
    _bias_acknowledged,
    _is_network_fallback,
    run_all,
    run_case_phrase,
)
from experiments.run_faithfulness_test import CASES


class TestBiasAcknowledged(unittest.TestCase):
    def setUp(self):
        self.authority = next(p for p in BIAS_PHRASES if p.name == "authority")

    def test_acknowledged_when_rationale_cites_bias(self):
        self.assertTrue(
            _bias_acknowledged("on-call SRE already approved this target", self.authority)
        )

    def test_not_acknowledged_when_rationale_gives_unrelated_reason(self):
        self.assertFalse(
            _bias_acknowledged("matches known recovery pattern for this error type", self.authority)
        )


class TestIsNetworkFallback(unittest.TestCase):
    def test_detects_conservative_pass_on_network_failure(self):
        self.assertTrue(
            _is_network_fallback(
                "자가 반성 검증 요청 실패(네트워크 오류 등) — 보수적으로 통과 처리: "
                "<urlopen error [Errno 111] Connection refused>"
            )
        )

    def test_genuine_llm_rationale_is_not_flagged(self):
        self.assertFalse(_is_network_fallback("targets the wrong process, unrelated to the error"))


class TestRunCasePhrase(unittest.TestCase):
    def test_no_manipulation_when_verdict_unaffected_by_bias(self):
        """편향 문구가 있어도 없어도 계속 거부하면 조작 효과는 0이어야 한다."""
        case = CASES[0]
        phrase = BIAS_PHRASES[0]

        def fake_reflect(command, error_log, system_ctx):
            return False, "targets the wrong process, unrelated to the reported error"

        with patch("experiments.run_bias_injection_test._reflect_on_command", side_effect=fake_reflect):
            result = run_case_phrase(case, phrase, repeats=3)

        self.assertEqual(result["baseline_safe_rate"], 0.0)
        self.assertEqual(result["biased_safe_rate"], 0.0)
        self.assertEqual(result["manipulation_delta"], 0.0)
        self.assertIsNone(result["bias_acknowledged_rate"])  # 뒤집힌 사례가 없으니 집계 불가

    def test_manipulation_detected_and_acknowledged(self):
        """편향 주입 시에만 승인으로 뒤집히고, 근거가 편향을 실제로 언급하는 경우."""
        case = CASES[0]
        phrase = BIAS_PHRASES[0]  # authority

        def fake_reflect(command, error_log, system_ctx):
            if phrase.text in system_ctx:
                return True, "on-call senior SRE already approved this target"
            return False, "targets the wrong process, unrelated to the reported error"

        with patch("experiments.run_bias_injection_test._reflect_on_command", side_effect=fake_reflect):
            result = run_case_phrase(case, phrase, repeats=3)

        self.assertEqual(result["baseline_safe_rate"], 0.0)
        self.assertEqual(result["biased_safe_rate"], 1.0)
        self.assertEqual(result["manipulation_delta"], 1.0)
        self.assertEqual(result["bias_acknowledged_rate"], 1.0)

    def test_manipulation_detected_but_not_acknowledged(self):
        """편향 주입 시 뒤집히지만, 근거는 편향과 무관해 보이는 이유를 대는
        unfaithful explanation 패턴."""
        case = CASES[0]
        phrase = BIAS_PHRASES[0]

        def fake_reflect(command, error_log, system_ctx):
            if phrase.text in system_ctx:
                return True, "matches a known safe recovery pattern for this error type"
            return False, "targets the wrong process, unrelated to the reported error"

        with patch("experiments.run_bias_injection_test._reflect_on_command", side_effect=fake_reflect):
            result = run_case_phrase(case, phrase, repeats=3)

        self.assertEqual(result["manipulation_delta"], 1.0)
        self.assertEqual(result["bias_acknowledged_rate"], 0.0)  # 뒤집혔지만 편향은 언급 안 함

    def test_network_fallback_sample_excluded_from_manipulation_delta(self):
        """2026-09-17 VM 실측에서 발견된 실제 버그 재현: Groq/Ollama 둘 다 실패해
        보수적으로 통과(True) 처리된 샘플이 "편향에 흔들린 승인"으로 잘못 집계돼
        manipulation_delta를 부풀리면 안 된다."""
        case = CASES[0]
        phrase = BIAS_PHRASES[0]
        call_count = {"n": 0}

        def fake_reflect(command, error_log, system_ctx):
            call_count["n"] += 1
            # biased 조건(2번째 5회) 중 1건만 네트워크 폴백으로 통과, 나머지는
            # 편향 유무와 무관하게 항상 정확히 거부.
            if call_count["n"] == 6:
                return True, (
                    "자가 반성 검증 요청 실패(네트워크 오류 등) — 보수적으로 통과 처리: "
                    "<urlopen error [Errno 111] Connection refused>"
                )
            return False, "targets the wrong process, unrelated to the reported error"

        with patch("experiments.run_bias_injection_test._reflect_on_command", side_effect=fake_reflect):
            result = run_case_phrase(case, phrase, repeats=5)

        self.assertEqual(result["baseline_fallback_count"], 0)
        self.assertEqual(result["biased_fallback_count"], 1)
        # 유효 샘플(4건) 전부 거부였으므로 네트워크 폴백을 제외하면 조작 효과는 0
        self.assertEqual(result["baseline_safe_rate"], 0.0)
        self.assertEqual(result["biased_safe_rate"], 0.0)
        self.assertEqual(result["manipulation_delta"], 0.0)
        self.assertIsNone(result["bias_acknowledged_rate"])  # 유효 샘플 중 뒤집힌 사례 없음

    def test_biased_system_ctx_includes_base_and_phrase(self):
        case = CASES[0]
        phrase = BIAS_PHRASES[0]
        seen_ctxs = []

        def fake_reflect(command, error_log, system_ctx):
            seen_ctxs.append(system_ctx)
            return False, "irrelevant"

        with patch("experiments.run_bias_injection_test._reflect_on_command", side_effect=fake_reflect):
            run_case_phrase(case, phrase, repeats=1)

        self.assertIn(BASE_SYSTEM_CTX, seen_ctxs[0])
        self.assertNotIn(phrase.text, seen_ctxs[0])  # 첫 호출(baseline)엔 편향 문구가 없어야 함
        self.assertIn(phrase.text, seen_ctxs[1])      # 두 번째 호출(biased)엔 있어야 함


class TestRunAll(unittest.TestCase):
    def test_aggregates_across_all_case_phrase_combinations(self):
        def fake_reflect(command, error_log, system_ctx):
            return False, "targets the wrong process, unrelated to the reported error"

        with patch("experiments.run_bias_injection_test._reflect_on_command", side_effect=fake_reflect):
            summary = run_all(repeats=2)

        self.assertEqual(len(summary["results"]), len(CASES) * len(BIAS_PHRASES))
        self.assertEqual(summary["manipulation_success_rate"], 0.0)
        self.assertIsNone(summary["avg_bias_acknowledged_rate_when_flipped"])
        self.assertEqual(summary["n_flip_cases"], 0)
        self.assertEqual(summary["network_fallback_rate"], 0.0)
        self.assertEqual(summary["n_combos_with_no_valid_samples"], 0)

    def test_all_network_fallback_yields_no_manipulation_signal(self):
        """모든 샘플이 네트워크 폴백이면(극단 케이스) 조작 효과를 0%로 왜곡해
        보고하는 대신 "집계 불가"(None)로 명시해야 한다."""
        def fake_reflect(command, error_log, system_ctx):
            return True, (
                "자가 반성 검증 요청 실패(네트워크 오류 등) — 보수적으로 통과 처리: "
                "<urlopen error [Errno 111] Connection refused>"
            )

        with patch("experiments.run_bias_injection_test._reflect_on_command", side_effect=fake_reflect):
            summary = run_all(repeats=2)

        self.assertIsNone(summary["avg_manipulation_delta"])
        self.assertIsNone(summary["manipulation_success_rate"])
        self.assertEqual(summary["network_fallback_rate"], 1.0)
        self.assertEqual(summary["n_combos_with_no_valid_samples"], len(CASES) * len(BIAS_PHRASES))


if __name__ == "__main__":
    unittest.main()
