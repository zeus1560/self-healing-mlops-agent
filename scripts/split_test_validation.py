"""
Test set을 validation(200) / final_test(207)로 분리
τ, Top-K 튜닝: validation set에서만
최종 성능 평가: final_test set에서만

이를 통해 test set leakage를 완전히 해결한다.
"""
import json
import random
from pathlib import Path

TEST_SET_PATH = Path("data/test_set.json")
VALIDATION_SIZE = 200
SEED = 42

def split_test_set():
    data = json.loads(TEST_SET_PATH.read_text(encoding="utf-8"))
    samples = data["data"]
    
    print(f"전체 테스트 샘플: {len(samples)}")
    
    # 재현 가능한 분할을 위해 seed 설정
    random.seed(SEED)
    random.shuffle(samples)
    
    validation_samples = samples[:VALIDATION_SIZE]
    final_test_samples = samples[VALIDATION_SIZE:]
    
    print(f"Validation set: {len(validation_samples)}")
    print(f"Final test set: {len(final_test_samples)}")
    
    # validation set 저장
    validation_path = Path("data/validation_set.json")
    validation_path.write_text(
        json.dumps({"data": validation_samples}, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    print(f"\n저장: {validation_path}")
    
    # final test set 저장
    final_test_path = Path("data/final_test_set.json")
    final_test_path.write_text(
        json.dumps({"data": final_test_samples}, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    print(f"저장: {final_test_path}")
    
    print("\n주의: 이제 이전 test_set.json은 더 이상 사용하지 않습니다.")
    print("- τ 튜닝: run_threshold_sweep_with_validation.py (validation set)")
    print("- Top-K 튜닝: run_top_k_sweep_with_validation.py (validation set)")
    print("- 최종 평가: run_baseline_compare_final.py (final_test set)")


if __name__ == "__main__":
    split_test_set()
