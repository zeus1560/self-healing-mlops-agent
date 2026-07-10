"""
베이스라인(키워드 매칭) vs RAG 최종 비교
Final Test Set (207개)에서만 평가
τ=0.50, K=10 (사전 튜닝값)
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
from sklearn.metrics import precision_recall_fscore_support

FINAL_TEST_SET_PATH = Path("data/final_test_set.json")
CHROMA_PATH   = Path("data/chroma_db")
RESULTS_DIR   = Path("experiments/results")
RAG_THRESHOLD = 0.50  # run_threshold_sweep_with_validation.py 결과
TOP_K         = 10    # run_top_k_sweep_with_validation.py 결과


KEYWORD_RULES = [
    (["out of memory", "oom killer", "cuda out of memory", "vram", "outofmemoryerror"],
     "Out_Of_Memory"),
    (["timeout", "timed out", "connection timeout", "read timeout"],
     "Network_Timeout"),
    (["improperlyconfigured", "configuration error", "config", "missing key", "environment variable"],
     "Configuration_Error"),
    (["connection refused", "econnrefused", "could not connect"],
     "DB_Connection"),
    (["permission denied", "permissionerror", "access denied"],
     "Permission_Denied"),
    (["no space left", "disk full", "diskquotaexceeded"],
     "Disk_Full"),
    (["crashloopbackoff", "oomkilled", "segmentation fault", "core dumped"],
     "Process_Crash"),
    (["address already in use", "bind() failed", "port already"],
     "Port_Conflict"),
    (["authentication failed", "unauthorized", "invalid token", "403"],
     "Auth_Error"),
    (["memory leak", "heap dump", "gc overhead"],
     "Memory_Leak"),
]


def keyword_classify(log_text: str) -> str:
    lower = log_text.lower()
    for keywords, category in KEYWORD_RULES:
        if any(kw in lower for kw in keywords):
            return category
    return "Unknown"


def rag_classify(collection, log_text: str, threshold: float) -> tuple[str, float, str]:
    result   = collection.query(query_texts=[log_text], n_results=TOP_K)
    
    # Top-K 결과 중 threshold 이내인 것들에서 다수결
    candidates = []
    for meta, dist in zip(result["metadatas"][0], result["distances"][0]):
        if dist < threshold:
            candidates.append(meta.get("error_category", "Unknown"))
    
    if not candidates:
        return "Unknown", result["distances"][0][0], ""
    
    category = max(set(candidates), key=candidates.count)
    
    # action_type: threshold 이내이고 action_type이 있는 것들 중 다수결
    action_candidates = []
    for meta, dist in zip(result["metadatas"][0], result["distances"][0]):
        if dist < threshold and meta.get("action_type"):
            action_candidates.append(meta.get("action_type"))
    
    action = max(set(action_candidates), key=action_candidates.count) if action_candidates else ""
    
    return category, result["distances"][0][0], action


def evaluate_system(name: str, predict_fn, test_samples: list[dict]) -> dict:
    true_categories = []
    pred_categories = []
    correct = action_correct = 0
    unknown = 0
    latencies = []

    for s in test_samples:
        t0  = time.perf_counter()
        result = predict_fn(s["log_text"])
        latencies.append((time.perf_counter() - t0) * 1000)

        if isinstance(result, tuple):
            pred, _, pred_action = result
        else:
            pred, pred_action = result, ""

        true_categories.append(s["error_category"])
        pred_categories.append(pred)

        if pred == s["error_category"]:
            correct += 1
        if pred == "Unknown":
            unknown += 1

        true_action = s.get("action_type", "")
        if pred_action and true_action:
            norm = lambda x: x.lower().replace("-", "_")
            if norm(pred_action) == norm(true_action):
                action_correct += 1

    total    = len(test_samples)
    accuracy = correct / total
    coverage = (total - unknown) / total
    precision, recall, f1, _ = precision_recall_fscore_support(
        true_categories,
        pred_categories,
        average="macro",
        zero_division=0,
    )

    return {
        "system":          name,
        "accuracy":        round(accuracy, 4),
        "precision":       round(precision, 4),
        "recall":          round(recall, 4),
        "f1":              round(f1, 4),
        "action_accuracy": round(action_correct / total, 4),
        "coverage":        round(coverage, 4),
        "correct":         correct,
        "action_correct":  action_correct,
        "unknown":         unknown,
        "total":           total,
        "avg_latency_ms":  round(sum(latencies) / len(latencies), 3),
    }


def main():
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 85)
    print("최종 베이스라인 비교: Final Test Set (207개) 에서만 평가")
    print("=" * 85)

    test_samples = json.loads(FINAL_TEST_SET_PATH.read_text(encoding="utf-8"))["data"]
    print(f"Final test set: {len(test_samples)}개\n")

    client = chromadb.PersistentClient(
        path=str(CHROMA_PATH),
        settings=Settings(anonymized_telemetry=False),
    )
    collection = client.get_collection("error_playbook_vectors")

    results = [
        evaluate_system(
            "Keyword Baseline",
            keyword_classify,
            test_samples,
        ),
        evaluate_system(
            f"RAG (τ={RAG_THRESHOLD}, K={TOP_K})",
            lambda txt: rag_classify(collection, txt, RAG_THRESHOLD),
            test_samples,
        ),
    ]

    print(f"{'System':<30} {'Accuracy':>8} {'Precision':>10} {'Recall':>8} {'F1':>8} {'ActAcc':>8} {'Coverage':>9} {'Latency(ms)':>12}")
    print("-" * 100)
    for r in results:
        print(f"{r['system']:<30} {r['accuracy']:>8.3f} {r['precision']:>10.3f} {r['recall']:>8.3f} "
              f"{r['f1']:>8.3f} {r['action_accuracy']:>8.3f} {r['coverage']:>9.3f} "
              f"{r['avg_latency_ms']:>12.1f}")

    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = RESULTS_DIR / f"baseline_compare_final_test_{ts}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=results[0].keys())
        writer.writeheader()
        writer.writerows(results)
    print(f"\n[Final Test] CSV 저장: {csv_path}")

    rag_acc = next(r["accuracy"] for r in results if "RAG" in r["system"])
    kw_acc  = next(r["accuracy"] for r in results if "Keyword" in r["system"])
    improvement = (rag_acc - kw_acc) * 100

    print("\n" + "=" * 85)
    print("✓ 최종 결과 요약 (Final Test Set 기준)")
    print("=" * 85)
    print(f"Keyword Baseline:  Accuracy={results[0]['accuracy']:.4f}, Precision={results[0]['precision']:.4f}, Recall={results[0]['recall']:.4f}")
    print(f"RAG (τ=0.50, K=10): Accuracy={results[1]['accuracy']:.4f}, Precision={results[1]['precision']:.4f}, Recall={results[1]['recall']:.4f}")
    print(f"\nRAG 향상도: {improvement:+.1f}%p vs 키워드 베이스라인")
    print(f"\n✓ 최종 성능 지표는 Final Test Set (n=207) 단독 평가 결과이며, test set leakage가 없습니다.")
    print(f"✓ 논문에는 이 지표들만 보고하세요.")


if __name__ == "__main__":
    main()
