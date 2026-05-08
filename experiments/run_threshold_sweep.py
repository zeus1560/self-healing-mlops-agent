"""
유사도 임계값(threshold) sweep 실험
ChromaDB distance 기준: distance < threshold → L1 Hit, else → L2 fallback
결과: experiments/results/threshold_results.csv + threshold_roc.png
"""
import csv
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import chromadb
from chromadb.config import Settings

TEST_SET_PATH = Path("data/test_set.json")
RESULTS_DIR   = Path("experiments/results")
# ChromaDB는 코사인/L2 '거리(Distance)'를 반환 — 0에 가까울수록 동일한 에러.
# 실제 임베딩 거리 분포는 0.1~0.7 구간에 집중되므로 그 범위를 촘촘히 탐색한다.
THRESHOLDS    = [0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80]


def load_test_set():
    data = json.loads(TEST_SET_PATH.read_text(encoding="utf-8"))
    return data["data"]


def get_collection():
    client = chromadb.PersistentClient(
        path=str(Path("data/chroma_db")),
        settings=Settings(anonymized_telemetry=False),
    )
    return client.get_collection("error_playbook_vectors")


def evaluate(collection, test_samples: list[dict], threshold: float):
    latencies = []
    tp = fp = fn = tn = 0

    for sample in test_samples:
        query_text  = sample["log_text"]
        true_category = sample["error_category"]

        t0 = time.perf_counter()
        result = collection.query(query_texts=[query_text], n_results=1)
        latency = (time.perf_counter() - t0) * 1000  # ms
        latencies.append(latency)

        distance = result["distances"][0][0]
        pred_category = result["metadatas"][0][0].get("error_category", "Unknown")

        l1_triggered = distance < threshold   # 거리 < threshold → L1 Hit (낮을수록 더 유사)
        correct = pred_category == true_category

        if l1_triggered and correct:
            tp += 1
        elif l1_triggered and not correct:
            fp += 1
        elif not l1_triggered and correct:
            fn += 1
        else:
            tn += 1

    total = len(test_samples)
    tpr       = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    fpr       = fp / (fp + tn) if (fp + tn) > 0 else 0.0
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall    = tpr
    f1        = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0
    l1_rate   = (tp + fp) / total  # L1이 처리한 비율

    return {
        "threshold":      threshold,
        "tpr":            round(tpr, 4),
        "fpr":            round(fpr, 4),
        "precision":      round(precision, 4),
        "recall":         round(recall, 4),
        "f1":             round(f1, 4),
        "l1_hit_rate":    round(l1_rate, 4),
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "avg_latency_ms": round(sum(latencies) / len(latencies), 3),
    }


def main():
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    test_samples = load_test_set()
    collection   = get_collection()

    print(f"테스트 샘플: {len(test_samples)}개 | threshold {len(THRESHOLDS)}개 sweep")
    print(f"{'Threshold':>10} {'TPR':>6} {'FPR':>6} {'Precision':>10} {'F1':>6} {'L1Rate':>8} {'Latency(ms)':>12}")
    print("-" * 65)

    rows = []
    for thresh in THRESHOLDS:
        r = evaluate(collection, test_samples, thresh)
        rows.append(r)
        print(f"{r['threshold']:>10.2f} {r['tpr']:>6.3f} {r['fpr']:>6.3f} "
              f"{r['precision']:>10.3f} {r['f1']:>6.3f} {r['l1_hit_rate']:>8.3f} "
              f"{r['avg_latency_ms']:>12.1f}")

    if not rows:
        print("평가 결과 없음 — 테스트셋 또는 ChromaDB를 확인하세요.")
        return

    best = max(rows, key=lambda x: x["f1"])

    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = RESULTS_DIR / f"threshold_results_{ts}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nCSV 저장: {csv_path}")

    # ROC curve 이미지
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fprs = [r["fpr"] for r in rows]
        tprs = [r["tpr"] for r in rows]
        f1s  = [r["f1"]  for r in rows]

        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))

        ax1.plot(fprs, tprs, "bo-", linewidth=2)
        ax1.plot([0, 1], [0, 1], "k--", alpha=0.4)
        for r in rows:
            ax1.annotate(f"{r['threshold']}", (r["fpr"], r["tpr"]),
                         textcoords="offset points", xytext=(5, 5), fontsize=8)
        ax1.set_xlabel("FPR"); ax1.set_ylabel("TPR")
        ax1.set_title("ROC Curve (Threshold Sweep)")
        ax1.set_xlim(0, 1); ax1.set_ylim(0, 1); ax1.grid(alpha=0.3)

        ax2.plot(THRESHOLDS, f1s, "rs-", linewidth=2)
        ax2.axvline(best["threshold"], color="gray", linestyle="--", alpha=0.6,
                    label=f"Best F1={best['f1']:.3f} @ {best['threshold']}")
        ax2.set_xlabel("Threshold"); ax2.set_ylabel("F1 Score")
        ax2.set_title("F1 Score vs Threshold")
        ax2.legend(); ax2.grid(alpha=0.3)

        plt.tight_layout()
        img_path = RESULTS_DIR / f"threshold_roc_{ts}.png"
        plt.savefig(img_path, dpi=150)
        print(f"ROC 이미지: {img_path}")
    except ImportError:
        print("matplotlib 없음 — 이미지 생략")

    print(f"\n최적 threshold: {best['threshold']} (F1={best['f1']:.3f})")


if __name__ == "__main__":
    main()
