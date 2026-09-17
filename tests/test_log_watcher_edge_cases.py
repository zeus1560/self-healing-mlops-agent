"""
tests/test_log_watcher_edge_cases.py

"정상 케이스만 도는 데모는 실전에서 한 번 삐끗하면 신뢰가 깎인다"는 피드백에 대한
회귀 테스트. LogTailHandler.on_modified()가 아래 세 가지 비정상 상황에서 실제로
어떻게 동작하는지 검증한다 — 전부 2026-09-17 이 테스트를 작성하며 실제 코드를
추적해 발견한 시나리오다.

1. 로그 줄에 깨진 인코딩(비-UTF8 바이트)이 섞여 들어오면?
   → 수정 전: open(..., encoding="utf-8")이 UnicodeDecodeError를 던지는데
     기존 except는 OSError만 잡아서 예외가 그대로 튀어나가 워커가 죽었다.
     errors="replace" 추가로 수정(2026-09-17).
2. 로그 파일이 로테이션/트렁케이트로 갑자기 작아지면?
   → 수정 전: 기존 file_ptr이 새 파일 끝을 넘어서서 seek되어, 이후 새로
     쓰인 줄을 에러도 없이 계속 놓치는 "조용한 감지 불능" 상태가 됐다.
     크기 축소를 감지해 file_ptr을 0으로 리셋하도록 수정(2026-09-17).
3. 서로 다른 두 장애가 같은 on_modified() 배치(같은 파일 변경 이벤트)에
   동시에 들어오면?
   → 크래시 없이 순차 처리되어 둘 다 파이프라인이 가동됨을 확인(이미 안전
     했던 동작 — 회귀 방지용으로 고정).
"""
import os
import tempfile
import unittest
from unittest.mock import MagicMock

from src.log_watcher import LogTailHandler
from src.utils.debouncer import LogDebouncer


class _FakeEvent:
    def __init__(self, src_path: str):
        self.src_path = src_path


def _make_handler(filepath: str, cooldown: int = 0):
    mock_engine   = MagicMock()
    mock_executor = MagicMock()
    mock_observer = MagicMock()
    mock_breaker  = MagicMock()
    mock_breaker.can_proceed.return_value = True
    mock_executor.execute.return_value = {
        "success": True, "result_category": "SUCCESS",
        "error_type": None, "error_detail": "",
    }

    handler = LogTailHandler(
        filepath, LogDebouncer(cooldown_seconds=cooldown),
        executor=mock_executor,
        observer_agent=mock_observer,
        engine=mock_engine,
        circuit_breaker=mock_breaker,
    )
    return handler, mock_engine, mock_executor


class TestMalformedEncoding(unittest.TestCase):
    """1. 깨진 인코딩이 섞인 줄이 들어와도 워커가 죽지 않아야 한다."""

    def setUp(self):
        self.tf = tempfile.mktemp(suffix=".log")
        with open(self.tf, "w", encoding="utf-8") as f:
            f.write("=== log start ===\n")
        self.handler, self.mock_engine, _ = _make_handler(self.tf)

    def tearDown(self):
        if os.path.exists(self.tf):
            os.unlink(self.tf)

    def test_invalid_utf8_bytes_do_not_crash_and_error_still_detected(self):
        # 정상 ASCII "ERROR" 텍스트 사이에 유효하지 않은 UTF-8 바이트(0xFF)를 섞는다
        # — 예: 바이너리 크래시 덤프가 로그 스트림에 잘못 끼어든 상황을 흉내낸다.
        with open(self.tf, "ab") as f:
            f.write(b"ERROR worker crashed \xff\xfe garbage bytes here\n")

        # 수정 전에는 이 호출에서 UnicodeDecodeError가 그대로 튀어나왔다.
        self.handler.on_modified(_FakeEvent(self.tf))

        self.mock_engine.analyze_error.assert_called_once()
        (context,), _ = self.mock_engine.analyze_error.call_args
        self.assertIn("ERROR worker crashed", context)

    def test_malformed_line_does_not_block_subsequent_valid_lines(self):
        with open(self.tf, "ab") as f:
            f.write(b"\xff\xfe\xfd binary garbage no keyword\n")
        self.handler.on_modified(_FakeEvent(self.tf))
        self.mock_engine.analyze_error.assert_not_called()  # 키워드 없는 줄 — 트리거 안 됨(정상)

        with open(self.tf, "a", encoding="utf-8") as f:
            f.write("CRITICAL real failure after garbage\n")
        self.handler.on_modified(_FakeEvent(self.tf))
        self.mock_engine.analyze_error.assert_called_once()


