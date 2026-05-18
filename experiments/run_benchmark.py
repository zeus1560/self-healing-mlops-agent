"""
Continuous Learning 벤치마크 — L1 vs L2 속도 비교

시나리오:
  Round 1 (Cold) : test_set에서 50건을 처음 처리 → 대부분 L2 LLM 추론
  Round 2 (Warm) : 동일 50건 재처리 → learn_from_feedback으로 쌓인 L1 캐시 적중

목표 수치:
  "Continuous Learning을 통해 재발 에러 해결 속도를 N% 단축"
"""

import json
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 벤치마크는 Slack 승인 대기 없이 자동 실행
os.environ.setdefault("AUTO_APPROVE", "true")

from src.llm_engine import RAGEngine
from src.executor import ActionExecutor
from src.observability import AgentObserver

# ── 설정 ──────────────────────────────────────────────────────────────────────
BENCHMARK_DB   = "./data/benchmark_metrics.db"   # 기존 운영 DB와 분리
N_SAMPLES      = 50
ROUND1_LABEL   = "Cold (첫 발생)"
ROUND2_LABEL   = "Warm (재발생 — L1 캐시)"
RESULTS_DIR    = "./experiments/results"
CSV_PATH       = os.path.join(RESULTS_DIR, "benchmark_l1_vs_l2.csv")


def load_benchmark_samples(n: int) -> list[dict]:
    """test_set.json에서 카테고리별 균등 샘플링."""
    with open("data/test_set.json") as f:
        data = json.load(f)
    items = data.get("data", data) if isinstance(data, dict) else data

    from collections import defaultdict
    import random
    random.seed(42)

    by_cat: dict[str, list] = defaultdict(list)
    for item in items:
        by_cat[item.get("error_category", "Unknown")].append(item)

    samples, per_cat = [], max(1, n // len(by_cat))
    for cat, lst in sorted(by_cat.items()):
        samples.extend(random.sample(lst, min(per_cat, len(lst))))

    # 부족하면 나머지에서 랜덤 추가
    remaining = [i for i in items if i not in samples]
    random.shuffle(remaining)
    samples.extend(remaining[: max(0, n - len(samples))])

    return samples[:n]


def run_round(
    samples: list[dict],
    engine: RAGEngine,
    executor: ActionExecutor,
    observer: AgentObserver,
    label: str,
) -> list[dict]:
    """단일 라운드 실행. 각 샘플의 소스·지연시간·성공 여부를 반환."""
    results = []
    total = len(samples)
    print(f"\n{'='*60}")
    print(f"  {label}  ({total}건)")
    print(f"{'='*60}")

    for i, item in enumerate(samples, 1):
        log_text = item["log_text"]
        short = log_text[:60].replace("\n", " ")
        print(f"  [{i:02d}/{total}] {short}...", end=" ", flush=True)

        t0 = time.perf_counter()
        try:
            decision = engine.analyze_error(log_text)
            exec_result = executor.execute(decision, original_error_log=log_text)
            latency = time.perf_counter() - t0
            source  = decision.resolution_source
            success = exec_result["success"]
            result_cat = exec_result.get("result_category", "UNKNOWN")

            # 성공한 L2/RULE은 L1 캐시에 학습
            if success and source in ("L2_LLM", "RULE") and decision.command:
                try:
                    engine.learn_from_feedback(log_text, decision.command)
                except Exception:
                    pass

            observer.log_event(
                error_log=log_text,
                source=source,
                action_type=decision.action_type.value,
                latency_sec=latency,
                success=success,
                result_category=result_cat,
                error_type=exec_result.get("error_type"),
                error_detail=exec_result.get("error_detail"),
            )

            print(f"{'✅' if success else '❌'} {source} {latency:.2f}s")
            results.append({
                "round":    label,
                "category": item.get("error_category", "Unknown"),
                "source":   source,
                "latency":  latency,
                "success":  success,
            })

        except Exception as e:
            latency = time.perf_counter() - t0
            print(f"💥 ERROR {latency:.2f}s — {e}")
            results.append({
                "round":    label,
                "category": item.get("error_category", "Unknown"),
                "source":   "ERROR",
                "latency":  latency,
                "success":  False,
            })

    return results


def print_stats(results: list[dict], label: str) -> dict:
    total   = len(results)
    success = sum(1 for r in results if r["success"])
    l1_rows = [r for r in results if r["source"] == "L1_CACHE"]
    l2_rows = [r for r in results if r["source"] in ("L2_LLM", "RULE")]

    l1_avg = sum(r["latency"] for r in l1_rows) / len(l1_rows) if l1_rows else 0
    l2_avg = sum(r["latency"] for r in l2_rows) / len(l2_rows) if l2_rows else 0

    print(f"\n  {label}")
    print(f"  {'─'*40}")
    print(f"  처리 건수    : {total}건")
    print(f"  성공률       : {success/total*100:.1f}%")
    print(f"  L1 적중      : {len(l1_rows)}건 ({len(l1_rows)/total*100:.1f}%) — 평균 {l1_avg:.3f}s")
    print(f"  L2 추론      : {len(l2_rows)}건 ({len(l2_rows)/total*100:.1f}%) — 평균 {l2_avg:.3f}s")

    return {"l1_count": len(l1_rows), "l2_count": len(l2_rows),
            "l1_avg": l1_avg, "l2_avg": l2_avg, "success_rate": success/total*100}


def save_csv(all_results: list[dict]) -> None:
    import csv
    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["round", "category", "source", "latency", "success"])
        writer.writeheader()
        writer.writerows(all_results)
    print(f"\n  📄 결과 저장: {CSV_PATH}")


