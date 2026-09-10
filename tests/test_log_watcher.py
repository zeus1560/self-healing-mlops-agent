"""
tests/test_log_watcher.py

_parse_leading_timestamp()(2026-09-10 추가, 탐지 지연 SLI 실측용, docs/SRE_PRACTICES.md
참고) — 에러 줄 맨 앞의 ISO 타임스탬프를 파싱해 탐지 지연 계산의 기준점으로 쓴다.
target-app(deploy/target-app/main.py)이 남기는 모든 줄이 datetime.utcnow().isoformat()
(naive, UTC 암묵) 형식으로 시작하므로 그 형식을 정확히 처리하는지, 그리고 형식이
다르거나 없는 줄은 조용히 None을 반환해 계측을 건너뛰는지 확인한다.
"""
import unittest
from datetime import timezone

from src.log_watcher import _parse_leading_timestamp


class TestParseLeadingTimestamp(unittest.TestCase):
    def test_parses_target_app_style_timestamp(self):
        line = (
            "2026-09-10T18:00:06.812646 CRITICAL chaos-injector: "
            "redis.exceptions.ConnectionError — Could not connect"
        )
        dt = _parse_leading_timestamp(line)
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.hour, 18)
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_parses_timestamp_without_microseconds(self):
        dt = _parse_leading_timestamp("2026-09-10T18:00:06 CRITICAL something")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.second, 6)

    def test_returns_none_for_line_without_leading_timestamp(self):
        # ProactiveMonitor 합성 로그(src/proactive_monitor.py)는 타임스탬프 없이 시작한다.
        line = "CRITICAL Disk usage 91.1% — no space left on device risk (free: 2GB)"
        self.assertIsNone(_parse_leading_timestamp(line))

    def test_returns_none_for_empty_line(self):
        self.assertIsNone(_parse_leading_timestamp(""))

    def test_returns_none_for_invalid_date_matching_shape(self):
        # 정규식 모양(YYYY-MM-DDTHH:MM:SS)은 맞지만 실제로는 불가능한 값(13월, 25시)
        # — datetime.fromisoformat()이 ValueError를 던지는 경로를 검증.
        self.assertIsNone(_parse_leading_timestamp("2026-13-40T25:99:99 CRITICAL bad"))


if __name__ == "__main__":
    unittest.main()
