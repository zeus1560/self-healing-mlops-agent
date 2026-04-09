import psycopg2
from psycopg2.extras import execute_values
import logging
import json
import os
from datetime import datetime

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

# DB 연결 정보 (로컬 환경에 맞게 수정 필요)
DB_CONFIG = {
    "dbname": "mlops_db",  # 생성하신 데이터베이스 이름
    "user": "postgres",  # 본인의 PostgreSQL 계정명
    "password": "password",  # 본인의 비밀번호
    "host": "localhost",
    "port": "5432",
}


def create_table_if_not_exists(cursor):
    """테이블이 없으면 생성합니다."""
    create_table_query = """
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
    cursor.execute(create_table_query)
    logging.info("✅ 테이블 스키마 확인 및 생성 완료.")


def extract_mock_data() -> list[tuple]:
    """
    [Extract 단계]
    실제로는 GitHub API나 공식 문서를 크롤링하는 로직이 들어갈 자리입니다.
    지금은 파이프라인 테스트를 위해 고품질의 골든 데이터 3개를 튜플 형태로 반환합니다.
    """
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


def load_data_to_pg(data: list[tuple]):
    """
    [Load 단계]
    수집된 데이터를 PostgreSQL에 벌크(Bulk)로 밀어 넣습니다.
    ON CONFLICT를 통해 이미 수집된 로그(log_text)는 무시하여 멱등성(Idempotency)을 보장합니다.
    DB 연결 실패 시 로컬 JSON 파일로 백업합니다.
    """
    conn = None
    cursor = None

    try:
        logging.info(f"총 {len(data)}건의 데이터를 DB에 적재합니다.")
        conn = psycopg2.connect(**DB_CONFIG, connect_timeout=5)
        cursor = conn.cursor()

        # 1. 테이블 셋업
        create_table_if_not_exists(cursor)

        # 2. 벌크 인서트 (execute_values 사용으로 오버헤드 최소화)
        insert_query = """
            INSERT INTO agent_error_playbook 
            (log_text, error_category, severity, action_type, target_process, reasoning) 
            VALUES %s
            ON CONFLICT (log_text) DO NOTHING;
        """

        execute_values(cursor, insert_query, data)
        conn.commit()

        inserted_count = cursor.rowcount
        logging.info(
            f"[Success] 총 {len(data)}개의 데이터 중, {inserted_count}개의 새로운 에러 로그가 DB에 적재되었습니다."
        )

    except (psycopg2.OperationalError, psycopg2.DatabaseError, Exception) as e:
        logging.warning(
            f"[Warning] DB 연결 실패 ({type(e).__name__}): PostgreSQL 서버를 확인하세요"
        )
        logging.info("[Fallback] 로컬 JSON 파일로 백업합니다...")

        # 로컬 백업 저장
        backup_dir = os.path.join(os.path.dirname(__file__), "..", "data")
        os.makedirs(backup_dir, exist_ok=True)
        backup_file = os.path.join(backup_dir, "etl_backup.json")

        backup_data = {
            "timestamp": datetime.now().isoformat(),
            "db_error": str(e),
            "backup_count": len(data),
            "data": [
                {
                    "log_text": row[0],
                    "error_category": row[1],
                    "severity": row[2],
                    "action_type": row[3],
                    "target_process": row[4],
                    "reasoning": row[5],
                }
                for row in data
            ],
        }

        with open(backup_file, "w", encoding="utf-8") as f:
            json.dump(backup_data, f, indent=2, ensure_ascii=False)

        logging.info(f"[Success] {len(data)}개 데이터를 {backup_file}에 백업했습니다.")
        logging.info("[Note] PostgreSQL을 실행하려면:")
        logging.info("       Option 1 (Docker):   docker-compose up -d")
        logging.info("       Option 2 (로컬):     PostgreSQL을 포트 5432에서 실행")

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

    # 1. 데이터 수집 (추후 크롤러로 대체)
    crawled_data = extract_mock_data()

    # 2. DB 적재
    load_data_to_pg(crawled_data)
