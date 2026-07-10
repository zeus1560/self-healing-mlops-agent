"""
AgentObserver — Self-Healing Agent 활동 메트릭 수집 및 Slack 경보 발송.

설계 원칙:
  - 별도 서버 없이 로컬 SQLite 하나로 동작 (sqlite_pool 통해 스레드당 연결 재사용).
  - 모든 타임스탬프는 UTC-aware ISO 형식 저장 (naive/aware 혼용 방지).
  - PII 마스킹 후 저장 (pii_masker.mask).
  - 조치 실패 및 에스컬레이션 시 Slack 경보 자동 발송.
  - 스키마 마이그레이션 컬럼 이름은 _SAFE_COL_RE 로 검증 후 SQL에 삽입한다.
"""
import json
import logging
import os
import re
import traceback
import urllib.request
from datetime import datetime, timezone

from src.utils.sqlite_pool import get_conn
from src.utils.pii_masker import mask as _mask_pii

# 스키마 마이그레이션 컬럼 정의.
# 컬럼 이름은 소문자 영문자·밑줄만 허용한다(_SAFE_COL_RE 로 검증).
# 이 목록에만 f-string SQL이 사용되므로, 새 컬럼 추가 시 이곳에만 등록하면 된다.
_SCHEMA_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("result_category", "TEXT DEFAULT 'SUCCESS'"),
    ("error_type",      "TEXT"),
    ("error_detail",    "TEXT"),
    ("error_category",  "TEXT"),
)
# 컬럼 이름 안전성 검증 패턴 — 소문자 영문자와 밑줄만 허용
_SAFE_COL_RE = re.compile(r'^[a-z_]+$')


class AgentObserver:
    """
    에이전트의 단일 조치 결과를 SQLite에 기록하고, 필요 시 Slack 알람을 발송한다.

    thread-safety:
        sqlite_pool의 스레드당 연결을 사용하므로 별도 Lock 없이 안전하다.
    """

    def __init__(self, db_path: str = "./data/agent_metrics.db",
                 slack_webhook_url: str | None = None):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.db_path           = db_path
        self.slack_webhook_url = slack_webhook_url or os.getenv("SLACK_WEBHOOK_URL")
        self._init_db()

    # ── 초기화 ──────────────────────────────────────────────────────────
    def _init_db(self) -> None:
        """
        metrics 테이블을 생성하고 누락 컬럼을 마이그레이션한다.

        _SCHEMA_MIGRATIONS의 컬럼 이름은 _SAFE_COL_RE 로 사전 검증해
        f-string SQL 구성 시 비안전 식별자가 삽입되지 않도록 보장한다.
        """
        conn = get_conn(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS metrics (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp        TEXT,
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
        """)
        existing = {row[1] for row in conn.execute("PRAGMA table_info(metrics)")}
        for col, definition in _SCHEMA_MIGRATIONS:
            # 소문자 영문자·밑줄 외 문자가 포함된 컬럼 이름은 건너뛴다.
            if not _SAFE_COL_RE.match(col):
                logging.error(f"[Observer] 비안전 컬럼 이름 건너뜀: {col!r}")
                continue
            if col not in existing:
                conn.execute(f"ALTER TABLE metrics ADD COLUMN {col} {definition}")
                logging.info(f"[Observer] 컬럼 추가: {col}")
        conn.commit()
        logging.info("[Observer] SQLite 메트릭 데이터베이스 초기화 완료.")

    # ── 공개 인터페이스 ──────────────────────────────────────────────────
    def log_event(
        self,
        error_log: str,
        source: str,
        action_type: str,
        latency_sec: float,
        success: bool,
        result_category: str = "SUCCESS",
        error_type: str | None = None,
        error_detail: str | None = None,
        error_category: str | None = None,
    ) -> None:
        """에이전트의 단일 조치 결과를 DB에 기록하고, 필요 시 Slack 알람을 발송한다."""
        safe_log  = _mask_pii(error_log)
        # datetime.now(timezone.utc): timezone-naive datetime과의 비교 오류 방지
        timestamp = datetime.now().isoformat()
        try:
            conn = get_conn(self.db_path)
            conn.execute(
                """
                INSERT INTO metrics
                    (timestamp, error_log, resolution_source, action_type,
                     latency_sec, success, result_category, error_type,
                     error_detail, error_category)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp, safe_log, source, action_type,
                    latency_sec, success, result_category,
                    error_type, error_detail, error_category,
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
                f"• *실패 상세*: {('`' + error_detail[:200] + '`') if error_detail else '없음'}\n"
                f"⚠️ *관리자의 즉각적인 확인이 필요합니다!*"
            )
            self._send_slack_alert(message)

    def print_performance_report(self) -> None:
        """성능 리포트를 콘솔에 출력한다. 실패 시 에러 로그만 남기고 계속."""
        try:
            self._print_performance_report_inner()
        except Exception:
            logging.error(f"📊 [Observer] 성능 리포트 생성 실패:\n{traceback.format_exc()}")

    # ── 내부 구현 ────────────────────────────────────────────────────────
    def _print_performance_report_inner(self) -> None:
        conn = get_conn(self.db_path)

        total_cases = conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0]
        if total_cases == 0:
            print("\n[Report] 수집된 데이터가 없습니다.")
            return

        success_cases = conn.execute(
            "SELECT COUNT(*) FROM metrics WHERE success = 1"
        ).fetchone()[0]
        l1_hits = conn.execute(
            "SELECT COUNT(*) FROM metrics WHERE resolution_source = 'L1_CACHE'"
        ).fetchone()[0]
        l1_latency = conn.execute(
            "SELECT AVG(latency_sec) FROM metrics WHERE resolution_source = 'L1_CACHE'"
        ).fetchone()[0] or 0.0
        l2_latency = conn.execute(
            "SELECT AVG(latency_sec) FROM metrics WHERE resolution_source = 'L2_LLM'"
        ).fetchone()[0] or 0.0
        category_counts = dict(
            conn.execute(
                "SELECT result_category, COUNT(*) FROM metrics GROUP BY result_category"
            ).fetchall()
        )

        print("\n" + "=" * 50)
        print("Self-Healing Agent Performance Report")
        print("=" * 50)
        print(f"총 처리 에러 수    : {total_cases}건")
        print(f"전체 조치 성공률   : {success_cases / total_cases * 100:.1f}%")
        print(f"L1 Cache 적중률   : {l1_hits / total_cases * 100:.1f}%")
        print(f"L1 평균 복구시간   : {l1_latency:.3f}초")
        print(f"L2 평균 복구시간   : {l2_latency:.3f}초")
        print("결과 분류")
        for cat in ("SUCCESS", "FAILURE", "IMPOSSIBLE"):
            cnt = category_counts.get(cat, 0)
            print(f"   {cat:12s}: {cnt}건 ({cnt / total_cases * 100:.1f}%)")
        print("=" * 50 + "\n")

    def _send_slack_alert(self, message: str) -> None:
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
