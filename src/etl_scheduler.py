"""
ETL 자동 주기 학습 스케줄러.

log_watcher 메인 루프에서 run_if_due()를 호출하면
_INTERVAL 초마다 train_set.json → ChromaDB 자동 동기화를 수행한다.

- 기존 llm_engine 싱글톤 클라이언트를 재사용해 프로세스 내 ChromaDB 클라이언트 중복 방지.
- upsert 방식이므로 중복 실행해도 안전(멱등).
- _last_run = time.time() 초기화로 시작 직후 즉시 실행 방지 (ErrorClusterer와 동일 패턴).
"""
import logging
import time
import traceback

_INTERVAL = 86400  # 24시간 (초)


class ETLScheduler:
    def __init__(self, interval_sec: int = _INTERVAL):
        self._interval = interval_sec
        self._last_run = time.time()  # 시작 시점 기준 → 첫 실행은 interval 후

    def run_if_due(self) -> bool:
        if time.time() - self._last_run < self._interval:
            return False
        self._last_run = time.time()
        try:
            self._run()
            return True
        except Exception:
            logging.error(f"[ETLScheduler] 자동 동기화 실패:\n{traceback.format_exc()}")
            return False

    def _run(self) -> None:
        from src.etl_vector_sync import get_data_from_backup
        from src.llm_engine import _get_chroma_client

        data = get_data_from_backup()
        if not data:
            logging.warning("[ETLScheduler] 동기화할 데이터 없음.")
            return

        client = _get_chroma_client()
        collection = client.get_or_create_collection("error_playbook_vectors")

        ids = [row["id"] for row in data]
        documents = [row["log_text"] for row in data]
        metadatas = [
            {
                "error_category": row["error_category"],
                "action_type":    row["action_type"],
                "target_process": row["target_process"],
                "reasoning":      row["reasoning"],
            }
            for row in data
        ]

        collection.upsert(ids=ids, documents=documents, metadatas=metadatas)
        logging.info(f"[ETLScheduler] 자동 동기화 완료: {len(data)}건 → ChromaDB")
