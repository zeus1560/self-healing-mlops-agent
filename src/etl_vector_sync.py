import psycopg2
import chromadb
import logging
import os
from utils.profiler import profile_memory

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


def get_data_from_pg():
    """PostgreSQL에서 원본 에러 로그와 ID를 가져옵니다."""
    conn = None
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cursor = conn.cursor()
        # Vector DB에는 검색에 쓰일 원본 텍스트와 메타데이터만 넘깁니다.
        cursor.execute(
            "SELECT id, log_text, error_category, action_type FROM agent_error_playbook;"
        )
        rows = cursor.fetchall()
        return rows
    except Exception as e:
        logging.error(f"PostgreSQL 조회 실패: {e}")
        return []
    finally:
        if conn:
            cursor.close()
            conn.close()


@profile_memory
def sync_to_chroma(pg_data):
    """
    ChromaDB를 로컬 파일 시스템에 초기화하고,
    PostgreSQL의 데이터를 벡터로 변환하여 동기화합니다.
    """
    if not pg_data:
        logging.warning("동기화할 데이터가 없습니다.")
        return

    # 1. ChromaDB 로컬 저장소 설정 (data/chroma_db 폴더에 데이터 영구 저장)
    persist_directory = os.path.join(os.getcwd(), "data", "chroma_db")
    chroma_client = chromadb.PersistentClient(path=persist_directory)

    # 2. 컬렉션(테이블 개념) 생성 또는 로드
    # ONNX 기반의 기본 임베딩 함수(all-MiniLM-L6-v2)가 자동 적용됩니다.
    collection = chroma_client.get_or_create_collection(name="error_playbook_vectors")

    # 데이터 파싱
    ids = [str(row[0]) for row in pg_data]  # ChromaDB는 ID를 문자열로 받습니다
    documents = [row[1] for row in pg_data]  # 임베딩될 실제 에러 로그 텍스트
    metadatas = [
        {"category": row[2], "action": row[3]} for row in pg_data
    ]  # 메타데이터

    logging.info(
        f"총 {len(documents)}개의 문서를 임베딩 및 Vector DB에 적재합니다. (수 초 정도 소요될 수 있습니다...)"
    )

    # 3. Vector DB에 밀어넣기 (내부적으로 C++ ONNX 런타임이 돌면서 텍스트를 숫자로 바꿈)
    collection.upsert(ids=ids, documents=documents, metadatas=metadatas)

    logging.info(f"✅ ChromaDB 동기화 완료! (저장 위치: {persist_directory})")
    logging.info(f"현재 Vector DB에 보관된 총 문서 수: {collection.count()}")


if __name__ == "__main__":
    logging.info("--- PostgreSQL to Vector DB 동기화 파이프라인 시작 ---")
    data = get_data_from_pg()
    sync_to_chroma(data)
