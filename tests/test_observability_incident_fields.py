"""
tests/test_observability_incident_fields.py

대시보드 "인시던트 상세" 타임라인(specs/spec-sre-practices.md)이 self-reflection
결과와 실행 명령어를 보여주려면 metrics 테이블에 reasoning/command가 저장돼야
한다 — 이 두 컬럼의 스키마 마이그레이션과 log_event() 배관을 검증한다.

detection_latency_sec(2026-09-10 추가, SRE 문서 SLI "탐지 지연" 실측용)도
같은 스키마 마이그레이션 패턴이라 여기서 함께 검증한다.
"""
import os
import tempfile
import unittest

from src.observability import AgentObserver
from src.utils.sqlite_pool import get_conn


class TestIncidentFieldsMigration(unittest.TestCase):
    def setUp(self):
        self.tf  = tempfile.mktemp(suffix=".db")
        self.obs = AgentObserver(db_path=self.tf)

    def tearDown(self):
        try:
            os.unlink(self.tf)
        except OSError:
            pass

    def _latest_row(self):
        conn = get_conn(self.tf)
        return conn.execute(
            "SELECT reasoning, command, detection_latency_sec "
            "FROM metrics ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def test_fresh_db_has_reasoning_and_command_columns(self):
        cols = {row[1] for row in get_conn(self.tf).execute("PRAGMA table_info(metrics)")}
        self.assertIn("reasoning", cols)
        self.assertIn("command", cols)
        self.assertIn("detection_latency_sec", cols)

    def test_log_event_persists_reasoning_and_command(self):
        self.obs.log_event(
            error_log="CRITICAL: out of memory",
            source="L2_LLM",
            action_type="EXECUTE_LLM_COMMAND",
            latency_sec=1.23,
            success=True,
            reasoning="Groq 추론 성공",
            command="systemctl restart demo-app",
        )
        row = self._latest_row()
        self.assertEqual(row["reasoning"], "Groq 추론 성공")
        self.assertEqual(row["command"], "systemctl restart demo-app")

    def test_log_event_defaults_reasoning_and_command_to_none(self):
        """L1_CACHE 히트처럼 self-reflection이 적용되지 않는 경로는 reasoning이 없어야 한다."""
        self.obs.log_event(
            error_log="CRITICAL: known error",
            source="L1_CACHE",
            action_type="RESTART_SERVICE",
            latency_sec=0.2,
            success=True,
        )
        row = self._latest_row()
        self.assertIsNone(row["reasoning"])
        self.assertIsNone(row["command"])
        self.assertIsNone(row["detection_latency_sec"])

    def test_log_event_persists_detection_latency(self):
        self.obs.log_event(
            error_log="CRITICAL: known error",
            source="L1_CACHE",
            action_type="RESTART_SERVICE",
            latency_sec=0.2,
            success=True,
            detection_latency_sec=0.842,
        )
        row = self._latest_row()
        self.assertAlmostEqual(row["detection_latency_sec"], 0.842)


if __name__ == "__main__":
    unittest.main()
