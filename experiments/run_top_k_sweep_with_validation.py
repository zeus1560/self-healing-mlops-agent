"""
Top-K 튜닝 (validation set에서만)
최종 성능 평가는 별도로 final_test set에서 수행
τ는 사전에 run_threshold_sweep_with_validation.py에서 튜닝한 값 사용
"""
import csv
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import chromadb
from chromadb.config import Settings

VALIDATION_SET_PATH = Path("data/validation_set.json")
FINAL_TEST_SET_PATH = Path("data/final_test_set.json")
CHROMA_PATH   = Path("data/chroma_db")
RESULTS_DIR   = Path("experiments/results")
THRESHOLD     = 0.50  # run_threshold_sweep_with_validation.py에서 결정한 최적값
TOP_K_VALUES  = [1, 2, 3, 5, 7, 10]


def load_samples(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["data"]


def get_collection():
    client = chromadb.PersistentClient(
        path=str(CHROMA_PATH),
        settings=Settings(anonymized_telemetry=False),
    )
    return client.get_collection("error_playbook_vectors")


def majority_vote(metadatas: list[dict], distances: list[float], threshold: float) -> str:
    """threshold 이내 결과들 중 다수결, 없으면 Unknown."""
    candidates = [
        m.get("error_category", "Unknown")
        for m, d in zip(metadatas, distances)
        if d < threshold
    ]
    if not candidates:
        return "Unknown"
    return max(set(candidates), key=candidates.count)


def majority_vote_action(metadatas: list[dict], distances: list[float], threshold: float) -> str:
    """threshold 이내 결과들 중 action_type 다수결, 없으면 빈 문자열."""
    candidates = [
        m.get("action_type", "")
        for m, d in zip(metadatas, distances)
        if d < threshold and m.get("action_type")
    ]
    if not candidates:
        return ""
    return max(set(candidates), key=candidates.count)


def evaluate_k(collection, test_samples: list[dict], k: int, threshold: float) -> dict:
    latencies = []
    correct = action_correct = 0
    unknown = 0

    for sample in test_samples:
        query_text    = sample["log_text"]
        true_category = sample["error_category"]
        true_action   = sample.get("action_type", "")

        t0 = time.perf_counter()
        result = collection.query(query_texts=[query_text], n_results=k)
        latency = (time.perf_counter() - t0) * 1000  # ms
        latencies.append(latency)

        pred        = majority_vote(result["metadatas"][0], result["distances"][0], threshold)
        pred_action = majority_vote_action(result["metadatas"][0], result["distances"][0], threshold)

        if pred == true_category:
            correct += 1
        if pred == "Unknown":
            unknown += 1

        if pred_action and true_action:
            norm = lambda x: x.lower().replace("-", "_")
            if norm(pred_action) == norm(true_action):
                action_correct += 1

    total    = len(test_samples)
    accuracy = correct / total
    coverage = (total - unknown) / total
    action_accuracy = action_correct / total if total > 0 else 0

    prec = correct / (total - unknown) if (total - unknown) > 0 else 0.0
    rec  = correct / (total - unknown) if (total - unknown) > 0 else 0.0
    f1   = (2 * prec * rec / (prec + rec)) if (prec + rec) > 0 else 0.0

    return {
        "k":                k,
        "accuracy":         round(accuracy, 4),
        "action_accuracy":  round(action_accuracy, 4),
        "f1":               round(f1, 4),
        "coverage":         round(coverage, 4),
        "unknown":          unknown,
        "total":            total,
        "avg_latency_ms":   round(sum(latencies) / len(latencies), 3),
    }


def main():
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print("Top-K 튜닝: Validation Set (200개) 에서만 수행")
    print(f"τ = {THRESHOLD:.2f} (사전 튜닝 값)")
    print("=" * 70)

    validation_samples = load_samples(VALIDATION_SET_PATH)
    final_test_samples = load_samples(FINAL_TEST_SET_PATH)
    collection = get_collection()

    print(f"\nValidation set: {len(validation_samples)}개 | Top-K {len(TOP_K_VALUES)}개 평가")
    print(f"{'K':>4} {'Accuracy':>10} {'ActAcc':>8} {'F1':>8} {'Coverage':>10} {'Unknown':>8} {'Latency(ms)':>12}")
    print("-" * 70)

    rows = []
    for k in TOP_K_VALUES:
        r = evaluate_k(collection, validation_samples, k, THRESHOLD)
        rows.append(r)
        print(f"{r['k']:>4} {r['accuracy']:>10.3f} {r['action_accuracy']:>8.3f} {r['f1']:>8.3f} "
              f"{r['coverage']:>10.3f} {r['unknown']:>8} {r['avg_latency_ms']:>12.1f}")

    if not rows:
        print("평가 결과 없음")
        return

    best = max(rows, key=lambda x: x["accuracy"])

    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = RESULTS_DIR / f"top_k_tuning_validation_{ts}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(f"\n[Validation] CSV 저장: {csv_path}")
    print(f"최적 K (validation 기준): {best['k']} (accuracy={best['accuracy']:.3f})")

    # ─────────────────────────────────────────────────────────────────
    # 최종 평가: Final Test Set (207개) 에서만 수행
    # ─────────────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("최종 성능: Final Test Set (207개) 에서만 평가")
    print(f"K = {best['k']}, τ = {THRESHOLD:.2f} 적용")
    print("=" * 70)

    final_result = evaluate_k(collection, final_test_samples, best["k"], THRESHOLD)

    print(f"\n최종 성능 (Final Test Set 기준):")
    print(f"  K: {final_result['k']}")
    print(f"  Accuracy (Category): {final_result['accuracy']:.4f}")
    print(f"  Action Accuracy: {final_result['action_accuracy']:.4f}")
    print(f"  F1 (Category): {final_result['f1']:.4f}")
    print(f"  Coverage: {final_result['coverage']:.4f}")
    print(f"  Avg Latency: {final_result['avg_latency_ms']:.1f}ms")

    final_csv_path = RESULTS_DIR / f"top_k_final_test_{ts}.csv"
    with open(final_csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=final_result.keys())
        writer.writeheader()
        writer.writerow(final_result)
    print(f"\n[Final Test] CSV 저장: {final_csv_path}")

    print("\n" + "=" * 70)
    print("✓ 결과 요약")
    print("=" * 70)
    print(f"Validation set (n=200): Top-K를 튜닝하여 최적값 K={best['k']} 선정")
    print(f"Final test set (n=207): K={best['k']}에서 최종 성능 평가")
    print(f"  → Accuracy={final_result['accuracy']:.4f}, F1={final_result['f1']:.4f}")
    print(f"\n✓ test set leakage 완벽 해결!")


if __name__ == "__main__":
    main()