class TestLogRotationTruncation(unittest.TestCase):
    """2. 로그 로테이션/트렁케이트 후에도 새 내용을 계속 감지해야 한다."""

    def setUp(self):
        self.tf = tempfile.mktemp(suffix=".log")
        with open(self.tf, "w", encoding="utf-8") as f:
            f.write("=" * 200 + "\n")  # file_ptr을 충분히 크게 시작
        self.handler, self.mock_engine, _ = _make_handler(self.tf)

    def tearDown(self):
        if os.path.exists(self.tf):
            os.unlink(self.tf)

    def test_truncation_is_detected_and_new_content_still_processed(self):
        ptr_before_rotation = self.handler.file_ptr
        self.assertGreater(ptr_before_rotation, 0)

        # 로테이션 흉내: 파일을 훨씬 짧은 새 내용으로 교체(트렁케이트).
        with open(self.tf, "w", encoding="utf-8") as f:
            f.write("ERROR short log after rotation\n")

        new_size = os.path.getsize(self.tf)
        self.assertLess(new_size, ptr_before_rotation)  # 실제로 작아졌는지 전제 확인

        self.handler.on_modified(_FakeEvent(self.tf))

        # 수정 전: file_ptr이 새 파일 끝(new_size)보다 커서 아무 줄도 못 읽었다.
        self.mock_engine.analyze_error.assert_called_once()
        (context,), _ = self.mock_engine.analyze_error.call_args
        self.assertIn("ERROR short log after rotation", context)


class TestOverlappingFailuresInSameBatch(unittest.TestCase):
    """3. 같은 on_modified() 배치에 서로 다른 두 장애가 동시에 들어와도
    크래시 없이 둘 다 순차적으로 파이프라인이 가동돼야 한다."""

    def setUp(self):
        self.tf = tempfile.mktemp(suffix=".log")
        with open(self.tf, "w", encoding="utf-8") as f:
            f.write("=== log start ===\n")
        self.handler, self.mock_engine, self.mock_executor = _make_handler(self.tf)

    def tearDown(self):
        if os.path.exists(self.tf):
            os.unlink(self.tf)

    def test_two_different_failures_in_one_write_both_trigger_pipeline(self):
        # 한 번의 파일 쓰기(=한 번의 on_modified 이벤트)에 서로 무관한 두 장애
        # (OOM, Process_Crash)가 동시에 찍힌 상황을 흉내낸다.
        with open(self.tf, "a", encoding="utf-8") as f:
            f.write("ERROR OOM killed worker.py (pid=1234)\n")
            f.write("CRITICAL Process_Crash unrelated_service exited nonzero\n")

        self.handler.on_modified(_FakeEvent(self.tf))

        self.assertEqual(self.mock_engine.analyze_error.call_count, 2)
        first_context  = self.mock_engine.analyze_error.call_args_list[0][0][0]
        second_context = self.mock_engine.analyze_error.call_args_list[1][0][0]
        # 각 컨텍스트의 첫 줄이 자기 자신의 에러 줄이어야 한다(서로 뒤바뀌면 안 됨).
        self.assertTrue(first_context.startswith("ERROR OOM killed worker.py"))
        self.assertTrue(second_context.startswith("CRITICAL Process_Crash"))
        # 둘 다 예외 없이 execute까지 도달했는지.
        self.assertEqual(self.mock_executor.execute.call_count, 2)


if __name__ == "__main__":
    unittest.main()
