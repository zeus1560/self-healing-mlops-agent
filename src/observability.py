import json
import logging
import os
import traceback
import urllib.request
from datetime import datetime

from src.utils.sqlite_pool import get_conn
from src.utils.pii_masker import mask as _mask_pii


class AgentObserver:
    """
    Self-Healing Agent의 활동 메트릭을 수집하고 통계를 내는 Observability 모듈.
    별도의 서버 없이 로컬 SQLite 파일 하나로 동작하며, 실패 시 Slack 알람을 발송합니다.
    """

    def __init__(self, db_path="./data/agent_metrics.db", slack_webhook_url=None):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.db_path = db_path
        self.slack_webhook_url = slack_webhook_url or os.getenv("SLACK_WEBHOOK_URL")
        self._init_db()

    def _init_db(self):
        conn = get_conn(self.db_path)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS metrics (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp        DATETIME,
                error_log        TEXT,
                resolution_source TEXT,
                action_type      TEXT,
                latency_sec      REAL,
                success          BOOLEAN,
                result_category  TEXT DEFAULT 'SUCCESS',
                error_type       TEXT,
                error_detail     TEXT,
                error_category   TEXT
            )
            """
        )
        existing = {row[1] for row in conn.execute("PRAGMA table_info(metrics)")}
        for col, definition in [
            ("result_category", "TEXT DEFAULT 'SUCCESS'"),
            ("error_type",      "TEXT"),
            ("error_detail",    "TEXT"),
            ("error_category",  "TEXT"),
        ]:
            if col not in existing:
                conn.execute(f"ALTER TABLE metrics ADD COLUMN {col} {definition}")
                logging.info(f"[Observer] 컬럼 추가: {col}")
        conn.commit()
        logging.info("[Observer] SQLite 메트릭 데이터베이스 초기화 완료.")

    def log_event(
        self,
        error_log: str,
        source: str,
        action_type: str,
        latency_sec: float,
        success: bool,
        result_category: str = "SUCCESS",
        error_type: str = None,
        error_detail: str = None,
        error_category: str = None,
    ):
        """에이전트의 단일 조치 결과를 DB에 기록하고, 필요시 Slack 알람을 보냅니다."""
        safe_log = _mask_pii(error_log)  # PII 마스킹 후 저장
        try:
            conn = get_conn(self.db_path)
            conn.execute(
                """
                INSERT INTO metrics
                    (timestamp, error_log, resolution_source, action_type,
                     latency_sec, success, result_category, error_type, error_detail,
                     error_category)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now().isoformat(),
                    safe_log,
                    source,
                    action_type,
                    latency_sec,
                    success,
                    result_category,
                    error_type,
                    error_detail,
                    error_category,
                ),
            )
            conn.commit()
        except Exception:
            logging.error(f"[Observer] 메트릭 SQLite 기록 실패:\n{traceback.format_exc()}")
            return

        logging.info(
            f"[Observer] 소스:{source} | 결과:{result_category} | {latency_sec:.4f}초"
            + (f" | {error_type}" if error_type else "")
        )

        if not success or action_type == "ESCALATE_TO_HUMAN":
            category_label = f"[{result_category}]" if result_category != "SUCCESS" else ""
            message = (
                f"🚨 *[Self-Healing Agent: 조치 실패/위험 감지]* {category_label}\n"
                f"• *판단 소스*: `{source}`\n"
                f"• *시도한 액션*: `{action_type}`\n"
                f"• *실패 유형*: `{error_type or 'N/A'}`\n"
                f"• *실패 상세*: `{(error_detail or '')[:200]}`\n"
                f"⚠️ *관리자의 즉각적인 확인이 필요합니다!*"
            )
            self._send_slack_alert(message)

    def _send_slack_alert(self, message: str):
        if not self.slack_webhook_url:
            logging.warning("🔔 [Slack Alert] SLACK_WEBHOOK_URL이 설정되지 않았습니다.")
            return
        try:
            req = urllib.request.Request(
                self.slack_webhook_url,
                data=json.dumps({"text": message}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                if response.status == 200:
                    logging.info("🔔 [Slack Alert] 경보 전송 완료.")
        except Exception:
            logging.error(f"❌ [Slack Alert] 전송 실패:\n{traceback.format_exc()}")

    def print_performance_report(self):
        try:
            self._print_performance_report_inner()
        except Exception:
            logging.error(f"📊 [Observer] 성능 리포트 생성 실패:\n{traceback.format_exc()}")

    def _print_performance_report_inner(self):
        conn = get_conn(self.db_path)
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM metrics")
        total_cases = cursor.fetchone()[0]
        if total_cases == 0:
            print("\n[Report] 수집된 데이터가 없습니다.")
            return

        cursor.execute("SELECT COUNT(*) FROM metrics WHERE success = 1")
        success_cases = cursor.fetchone()[0]
        success_rate = (success_cases / total_cases) * 100

        cursor.execute("SELECT COUNT(*) FROM metrics WHERE resolution_source = 'L1_CACHE'")
        l1_hits = cursor.fetchone()[0]
        hit_ratio = (l1_hits / total_cases) * 100

        cursor.execute("SELECT AVG(latency_sec) FROM metrics WHERE resolution_source = 'L1_CACHE'")
        l1_latency = cursor.fetchone()[0] or 0.0

        cursor.execute("SELECT AVG(latency_sec) FROM metrics WHERE resolution_source = 'L2_LLM'")
        l2_latency = cursor.fetchone()[0] or 0.0

        cursor.execute(
            "SELECT result_category, COUNT(*) FROM metrics GROUP BY result_category"
        )
        category_counts = dict(cursor.fetchall())

        print("\n" + "=" * 50)
        print("Self-Healing Agent Performance Report")
        print("=" * 50)
        print(f"총 처리 에러 수    : {total_cases}건")
        print(f"전체 조치 성공률   : {success_rate:.1f}%")
        print(f"L1 Cache 적중률   : {hit_ratio:.1f}%")
        print(f"L1 평균 복구시간   : {l1_latency:.3f}초")
        print(f"L2 평균 복구시간   : {l2_latency:.3f}초")
        print("결과 분류")
        for cat in ["SUCCESS", "FAILURE", "IMPOSSIBLE"]:
            cnt = category_counts.get(cat, 0)
            print(f"   {cat:12s}: {cnt}건 ({cnt/total_cases*100:.1f}%)")
        print("=" * 50 + "\n")
