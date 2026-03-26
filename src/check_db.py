import psycopg2

DB_CONFIG = {
    "dbname": "mlops_db",
    "user": "postgres",
    "password": "password",
    "host": "localhost",
    "port": "5432",
}


def verify_data():
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cursor = conn.cursor()

        # DB에 있는 모든 데이터를 최신순으로 가져옵니다
        cursor.execute(
            "SELECT id, error_category, action_type, log_text, reasoning FROM agent_error_playbook ORDER BY id DESC;"
        )
        rows = cursor.fetchall()

        print(f"\n✅ 현재 DB에 총 {len(rows)}개의 에러 플레이북이 저장되어 있습니다.\n")

        for row in rows:
            print(f"🔹 [ID: {row[0]}] {row[1]} -> 액션: {row[2]}")
            print(f"   - 원본 로그: {row[3]}")
            print(f"   - 조치 사유: {row[4]}")
            print("-" * 60)

    except Exception as e:
        print(f"❌ DB 조회 실패: {e}")
    finally:
        if "conn" in locals() and conn:
            cursor.close()
            conn.close()


if __name__ == "__main__":
    verify_data()
