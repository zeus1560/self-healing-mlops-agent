"""
데이터셋 규모별 성능 실험 (Learning Curve)
50 → 100 → 150 → 200 → 350(전체) 단계별 L1 정확도 측정
같은 test_set.json으로 고정 평가
결과: experiments/results/dataset_scale_results.csv
"""
import csv
import json
import random
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import chromadb
from chromadb.config import Settings

TRAIN_PATH    = Path("data/train_set.json")
TEST_SET_PATH = Path("data/test_set.json")
RESULTS_DIR   = Path("experiments/results")
THRESHOLD     = 0.80
SEED          = 42
# 전체 350개 이하 단계만 포함
SCALE_STEPS   = [50, 100, 150, 200, 350]


def build_temp_collection(client, train_subset: list[dict], name: str):
    import hashlib
    try:
        client.delete_collection(name)
    except Exception as e:
        print(f"  [skip] 기존 컬렉션 없음, 신규 생성: {e}")
    col = client.create_collection(name)
    ids       = [hashlib.md5(r["log_text"].encode()).hexdigest() for r in train_subset]
    documents = [r["log_text"] for r in train_subset]
    metadatas = [{"error_category": str(r.get("error_category") or "Unknown"),
                  "action_type":    str(r.get("action_type") or "escalate_to_human"),
                  "target_process": str(r.get("target_process") or "unknown"),
                  "reasoning":      str(r.get("reasoning") or "")} for r in train_subset]
    col.upsert(ids=ids, documents=documents, metadatas=metadatas)
    return col


def evaluate_col(col, test_samples: list[dict], threshold: float) -> dict:
    correct = unknown = 0
    latencies = []
    for s in test_samples:
        t0     = time.perf_counter()
        result = col.query(query_texts=[s["log_text"]], n_results=1)
        latencies.append((time.perf_counter() - t0) * 1000)

        dist = result["distances"][0][0]
        pred = result["metadatas"][0][0].get("error_category", "Unknown")
        if dist >= threshold:
            pred = "Unknown"
            unknown += 1
        if pred == s["error_category"]:
            correct += 1

    total = len(test_samples)
    return {
        "accuracy":       round(correct / total, 4),
        "coverage":       round((total - unknown) / total, 4),
        "avg_latency_ms": round(sum(latencies) / len(latencies), 3),
    }


def main():
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    random.seed(SEED)

    train_all    = json.loads(TRAIN_PATH.read_text(encoding="utf-8"))["data"]
    test_samples = json.loads(TEST_SET_PATH.read_text(encoding="utf-8"))["data"]

    client = chromadb.PersistentClient(
        path=str(Path("data/chroma_db_scale_tmp")),
        settings=Settings(anonymized_telemetry=False),
    )

    print(f"테스트 샘플 고정: {len(test_samples)}개 | threshold={THRESHOLD}")
    print(f"{'Scale':>8} {'Accuracy':>10} {'Coverage':>10} {'Latency(ms)':>12}")
    print("-" * 44)

    rows = []
    for scale in SCALE_STEPS:
        if scale > len(train_all):
            print(f"  scale={scale} 스킵 (train 전체={len(train_all)}개)")
            continue

        subset = random.sample(train_all, scale)
        col    = build_temp_collection(client, subset, "scale_tmp")
        m      = evaluate_col(col, test_samples, THRESHOLD)

        row = {"scale": scale, **m}
        rows.append(row)
        print(f"{scale:>8} {m['accuracy']:>10.3f} {m['coverage']:>10.3f} {m['avg_latency_ms']:>12.1f}")

    # 임시 컬렉션 정리
    try:
        client.delete_collection("scale_tmp")
    except Exception as e:
        print(f"  [skip] 임시 컬렉션 정리 실패: {e}")

    if not rows:
        print("평가 결과 없음 — train_set.json 또는 ChromaDB를 확인하세요.")
        return

    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = RESULTS_DIR / f"dataset_scale_results_{ts}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nCSV 저장: {csv_path}")

    # Learning Curve 이미지
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        scales    = [r["scale"] for r in rows]
        accuracies = [r["accuracy"] for r in rows]

        plt.figure(figsize=(8, 5))
        plt.plot(scales, accuracies, "go-", linewidth=2, markersize=8)
        plt.xlabel("Training Set Size"); plt.ylabel("Accuracy")
        plt.title("Learning Curve (RAG L1 Accuracy vs Dataset Size)")
        plt.grid(alpha=0.3); plt.ylim(0, 1)
        for x, y in zip(scales, accuracies):
            plt.annotate(f"{y:.3f}", (x, y), textcoords="offset points",
                         xytext=(0, 8), ha="center", fontsize=9)
        img_path = RESULTS_DIR / f"learning_curve_{ts}.png"
        plt.tight_layout()
        plt.savefig(img_path, dpi=150)
        print(f"Learning Curve 이미지: {img_path}")
    except ImportError:
        print("matplotlib 없음 — 이미지 생략")

    shutil.rmtree("data/chroma_db_scale_tmp", ignore_errors=True)


if __name__ == "__main__":
    main()