def main():
    print("Self-Healing Agent — L1 vs L2 연속 학습 벤치마크")
    print(f"샘플: {N_SAMPLES}건 | DB: {BENCHMARK_DB}\n")

    # 컴포넌트 초기화 (벤치마크 전용 DB 사용)
    engine   = RAGEngine()
    executor = ActionExecutor()
    observer = AgentObserver(db_path=BENCHMARK_DB)

    samples = load_benchmark_samples(N_SAMPLES)
    print(f"샘플 로드 완료: {len(samples)}건")

    # ── Round 1: Cold ─────────────────────────────────────────────────────────
    r1_results = run_round(samples, engine, executor, observer, ROUND1_LABEL)

    print(f"\n⏳ 3초 대기 후 Round 2 시작 (캐시 안정화)...")
    time.sleep(3)

    # ── Round 2: Warm ─────────────────────────────────────────────────────────
    r2_results = run_round(samples, engine, executor, observer, ROUND2_LABEL)

    # ── 결과 분석 ─────────────────────────────────────────────────────────────
    all_results = r1_results + r2_results
    save_csv(all_results)

    print(f"\n{'='*60}")
    print("  📊 벤치마크 결과 요약")
    print(f"{'='*60}")
    s1 = print_stats(r1_results, ROUND1_LABEL)
    s2 = print_stats(r2_results, ROUND2_LABEL)

    # 핵심 수치: 속도 개선율
    if s1["l2_avg"] > 0 and s2["l1_avg"] > 0:
        improvement = (1 - s2["l1_avg"] / s1["l2_avg"]) * 100
        ratio = s1["l2_avg"] / s2["l1_avg"]
        print(f"\n{'='*60}")
        print(f"  🎯 핵심 결론")
        print(f"{'='*60}")
        print(f"  L2 평균 처리시간 : {s1['l2_avg']:.3f}s  (최초 발생)")
        print(f"  L1 평균 처리시간 : {s2['l1_avg']:.3f}s  (재발생 — 캐시 적중)")
        print(f"  속도 향상        : {ratio:.1f}배 빠름 ({improvement:.1f}% 단축)")
        print(f"  → \"Continuous Learning으로 재발 에러 해결 속도를 {improvement:.0f}% 단축\"")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    import logging
    logging.basicConfig(
        level=logging.WARNING,          # 벤치마크 중 INFO 로그 숨김
        format="%(levelname)s %(message)s",
    )
    main()
