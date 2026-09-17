"""
False Positive 실측 — LogHub 무관 로그로 오탐률 측정
=====================================================
배경: "confidence score/shadow mode가 구현되어 있다"는 것과 "실제로 정확하다"는
건 다른 얘기라는 피드백. 지금까지의 실험(threshold sweep, security audit 등)은
전부 이 시스템이 "알아야 하는" 에러(train/test_set.json, 카오스 인젝터 문구)에
대한 정확도만 재왔다 — 이 시스템과 **전혀 무관한** 정상 로그를 오탐 안 하는지는
한 번도 실측한 적이 없었다.

data/loghub_cache/*.log (Apache/BGL/HDFS/Hadoop/Linux/OpenSSH/OpenStack/Spark/
Thunderbird/Zookeeper — 이 프로젝트의 target-app과 아무 관련 없는 10개 오픈소스
시스템의 실제 운영 로그, LogHub 2k 샘플)를 "정상 구간에 무관한 로그가 섞여
들어온 상황"으로 간주해 두 단계로 오탐률을 측정한다.

Tier A — 1차 탐지 게이트 (항상 실행 가능, 의존성 없음):
  src/log_watcher.py::LogTailHandler.error_pattern와 동일한 정규식
  (r"(ERROR|CRITICAL|OOM|Timeout|Exception)", re.IGNORECASE)을 그대로 재사용해,
  무관한 로그 줄이 이 1차 게이트를 얼마나 잘못 통과해 파이프라인을 깨우는지
  측정한다. 이 게이트를 통과한 줄은 전부 "이 시스템 기준으로는 오탐"이다
  (LogHub 원본 시스템 관점의 진짜 이상 여부와는 무관 — 우리 target-app과
  아무 상관 없는 로그이므로 자체가 이미 거짓 신호).

Tier B — L1(RAG) 분류 게이트 (chromadb 설치된 환경에서만 실행, --tier-b):
  Tier A를 통과한 줄들을 실제 RAGEngine.analyze_error()에 흘려, L1 벡터
  검색이 confident hit(RAG_THRESHOLD 이내 → 실제 액션으로 이어지는 판단)을
  내리는 비율을 측정한다. 이게 "오탐이 실제 조치로 이어지는 비율"에 훨씬
  가깝다. L2(Groq 등)는 호출하지 않는다 — 무관한 로그가 L1을 미스하는 것
  자체는 정상이고(오히려 안전한 경로), 그 다음 L2/Rule이 안전하게 빠지는지는
  이 실험의 범위 밖(별도로 측정 가능하나 Groq 호출 비용/레이트리밋 때문에
  분리함).

실행 (Tier A만 — 의존성 없이 아무 환경에서나):
    python3 experiments/run_false_positive_analysis.py

실행 (Tier A+B — chromadb 설치된 개발환경/VM에서):
    python3 experiments/run_false_positive_analysis.py --tier-b
    python3 experiments/run_false_positive_analysis.py --tier-b --sample-per-dataset 50

결과: experiments/results/false_positive_summary_<ts>.json
      experiments/results/false_positive_triggered_<ts>.csv (Tier A 통과 줄 전체)
      experiments/results/false_positive_tier_b_<ts>.csv (--tier-b 시 L1 판단 결과)
"""
import argparse
import csv
import json
import random
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

CACHE_DIR   = Path("data/loghub_cache")
RESULTS_DIR = Path("experiments/results")

# src/log_watcher.py::LogTailHandler.error_pattern 와 완전히 동일한 정규식.
# 두 곳이 갈라지면 이 실험이 실제 운영 게이트와 다른 걸 재는 셈이 되므로,
# 상수를 직접 import하지 않고 리터럴로 고정해 그 사실이 코드 리뷰에서
# 바로 드러나게 한다(순환 import 방지 목적도 있음 — log_watcher는 watchdog/
# chromadb 등 무거운 의존성을 끌고 와서 Tier A 전용 실행을 막는다).
ERROR_PATTERN = re.compile(r"(ERROR|CRITICAL|OOM|Timeout|Exception)", re.IGNORECASE)

SEED = 42  # scripts/split_dataset.py와 동일한 컨벤션(재현 가능한 표본 추출)


