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
# ChromaDB는 L2 '거리(Distance)'를 반환 — 0에 가까울수록 동일한 에러.
# 0.8에서 F1이 아직 오르는 추세이므로 1.5까지 확장해서 진짜 최적점을 탐색한다.
THRESHOLDS    = [0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 1.00, 1.10, 1.20, 1.50]


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
    # error_category 기준 분류
    tp = fp = fn = tn = 0
    # action_type 기준 분류 (실제 조치 정확도 — 더 중요한 지표)
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
        # action_type은 대소문자/포맷 차이를 흡수해서 비교
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
        "action_f1":      round(action_f1, 4),   # 실제 조치 정확도 F1
        "action_precision": round(a_prec, 4),
        "l1_hit_rate":    round(l1_rate, 4),
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "avg_latency_ms": round(sum(latencies) / len(latencies), 3),
    }


def main():
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    test_samples = load_test_set()
    collection   = get_collection()

    print(f"테스트 샘플: {len(test_samples)}개 | threshold {len(THRESHOLDS)}개 sweep")
    print(f"{'Threshold':>10} {'F1(cat)':>8} {'F1(action)':>11} {'Precision':>10} {'L1Rate':>8} {'Latency(ms)':>12}")
    print("-" * 70)

    rows = []
    for thresh in THRESHOLDS:
        r = evaluate(collection, test_samples, thresh)
        rows.append(r)
        print(f"{r['threshold']:>10.2f} {r['f1']:>8.3f} {r['action_f1']:>11.3f} "
              f"{r['precision']:>10.3f} {r['l1_hit_rate']:>8.3f} "
              f"{r['avg_latency_ms']:>12.1f}")

    if not rows:
        print("평가 결과 없음 — 테스트셋 또는 ChromaDB를 확인하세요.")
        return

    # action_f1 기준 최적값 선정.
    # F1이 최고값 대비 0.01 이내로 동률인 경우 precision이 가장 높은 쪽을 선택.
    # precision도 같으면 더 낮은(보수적인) threshold를 선택한다.
    best_action_f1 = max(r["action_f1"] for r in rows)
    candidates = [r for r in rows if best_action_f1 - r["action_f1"] <= 0.01]
    best_by_action = min(candidates, key=lambda x: (-x["action_precision"], x["threshold"]))

    best_cat_f1    = max(r["f1"] for r in rows)
    cat_candidates = [r for r in rows if best_cat_f1 - r["f1"] <= 0.01]
    best_by_cat    = min(cat_candidates, key=lambda x: (-x["precision"], x["threshold"]))

    best = best_by_action

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

        action_f1s = [r["action_f1"] for r in rows]
        ax2.plot(THRESHOLDS, f1s, "rs-", linewidth=2, label="F1 (category)")
        ax2.plot(THRESHOLDS, action_f1s, "b^-", linewidth=2, label="F1 (action_type)")
        ax2.axvline(best_by_action["threshold"], color="blue", linestyle="--", alpha=0.7,
                    label=f"Best action F1={best_by_action['action_f1']:.3f} @ {best_by_action['threshold']}")
        ax2.axvline(best_by_cat["threshold"], color="red", linestyle=":", alpha=0.7,
                    label=f"Best cat F1={best_by_cat['f1']:.3f} @ {best_by_cat['threshold']}")
        ax2.set_xlabel("Threshold"); ax2.set_ylabel("F1 Score")
        ax2.set_title("F1 Score vs Threshold (Category vs Action)")
        ax2.legend(); ax2.grid(alpha=0.3)

        plt.tight_layout()
        img_path = RESULTS_DIR / f"threshold_roc_{ts}.png"
        plt.savefig(img_path, dpi=150)
        print(f"ROC 이미지: {img_path}")
    except ImportError:
        print("matplotlib 없음 — 이미지 생략")

    print(f"\n최적 threshold (action_f1 기준): {best_by_action['threshold']} "
          f"(action_f1={best_by_action['action_f1']:.3f}, cat_f1={best_by_action['f1']:.3f})")
    print(f"최적 threshold (category_f1 기준): {best_by_cat['threshold']} "
          f"(cat_f1={best_by_cat['f1']:.3f})")

    # llm_engine.py의 distance 임계값 자동 업데이트
    _apply_best_threshold(best_by_action["threshold"])


def _apply_best_threshold(threshold: float) -> None:
    """최적 threshold를 llm_engine.py에 자동 반영한다."""
    import re as _re
    engine_path = Path(__file__).parent.parent / "src" / "llm_engine.py"
    if not engine_path.exists():
        print(f"[Auto-apply] llm_engine.py 없음: {engine_path}")
        return
    text = engine_path.read_text(encoding="utf-8")
    # "if distance > 숫자:" 패턴만 교체 — 줄 시작 공백 + if 로 범위를 좁혀 리스트 숫자 오탐 방지
    new_text, n = _re.subn(
        r"([ \t]+if distance\s*[><=]+\s*)\d+\.\d+",
        lambda m: f"{m.group(1)}{threshold}",
        text,
    )
    if n == 0:
        print("[Auto-apply] llm_engine.py에서 distance 임계값 패턴을 찾지 못했습니다.")
        return
    engine_path.write_text(new_text, encoding="utf-8")
    print(f"[Auto-apply] llm_engine.py distance 임계값 → {threshold} (변경 {n}곳)")


if __name__ == "__main__":
    main()
