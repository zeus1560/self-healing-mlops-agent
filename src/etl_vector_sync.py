"""
ETL Vector Sync — train_set.json / etl_backup.json → ChromaDB 동기화.

데이터 우선순위:
  1. train_set.json (테스트셋 분리 완료)
  2. etl_backup.json (폴백)
  3. PostgreSQL (get_data_from_pg)

ChromaDB 클라이언트:
  sync_to_chroma()는 llm_engine._get_chroma_client() 싱글톤을 재사용한다.
  동일 프로세스 내에서 별도 클라이언트를 생성하면 파일 락 경합이 발생하므로
  반드시 싱글톤을 통해야 한다.

주의:
  logging.basicConfig()는 __main__ 실행 시에만 설정한다.
"""
import hashlib
import json
import logging
import os
import traceback

_log = logging.getLogger(__name__)

DB_CONFIG = {
    "dbname":   os.getenv("PG_DBNAME",   "mlops_db"),
    "user":     os.getenv("PG_USER",     "postgres"),
    "password": os.getenv("PG_PASSWORD", "password"),
    "host":     os.getenv("PG_HOST",     "localhost"),
    "port":     os.getenv("PG_PORT",     "5432"),
}

_DATA_DIR   = os.path.join(os.path.dirname(__file__), "..", "data")
TRAIN_PATH  = os.path.join(_DATA_DIR, "train_set.json")
BACKUP_PATH = os.path.join(_DATA_DIR, "etl_backup.json")


def get_data_from_pg() -> list[dict]:
    """PostgreSQL에서 에러 플레이북을 읽는다. 실패 시 빈 리스트 반환."""
    try:
        import psycopg2
        with psycopg2.connect(**DB_CONFIG) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT id, log_text, error_category, action_type, "
                    "target_process, reasoning FROM agent_error_playbook;"
                )
                rows = cursor.fetchall()
        return [
            {
                "id":             str(row[0]),
                "log_text":       row[1],
                "error_category": str(row[2]),
                "action_type":    str(row[3]),
                "target_process": str(row[4]) if row[4] else "unknown",
                "reasoning":      str(row[5]) if row[5] else "",
            }
            for row in rows
        ]
    except Exception:
        _log.error(f"PostgreSQL 조회 실패:\n{traceback.format_exc()}")
        return []


def get_data_from_backup() -> list[dict]:
    """
    train_set.json 우선 사용, 없으면 etl_backup.json 폴백.

    ID는 log_text MD5 해시로 생성 — 동일 텍스트는 항상 같은 ID → upsert 시 중복 방지.
    """
    train_path  = os.path.abspath(TRAIN_PATH)
    backup_path = os.path.abspath(BACKUP_PATH)

    if os.path.exists(train_path):
        load_path = train_path
        _log.info("train_set.json 사용 (테스트셋 분리됨)")
    elif os.path.exists(backup_path):
        load_path = backup_path
        _log.warning("train_set.json 없음 → etl_backup.json 폴백 (split_dataset.py 실행 권장)")
    else:
        _log.error("데이터 파일 없음")
        return []

    try:
        with open(load_path, encoding="utf-8") as f:
            backup = json.load(f)
        records = backup.get("data", [])
        result  = []
        for row in records:
            log_text = row.get("log_text", "")
            if not log_text:
                continue
            doc_id = hashlib.md5(log_text.encode("utf-8")).hexdigest()
            result.append({
                "id":             doc_id,
                "log_text":       log_text,
                "error_category": str(row.get("error_category", "Unknown")),
                "action_type":    str(row.get("action_type", "escalate_to_human")),
                "target_process": str(row.get("target_process") or "unknown"),
                "reasoning":      str(row.get("reasoning", "")),
            })
        _log.info(f"{os.path.basename(load_path)}에서 {len(result)}건 로드 완료.")
        return result
    except Exception:
        _log.error(f"백업 파일 로드 실패:\n{traceback.format_exc()}")
        return []


def sync_to_chroma(data: list[dict]) -> None:
    """
    에러 플레이북 데이터를 ChromaDB에 upsert한다.

    llm_engine._get_chroma_client() 싱글톤을 재사용해 동일 프로세스 내
    클라이언트 중복 생성과 파일 락 경합을 방지한다.
    """
    if not data:
        _log.warning("동기화할 데이터가 없습니다.")
        return

    # 싱글톤 클라이언트 재사용 (프로세스 내 중복 클라이언트 생성 방지)
    try:
        from src.llm_engine import _get_chroma_client
        client     = _get_chroma_client()
        collection = client.get_or_create_collection(name="error_playbook_vectors")
    except Exception:
        _log.error(f"ChromaDB 클라이언트 초기화 실패:\n{traceback.format_exc()}")
        return

    ids       = [row["id"]       for row in data]
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

    try:
        collection.upsert(ids=ids, documents=documents, metadatas=metadatas)
        _log.info(f"{len(data)}건이 ChromaDB(L1 Cache)에 동기화됐습니다.")
    except Exception:
        _log.error(f"ChromaDB 동기화 실패:\n{traceback.format_exc()}")


if __name__ == "__main__":
    # 직접 실행 시에만 basicConfig를 설정 (모듈 임포트 시 호출자 설정 보존)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )
    _log.info("--- ETL Sync 시작 ---")
    data = get_data_from_pg()
    if data:
        _log.info(f"PostgreSQL에서 {len(data)}건 로드.")
    else:
        _log.warning("PostgreSQL 미사용. 파일 백업으로 대체합니다.")
        data = get_data_from_backup()
    sync_to_chroma(data)
