import psycopg2
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
DB_CONFIG = {
    "dbname": "mlops_db",
    "user": "postgres",
    "password": "password",
    "host": "localhost",
    "port": "5432",
}

try:
    conn = psycopg2.connect(**DB_CONFIG)
    cursor = conn.cursor()
    # 테이블 내부 데이터를 싹 비우고, ID(Auto Increment)도 1부터 다시 시작하도록 초기화
    cursor.execute("TRUNCATE TABLE agent_error_playbook RESTART IDENTITY;")
    conn.commit()
    logging.info("🧹 DB 테이블이 완벽하게 초기화되었습니다. (가짜 데이터 삭제 완료)")
except Exception as e:
    logging.error(f"초기화 실패: {e}")
finally:
    if "conn" in locals() and conn:
        cursor.close()
        conn.close()
