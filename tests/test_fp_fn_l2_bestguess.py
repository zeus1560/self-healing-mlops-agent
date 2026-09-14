"""
tests/test_fp_fn_l2_bestguess.py

experiments/run_fp_fn_analysis.py의 "l2_path_l1_bestguess" 지표(2026-09-15 추가)
회귀 테스트 — L2_LLM/RULE 경로 사건에 한해 "L1이 임계값 미달로 포기한 최근접
카테고리 추측"이 실제 정답과 얼마나 맞았는지, 기존 confusion matrix/recall과는
완전히 분리된 별도 집계로 나오는지 확인한다.

experiments.run_fp_fn_analysis.analyze()는 CHAOS_LOG/METRICS_DB를 모듈 전역
상수로 참조하므로 monkeypatch로 임시 파일로 바꿔치기한다(test_check_pipeline_health.py
와 동일 패턴).
"""
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import experiments.run_fp_fn_analysis as fp_fn

_DB_DEADLOCK_EVIDENCE = "chaos-injector: sqlite3.OperationalError — database is locked"
_AUTH_ERROR_EVIDENCE = "chaos-injector: requests.exceptions.HTTPError — 401 Unauthorized"


class TestL2PathBestGuessMetric(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.chaos_log = self.tmpdir / "chaos_injector.log"
        self.metrics_db = self.tmpdir / "agent_metrics.db"
        patcher1 = patch.object(fp_fn, "CHAOS_LOG", self.chaos_log)
        patcher2 = patch.object(fp_fn, "METRICS_DB", self.metrics_db)
        patcher1.start()
        patcher2.start()
        self.addCleanup(patcher1.stop)
        self.addCleanup(patcher2.stop)

        conn = sqlite3.connect(self.metrics_db)
        conn.execute("""
            CREATE TABLE metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT, error_log TEXT, resolution_source TEXT,
                action_type TEXT, latency_sec REAL, success BOOLEAN,
                result_category TEXT, error_category TEXT,
                l1_nearest_category TEXT
            )
        """)
        conn.commit()
        conn.close()
        self.conn = sqlite3.connect(self.metrics_db)

    def _add_metrics_row(self, ts, error_log, resolution_source, error_category, l1_nearest_category):
        self.conn.execute(
            "INSERT INTO metrics (timestamp, error_log, resolution_source, action_type, "
            "success, result_category, error_category, l1_nearest_category) "
            "VALUES (?, ?, ?, 'EXECUTE_LLM_COMMAND', 1, 'SUCCESS', ?, ?)",
            (ts.isoformat(), error_log, resolution_source, error_category, l1_nearest_category),
        )
        self.conn.commit()

    def _write_chaos_log(self, events):
        lines = [f"{ts.isoformat().replace('+00:00', 'Z')} OK fault={fault} http=200" for ts, fault in events]
        self.chaos_log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_correct_and_incorrect_guesses_computed_separately_from_recall(self):
        now = datetime.now(timezone.utc)
        t1, t2 = now - timedelta(hours=2), now - timedelta(hours=1)
        self._write_chaos_log([(t1, "db_deadlock"), (t2, "auth_error")])

        # db_deadlock: L2가 처리, L1 최근접 추측이 정답과 일치 → 적중
        self._add_metrics_row(
            t1 + timedelta(seconds=5), _DB_DEADLOCK_EVIDENCE, "L2_LLM", "LLM_Inferred", "DB_Deadlock"
        )
        # auth_error: RULE이 처리, L1 최근접 추측이 오답 → 미적중
        self._add_metrics_row(
            t2 + timedelta(seconds=5), _AUTH_ERROR_EVIDENCE, "RULE", "Rule_Inferred", "Configuration_Error"
        )

        summary = fp_fn.analyze()
        bg = summary["l2_path_l1_bestguess"]
        self.assertEqual(bg["n"], 2)
        self.assertEqual(bg["correct"], 1)
        self.assertEqual(bg["accuracy"], 0.5)
        self.assertEqual(bg["missing_pre_migration_rows"], 0)

        # 기존 confusion matrix/recall은 이 새 지표와 별개로 여전히 "오분류"로 집계돼야 한다
        # (l2_path_l1_bestguess가 recall을 대체하거나 섞어 좋게 보이게 만들면 안 됨).
        self.assertEqual(summary["per_category"]["DB_Deadlock"]["recall"], 0.0)
        self.assertEqual(summary["per_category"]["Auth_Error"]["recall"], 0.0)

    def test_l1_cache_hits_excluded_from_bestguess_metric(self):
        """L1_CACHE 히트는 애초에 l1_nearest_category가 필요 없는 경로라 집계에서 빠져야 한다."""
        now = datetime.now(timezone.utc)
        t1 = now - timedelta(hours=1)
        self._write_chaos_log([(t1, "db_deadlock")])
        self._add_metrics_row(
            t1 + timedelta(seconds=5), _DB_DEADLOCK_EVIDENCE, "L1_CACHE", "DB_Deadlock", None
        )

        summary = fp_fn.analyze()
        bg = summary["l2_path_l1_bestguess"]
        self.assertEqual(bg["n"], 0)
        self.assertIsNone(bg["accuracy"])

    def test_pre_migration_rows_without_nearest_category_counted_separately(self):
        """l1_nearest_category 컬럼 마이그레이션 이전(2026-09-15 이전) 행은 n에서 빠지고
        missing_pre_migration_rows로만 집계돼야 한다(오탐/오해 방지)."""
        now = datetime.now(timezone.utc)
        t1 = now - timedelta(hours=1)
        self._write_chaos_log([(t1, "db_deadlock")])
        self._add_metrics_row(
            t1 + timedelta(seconds=5), _DB_DEADLOCK_EVIDENCE, "L2_LLM", "LLM_Inferred", None
        )

        summary = fp_fn.analyze()
        bg = summary["l2_path_l1_bestguess"]
        self.assertEqual(bg["n"], 0)
        self.assertEqual(bg["missing_pre_migration_rows"], 1)
        self.assertIsNone(bg["accuracy"])


if __name__ == "__main__":
    unittest.main()
