"""
tests/test_log_watcher_volume_surge.py

"로그 볼륨이 급증하면 처리 지연/큐 적체가 생기는지" 확인하는 테스트
(2026-09-17 코드 신뢰도 점검).

src/log_watcher.py::LogTailHandler.on_modified()은 한 번의 파일 변경 이벤트
안에 여러 에러 줄이 들어와도 for 루프로 한 줄씩 순차 처리한다 — 스레드풀/큐/
백프레셔가 전혀 없다. 즉 서로 다른(디바운스에 안 걸리는) 장애가 짧은 시간에
몰리면, 뒤에 처리되는 줄일수록 실제 로그 시각과 파이프라인 가동 시각의 차이
(detection_latency_sec)가 그만큼 벌어진다 — 크래시는 안 나지만 "몇 번째로
몰렸느냐"에 따라 탐지 지연이 선형으로 늘어나는 구조적 한계다.

이 테스트는 그 사실을 크래시 여부(안 죽는다)와 처리 시간(순차 누적된다)
둘 다로 실측한다.
"""
import os
import tempfile
import time
import unittest
from unittest.mock import MagicMock

from src.log_watcher import LogTailHandler
from src.utils.debouncer import LogDebouncer

N_BURST_LINES     = 30
PER_ACTION_DELAY  = 0.05  # 초 — 실제 셸 커맨드 실행/승인 대기를 흉내낸 지연


class _FakeEvent:
    def __init__(self, src_path: str):
        self.src_path = src_path


class TestLogVolumeSurge(unittest.TestCase):
    def setUp(self):
        self.tf = tempfile.mktemp(suffix=".log")
        with open(self.tf, "w", encoding="utf-8") as f:
            f.write("=== log start ===\n")

        self.mock_engine   = MagicMock()
        self.mock_executor = MagicMock()
        self.mock_observer = MagicMock()
        self.mock_breaker  = MagicMock()
        self.mock_breaker.can_proceed.return_value = True

        def _slow_execute(decision, original_error_log=""):
            time.sleep(PER_ACTION_DELAY)
            return {"success": True, "result_category": "SUCCESS",
                    "error_type": None, "error_detail": ""}
        self.mock_executor.execute.side_effect = _slow_execute

        self.handler = LogTailHandler(
            self.tf, LogDebouncer(cooldown_seconds=0),
            executor=self.mock_executor,
            observer_agent=self.mock_observer,
            engine=self.mock_engine,
            circuit_breaker=self.mock_breaker,
        )

    def tearDown(self):
        if os.path.exists(self.tf):
            os.unlink(self.tf)

    def test_burst_of_distinct_failures_processed_serially_no_crash(self):
        # 서로 다른 장애 N개가 한 번의 파일 변경(=한 번의 on_modified 배치)에
        # 동시에 몰린 상황을 흉내낸다 — 디바운스에 안 걸리게 매 줄을 다르게 만든다.
        with open(self.tf, "a", encoding="utf-8") as f:
            for i in range(N_BURST_LINES):
                f.write(f"ERROR burst-test unique failure #{i} pid={10000 + i}\n")

        start = time.perf_counter()
        self.handler.on_modified(_FakeEvent(self.tf))
        elapsed = time.perf_counter() - start

        # 1) 크래시 없이 N개 전부 처리됐는지 (데이터 유실 없음)
        self.assertEqual(self.mock_engine.analyze_error.call_count, N_BURST_LINES)
        self.assertEqual(self.mock_executor.execute.call_count, N_BURST_LINES)

        # 2) 병렬 처리가 전혀 없어 총 소요시간이 N * PER_ACTION_DELAY에 근접
        #    (스레드풀/큐였다면 이보다 훨씬 짧아야 한다) — 순차 처리임을 실측으로 증명.
        expected_min = N_BURST_LINES * PER_ACTION_DELAY * 0.8  # 스케줄링 오차 감안
        self.assertGreaterEqual(
            elapsed, expected_min,
            f"처리 시간이 예상(순차 누적, >= {expected_min:.2f}s)보다 훨씬 짧다 "
            f"— 병렬 처리 여부를 다시 확인할 것 (실측 {elapsed:.2f}s)",
        )
        print(f"\n[볼륨 급증 실측] {N_BURST_LINES}건 순차 처리 총 {elapsed:.2f}s "
              f"(건당 평균 {elapsed / N_BURST_LINES * 1000:.0f}ms) — "
              "마지막 줄일수록 실제 탐지 지연이 선형으로 늘어남")


if __name__ == "__main__":
    unittest.main()
