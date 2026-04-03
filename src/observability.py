import sqlite3
import os
import logging
from datetime import datetime


class AgentObserver:
    """
    Self-Healing Agent의 활동 메트릭을 수집하고 통계를 내는 Observability 모듈.
    별도의 서버 없이 로컬 SQLite 파일 하나로 동작합니다.
    """

    def __init__(self, db_path="./data/agent_metrics.db"):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """메트릭 테이블을 생성합니다."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME,
                    error_log TEXT,
                    resolution_source TEXT,  -- 'L1_CACHE' (VectorDB) vs 'L2_LLM' (Inference)
                    action_type TEXT,
                    latency_sec REAL,        -- 처리 소요 시간 (초)
                    success BOOLEAN          -- 조치 성공 여부 (1=True, 0=False)
                )
            """
            )
            conn.commit()
        logging.info("📊 [Observer] SQLite 메트릭 데이터베이스 초기화 완료.")

    def log_event(
        self,
        error_log: str,
        source: str,
        action_type: str,
        latency_sec: float,
        success: bool,
    ):
        """에이전트의 단일 조치 결과를 DB에 기록(Load)합니다."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO metrics (timestamp, error_log, resolution_source, action_type, latency_sec, success)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                (
                    datetime.now().isoformat(),
                    error_log,
                    source,
                    action_type,
                    latency_sec,
                    success,
                ),
            )
            conn.commit()
        logging.info(
            f"📝 [Observer] 이벤트 기록 - 소스: {source}, 성공: {success}, 소요시간: {latency_sec:.4f}초"
        )

    def print_performance_report(self):
        """
        [핵심] 모델의 Loss Curve를 대체하는 '시스템 성능 검증 리포트' 출력
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()

            # 전체 처리 건수
            cursor.execute("SELECT COUNT(*) FROM metrics")
            total_cases = cursor.fetchone()[0]

            if total_cases == 0:
                print("\n[Report] 수집된 데이터가 없습니다.")
                return

            # 조치 성공률 (Success Rate)
            cursor.execute("SELECT COUNT(*) FROM metrics WHERE success = 1")
            success_cases = cursor.fetchone()[0]
            success_rate = (success_cases / total_cases) * 100

            # L1 캐시 히트율 (Cache Hit Ratio) -> 이 수치가 올라갈수록 '학습'이 잘 된 것
            cursor.execute(
                "SELECT COUNT(*) FROM metrics WHERE resolution_source = 'L1_CACHE'"
            )
            l1_hits = cursor.fetchone()[0]
            hit_ratio = (l1_hits / total_cases) * 100

            # 평균 Latency (L1 vs L2)
            cursor.execute(
                "SELECT AVG(latency_sec) FROM metrics WHERE resolution_source = 'L1_CACHE'"
            )
            l1_latency = cursor.fetchone()[0] or 0.0

            cursor.execute(
                "SELECT AVG(latency_sec) FROM metrics WHERE resolution_source = 'L2_LLM'"
            )
            l2_latency = cursor.fetchone()[0] or 0.0

            print("\n" + "=" * 50)
            print("📈 Self-Healing Agent Performance Report")
            print("=" * 50)
            print(f"총 처리 에러 수 : {total_cases}건")
            print(f"✅ 전체 조치 성공률 : {success_rate:.1f}%")
            print(f"🎯 L1 Cache 적중률 : {hit_ratio:.1f}% (시스템 지능화 지표)")
            print(f"⚡ 평균 L1 (기억) 복구 시간 : {l1_latency:.3f} 초")
            print(f"🐢 평균 L2 (추론) 복구 시간 : {l2_latency:.3f} 초")
            print("=" * 50 + "\n")
