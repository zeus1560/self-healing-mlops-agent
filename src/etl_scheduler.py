"""
ETLScheduler — train_set.json → ChromaDB 자동 주기 동기화.

log_watcher 메인 루프에서 run_if_due()를 호출하면
_interval 초마다 etl_vector_sync.get_data_from_backup() → ChromaDB upsert를 수행한다.

설계 결정:
  - _last_run은 인스턴스 생성 시각으로 초기화 → 시작 직후 즉시 실행 방지.
  - _last_run은 실행 시작 전에 갱신 → 실패해도 interval 후에 재시도하지 않음.
    (ETL 장애가 반복될 때 빠른 재시도가 시스템을 과부하시키는 것을 방지)
  - upsert 방식이므로 중복 실행해도 안전(멱등).
  - llm_engine 싱글톤 클라이언트 재사용 → 프로세스 내 ChromaDB 중복 클라이언트 방지.
"""
import logging
import time
import traceback

_INTERVAL = 86400  # 24시간 (초)


class ETLScheduler:
    """ChromaDB 지식베이스를 주기적으로 파일 데이터와 동기화한다."""

    def __init__(self, interval_sec: int = _INTERVAL):
        self._interval = interval_sec
        # 시작 시점 기준으로 초기화 → 첫 자동 실행은 interval 후
        self._last_run = time.time()

    def run_if_due(self) -> bool:
        """interval 경과 시 동기화를 실행하고 성공 여부를 반환한다."""
        if time.time() - self._last_run < self._interval:
            return False
        # 실행 전에 갱신 — 실패 시에도 interval 후 재시도 (재시도 폭풍 방지)
        self._last_run = time.time()
        try:
            self._run()
            return True
        except Exception:
            logging.error(f"[ETLScheduler] 자동 동기화 실패:\n{traceback.format_exc()}")
            return False

    def _run(self) -> None:
        from src.etl_vector_sync import get_data_from_backup, sync_to_chroma

        data = get_data_from_backup()
        if not data:
            logging.warning("[ETLScheduler] 동기화할 데이터 없음.")
            return

        sync_to_chroma(data)
        logging.info(f"[ETLScheduler] 자동 동기화 완료: {len(data)}건 → ChromaDB")
