# setup_model.py
import os
from huggingface_hub import snapshot_download

def init_model_environment():
    # 실제 사용 중인 Qwen2 모델의 정확한 허깅페이스 Repo ID로 변경해줘.
    # 예: "Qwen/Qwen2-7B-Instruct" 
    MODEL_ID = "여기에_모델_REPO_ID_입력" 
    
    # 모델이 저장될 로컬 경로 지정 (gitignore에 등록해둔 경로)
    LOCAL_DIR = "./models/qwen2_quantized"
    
    print(f"🚀 [System] Init: '{MODEL_ID}' 모델 다운로드를 시작합니다...")
    print(f"📂 [System] 저장 경로: {LOCAL_DIR}")
    print("⏳ 파일 크기에 따라 수 분 정도 소요될 수 있습니다. (네트워크 상태 확인)")
    
    try:
        # 지정된 로컬 폴더에 모델 가중치를 다운로드 (이미 있으면 캐시 확인 후 스킵)
        snapshot_download(
            repo_id=MODEL_ID,
            local_dir=LOCAL_DIR,
            local_dir_use_symlinks=False # WSL 환경 파일 시스템 충돌 방지
        )
        print("✅ [System] Success: 모델 다운로드 및 준비가 완료되었습니다!")
    except Exception as e:
        print(f"❌ [System] Error: 모델 다운로드 중 에러 발생: {e}")

if __name__ == "__main__":
    init_model_environment()