import hashlib
import json
import logging
import os
import traceback

import chromadb

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

DB_CONFIG = {
    "dbname": "mlops_db",
    "user": "postgres",
    "password": "password",
    "host": "localhost",
    "port": "5432",
}

_DATA_DIR   = os.path.join(os.path.dirname(__file__), "..", "data")
TRAIN_PATH  = os.path.join(_DATA_DIR, "train_set.json")
BACKUP_PATH = os.path.join(_DATA_DIR, "etl_backup.json")


def get_data_from_pg() -> list[dict]:
    try:
        import psycopg2
        with psycopg2.connect(**DB_CONFIG) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT id, log_text, error_category, action_type, target_process, reasoning "
                    "FROM agent_error_playbook;"
                )
                rows = cursor.fetchall()
        return [
            {
                "id": str(row[0]),
                "log_text": row[1],
                "error_category": str(row[2]),
                "action_type": str(row[3]),
                "target_process": str(row[4]) if row[4] else "unknown",
                "reasoning": str(row[5]) if row[5] else "",
            }
            for row in rows
        ]
    except Exception:
        logging.error(f"PostgreSQL 조회 실패:\n{traceback.format_exc()}")
        return []


def get_data_from_backup() -> list[dict]:
    """train_set.json 우선 사용, 없으면 etl_backup.json 폴백. 테스트셋 오염 방지."""
    train_path  = os.path.abspath(TRAIN_PATH)
    backup_path = os.path.abspath(BACKUP_PATH)

    if os.path.exists(train_path):
        load_path = train_path
        logging.info("train_set.json 사용 (테스트셋 분리됨)")
    elif os.path.exists(backup_path):
        load_path = backup_path
        logging.warning("train_set.json 없음 → etl_backup.json 폴백 (split_dataset.py 실행 권장)")
    else:
        logging.error("데이터 파일 없음")
        return []

    try:
        with open(load_path, encoding="utf-8") as f:
            backup = json.load(f)
        records = backup.get("data", [])
        result = []
        for row in records:
            log_text = row.get("log_text", "")
            if not log_text:
                continue
            # log_text MD5 해시를 ID로 사용 → 동일 텍스트는 항상 같은 ID → upsert 시 중복 방지
            doc_id = hashlib.md5(log_text.encode("utf-8")).hexdigest()
            result.append({
                "id": doc_id,
                "log_text": log_text,
                "error_category": str(row.get("error_category", "Unknown")),
                "action_type": str(row.get("action_type", "escalate_to_human")),
                "target_process": str(row.get("target_process") or "unknown"),
                "reasoning": str(row.get("reasoning", "")),
            })
        logging.info(f"{os.path.basename(load_path)}에서 {len(result)}건 로드 완료.")
        return result
    except Exception:
        logging.error(f"백업 파일 로드 실패:\n{traceback.format_exc()}")
        return []


def sync_to_chroma(data: list[dict]) -> None:
    if not data:
        logging.warning("동기화할 데이터가 없습니다.")
        return

    persist_directory = os.path.join(os.getcwd(), "data", "chroma_db")
    os.makedirs(persist_directory, exist_ok=True)
    client = chromadb.PersistentClient(path=persist_directory)
    collection = client.get_or_create_collection(name="error_playbook_vectors")

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
        logging.info(f"{len(data)}건이 ChromaDB(L1 Cache)에 동기화됐습니다.")
    except Exception:
        logging.error(f"ChromaDB 동기화 실패:\n{traceback.format_exc()}")


if __name__ == "__main__":
    logging.info("--- ETL Sync 시작 ---")
    data = get_data_from_pg()
    if data:
        logging.info(f"PostgreSQL에서 {len(data)}건 로드.")
    else:
        logging.warning("PostgreSQL 미사용. 파일 백업으로 대체합니다.")
        data = get_data_from_backup()
    sync_to_chroma(data)
