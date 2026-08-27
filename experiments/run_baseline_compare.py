"""
베이스라인(키워드 매칭) vs RAG 시스템 비교
결과: experiments/results/baseline_results.csv
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

TEST_SET_PATH = Path("data/test_set.json")
CHROMA_PATH   = Path("data/chroma_db")
RESULTS_DIR   = Path("experiments/results")
RAG_THRESHOLD = 0.60  # threshold sweep 최적값 (run_threshold_sweep.py Auto-apply 결과)


# ── 베이스라인: 키워드 매칭 ────────────────────────────────────────────
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
    result   = collection.query(query_texts=[log_text], n_results=1)
    distance = result["distances"][0][0]
    meta     = result["metadatas"][0][0]
    category = meta.get("error_category", "Unknown")
    action   = meta.get("action_type", "")
    if distance >= threshold:
        return "Unknown", distance, ""
    return category, distance, action


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

        # predict_fn may return (category, dist, action) tuple or plain string
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
    test_samples = json.loads(TEST_SET_PATH.read_text(encoding="utf-8"))["data"]

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
            f"RAG (threshold={RAG_THRESHOLD})",
            lambda txt: rag_classify(collection, txt, RAG_THRESHOLD),
            test_samples,
        ),
    ]

    print(f"\n{'System':<30} {'Cat Acc':>8} {'Prec':>8} {'Recall':>8} {'F1':>8} {'Act Acc':>8} {'Coverage':>9} {'Correct':>8} {'Unknown':>8} {'Latency(ms)':>12}")
    print("-" * 106)
    for r in results:
        print(f"{r['system']:<30} {r['accuracy']:>8.3f} {r['precision']:>8.3f} {r['recall']:>8.3f} "
              f"{r['f1']:>8.3f} {r['action_accuracy']:>8.3f} {r['coverage']:>9.3f} "
              f"{r['correct']:>8} {r['unknown']:>8} {r['avg_latency_ms']:>12.1f}")

    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = RESULTS_DIR / f"baseline_results_{ts}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=results[0].keys())
        writer.writeheader()
        writer.writerows(results)
    print(f"\nCSV 저장: {csv_path}")

    rag_acc = next(r["accuracy"] for r in results if "RAG" in r["system"])
    kw_acc  = next(r["accuracy"] for r in results if "Keyword" in r["system"])
    print(f"\nRAG 향상: {(rag_acc - kw_acc)*100:+.1f}%p vs 키워드 베이스라인")


if __name__ == "__main__":
    main()
