import time
import logging
import textwrap
import torch
import warnings

# transformers 라이브러리의 do_sample warning 억제
warnings.filterwarnings("ignore", message=".*do_sample.*is set to.*False.*")

logging.basicConfig(level=logging.INFO)


def build_few_shot_prompt(error_log: str) -> str:
    return textwrap.dedent(f"""
    <|begin_of_text|><|start_header_id|>system<|end_header_id|>
    You are a ruthless, highly efficient Linux MLOps Agent operating directly on the host OS. 
    Your ONLY purpose is to analyze system/application errors and output a SINGLE, raw shell or python command to resolve it.
    
    STRICT RULES:
    1. DO NOT output any explanations, apologies, or markdown formatting (no ```bash).
    2. Output ONLY the raw command string.
    3. Use safe commands like `systemctl`, `pkill`, `rm -rf /tmp/`, or inline python scripts.
    <|eot_id|>

    <|start_header_id|>user<|end_header_id|>
    Error: torch.cuda.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB.
    <|eot_id|>
    <|start_header_id|>assistant<|end_header_id|>
    python3 -c "import torch; torch.cuda.empty_cache()"<|eot_id|>

    <|start_header_id|>user<|end_header_id|>
    Error: {error_log}
    <|eot_id|>
    <|start_header_id|>assistant<|end_header_id|>
    """).strip()


def run_test():
    model_path = "./models/llama3-8b-ipex-int4"
    logging.info(f"1. [Test] 양자화된 로컬 모델 로딩 시작... ({model_path})")
    start_load = time.perf_counter()

    # 지연 로딩 검증
    from ipex_llm.transformers import AutoModelForCausalLM
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(model_path)
    model = AutoModelForCausalLM.load_low_bit(model_path, trust_remote_code=True)

    # 핵심: 모델을 CPU로 적재 (XPU 미지원 환경 호환)
    logging.info("2. [Test] CPU로 모델 적재 중...")
    device = "cpu"
    model = model.to(device)

    # do_sample=False 모드에서 불필요한 샘플링 변수들 제거하여 warning 방지
    model.generation_config.temperature = None
    model.generation_config.top_p = None

    load_time = time.perf_counter() - start_load
    logging.info(f"✅ 모델 로딩 및 XPU 적재 완료! (소요 시간: {load_time:.2f}초)")

    # 3. 가상의 치명적 에러 주입
    fake_error = "FATAL: ai-backend-service memory leak detected at 0x00A1F. OOM Killer is targeting the process."
    prompt = build_few_shot_prompt(fake_error)

    inputs = tokenizer(prompt, return_tensors="pt").to(device)

    logging.info("3. [Test] XPU 가속 추론 시작...")
    start_infer = time.perf_counter()

    outputs = model.generate(**inputs, max_new_tokens=64, do_sample=False)

    infer_time = time.perf_counter() - start_infer

    # 입력 프롬프트 길이만큼 잘라낸 후, 순수 생성된 답변만 디코딩
    input_length = inputs.input_ids.shape[1]
    generated_tokens = outputs[0][input_length:]
    final_command = tokenizer.decode(generated_tokens, skip_special_tokens=True).strip()

    logging.info(f"✅ 추론 완료! (소요 시간: {infer_time:.2f}초)")
    print("\n" + "=" * 50)
    print(f"[Error] 주입된 에러:\n{fake_error}")
    print("-" * 50)
    print(f"[LLM Response] LLM이 제시한 해결 커맨드:\n{final_command}")
    print("=" * 50 + "\n")


if __name__ == "__main__":
    run_test()
    exit(0)
