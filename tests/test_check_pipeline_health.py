"""
tests/test_check_pipeline_health.py

scripts/check_pipeline_health.py 회귀 테스트 — 2026-09-11~14에 l1_evidence 스키마
마이그레이션 버그로 3일간 조용히 죽어있던 메트릭 기록을, chaos_injector.log와
agent_metrics.db를 대조하는 것만으로 잡아낼 수 있는지 검증한다.

experiments.run_fp_fn_analysis.analyze()는 CHAOS_LOG/METRICS_DB를 모듈 전역
상수로 참조하므로(파라미터 아님), monkeypatch로 그 전역만 임시 파일로 바꿔치기한다.
"""
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import experiments.run_fp_fn_analysis as fp_fn
from scripts import check_pipeline_health as health


def _write_chaos_log(path: Path, events: list[tuple[datetime, str]]) -> None:
    lines = [f"{ts.isoformat().replace('+00:00', 'Z')} OK fault={fault} http=200" for ts, fault in events]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _make_metrics_db(path: Path, rows: list[tuple[datetime, str, str]]) -> None:
    """rows: (timestamp, error_log, error_category) — chaos-injector 마커가 찍힌 행만."""
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT, error_log TEXT, resolution_source TEXT,
            action_type TEXT, latency_sec REAL, success BOOLEAN,
            result_category TEXT, error_category TEXT
        )
    """)
    for ts, error_log, error_category in rows:
        conn.execute(
            "INSERT INTO metrics (timestamp, error_log, resolution_source, action_type, "
            "success, result_category, error_category) VALUES (?, ?, 'L1_CACHE', 'RESTART_SERVICE', 1, 'SUCCESS', ?)",
            (ts.isoformat(), error_log, error_category),
        )
    conn.commit()
    conn.close()


class TestPipelineHealthCheck(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmpdir = Path(tempfile.mkdtemp())
        self.chaos_log = self.tmpdir / "chaos_injector.log"
        self.metrics_db = self.tmpdir / "agent_metrics.db"
        patcher1 = patch.object(fp_fn, "CHAOS_LOG", self.chaos_log)
        patcher2 = patch.object(fp_fn, "METRICS_DB", self.metrics_db)
        patcher1.start()
        patcher2.start()
        self.addCleanup(patcher1.stop)
        self.addCleanup(patcher2.stop)

    def test_no_alert_when_all_events_matched(self):
        now = datetime.now(timezone.utc)
        events = [(now - timedelta(hours=h), "auth_error") for h in (1, 7, 13)]
        _write_chaos_log(self.chaos_log, events)
        rows = [
            (ts + timedelta(seconds=5), "chaos-injector: requests.exceptions.HTTPError — 401 Unauthorized", "Auth_Error")
            for ts, _ in events
        ]
        _make_metrics_db(self.metrics_db, rows)

        result = health.check(lookback_hours=24)
        self.assertEqual(result["chaos_injector_log_events"], 3)
        self.assertEqual(result["missed_entirely"], 0)
        self.assertFalse(result["alert"])

    def test_alert_when_pipeline_silently_stops_recording(self):
        """l1_evidence 버그와 동일한 증상 재현: 주입은 계속되는데 metrics.db는 완전히 비어있음."""
        now = datetime.now(timezone.utc)
        events = [(now - timedelta(hours=h), "auth_error") for h in (1, 7, 13, 19)]
        _write_chaos_log(self.chaos_log, events)
        _make_metrics_db(self.metrics_db, [])  # 빈 테이블 — 스키마만 존재

        result = health.check(lookback_hours=24)
        self.assertEqual(result["chaos_injector_log_events"], 4)
        self.assertEqual(result["missed_entirely"], 4)
        self.assertTrue(result["alert"])

    def test_no_alert_below_min_events_threshold(self):
        """표본이 1건뿐이면 100% 미탐이어도 아직 판단하지 않는다(오탐 방지)."""
        now = datetime.now(timezone.utc)
        _write_chaos_log(self.chaos_log, [(now - timedelta(hours=1), "auth_error")])
        _make_metrics_db(self.metrics_db, [])

        result = health.check(lookback_hours=24)
        self.assertEqual(result["chaos_injector_log_events"], 1)
        self.assertEqual(result["missed_entirely"], 1)
        self.assertFalse(result["alert"])

    def test_main_sends_notification_only_when_alert_and_notify_enabled(self):
        now = datetime.now(timezone.utc)
        events = [(now - timedelta(hours=h), "auth_error") for h in (1, 7, 13, 19)]
        _write_chaos_log(self.chaos_log, events)
        _make_metrics_db(self.metrics_db, [])

        with patch.object(health, "_notify") as mock_notify:
            result = health.main(lookback_hours=24, notify=True)
        mock_notify.assert_called_once()
        self.assertTrue(result["alert"])

        with patch.object(health, "_notify") as mock_notify:
            health.main(lookback_hours=24, notify=False)
        mock_notify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
