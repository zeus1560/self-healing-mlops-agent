"""
ChromaDB Top-K 검색 개수 튜닝
K=1, 2, 3, 5 비교 — 다수결(majority voting) 적용
결과: experiments/results/topk_results.csv
"""
import csv
import json
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import chromadb
from chromadb.config import Settings

TEST_SET_PATH = Path("data/test_set.json")
CHROMA_PATH   = Path("data/chroma_db")
RESULTS_DIR   = Path("experiments/results")
THRESHOLD     = 0.60
K_VALUES      = [1, 2, 3, 5]


def majority_vote(metadatas: list[dict], distances: list[float], threshold: float) -> str:
    """threshold 이내 결과들 중 다수결, 없으면 Unknown."""
    candidates = [
        m.get("error_category", "Unknown")
        for m, d in zip(metadatas, distances)
        if d < threshold
    ]
    if not candidates:
        return "Unknown"
    return Counter(candidates).most_common(1)[0][0]


def majority_vote_action(metadatas: list[dict], distances: list[float], threshold: float) -> str:
    """threshold 이내 결과들 중 action_type 다수결, 없으면 빈 문자열."""
    candidates = [
        m.get("action_type", "")
        for m, d in zip(metadatas, distances)
        if d < threshold and m.get("action_type")
    ]
    if not candidates:
        return ""
    return Counter(candidates).most_common(1)[0][0]


def evaluate_k(collection, test_samples: list[dict], k: int, threshold: float) -> dict:
    correct = action_correct = unknown = 0
    tp = fp = fn = tn = 0
    latencies = []

    for s in test_samples:
        t0     = time.perf_counter()
        result = collection.query(query_texts=[s["log_text"]], n_results=k)
        latencies.append((time.perf_counter() - t0) * 1000)

        pred        = majority_vote(result["metadatas"][0], result["distances"][0], threshold)
        pred_action = majority_vote_action(result["metadatas"][0], result["distances"][0], threshold)
        true_cat    = s["error_category"]
        true_action = s.get("action_type", "")

        l1_hit     = pred != "Unknown"
        cat_ok     = pred == true_cat
        norm       = lambda x: x.lower().replace("-", "_")
        action_ok  = bool(pred_action and true_action and norm(pred_action) == norm(true_action))

        if pred == true_cat:
            correct += 1
        if pred == "Unknown":
            unknown += 1
        if action_ok:
            action_correct += 1

        if l1_hit and cat_ok:
            tp += 1
        elif l1_hit and not cat_ok:
            fp += 1
        elif not l1_hit and cat_ok:
            fn += 1
        else:
            tn += 1

    total     = len(test_samples)
    prec      = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    rec       = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1        = (2 * prec * rec / (prec + rec)) if (prec + rec) > 0 else 0.0
    return {
        "k":              k,
        "accuracy":       round(correct / total, 4),
        "action_accuracy":round(action_correct / total, 4),
        "f1":             round(f1, 4),
        "coverage":       round((total - unknown) / total, 4),
        "correct":        correct,
        "unknown":        unknown,
        "avg_latency_ms": round(sum(latencies) / len(latencies), 3),
    }


def main():
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    test_samples = json.loads(TEST_SET_PATH.read_text(encoding="utf-8"))["data"]
    collection   = chromadb.PersistentClient(
        path=str(CHROMA_PATH),
        settings=Settings(anonymized_telemetry=False),
    ).get_collection("error_playbook_vectors")

    print(f"테스트 샘플: {len(test_samples)}개 | threshold={THRESHOLD}")
    print(f"{'K':>4} {'Accuracy':>10} {'ActAcc':>8} {'F1':>8} {'Coverage':>10} {'Latency(ms)':>12}")
    print("-" * 56)

    rows = []
    for k in K_VALUES:
        r = evaluate_k(collection, test_samples, k, THRESHOLD)
        rows.append(r)
        print(f"{r['k']:>4} {r['accuracy']:>10.3f} {r['action_accuracy']:>8.3f} {r['f1']:>8.3f} {r['coverage']:>10.3f} {r['avg_latency_ms']:>12.1f}")

    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = RESULTS_DIR / f"topk_results_{ts}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nCSV 저장: {csv_path}")

    best = max(rows, key=lambda x: x["accuracy"])
    print(f"최적 K: {best['k']} (accuracy={best['accuracy']:.3f})")


if __name__ == "__main__":
    main()
