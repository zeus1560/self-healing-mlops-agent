import time
import logging
import torch

# 파이프라인이나 멀티프로세싱 없이 순수하게 모델만 띄워보는 테스트
logging.basicConfig(level=logging.INFO)


def test_local_llm():
    model_path = "./models/llama3-8b-ipex-int4"

    logging.info(f"1. [Test] 양자화된 로컬 모델 로딩 시작... ({model_path})")
    start_load = time.perf_counter()

    try:
        from ipex_llm.transformers import AutoModelForCausalLM
        from transformers import AutoTokenizer

        # 모델과 토크나이저 로드 (trust_remote_code=True 필수)
        tokenizer = AutoTokenizer.from_pretrained(model_path)
        model = AutoModelForCausalLM.load_low_bit(model_path, trust_remote_code=True)

        # XPU(Intel GPU)로 모델 밀어넣기
        model = model.to("xpu")

        load_time = time.perf_counter() - start_load
        logging.info(f"✅ 로딩 성공! (소요 시간: {load_time:.2f}초)")

    except Exception as e:
        logging.error(f"❌ 로딩 실패. 모델이 없거나 손상되었습니다: {e}")
        return

    # 2. 가상의 에러 로그 주입
    logging.info("2. [Test] 가상의 에러 로그 추론 시작...")
    fake_error = """
    Traceback (most recent call last):
      File "/opt/app/main.py", line 45, in <module>
        data = fetch_large_dataset()
    MemoryError: Unable to allocate 4.2 GiB for an array with shape (10000, 50000) and data type float64
    """

    # Llama 3에게 명령을 내리는 System Prompt (해결책만 내놓도록 강제)
    prompt = f"System: You are an MLOps AI. Analyze this error and provide a single safe shell/python command to fix or clear memory. No explanation.\nError: {fake_error}\nCommand:"

    inputs = tokenizer(prompt, return_tensors="pt").to("xpu")

    start_infer = time.perf_counter()

    # 3. 모델 추론 (생성)
    outputs = model.generate(
        **inputs,
        max_new_tokens=64,  # 명령어만 뱉을 테니 길게 뽑을 필요 없음
        temperature=0.1,  # 가장 확률 높은(안전한) 토큰만 선택
        do_sample=False,
    )

    infer_time = time.perf_counter() - start_infer
    result_text = tokenizer.decode(outputs[0], skip_special_tokens=True)

    # 프롬프트 부분을 잘라내고 순수 결과만 추출
    final_command = result_text.split("Command:")[-1].strip()

    logging.info(f"✅ 추론 완료! (소요 시간: {infer_time:.2f}초)")
    print("\n" + "=" * 50)
    print(f"🚨 주입된 에러:\n{fake_error.strip()}")
    print("-" * 50)
    print(f"🤖 LLM이 제시한 해결 커맨드:\n{final_command}")
    print("=" * 50 + "\n")


if __name__ == "__main__":
    test_local_llm()
