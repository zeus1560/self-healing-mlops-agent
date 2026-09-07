"""
run_l1_production_shape_check.py
L1(RAG) 캐시가 실제 운영 입력 형태에서 학습 데이터와 같은 형태보다 얼마나 더
멀어지는지 직접 측정한다.

배경: 2026-09-07 FP/FN 재분석(run_fp_fn_analysis.py, n=21)에서 이미 auto로
승격된 6개 카테고리 전부 recall 0%가 나옴. 원인으로 의심되는 것 — log_watcher.py의
_build_context_window()가 실제 운영에서는 에러 줄 앞뒤로 최대 10줄씩 붙여
"[LOG CONTEXT]" 블록으로 감싸는데, ChromaDB 학습 데이터는 이 래핑 없이 순수
한 줄(또는 몇 줄)짜리 스니펫이라 임베딩 거리가 벌어질 수 있다는 가설.

run_fp_fn_analysis.py와 같은 철학 — 새 LLM 호출이나 가짜 데이터 없이, 이미
운영 중 쌓인 agent_metrics.db의 실제 error_log 원문 + 이미 있는 ChromaDB
컬렉션만 읽는다. 같은 사건에 대해 (1) 운영에서 실제로 잡힌 원문 그대로와
(2) 그 안에서 증거 문구가 있는 한 줄만 추출한 버전(학습 데이터와 동일한 형태)
각각으로 L1을 재질의해 거리 차이를 직접 비교한다.

실행 위치 주의: run_fp_fn_analysis.py와 동일 — 의미 있는 결과를 보려면
실제 카오스 이력 + 실제 ChromaDB가 있는 GCP VM에서 실행해야 한다.
"""
import json
import re
import sqlite3
from pathlib import Path

from src.llm_engine import _get_chroma_client, _RAG_THRESHOLD

METRICS_DB  = Path("data/agent_metrics.db")
RESULTS_DIR = Path("experiments/results")

# run_fp_fn_analysis.py와 동일한 매핑 — 두 스크립트가 어긋나지 않도록 유지할 것.
FAULT_TO_CATEGORY = {
    "oom":               "Out_Of_Memory",
    "diskfull":          "Disk_Full",
    "process_crash":     "Process_Crash",
    "permission_denied": "Permission_Denied",
    "path_not_found":    "Path_Not_Found",
    "config_error":      "Configuration_Error",
}

EVIDENCE_MARKERS = {
    "oom":               r"python memory allocator vs cgroup mem_limit=512m",
    "diskfull":          r"dd wrote into 150m tmpfs",
    "process_crash":     r"about to crash — real process crash injection",
    "permission_denied": r"no execute bit set",
    "path_not_found":    r"FileNotFoundError — \[Errno 2\] No such file or directory",
    "config_error":      r"Configuration Error — JSONDecodeError parsing",
}

_INJECTOR_SELF_FAILURE_MARKER = "investigate"


def _identify_fault(error_log: str) -> str | None:
    for fault, pattern in EVIDENCE_MARKERS.items():
        if re.search(pattern, error_log):
            return fault
    return None


def _extract_clean_line(error_log: str, fault: str) -> str:
    """운영 입력 전체에서 증거 문구가 있는 한 줄만 뽑는다(학습 데이터와 동일한 형태)."""
    pattern = EVIDENCE_MARKERS[fault]
    for line in error_log.splitlines():
        if re.search(pattern, line):
            return line.strip()
    return error_log


def _load_labeled_rows() -> list[dict]:
    if not METRICS_DB.exists():
        return []
    conn = sqlite3.connect(f"file:{METRICS_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT error_log FROM metrics WHERE error_log LIKE '%chaos-injector:%' "
            "ORDER BY timestamp"
        ).fetchall()
    finally:
        conn.close()

    out = []
    for r in rows:
        log = r["error_log"] or ""
        if _INJECTOR_SELF_FAILURE_MARKER in log:
            continue
        fault = _identify_fault(log)
        if fault is None:
            continue
        out.append({
            "error_log":     log,
            "fault":         fault,
            "true_category": FAULT_TO_CATEGORY[fault],
        })
    return out


def _query_one(collection, text: str) -> tuple[float | None, str | None]:
    result = collection.query(query_texts=[text], n_results=1)
    dists = result["distances"][0]
    metas = result["metadatas"][0]
    if not dists:
        return None, None
    return dists[0], metas[0].get("error_category")


def main() -> dict:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    client     = _get_chroma_client()
    collection = client.get_collection(name="error_playbook_vectors")

    rows = _load_labeled_rows()

    print("=" * 90)
    print("  L1(RAG) 운영 입력 형태 vs 학습 데이터 형태 — 거리/히트율 비교")
    print(f"  RAG_THRESHOLD = {_RAG_THRESHOLD}")
    print("=" * 90)

    if not rows:
        print("\n  [분석 대상 없음 — chaos-injector 이벤트가 agent_metrics.db에 없음]")
        return {"n": 0}

    results = []
    for row in rows:
        clean_line = _extract_clean_line(row["error_log"], row["fault"])

        wrapped_dist, wrapped_cat = _query_one(collection, row["error_log"])
        clean_dist,   clean_cat   = _query_one(collection, clean_line)

        wrapped_hit = wrapped_dist is not None and wrapped_dist <= _RAG_THRESHOLD
        clean_hit   = clean_dist is not None and clean_dist <= _RAG_THRESHOLD

        results.append({
            "true_category":            row["true_category"],
            "wrapped_distance":         wrapped_dist,
            "wrapped_hit":              wrapped_hit,
            "wrapped_matched_category": wrapped_cat,
            "clean_distance":           clean_dist,
            "clean_hit":                clean_hit,
            "clean_matched_category":   clean_cat,
        })

        print(f"\n  [{row['true_category']}]")
        print(f"    운영 입력(컨텍스트 포함) 거리 = {wrapped_dist:.4f}  "
              f"{'HIT ✓' if wrapped_hit else 'MISS ✗'}  (매칭: {wrapped_cat})")
        print(f"    학습 형태(한 줄만)       거리 = {clean_dist:.4f}  "
              f"{'HIT ✓' if clean_hit else 'MISS ✗'}  (매칭: {clean_cat})")

    n = len(results)
    wrapped_hit_n = sum(r["wrapped_hit"] for r in results)
    clean_hit_n   = sum(r["clean_hit"] for r in results)

    print("\n" + "=" * 90)
    print(f"  운영 입력 형태(컨텍스트 포함) L1 히트율   : {wrapped_hit_n/n*100:.1f}% ({wrapped_hit_n}/{n})")
    print(f"  학습 데이터 형태(한 줄만)     L1 히트율   : {clean_hit_n/n*100:.1f}% ({clean_hit_n}/{n})")
    print("=" * 90)

    summary = {"n": n, "wrapped_hit_rate": round(wrapped_hit_n / n, 4),
               "clean_hit_rate": round(clean_hit_n / n, 4), "records": results}
    out_path = RESULTS_DIR / "l1_production_shape_check.json"
    out_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n  JSON 저장: {out_path}")
    return summary


if __name__ == "__main__":
    main()
