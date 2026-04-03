import os
import time
import logging
from transformers import AutoTokenizer
from ipex_llm.transformers import AutoModelForCausalLM

logging.basicConfig(level=logging.INFO)


def build_ipex_int4_model():
    model_id = "meta-llama/Meta-Llama-3-8B-Instruct"
    save_dir = "./models/llama3-8b-ipex-int4"

    os.makedirs(save_dir, exist_ok=True)

    logging.info(
        f"[Build] 허깅페이스에서 '{model_id}' 원본 다운로드 및 INT4 양자화 시작..."
    )
    logging.info(
        "[Build] ⚠️ 주의: 이 과정은 네트워크 속도에 따라 수 분이 소요되며, 시스템 RAM을 최대 16GB까지 사용합니다."
    )

    start_time = time.perf_counter()

    # 1. 토크나이저 다운로드 및 저장
    tokenizer = AutoTokenizer.from_pretrained(model_id)
    tokenizer.save_pretrained(save_dir)
    logging.info("[Build] 토크나이저 저장 완료.")

    # 2. IPEX-LLM을 이용해 다운로드와 동시에 INT4 양자화 (RAM 활용)
    # load_in_low_bit="sym_int4" 옵션이 핵심. 여기서 16GB -> 5.5GB로 다이어트됨.
    model = AutoModelForCausalLM.from_pretrained(
        model_id,
        load_in_low_bit="sym_int4",
        trust_remote_code=True,
        use_cache=True,
        resume_download=True,
        low_cpu_mem_usage=True,
    )

    # 3. 양자화된 바이너리를 로컬 디스크에 영구 저장
    logging.info(f"[Build] 양자화 완료. 로컬 디스크({save_dir})에 압축 모델 저장 중...")
    model.save_low_bit(save_dir)

    elapsed_time = time.perf_counter() - start_time
    logging.info(f"[Build] 🎉 모든 빌드 완료! (소요 시간: {elapsed_time:.2f}초)")
    logging.info(
        f"[Build] 이제 llm_engine.py가 지연 없이(Zero-Download) {save_dir}에서 모델을 로드할 수 있습니다."
    )


if __name__ == "__main__":
    build_ipex_int4_model()
