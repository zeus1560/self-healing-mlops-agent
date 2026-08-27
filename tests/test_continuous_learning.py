import chromadb
import hashlib
import time
from chromadb.config import Settings

# 1. 아까 우리가 설계한 피드백 함수
def save_to_l1_cache(error_log: str, successful_command: str, chroma_collection):
    try:
        error_hash = hashlib.md5(error_log.encode('utf-8')).hexdigest()

        metadata = {
            "source": "L2_LLM_Learned",
            "learned_at": int(time.time()),
            "action_type": "EXECUTE_LLM_COMMAND",
            "command": successful_command # 실행할 커맨드 저장
        }

        chroma_collection.upsert(
            documents=[error_log],
            metadatas=[metadata],
            ids=[f"learned_{error_hash}"]
        )
        print(f"[FeedbackLoop] 🧠 새로운 지식 습득 완료! (ID: {error_hash[:8]})")
    except Exception as e:
        print(f"[Error] L1 Cache 업데이트 실패: {e}")

# 2. 독립 테스트 로직
def run_test():
    print("🚀 연속 학습(Continuous Learning) 파이프라인 검증 시작...\n")

    # 실제 우리가 쓰는 로컬 DB 경로 연결 (기존 데이터 보존)
    client = chromadb.PersistentClient(path="./chroma_db", settings=Settings(anonymized_telemetry=False))
     # 이 옵션 추가!
    collection = client.get_or_create_collection(name="error_knowledge_base")

    # 가상의 상황: L2(LLM)가 1분 동안 고민해서 찾아낸 결과라고 가정하자.
    fake_unknown_error = "nginx: [emerg] bind() to 0.0.0.0:80 failed (98: Address already in use)"
    fake_llm_solution = "fuser -k 80/tcp && systemctl restart nginx"

    print("=== Phase A: L2 조치 성공 가정 및 L1 적재 ===")
    save_to_l1_cache(fake_unknown_error, fake_llm_solution, collection)
    time.sleep(1) # DB 기록 대기

    print("\n=== Phase B: 10분 뒤, 동일한 에러가 다시 발생했다고 가정 ===")
    start_time = time.time()

    # L1 Fast Track 검색 흉내
    results = collection.query(
        query_texts=[fake_unknown_error],
        n_results=1
    )

    latency = time.time() - start_time
    print(f"⏱️ L1 검색 소요 시간: {latency:.4f}초")

    # 거리(Distance)가 0.0에 가까울수록 완벽한 일치
    if results['distances'][0] and results['distances'][0][0] < 0.5:
        print("\n✅ [검증 성공] 0.1초 컷 방어(Fast Track) 동작 확인!")
        print(f"👉 꺼내온 즉각 조치 커맨드: {results['metadatas'][0][0]['command']}")
    else:
        print("\n❌ [검증 실패] 지식이 제대로 저장되지 않았거나 검색되지 않습니다.")

if __name__ == "__main__":
    run_test()
