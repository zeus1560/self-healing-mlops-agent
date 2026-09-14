"""
tests/test_observability_incident_fields.py

대시보드 "인시던트 상세" 타임라인(specs/spec-sre-practices.md)이 self-reflection
결과와 실행 명령어를 보여주려면 metrics 테이블에 reasoning/command가 저장돼야
한다 — 이 두 컬럼의 스키마 마이그레이션과 log_event() 배관을 검증한다.

detection_latency_sec(2026-09-10 추가, SRE 문서 SLI "탐지 지연" 실측용)와
l1_evidence(2026-09-11 추가, Explainability — L1 앙상블 투표 근거)도 같은 스키마
마이그레이션 패턴이라 여기서 함께 검증한다.
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
            "SELECT reasoning, command, detection_latency_sec, l1_evidence, "
            "l1_nearest_category, l1_nearest_distance "
            "FROM metrics ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def test_fresh_db_has_reasoning_and_command_columns(self):
        cols = {row[1] for row in get_conn(self.tf).execute("PRAGMA table_info(metrics)")}
        self.assertIn("reasoning", cols)
        self.assertIn("command", cols)
        self.assertIn("detection_latency_sec", cols)
        self.assertIn("l1_evidence", cols)
        self.assertIn("l1_nearest_category", cols)
        self.assertIn("l1_nearest_distance", cols)

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
        self.assertIsNone(row["l1_evidence"])
        self.assertIsNone(row["l1_nearest_category"])
        self.assertIsNone(row["l1_nearest_distance"])

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

    def test_migration_adds_columns_with_digits_in_name(self):
        """회귀 테스트 — 2026-09-11 Explainability 배포 후 실서비스에서 실제로 터진 버그.

        _SAFE_COL_RE가 숫자를 거부하는 바람에 `l1_evidence`(숫자 '1' 포함)가
        "비안전 컬럼"으로 오판되어 기존 DB에 영원히 추가되지 않고, 그 결과
        모든 log_event() 호출이 `no column named l1_evidence`로 계속 실패하며
        VM의 메트릭 수집이 조용히 3일간 중단됐다. l1_evidence가 없는(Explainability
        이전) 구버전 스키마를 흉내 낸 DB에 AgentObserver를 다시 붙여 마이그레이션이
        실제로 컬럼을 추가하는지 확인한다.
        """
        old_schema_path = tempfile.mktemp(suffix=".db")
        conn = get_conn(old_schema_path)
        conn.execute("""
            CREATE TABLE metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT, error_log TEXT, resolution_source TEXT,
                action_type TEXT, latency_sec REAL, success BOOLEAN
            )
        """)
        conn.commit()
        try:
            AgentObserver(db_path=old_schema_path)
            cols = {row[1] for row in get_conn(old_schema_path).execute("PRAGMA table_info(metrics)")}
            self.assertIn("l1_evidence", cols)
        finally:
            os.unlink(old_schema_path)

    def test_log_event_persists_l1_evidence(self):
        evidence = "L1 앙상블: 후보 3개 중 2개가 'clear_memory' 선택(다수결)"
        self.obs.log_event(
            error_log="CRITICAL: known error",
            source="L1_CACHE",
            action_type="CLEAR_MEMORY",
            latency_sec=0.2,
            success=True,
            l1_evidence=evidence,
        )
        row = self._latest_row()
        self.assertEqual(row["l1_evidence"], evidence)

    def test_log_event_persists_l1_nearest_category_and_distance(self):
        """2026-09-15 추가: L2/RULE 경로에서 임계값 미달 최근접 카테고리 추측 배관 확인."""
        self.obs.log_event(
            error_log="CRITICAL: novel error",
            source="L2_LLM",
            action_type="EXECUTE_LLM_COMMAND",
            latency_sec=0.5,
            success=True,
            l1_nearest_category="DB_Deadlock",
            l1_nearest_distance=0.83,
        )
        row = self._latest_row()
        self.assertEqual(row["l1_nearest_category"], "DB_Deadlock")
        self.assertAlmostEqual(row["l1_nearest_distance"], 0.83)


if __name__ == "__main__":
    unittest.main()
