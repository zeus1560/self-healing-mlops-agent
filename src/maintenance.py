"""
MaintenanceRunner — SQLite 오래된 레코드 정리 + VACUUM.

수행 작업:
  - metrics 테이블에서 RETENTION_DAYS 초과 레코드 삭제
  - circuit_breaker 테이블에서 오래된 CLOSED 레코드 삭제
  - VACUUM으로 디스크 공간 반환 (트랜잭션 외부에서 실행)
  - 마지막 실행 시각을 maintenance_log 테이블에 기록 (하루 1회 제한)

타임존 안전성:
  저장 타임스탬프는 항상 UTC-aware ISO 형식. 구형 naive 레코드가
  혼재할 경우를 대비해 fromisoformat() 결과에 tzinfo를 보정한다.
"""
import logging
import os
import sqlite3
import traceback
from datetime import datetime, timezone, timedelta

RETENTION_DAYS    = 30
_RUN_INTERVAL_SEC = 86400  # 24시간


def _parse_utc(iso_str: str) -> datetime:
    """
    ISO 8601 문자열을 UTC-aware datetime으로 파싱한다.
    tzinfo 없는 구형 레코드는 UTC로 간주해 TypeError를 방지한다.
    """
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class MaintenanceRunner:
    """SQLite 메트릭 DB의 오래된 레코드를 주기적으로 정리한다."""

    def __init__(self, db_path: str = "./data/agent_metrics.db"):
        self.db_path = db_path
        self._init_log_table()

    def _init_log_table(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS maintenance_log (
                    id       INTEGER PRIMARY KEY CHECK (id = 1),
                    last_run TEXT NOT NULL,
                    deleted  INTEGER NOT NULL DEFAULT 0
                )
            """)
            conn.commit()

    # ── 공개 인터페이스 ────────────────────────────────────────────────
    def should_run(self) -> bool:
        """마지막 실행으로부터 24시간 이상 경과했으면 True."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT last_run FROM maintenance_log WHERE id = 1"
            ).fetchone()
        if row is None:
            return True
        try:
            last    = _parse_utc(row[0])
            elapsed = (datetime.now(timezone.utc) - last).total_seconds()
            return elapsed >= _RUN_INTERVAL_SEC
        except (ValueError, TypeError):
            # 파싱 실패 시 실행 허용 (보수적)
            logging.warning("[Maintenance] last_run 파싱 실패 — 강제 실행.")
            return True

    def run(self) -> dict:
        """정리 실행. should_run() 확인 없이 즉시 수행."""
        try:
            return self._run_inner()
        except Exception:
            logging.error(f"[Maintenance] 실행 실패:\n{traceback.format_exc()}")
            return {"deleted": 0, "size_before_kb": 0, "size_after_kb": 0}

    def run_if_due(self) -> None:
        """should_run()이 True일 때만 run() 호출. 메인 루프에서 사용."""
        if self.should_run():
            result = self.run()
            logging.info(
                f"[Maintenance] 완료 — 삭제 {result['deleted']}건 | "
                f"DB {result['size_before_kb']}KB → {result['size_after_kb']}KB"
            )

    # ── 내부 구현 ──────────────────────────────────────────────────────
    def _run_inner(self) -> dict:
        size_before = os.path.getsize(self.db_path) // 1024
        cutoff      = (
            datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
        ).isoformat()
        deleted = 0

        with sqlite3.connect(self.db_path) as conn:
            cur      = conn.execute("DELETE FROM metrics WHERE timestamp < ?", (cutoff,))
            deleted += cur.rowcount

            if self._table_exists(conn, "circuit_breaker"):
                cur      = conn.execute(
                    "DELETE FROM circuit_breaker "
                    "WHERE state = 'CLOSED' AND last_updated < ?",
                    (cutoff,),
                )
                deleted += cur.rowcount

            now_iso = datetime.now(timezone.utc).isoformat()
            conn.execute("""
                INSERT INTO maintenance_log (id, last_run, deleted)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    last_run = excluded.last_run,
                    deleted  = excluded.deleted
            """, (now_iso, deleted))
            conn.commit()

        # VACUUM은 트랜잭션 밖에서 실행해야 함
        with sqlite3.connect(self.db_path) as conn:
            conn.isolation_level = None  # autocommit
            conn.execute("VACUUM")

        size_after = os.path.getsize(self.db_path) // 1024
        logging.info(
            f"[Maintenance] {RETENTION_DAYS}일 초과 레코드 {deleted}건 삭제 | "
            f"VACUUM 완료 ({size_before}KB → {size_after}KB)"
        )
        return {"deleted": deleted, "size_before_kb": size_before, "size_after_kb": size_after}

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        return row is not None