def _iter_loghub_lines() -> dict[str, list[str]]:
    if not CACHE_DIR.exists():
        print(f"[ERROR] {CACHE_DIR}가 없습니다. scripts/loghub_pipeline.py로 먼저 캐시를 채우세요.",
              file=sys.stderr)
        sys.exit(1)
    out: dict[str, list[str]] = {}
    for path in sorted(CACHE_DIR.glob("*.log")):
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            out[path.stem] = [ln.rstrip("\n") for ln in f]
    if not out:
        print(f"[ERROR] {CACHE_DIR}에 .log 파일이 없습니다.", file=sys.stderr)
        sys.exit(1)
    return out


def run_tier_a(datasets: dict[str, list[str]]) -> tuple[list[dict], list[dict]]:
    """반환: (dataset별 요약 rows, 트리거된 줄 상세 rows)"""
    summary_rows: list[dict] = []
    triggered_rows: list[dict] = []

    for name, lines in datasets.items():
        triggered = [(i, ln) for i, ln in enumerate(lines) if ln.strip() and ERROR_PATTERN.search(ln)]
        total = len(lines)
        n_trig = len(triggered)
        rate = n_trig / total * 100 if total else 0.0
        summary_rows.append({
            "dataset": name, "total_lines": total,
            "triggered_lines": n_trig, "trigger_rate_pct": round(rate, 2),
        })
        print(f"  [{name}] {n_trig}/{total} 트리거 ({rate:.2f}%)")
        for line_no, line in triggered:
            triggered_rows.append({"dataset": name, "line_no": line_no, "line": line[:300]})

    return summary_rows, triggered_rows


def run_tier_b(triggered_rows: list[dict], sample_per_dataset: int | None) -> list[dict]:
    try:
        from src.llm_engine import RAGEngine, _RAG_THRESHOLD, _ensemble_vote, _build_response_from_meta
    except Exception as exc:
        print(
            "[ERROR] --tier-b는 src.llm_engine(chromadb 등)이 설치된 환경에서만 실행할 수 "
            f"있습니다. import 실패: {exc}\n"
            "        chromadb가 설치된 개발환경/VM에서 다시 실행하세요.",
            file=sys.stderr,
        )
        sys.exit(1)

    rng = random.Random(SEED)
    by_dataset: dict[str, list[dict]] = {}
    for row in triggered_rows:
        by_dataset.setdefault(row["dataset"], []).append(row)

    sample: list[dict] = []
    for name, rows in by_dataset.items():
        if sample_per_dataset is not None and len(rows) > sample_per_dataset:
            sample.extend(rng.sample(rows, sample_per_dataset))
        else:
            sample.extend(rows)

    print(f"\n[Tier B] L1(RAG) 분류 게이트 검증 — 표본 {len(sample)}건 "
          f"(RAG_THRESHOLD={_RAG_THRESHOLD})")

    engine = RAGEngine()
    results: list[dict] = []
    raw_neighbors: list[list[tuple]] = []  # threshold_sweep()용 — 샘플별 (meta,dist,id,doc) 5개 원본
    for idx, row in enumerate(sample, 1):
        # 주의: engine.analyze_error()를 그대로 쓰면 L1 미스 시 _l2_slow_track()으로
        # 빠져 Groq/Ollama/ipex_llm까지 전부 호출한다(각 샘플마다 API 레이트리밋 대기
        # +Ollama 미실행 시 커넥션 재시도+ipex_llm spawn까지 겹쳐 샘플당 수십 초씩
        # 걸리고 사실상 끝나지 않는 것처럼 보인다 — 2026-09-17 실측 중 발견한 이 스크립트
        # 자체의 버그). Tier B는 "L1 분류 게이트"만 재는 게 목적이므로 _query_l1()을
        # 직접 호출해 L2를 아예 타지 않는다.
        try:
            r = engine._query_l1([row["line"]])
            dists = r["distances"][0]
            metas = r["metadatas"][0]
            ids   = r["ids"][0]
            docs  = r["documents"][0]
        except Exception as exc:
            results.append({
                **row, "resolution_source": "ERROR", "action_type": "",
                "l1_nearest_distance": "", "confident_action": False,
                "note": f"_query_l1 예외: {exc}",
            })
            raw_neighbors.append([])
            continue

        neighbors = [(metas[i], dists[i], ids[i], docs[i]) for i in range(len(dists))]
        raw_neighbors.append(neighbors)
        candidates = [c for c in neighbors if c[1] <= _RAG_THRESHOLD]

        if not candidates:
            results.append({
                **row, "resolution_source": "L1_MISS", "action_type": "",
                "error_category": "", "l1_nearest_distance": dists[0] if dists else "",
                "confident_action": False, "note": "",
            })
        else:
            best_meta, best_id, evidence = _ensemble_vote(candidates)
            decision = _build_response_from_meta(best_meta, "L1_CACHE", doc_id=best_id, evidence=evidence)
            # ESCALATE_TO_HUMAN/ALERT_ONLY는 실제 조치가 아니므로 "오탐이 조치로
            # 이어졌다"고 볼 수 없다 — 그 외 액션(RESTART_SERVICE/KILL_PROCESS/
            # CLEAR_MEMORY/EXECUTE_LLM_COMMAND 등)으로 판단했으면 confident FP.
            confident_action = decision.action_type.name not in ("ESCALATE_TO_HUMAN", "ALERT_ONLY")
            results.append({
                **row,
                "resolution_source": "L1_CACHE",
                "action_type": decision.action_type.name,
                "error_category": decision.error_category or "",
                "l1_nearest_distance": "",
                "confident_action": confident_action,
                "note": "",
            })
        if idx % 100 == 0:
            print(f"    ... {idx}/{len(sample)} 처리")

    return results, raw_neighbors


