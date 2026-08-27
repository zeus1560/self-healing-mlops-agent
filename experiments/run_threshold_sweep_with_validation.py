"""
τ 튜닝 (validation set에서만)
최종 성능 평가는 별도로 final_test set에서 수행
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
THRESHOLDS    = [0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 1.00, 1.20, 1.50]


def load_samples(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["data"]


def get_collection():
    client = chromadb.PersistentClient(
        path=str(CHROMA_PATH),
        settings=Settings(anonymized_telemetry=False),
    )
    return client.get_collection("error_playbook_vectors")


def evaluate(collection, test_samples: list[dict], threshold: float):
    latencies = []
    tp = fp = fn = tn = 0
    action_tp = action_fp = action_fn = action_tn = 0

    for sample in test_samples:
        query_text    = sample["log_text"]
        true_category = sample["error_category"]
        true_action   = sample.get("action_type", "")

        t0 = time.perf_counter()
        result = collection.query(query_texts=[query_text], n_results=1)
        latency = (time.perf_counter() - t0) * 1000  # ms
        latencies.append(latency)

        distance      = result["distances"][0][0]
        pred_meta     = result["metadatas"][0][0]
        pred_category = pred_meta.get("error_category", "Unknown")
        pred_action   = pred_meta.get("action_type", "")

        l1_triggered   = distance < threshold
        cat_correct    = pred_category == true_category
        action_correct = pred_action.lower().replace("-", "_") == true_action.lower().replace("-", "_")

        if l1_triggered and cat_correct:
            tp += 1
        elif l1_triggered and not cat_correct:
            fp += 1
        elif not l1_triggered and cat_correct:
            fn += 1
        else:
            tn += 1

        if l1_triggered and action_correct:
            action_tp += 1
        elif l1_triggered and not action_correct:
            action_fp += 1
        elif not l1_triggered and action_correct:
            action_fn += 1
        else:
            action_tn += 1

    total = len(test_samples)
    tpr       = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    fpr       = fp / (fp + tn) if (fp + tn) > 0 else 0.0
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall    = tpr
    f1        = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0

    a_prec    = action_tp / (action_tp + action_fp) if (action_tp + action_fp) > 0 else 0.0
    a_rec     = action_tp / (action_tp + action_fn) if (action_tp + action_fn) > 0 else 0.0
    action_f1 = (2 * a_prec * a_rec / (a_prec + a_rec)) if (a_prec + a_rec) > 0 else 0.0
    l1_rate   = (tp + fp) / total

    return {
        "threshold":      threshold,
        "tpr":            round(tpr, 4),
        "fpr":            round(fpr, 4),
        "precision":      round(precision, 4),
        "recall":         round(recall, 4),
        "f1":             round(f1, 4),
        "action_f1":      round(action_f1, 4),
        "action_precision": round(a_prec, 4),
        "l1_hit_rate":    round(l1_rate, 4),
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "avg_latency_ms": round(sum(latencies) / len(latencies), 3),
    }


def main():
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    
    print("=" * 70)
    print("τ 튜닝: Validation Set (200개) 에서만 수행")
    print("=" * 70)
    
    validation_samples = load_samples(VALIDATION_SET_PATH)
    final_test_samples = load_samples(FINAL_TEST_SET_PATH)
    collection = get_collection()

    print(f"\nValidation set: {len(validation_samples)}개 | threshold {len(THRESHOLDS)}개 sweep")
    print(f"{'Threshold':>10} {'F1(cat)':>8} {'F1(action)':>11} {'Precision':>10} {'Recall':>8} {'L1Rate':>8}")
    print("-" * 75)

    rows = []
    for thresh in THRESHOLDS:
        r = evaluate(collection, validation_samples, thresh)
        rows.append(r)
        print(f"{r['threshold']:>10.2f} {r['f1']:>8.3f} {r['action_f1']:>11.3f} "
              f"{r['precision']:>10.3f} {r['recall']:>8.3f} {r['l1_hit_rate']:>8.3f}")

    if not rows:
        print("평가 결과 없음")
        return

    best_action_f1 = max(r["action_f1"] for r in rows)
    candidates = [r for r in rows if best_action_f1 - r["action_f1"] <= 0.01]
    best_by_action = min(candidates, key=lambda x: (-x["action_precision"], x["threshold"]))

    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = RESULTS_DIR / f"threshold_tuning_validation_{ts}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(f"\n[Validation] CSV 저장: {csv_path}")
    print(f"최적 threshold (validation 기준): {best_by_action['threshold']:.2f} "
          f"(action_f1={best_by_action['action_f1']:.3f})")

    # ─────────────────────────────────────────────────────────────────
    # 최종 평가: Final Test Set (207개) 에서만 수행
    # ─────────────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("최종 성능: Final Test Set (207개) 에서만 평가")
    print(f"τ = {best_by_action['threshold']:.2f} 적용")
    print("=" * 70)

    final_result = evaluate(collection, final_test_samples, best_by_action["threshold"])
    
    print(f"\n최종 성능 (Final Test Set 기준):")
    print(f"  Threshold: {final_result['threshold']:.2f}")
    print(f"  Precision: {final_result['precision']:.4f}")
    print(f"  Recall: {final_result['recall']:.4f}")
    print(f"  F1 (category): {final_result['f1']:.4f}")
    print(f"  F1 (action): {final_result['action_f1']:.4f}")
    print(f"  L1 Hit Rate: {final_result['l1_hit_rate']:.4f}")
    print(f"  Avg Latency: {final_result['avg_latency_ms']:.1f}ms")

    final_csv_path = RESULTS_DIR / f"threshold_final_test_{ts}.csv"
    with open(final_csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "threshold", "precision", "recall", "f1", "action_f1", 
            "l1_hit_rate", "avg_latency_ms", "tp", "fp", "fn", "tn"
        ])
        writer.writeheader()
        writer.writerow({
            "threshold": final_result["threshold"],
            "precision": final_result["precision"],
            "recall": final_result["recall"],
            "f1": final_result["f1"],
            "action_f1": final_result["action_f1"],
            "l1_hit_rate": final_result["l1_hit_rate"],
            "avg_latency_ms": final_result["avg_latency_ms"],
            "tp": final_result["tp"],
            "fp": final_result["fp"],
            "fn": final_result["fn"],
            "tn": final_result["tn"],
        })
    print(f"\n[Final Test] CSV 저장: {final_csv_path}")

    print("\n" + "=" * 70)
    print("✓ 결과 요약")
    print("=" * 70)
    print(f"Validation set (n=200): τ를 튜닝하여 최적값 {best_by_action['threshold']:.2f} 선정")
    print(f"Final test set (n=207): {best_by_action['threshold']:.2f}에서 최종 성능 평가")
    print(f"  → F1(cat)={final_result['f1']:.4f}, Precision={final_result['precision']:.4f}, Recall={final_result['recall']:.4f}")
    print(f"\n✓ test set leakage 완벽 해결! 이제 final test 결과만 논문에 보고하세요.")


if __name__ == "__main__":
    main()
