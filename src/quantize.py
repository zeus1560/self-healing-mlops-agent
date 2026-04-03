from ipex_llm.transformers import AutoModelForCausalLM
from transformers import AutoTokenizer

original_model_id = "meta-llama/Meta-Llama-3-8B-Instruct"
save_dir = "./models/llama3-8b-ipex-woq-int4"  # 새로운 woq 전용 폴더

print("⏳ [ETL] Llama 3 모델 최신 woq_int4 양자화 시작 (시간 및 메모리 소요됨)...")

# 1. 모델을 최신 포맷으로 양자화하여 로드
model = AutoModelForCausalLM.from_pretrained(
    original_model_id,
    load_in_low_bit="woq_int4",
    optimize_model=True,
    trust_remote_code=True,
    use_cache=True,
)
tokenizer = AutoTokenizer.from_pretrained(original_model_id)

# 2. 로컬 디스크에 영구 저장 (다음부터는 이 폴더만 읽으면 됨)
print("💾 [ETL] 양자화된 모델을 디스크에 저장 중...")
model.save_low_bit(save_dir)
tokenizer.save_pretrained(save_dir)
print(f"✅ [ETL] 완료! 모델이 {save_dir} 에 저장되었습니다.")
