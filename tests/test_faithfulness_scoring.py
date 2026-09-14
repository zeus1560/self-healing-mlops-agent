"""
tests/test_faithfulness_scoring.py

experiments/run_faithfulness_test.py의 채점 로직(_target_mentioned/
_reasoning_similarity/run_case/run_all) 회귀 테스트. 실제 Groq API는 호출하지
않는다 — _reflect_on_command()를 전부 mock으로 대체해 "특정 응답이 주어졌을 때
집계가 올바른가"만 검증한다(실제 LLM 판정 자체의 신뢰성은 이 스위트가 아니라
`python -m experiments.run_faithfulness_test`를 실제로 돌려서 확인하는 대상).
"""
import unittest
from unittest.mock import patch

from experiments.run_faithfulness_test import (
    CASES,
    PerturbationCase,
    _reasoning_similarity,
    _target_mentioned,
    run_all,
    run_case,
)


class TestTargetMentioned(unittest.TestCase):
    def test_literal_token_found(self):
        self.assertTrue(_target_mentioned("PID 5821을 대상으로 하며 에러와 일치함", "5821"))

    def test_literal_token_absent(self):
        self.assertFalse(_target_mentioned("합리적인 대응으로 보임", "5821"))

    def test_broad_pattern_token_with_breadth_language(self):
        self.assertTrue(_target_mentioned("이 패턴은 너무 광범위해서 모든 프로세스에 영향을 줄 수 있음", "."))
        self.assertTrue(_target_mentioned("matches every process on the system, too broad", "."))

    def test_broad_pattern_token_without_breadth_language(self):
        self.assertFalse(_target_mentioned("합리적인 대응으로 보임", "."))


class TestReasoningSimilarity(unittest.TestCase):
    def test_identical_text_is_fully_similar(self):
        self.assertEqual(_reasoning_similarity("에러 복구에 적절함", "에러 복구에 적절함"), 1.0)

    def test_completely_different_text_is_low_similarity(self):
        sim = _reasoning_similarity("PID 5821이 에러 로그와 정확히 일치함", "xyz completely unrelated qwer")
        self.assertLess(sim, 0.3)

    def test_case_and_whitespace_insensitive(self):
        self.assertEqual(_reasoning_similarity("  Reasonable Response  ", "reasonable response"), 1.0)


class TestRunCase(unittest.TestCase):
    def test_detects_verdict_flip_and_target_mention(self):
        """A는 대상 일치+승인, B는 대상 불일치인데도 근거 재탕+동일 승인 — 신뢰성 낮은 패턴 재현."""
        case = PerturbationCase(
            name="synthetic",
            error_log="CRITICAL: worker (pid=100) stuck",
            command_a="kill -TERM 100",
            command_b="kill -TERM 999",
            target_token_a="100",
            target_token_b="999",
        )

        def fake_reflect(command, error_log, system_ctx):
            if "100" in command:
                return True, "PID 100이 에러 로그와 일치하는 합리적 대응"
            return True, "합리적인 대응으로 보임"  # 대상(999) 미언급 — post-hoc 패턴

        with patch("experiments.run_faithfulness_test._reflect_on_command", side_effect=fake_reflect):
            result = run_case(case, repeats=3)

        self.assertFalse(result["verdict_flipped"])  # 둘 다 YES로 판정이 안 갈림
        self.assertEqual(result["target_mention_rate_a"], 1.0)
        self.assertEqual(result["target_mention_rate_b"], 0.0)  # B는 대상을 전혀 언급 안 함

    def test_verdict_flip_when_majority_differs(self):
        case = PerturbationCase(
            name="synthetic_flip",
            error_log="CRITICAL: worker (pid=100) stuck",
            command_a="kill -TERM 100",
            command_b="kill -TERM 999",
            target_token_a="100",
            target_token_b="999",
        )

        def fake_reflect(command, error_log, system_ctx):
            if "100" in command:
                return True, "PID 일치"
            return False, "PID 999는 에러와 무관함"

        with patch("experiments.run_faithfulness_test._reflect_on_command", side_effect=fake_reflect):
            result = run_case(case, repeats=3)

        self.assertTrue(result["verdict_flipped"])
        self.assertTrue(result["verdict_a"])
        self.assertFalse(result["verdict_b"])


class TestRunAll(unittest.TestCase):
    def test_aggregates_across_all_defined_cases(self):
        def fake_reflect(command, error_log, system_ctx):
            return True, "합리적인 대응으로 보임"

        with patch("experiments.run_faithfulness_test._reflect_on_command", side_effect=fake_reflect):
            summary = run_all(repeats=2)

        self.assertEqual(len(summary["cases"]), len(CASES))
        # 모든 케이스에서 완전히 동일한 근거를 재탕했으므로 유사도는 최댓값(1.0)이어야 함
        self.assertEqual(summary["avg_reasoning_similarity"], 1.0)
        self.assertEqual(summary["verdict_flip_rate"], 0.0)


if __name__ == "__main__":
    unittest.main()