def threshold_sweep(raw_neighbors: list[list[tuple]], thresholds: list[float]) -> list[dict]:
    """이미 가져온 L1 이웃 데이터(재쿼리 없음)로 RAG_THRESHOLD를 바꿔가며
    L1 hit율/confident FP율이 어떻게 바뀌는지 표로 정리한다."""
    from src.llm_engine import _ensemble_vote, _build_response_from_meta

    rows: list[dict] = []
    n = len([nb for nb in raw_neighbors if nb])
    for th in thresholds:
        n_hit = 0
        n_confident = 0
        for neighbors in raw_neighbors:
            if not neighbors:
                continue
            candidates = [c for c in neighbors if c[1] <= th]
            if not candidates:
                continue
            n_hit += 1
            best_meta, best_id, evidence = _ensemble_vote(candidates)
            decision = _build_response_from_meta(best_meta, "L1_CACHE", doc_id=best_id, evidence=evidence)
            if decision.action_type.name not in ("ESCALATE_TO_HUMAN", "ALERT_ONLY"):
                n_confident += 1
        rows.append({
            "threshold": th,
            "sampled": n,
            "l1_hit": n_hit,
            "l1_hit_rate_pct": round(n_hit / n * 100, 2) if n else 0.0,
            "confident_false_action": n_confident,
            "confident_false_action_rate_pct": round(n_confident / n * 100, 2) if n else 0.0,
        })
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tier-b", action="store_true",
                         help="L1(RAG) 분류 게이트까지 검증 (chromadb 필요)")
    parser.add_argument("--sample-per-dataset", type=int, default=100,
                         help="Tier B 표본 크기(데이터셋당, 기본 100). 0이면 전체.")
    args = parser.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    print("=" * 70)
    print("Tier A — 1차 탐지 게이트(정규식) 오탐률")
    print("=" * 70)
    datasets = _iter_loghub_lines()
    summary_rows, triggered_rows = run_tier_a(datasets)

    total_lines     = sum(r["total_lines"] for r in summary_rows)
    total_triggered = sum(r["triggered_lines"] for r in summary_rows)
    overall_rate    = total_triggered / total_lines * 100 if total_lines else 0.0
    print(f"\n전체: {total_triggered}/{total_lines} 트리거 ({overall_rate:.2f}%)")
    print("이 줄들은 전부 이 프로젝트의 target-app과 무관한 다른 시스템의 정상 운영 "
          "로그이므로, 트리거됐다는 것 자체가 1차 게이트 기준 오탐이다.")

    triggered_csv = RESULTS_DIR / f"false_positive_triggered_{ts}.csv"
    with open(triggered_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["dataset", "line_no", "line"])
        writer.writeheader()
        writer.writerows(triggered_rows)
    print(f"Tier A 상세 CSV: {triggered_csv}")

    tier_b_results: list[dict] = []
    tier_b_summary: dict = {}
    if args.tier_b:
        print()
        print("=" * 70)
        print("Tier B — L1(RAG) 분류 게이트 오탐률")
        print("=" * 70)
        cap = None if args.sample_per_dataset == 0 else args.sample_per_dataset
        tier_b_results, raw_neighbors = run_tier_b(triggered_rows, cap)

        n = len(tier_b_results)
        n_l1_hit = sum(1 for r in tier_b_results if r["resolution_source"] == "L1_CACHE")
        n_confident = sum(1 for r in tier_b_results if r["confident_action"])
        tier_b_summary = {
            "sampled": n,
            "l1_hit": n_l1_hit,
            "l1_hit_rate_pct": round(n_l1_hit / n * 100, 2) if n else 0.0,
            "confident_false_action": n_confident,
            "confident_false_action_rate_pct": round(n_confident / n * 100, 2) if n else 0.0,
        }
        print(f"\n표본 {n}건 중 L1 hit {n_l1_hit}건({tier_b_summary['l1_hit_rate_pct']}%), "
              f"실제 조치로 이어질 confident FP {n_confident}건"
              f"({tier_b_summary['confident_false_action_rate_pct']}%)")
        if n_confident:
            print("\n⚠️  실제 조치로 이어진 오탐 샘플(검토 필요):")
            for r in tier_b_results:
                if r["confident_action"]:
                    print(f"    [{r['dataset']}:{r['line_no']}] {r['action_type']} "
                          f"({r['error_category']}) <- {r['line'][:80]!r}")

        tier_b_csv = RESULTS_DIR / f"false_positive_tier_b_{ts}.csv"
        with open(tier_b_csv, "w", newline="", encoding="utf-8") as f:
            fieldnames = ["dataset", "line_no", "line", "resolution_source", "action_type",
                          "error_category", "l1_nearest_distance", "confident_action", "note"]
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(tier_b_results)
        print(f"Tier B 상세 CSV: {tier_b_csv}")

        # 함수 지역 import였던 _RAG_THRESHOLD가 main()에선 안 보여 NameError가
        # 났었다(2026-09-17 첫 실행 중 발견) — main()에서 다시 로컬 import.
        from src.llm_engine import _RAG_THRESHOLD as _current_threshold

        print()
        print("=" * 70)
        print("Threshold Sweep — RAG_THRESHOLD를 바꾸면 오탐률이 어떻게 변하는가")
        print("=" * 70)
        sweep_rows = threshold_sweep(raw_neighbors, [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 1.0, 1.2])
        print(f"{'threshold':>10} | {'L1 hit':>8} | {'hit%':>7} | {'confident FP':>13} | {'FP%':>6}")
        print("-" * 60)
        for r in sweep_rows:
            marker = "  <- 현재값" if r["threshold"] == _current_threshold else (
                "  <- README 최적값" if r["threshold"] == 1.2 else "")
            print(f"{r['threshold']:>10} | {r['l1_hit']:>8} | {r['l1_hit_rate_pct']:>6}% | "
                  f"{r['confident_false_action']:>13} | {r['confident_false_action_rate_pct']:>5}%{marker}")
        tier_b_summary["threshold_sweep"] = sweep_rows
    else:
        print("\n(Tier B 생략됨 — chromadb가 설치된 환경에서 --tier-b로 재실행하면 "
              "'오탐이 실제 조치로 이어지는 비율'까지 측정됩니다.)")

    summary = {
        "timestamp": ts,
        "tier_a": {
            "per_dataset": summary_rows,
            "total_lines": total_lines,
            "total_triggered": total_triggered,
            "overall_trigger_rate_pct": round(overall_rate, 2),
        },
        "tier_b": tier_b_summary,
    }
    summary_path = RESULTS_DIR / f"false_positive_summary_{ts}.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"\n요약 JSON: {summary_path}")


if __name__ == "__main__":
    main()
