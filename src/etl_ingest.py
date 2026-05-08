import json
import logging
import os
import traceback
from datetime import datetime

import psycopg2
from psycopg2.extras import execute_values

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

DB_CONFIG = {
    "dbname": "mlops_db",
    "user": "postgres",
    "password": "password",
    "host": "localhost",
    "port": "5432",
}


def create_table_if_not_exists(cursor) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_error_playbook (
            id SERIAL PRIMARY KEY,
            log_text TEXT NOT NULL UNIQUE,
            error_category VARCHAR(50) NOT NULL,
            severity VARCHAR(20) NOT NULL,
            action_type VARCHAR(50) NOT NULL,
            target_process VARCHAR(100),
            reasoning TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    logging.info("테이블 스키마 확인 및 생성 완료.")


def extract_mock_data() -> list[tuple]:
    return [
        (
            "[ERROR] psycopg2.OperationalError: server closed the connection unexpectedly",
            "Network_Timeout",
            "HIGH",
            "restart_service",
            "postgres_pool",
            "DB 커넥션이 예기치 않게 끊어짐. 커넥션 풀을 관리하는 서비스 재시작 필요.",
        ),
        (
            "RuntimeError: CUDA out of memory. Tried to allocate 1.50 GiB.",
            "Out_Of_Memory",
            "CRITICAL",
            "clear_memory",
            None,
            "GPU VRAM 고갈. 즉각적인 캐시 클리어(torch.cuda.empty_cache) 및 OS 메모리 확보 필요.",
        ),
        (
            "django.core.exceptions.ImproperlyConfigured: Set the SECRET_KEY environment variable.",
            "Configuration_Error",
            "MEDIUM",
            "escalate_to_human",
            None,
            "환경 변수 누락. 시스템이 자동으로 해결할 수 없으므로 관리자에게 즉시 알림.",
        ),
    ]


def load_data_to_pg(data: list[tuple]) -> None:
    conn = None
    cursor = None
    try:
        logging.info(f"총 {len(data)}건의 데이터를 DB에 적재합니다.")
        conn = psycopg2.connect(**DB_CONFIG, connect_timeout=5)
        cursor = conn.cursor()
        create_table_if_not_exists(cursor)

        insert_query = """
            INSERT INTO agent_error_playbook
            (log_text, error_category, severity, action_type, target_process, reasoning)
            VALUES %s
            ON CONFLICT (log_text) DO NOTHING;
        """
        execute_values(cursor, insert_query, data)
        conn.commit()
        logging.info(
            f"[Success] {len(data)}개 중 {cursor.rowcount}개 신규 적재 완료."
        )

    except Exception:
        logging.error(f"[DB 적재 실패] PostgreSQL 연결 또는 쿼리 오류:\n{traceback.format_exc()}")
        logging.info("[Fallback] 로컬 JSON 파일로 백업합니다...")

        backup_dir = os.path.join(os.path.dirname(__file__), "..", "data")
        os.makedirs(backup_dir, exist_ok=True)
        backup_file = os.path.join(backup_dir, "etl_backup.json")

        existing_items: list[dict] = []
        if os.path.exists(backup_file):
            try:
                with open(backup_file, encoding="utf-8") as f:
                    existing_items = json.load(f).get("data", [])
            except Exception:
                logging.error(
                    f"기존 백업 파일 파싱 실패 — 빈 목록으로 시작:\n{traceback.format_exc()}"
                )
                existing_items = []

        existing_texts = {item["log_text"] for item in existing_items}
        new_items = [
            {
                "log_text": row[0],
                "error_category": row[1],
                "severity": row[2],
                "action_type": row[3],
                "target_process": row[4],
                "reasoning": row[5],
            }
            for row in data
            if row[0] not in existing_texts
        ]

        merged = existing_items + new_items
        backup_data = {
            "timestamp": datetime.now().isoformat(),
            "backup_count": len(merged),
            "data": merged,
        }

        with open(backup_file, "w", encoding="utf-8") as f:
            json.dump(backup_data, f, indent=2, ensure_ascii=False)

        logging.info(f"[Backup] 신규 {len(new_items)}건 추가, 전체 {len(merged)}건 보존 ({backup_file})")
        logging.info("[Note] PostgreSQL 실행: Option 1 (Docker): docker-compose up -d")

        if conn:
            conn.rollback()

    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()
            logging.info("[Info] DB 커넥션 종료.")


if __name__ == "__main__":
    logging.info("--- MLOps Agent ETL 파이프라인 시작 ---")
    crawled_data = extract_mock_data()
    load_data_to_pg(crawled_data)
