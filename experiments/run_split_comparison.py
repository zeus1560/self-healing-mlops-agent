"""
run_split_comparison.py
Train/Test split 비율 비교 실험: 7:3 vs 8:2

ChromaDB 쿼리는 전체 테스트셋(407건)에 대해 한 번만 수행하고,
임계값(threshold)은 캐시된 거리(distance) 값에 대해 계산으로 적용한다.
→ 407 × 1회 쿼리 후 threshold sweep은 메모리 연산으로 처리.
"""
import json
import math
import random
import time
import csv
from collections import defaultdict
from pathlib import Path

import chromadb
from chromadb.config import Settings

CHROMA_PATH  = Path("data/chroma_db")
BACKUP_PATH  = Path("data/etl_backup.json")
TEST_73_PATH = Path("data/test_set.json")
COLLECTION   = "error_playbook_vectors"
SEED         = 42

THRESHOLDS = [0.10, 0.20, 0.30, 0.40, 0.50, 0.60,
              0.70, 0.80, 0.90, 1.00, 1.20, 1.50]


def stratified_split(data: list[dict], test_ratio: float, seed: int = 42):
    random.seed(seed)
    by_cat: dict[str, list] = defaultdict(list)
    for item in data:
        by_cat[item["error_category"]].append(item)
    train, test = [], []
    for cat, items in sorted(by_cat.items()):
        random.shuffle(items)
        n_test = int(len(items) * test_ratio)
        if n_test >= len(items):
            n_test = len(items) - 1
        test.extend(items[:n_test])
        train.extend(items[n_test:])
    return train, test


def query_all(collection, test_samples: list[dict]) -> list[dict]:
    """전체 테스트셋에 대해 ChromaDB 쿼리를 한 번씩 실행하고 결과를 캐시."""
    results = []
    total = len(test_samples)
    for i, s in enumerate(test_samples):
        if i % 50 == 0:
            print(f"  쿼리 중... {i}/{total}", end="\r")
        res = collection.query(query_texts=[s["log_text"]], n_results=1)
        dist = res["distances"][0][0] if res["distances"][0] else 999.0
        pred_cat = res["metadatas"][0][0].get("error_category", "") if res["metadatas"][0] else ""
        pred_act = res["metadatas"][0][0].get("action_type", "") if res["metadatas"][0] else ""
        results.append({
            "log_text":     s["log_text"],
            "true_cat":     s.get("error_category", ""),
            "true_act":     s.get("action_type", ""),
            "dist":         dist,
            "pred_cat":     pred_cat,
            "pred_act":     pred_act,
        })
    print(f"  쿼리 완료: {total}건{' '*20}")
    return results


def sweep(cached: list[dict], label: str) -> list[dict]:
    """캐시된 쿼리 결과에 threshold를 적용하여 지표를 계산."""
    print(f"\n[{label}] threshold sweep (n={len(cached)})")
    print(f"{'Threshold':>10} {'F1(cat)':>8} {'ActAcc':>8} "
          f"{'Precision':>10} {'L1Rate':>8} {'Recall':>8}")
    print("-" * 60)
    rows = []
    for thresh in THRESHOLDS:
        tp = fp = fn = tn = 0
        a_correct = a_total = 0
        for r in cached:
            if r["dist"] <= thresh:
                a_total += 1
                if r["pred_cat"] == r["true_cat"]:
                    tp += 1
                else:
                    fp += 1
                fn += 1
                if r["pred_act"] == r["true_act"]:
                    a_correct += 1
            else:
                fn += 1
                tn += 1
        total     = len(cached)
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall    = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1_cat    = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
        act_acc   = a_correct / a_total if a_total > 0 else 0.0
        l1_rate   = (tp + fp) / total
        row = {
            "threshold":  thresh,
            "n_test":     total,
            "f1_cat":     round(f1_cat, 4),
            "act_acc":    round(act_acc, 4),
            "precision":  round(precision, 4),
            "recall":     round(recall, 4),
            "l1_rate":    round(l1_rate, 4),
        }
        rows.append(row)
        print(f"{thresh:>10.2f} {f1_cat:>8.4f} {act_acc:>8.4f} "
              f"{precision:>10.4f} {l1_rate:>8.4f} {recall:>8.4f}")
    return rows


def summarize_at(rows: list[dict], threshold: float) -> dict:
    for r in rows:
        if abs(r["threshold"] - threshold) < 1e-9:
            return r
    return {}


