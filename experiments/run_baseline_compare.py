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

TEST_SET_PATH = Path("data/test_set.json")
RESULTS_DIR   = Path("experiments/results")
RAG_THRESHOLD = 1.20  # 현재 운영 threshold (threshold sweep 최적값)


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


def rag_classify(collection, log_text: str, threshold: float) -> tuple[str, float]:
    result   = collection.query(query_texts=[log_text], n_results=1)
    distance = result["distances"][0][0]
    category = result["metadatas"][0][0].get("error_category", "Unknown")
    if distance >= threshold:
        return "Unknown", distance
    return category, distance


def evaluate_system(name: str, predict_fn, test_samples: list[dict]) -> dict:
    correct = 0
    unknown = 0
    latencies = []

    for s in test_samples:
        t0  = time.perf_counter()
        pred = predict_fn(s["log_text"])
        latencies.append((time.perf_counter() - t0) * 1000)

        if pred == s["error_category"]:
            correct += 1
        if pred == "Unknown":
            unknown += 1

    total    = len(test_samples)
    accuracy = correct / total
    coverage = (total - unknown) / total  # Unknown이 아닌 비율

    return {
        "system":         name,
        "accuracy":       round(accuracy, 4),
        "coverage":       round(coverage, 4),
        "correct":        correct,
        "unknown":        unknown,
        "total":          total,
        "avg_latency_ms": round(sum(latencies) / len(latencies), 3),
    }


def main():
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    test_samples = json.loads(TEST_SET_PATH.read_text(encoding="utf-8"))["data"]

    client = chromadb.PersistentClient(
        path=str(Path("data/chroma_db")),
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
            lambda txt: rag_classify(collection, txt, RAG_THRESHOLD)[0],
            test_samples,
        ),
    ]

    print(f"\n{'System':<30} {'Accuracy':>9} {'Coverage':>9} {'Correct':>8} {'Unknown':>8} {'Latency(ms)':>12}")
    print("-" * 80)
    for r in results:
        print(f"{r['system']:<30} {r['accuracy']:>9.3f} {r['coverage']:>9.3f} "
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
