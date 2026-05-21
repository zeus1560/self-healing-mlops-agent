"""
VectorDBPurger — 반복 실패를 유발한 ChromaDB 항목 자동 정제.

동작 방식:
  1. metrics 테이블에서 최근 LOOKBACK_DAYS 내 L1_CACHE 실패 로그 수집
  2. 각 실패 로그로 ChromaDB 재쿼리 → 실패를 유발한 문서 ID 집계
  3. PURGE_THRESHOLD 이상 실패를 유발한 문서 삭제

실행 주기:
  하루 1회 (log_watcher 메인 루프에서 run_if_due() 호출)
  삭제 항목은 purge_log 테이블에 기록

타임존 안전성:
  모든 타임스탬프는 UTC-aware ISO 형식. 구형 naive 레코드 혼재 시
  tzinfo를 UTC로 보정해 TypeError를 방지한다.
"""
import logging
import os
import sqlite3
import traceback
from collections import Counter
from datetime import datetime, timezone, timedelta

_PURGE_THRESHOLD  = int(os.getenv("PURGE_FAILURE_THRESHOLD", "3"))
_LOOKBACK_DAYS    = int(os.getenv("PURGE_LOOKBACK_DAYS",     "7"))
_RAG_THRESHOLD    = float(os.getenv("RAG_THRESHOLD",         "1.2"))
_RUN_INTERVAL_SEC = 86400  # 24시간


def _parse_utc(iso_str: str) -> datetime:
    """ISO 8601 문자열을 UTC-aware datetime으로 파싱. naive 레코드는 UTC로 간주."""
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class VectorDBPurger:
    """ChromaDB에서 품질이 낮은 항목을 주기적으로 제거한다."""

    def __init__(self, db_path: str = "./data/agent_metrics.db",
                 chroma_collection=None):
        self.db_path     = db_path
        self._collection = chroma_collection
        self._init_log_table()

    def _init_log_table(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS purge_log (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp  TEXT NOT NULL,
                    doc_id     TEXT NOT NULL,
                    fail_count INTEGER NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS purger_run_log (
                    id       INTEGER PRIMARY KEY CHECK (id = 1),
                    last_run TEXT NOT NULL
                )
            """)
            conn.commit()

    def _get_collection(self):
        if self._collection is not None:
            return self._collection
        from src.llm_engine import _get_chroma_client
        client = _get_chroma_client()
        return client.get_or_create_collection("error_playbook_vectors")

    def should_run(self) -> bool:
        """마지막 실행으로부터 24시간 이상 경과했으면 True."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT last_run FROM purger_run_log WHERE id = 1"
            ).fetchone()
        if row is None:
            return True
        try:
            elapsed = (datetime.now(timezone.utc) - _parse_utc(row[0])).total_seconds()
            return elapsed >= _RUN_INTERVAL_SEC
        except (ValueError, TypeError):
            logging.warning("[VectorDBPurger] last_run 파싱 실패 — 강제 실행.")
            return True

    def run_if_due(self) -> dict | None:
        """should_run()이 True일 때만 run()을 호출한다."""
        if not self.should_run():
            return None
        return self.run()

    def run(self) -> dict:
        """정제 실행. 실패 시 빈 결과 반환."""
        try:
            return self._run_inner()
        except Exception:
            logging.error(f"[VectorDBPurger] 실패:\n{traceback.format_exc()}")
            return {"purged": [], "checked": 0}

    def _run_inner(self) -> dict:
        self._record_run()

        failing_logs = self._get_failing_logs()
        if not failing_logs:
            logging.info("[VectorDBPurger] 정제 대상 없음.")
            return {"purged": [], "checked": 0}

        logging.info(f"[VectorDBPurger] 실패 로그 {len(failing_logs)}건 분석 시작...")

        col = self._get_collection()
        if col.count() == 0:
            logging.info("[VectorDBPurger] Vector DB가 비어있음 — 생략.")
            return {"purged": [], "checked": len(failing_logs)}

        doc_hit_counter: Counter = Counter()
        for log_text in failing_logs:
            try:
                results = col.query(query_texts=[log_text], n_results=1)
                ids     = results["ids"][0]
                dists   = results["distances"][0]
                if ids and dists and dists[0] <= _RAG_THRESHOLD:
                    doc_hit_counter[ids[0]] += 1
            except Exception:
                logging.debug(f"[VectorDBPurger] 쿼리 실패 (무시): {traceback.format_exc()}")

        to_purge = [
            (doc_id, cnt)
            for doc_id, cnt in doc_hit_counter.items()
            if cnt >= _PURGE_THRESHOLD
        ]

        purged: list[str] = []
        now_iso = datetime.now(timezone.utc).isoformat()
        for doc_id, fail_count in to_purge:
            try:
                col.delete(ids=[doc_id])
                purged.append(doc_id)
                self._record_purge(doc_id, fail_count, now_iso)
                logging.warning(
                    f"[VectorDBPurger] 불량 항목 삭제 — ID:{doc_id[:20]}... "
                    f"(최근 {_LOOKBACK_DAYS}일 내 실패 {fail_count}회)"
                )
            except Exception:
                logging.error(
                    f"[VectorDBPurger] 삭제 실패: {doc_id}\n{traceback.format_exc()}"
                )

        logging.info(
            f"[VectorDBPurger] 완료 — 검사:{len(failing_logs)}건 | 삭제:{len(purged)}건"
        )
        return {"purged": purged, "checked": len(failing_logs)}

    def _get_failing_logs(self) -> list[str]:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=_LOOKBACK_DAYS)
        ).isoformat()
        try:
            with sqlite3.connect(self.db_path) as conn:
                exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='metrics'"
                ).fetchone()
                if not exists:
                    return []
                rows = conn.execute(
                    """
                    SELECT error_log FROM metrics
                    WHERE resolution_source = 'L1_CACHE'
                      AND success = 0
                      AND timestamp >= ?
                    """,
                    (cutoff,),
                ).fetchall()
            return [row[0] for row in rows if row[0]]
        except Exception:
            logging.error(f"[VectorDBPurger] metrics 조회 실패:\n{traceback.format_exc()}")
            return []

    def _record_purge(self, doc_id: str, fail_count: int, timestamp: str) -> None:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT INTO purge_log (timestamp, doc_id, fail_count) VALUES (?, ?, ?)",
                    (timestamp, doc_id, fail_count),
                )
                conn.commit()
        except Exception:
            logging.warning(
                f"[VectorDBPurger] purge_log 기록 실패: {traceback.format_exc()}"
            )

    def _record_run(self) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT INTO purger_run_log (id, last_run) VALUES (1, ?) "
                    "ON CONFLICT(id) DO UPDATE SET last_run = excluded.last_run",
                    (now_iso,),
                )
                conn.commit()
        except Exception:
            logging.warning(
                f"[VectorDBPurger] 실행 시각 기록 실패: {traceback.format_exc()}"
            )