def main():
    client = chromadb.PersistentClient(
        path=str(CHROMA_PATH),
        settings=Settings(anonymized_telemetry=False),
    )
    collection = client.get_collection(COLLECTION)
    db_count   = collection.count()

    # ── 테스트셋 구성 ──────────────────────────────────────────
    test_73 = json.loads(TEST_73_PATH.read_text(encoding="utf-8"))["data"]

    raw_backup  = json.loads(BACKUP_PATH.read_text(encoding="utf-8"))["data"]
    backup_keys = {item["log_text"][:80] for item in raw_backup}
    non_etl     = [s for s in test_73 if s["log_text"][:80] not in backup_keys]

    _, etl_test_82 = stratified_split(raw_backup, test_ratio=0.2, seed=SEED)
    etl_test_82_keys = {s["log_text"][:80] for s in etl_test_82}

    # 8:2 테스트셋은 7:3의 부분집합이므로 별도 쿼리 불필요
    # (동일 seed로 8:2 etl test ⊆ 7:3 etl test)
    print("=" * 65)
    print("  Train/Test Split 비율 비교 실험: 7:3 vs 8:2")
    print("=" * 65)
    print(f"ChromaDB 지식베이스: {db_count}건 (양쪽 동일 고정)")
    print(f"7:3 테스트셋: {len(test_73)}건  "
          f"(etl {len(test_73)-len(non_etl)}건 + 비-etl {len(non_etl)}건)")
    print(f"8:2 테스트셋: {len(etl_test_82)+len(non_etl)}건  "
          f"(etl {len(etl_test_82)}건 + 비-etl {len(non_etl)}건)")

    # ── ChromaDB 쿼리 (전체 7:3 기준, 1회만) ──────────────────
    print(f"\n[ChromaDB 쿼리] 총 {len(test_73)}건 — 약 {len(test_73)*1.5/60:.0f}분 소요 예상")
    t_start = time.perf_counter()
    cached_73 = query_all(collection, test_73)
    elapsed = time.perf_counter() - t_start
    print(f"  총 소요: {elapsed:.1f}초 ({elapsed/len(test_73)*1000:.0f}ms/건)")

    # 8:2용 캐시: etl_test_82 키 + 비-etl 항목 필터링
    cached_82 = [r for r in cached_73
                 if r["log_text"][:80] in etl_test_82_keys
                 or r["log_text"][:80] not in backup_keys]
    print(f"  8:2 서브셋: {len(cached_82)}건 (재쿼리 없이 필터링)")

    # ── Threshold Sweep ────────────────────────────────────────
    rows_73 = sweep(cached_73, "7:3 split")
    rows_82 = sweep(cached_82, "8:2 split")

    # ── τ=0.6 요약 비교 ────────────────────────────────────────
    r73 = summarize_at(rows_73, 0.6)
    r82 = summarize_at(rows_82, 0.6)

    print("\n" + "=" * 65)
    print("  τ=0.6 기준 요약 비교")
    print("=" * 65)
    n73, n82 = r73["n_test"], r82["n_test"]
    print(f"{'항목':<22} {'7:3 (n='+str(n73)+')':>18} {'8:2 (n='+str(n82)+')':>18}  {'Δ':>8}")
    print("-" * 65)
    for key, label in [
        ("f1_cat",    "F1 (category)"),
        ("act_acc",   "Action Accuracy"),
        ("precision", "Precision"),
        ("recall",    "Recall"),
        ("l1_rate",   "L1 Hit Rate"),
    ]:
        v73 = r73.get(key, 0.0)
        v82 = r82.get(key, 0.0)
        diff = v82 - v73
        print(f"{label:<22} {v73:>18.4f} {v82:>18.4f}  {diff:>+8.4f}")
    print("=" * 65)

    # ── 95% 신뢰구간 ───────────────────────────────────────────
    print("\n[통계적 신뢰도]")
    for split_label, r in [("7:3", r73), ("8:2", r82)]:
        p = r["f1_cat"]
        n = r["n_test"]
        ci = 1.96 * math.sqrt(p * (1 - p) / n) if n > 0 else 0
        print(f"  F1(cat) 95% CI ({split_label}): {p:.4f} ± {ci:.4f}  [n={n}]")
    print(f"  → 7:3이 테스트셋 {n73-n82}건 많아 신뢰구간이 "
          f"{'더 좁음 (통계적으로 안정적)' if n73 > n82 else '더 넓음'}")

    # ── 전체 sweep 결과 CSV 저장 ───────────────────────────────
    out_path = Path("experiments/results/split_comparison.csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["split", "threshold", "n_test",
                         "f1_cat", "act_acc", "precision", "recall", "l1_rate"])
        for r in rows_73:
            writer.writerow(["7:3"] + [r[k] for k in
                ["threshold","n_test","f1_cat","act_acc","precision","recall","l1_rate"]])
        for r in rows_82:
            writer.writerow(["8:2"] + [r[k] for k in
                ["threshold","n_test","f1_cat","act_acc","precision","recall","l1_rate"]])
    print(f"\nCSV 저장: {out_path}")


if __name__ == "__main__":
    main()
