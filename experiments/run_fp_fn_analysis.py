"""
run_fp_fn_analysis.py
False Positive / False Negative 원인 분석 — 실서비스 파이프라인 기준.

run_l2_accuracy.py와 다르게 새 가짜 로그를 만들거나 LLM을 새로 호출하지 않는다.
이미 돌아가고 있는 두 산출물만 읽는다:
  1. data/chaos_injector.log — chaos_cron.sh가 6시간마다 남기는 주입 감사 로그
     (타임스탬프 + 주입한 fault 종류 = 정답 라벨의 원천)
  2. data/agent_metrics.db  — AgentObserver가 실전 파이프라인(L1 RAG/L2 LLM/
     ActionExecutor)이 실제로 처리한 모든 사건을 기록한 DB (예측값의 원천)

target-app의 각 /inject/* 핸들러가 realtime_system.log에 남기는 고유한 증거
문구(EVIDENCE_MARKERS)로 어떤 fault였는지를 error_log 본문에서 직접 식별하므로,
타임스탬프만으로 매칭하는 것보다 훨씬 안정적이다(동시각에 발생한 무관한 실서비스
에러와 혼동하지 않음).

실행 위치 주의:
  로컬 개발 샌드박스의 data/agent_metrics.db는 카오스 주입 이력이 거의 없어
  결과가 비어있을 수 있다. 의미 있는 결과를 보려면 실제로 90일 데이터를 쌓고
  있는 GCP VM에서 실행해야 한다(cd ~/agent && python -m experiments.run_fp_fn_analysis).
"""
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

CHAOS_LOG   = Path("data/chaos_injector.log")
METRICS_DB  = Path("data/agent_metrics.db")
RESULTS_DIR = Path("experiments/results")

# 매칭 윈도우 — 주입 시각 이후 이 시간 내에 남은 metrics row만 같은 사건으로 본다.
# (worst case: dd/stress-ng 타임아웃 최대 30s + L2 LLM 호출 최대 30s + 조치 실행
#  시간을 넉넉히 더해도 수 분이면 충분하다.
#  주의: "완전 미탐지" 판정은 fault 종류를 구분하지 않고 이 윈도 안에 chaos-injector
#  마커가 찍힌 row가 하나라도 있는지만 본다 — 실서비스에서는 chaos_cron.sh가
#  6시간 간격으로만 주입하고 target-app의 _injection_lock이 동시 주입을 막아주므로
#  윈도가 겹칠 일이 없지만, 수동으로 /inject/*를 몇 분 간격으로 연달아 호출해
#  테스트하는 경우엔 윈도가 겹쳐 오탐이 날 수 있다.)
MATCH_WINDOW = timedelta(minutes=3)

# fault_type -> ErrorCategory(정답). ErrorCategory에 대응 항목이 아예 없는
# fault는 None으로 두고 별도로 보고한다(모델 오류가 아니라 스키마 공백이므로).
FAULT_TO_CATEGORY = {
    "oom":               "Out_Of_Memory",
    "cpu":               None,  # ErrorCategory에 CPU 포화 관련 항목이 없음 — 구조적 공백
    "diskfull":          "Disk_Full",
    "process_crash":     "Process_Crash",
    "permission_denied": "Permission_Denied",
    "path_not_found":    "Path_Not_Found",
    "config_error":      "Configuration_Error",
}

# fault_type -> realtime_system.log에 남는 고유 증거 문구(정규식).
# deploy/target-app/main.py의 _append_evidence() 호출 문자열과 반드시 일치해야 한다.
EVIDENCE_MARKERS = {
    "oom":               r"python memory allocator vs cgroup mem_limit=512m",
    "cpu":               r"stress-ng --cpu=2 against cpus=1\.0 limit",
    "diskfull":          r"dd wrote into 150m tmpfs",
    "process_crash":     r"about to crash — real process crash injection",
    "permission_denied": r"no execute bit set",
    "path_not_found":    r"FileNotFoundError — \[Errno 2\] No such file or directory",
    "config_error":      r"Configuration Error — JSONDecodeError parsing",
}

# 인젝터 자신이 기대한 예외를 못 일으켰을 때 남기는 표식 — 이런 사건은 정답 자체가
# 불확실하므로 confusion matrix에서 빼고 별도로 보고한다.
_INJECTOR_SELF_FAILURE_MARKER = "investigate"

# 8/27 세션에서 실측한 학습 데이터 희소 카테고리 — 오분류 원인 태깅에 사용.
_DATA_SCARCE_CATEGORIES = {
    "Disk_Full", "Port_Conflict", "DB_Timeout", "DB_Deadlock",
    "Network_Unreachable", "Path_Not_Found", "Unknown",
}


def _parse_chaos_log(path: Path) -> list[dict]:
    """chaos_cron.sh가 남긴 OK 라인만 (timestamp, fault) 목록으로 파싱."""
    if not path.exists():
        return []
    events = []
    line_re = re.compile(r"^(\S+) OK fault=(\S+)(?: http=(\d+))?")
    for line in path.read_text(encoding="utf-8").splitlines():
        m = line_re.match(line)
        if not m:
            continue
        ts_str, fault = m.group(1), m.group(2)
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except ValueError:
            continue
        events.append({"timestamp": ts, "fault": fault})
    return events


def _load_chaos_metrics_rows(db_path: Path) -> list[dict]:
    """agent_metrics.db에서 chaos-injector 마커가 찍힌 행만 읽는다(읽기 전용)."""
    if not db_path.exists():
        return []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT timestamp, error_log, resolution_source, action_type, "
            "success, result_category, error_category "
            "FROM metrics WHERE error_log LIKE '%chaos-injector:%' "
            "ORDER BY timestamp"
        ).fetchall()
    finally:
        conn.close()

    out = []
    for r in rows:
        try:
            ts = datetime.fromisoformat(r["timestamp"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        out.append({
            "timestamp":        ts,
            "error_log":        r["error_log"] or "",
            "resolution_source": r["resolution_source"],
            "action_type":      r["action_type"],
            "success":          bool(r["success"]),
            "result_category":  r["result_category"],
            "error_category":   r["error_category"],
        })
    return out


def _identify_fault(error_log: str) -> str | None:
    """error_log 본문에서 EVIDENCE_MARKERS로 실제 주입된 fault_type을 역추적."""
    for fault, pattern in EVIDENCE_MARKERS.items():
        if re.search(pattern, error_log):
            return fault
    return None


def analyze() -> dict:
    chaos_events = _parse_chaos_log(CHAOS_LOG)
    metrics_rows = _load_chaos_metrics_rows(METRICS_DB)

    # 1) metrics row별로 진짜 fault_type을 증거 문구로 역추적
    labeled = []
    unrecognized = 0
    injector_self_failed = 0
    for row in metrics_rows:
        if _INJECTOR_SELF_FAILURE_MARKER in row["error_log"]:
            injector_self_failed += 1
            continue
        fault = _identify_fault(row["error_log"])
        if fault is None:
            unrecognized += 1
            continue
        labeled.append({**row, "fault": fault, "true_category": FAULT_TO_CATEGORY[fault]})

    # 2) 완전 미탐지 — chaos_injector.log엔 OK로 남았는데 파이프라인이 아예 반응한
    #    흔적(chaos-injector: 마커가 찍힌 metrics row)조차 없는 경우.
    #    target-app의 _injection_lock 덕에 한 번에 하나의 주입만 진행되므로,
    #    라벨링 실패/인젝터 자체 실패 행이라도 시간 윈도 안에 있으면 "반응은 했다"로 본다
    #    (labeled만 보면 injector_self_failed·unrecognized 행이 이중으로 missed에도
    #    잡히는 버그가 생김).
    missed = []
    for ev in chaos_events:
        window_end = ev["timestamp"] + MATCH_WINDOW
        hit = any(ev["timestamp"] <= r["timestamp"] <= window_end for r in metrics_rows)
        if not hit:
            missed.append(ev)

    # 3) confusion matrix (true_category가 존재하는 것만 — cpu처럼 매핑이 없는 건 별도 집계)
    confusion: dict[str, Counter] = defaultdict(Counter)
    action_mismatches = 0
    no_category_mapping = []
    for item in labeled:
        if item["true_category"] is None:
            no_category_mapping.append(item)
            continue
        pred = item["error_category"] or "(없음)"
        confusion[item["true_category"]][pred] += 1

    # 4) 카테고리별 recall(재현율) + 오분류 원인 태깅
    per_category_report = {}
    root_cause_tags = Counter()
    for true_cat, pred_counter in confusion.items():
        total = sum(pred_counter.values())
        correct = pred_counter.get(true_cat, 0)
        recall = correct / total if total else 0.0
        per_category_report[true_cat] = {
            "n": total,
            "recall": round(recall, 4),
            "predicted_as": dict(pred_counter),
        }
        for pred, cnt in pred_counter.items():
            if pred == true_cat:
                continue
            if pred == "(없음)":
                root_cause_tags["예측 카테고리 없음 (L1/L2가 error_category 미기록)"] += cnt
            elif true_cat in _DATA_SCARCE_CATEGORIES:
                root_cause_tags[f"학습 데이터 희소 카테고리({true_cat}) 오분류 의심"] += cnt
            else:
                root_cause_tags[f"{true_cat} → {pred} 오분류"] += cnt

    summary = {
        "generated_at":            datetime.now(timezone.utc).isoformat(),
        "chaos_injector_log_events": len(chaos_events),
        "matched_and_labeled":     len(labeled),
        "missed_entirely":         len(missed),
        "missed_events":           [
            {"timestamp": e["timestamp"].isoformat(), "fault": e["fault"]} for e in missed
        ],
        "unrecognized_marker":     unrecognized,
        "injector_self_failed":    injector_self_failed,
        "no_category_mapping_n":  len(no_category_mapping),
        "no_category_mapping_faults": sorted({i["fault"] for i in no_category_mapping}),
        "per_category":            per_category_report,
        "root_cause_tags":         dict(root_cause_tags),
    }
    return summary


def print_report(summary: dict) -> None:
    print("=" * 65)
    print("  False Positive / False Negative 원인 분석 — 실서비스 카오스 기준")
    print("=" * 65)
    print(f"  chaos_injector.log 주입 이벤트 : {summary['chaos_injector_log_events']}건")
    print(f"  실서비스 파이프라인이 라벨링됨  : {summary['matched_and_labeled']}건")
    print(f"  완전 미탐지(파이프라인이 놓침)  : {summary['missed_entirely']}건")
    print(f"  인젝터 자체 실패(정답 불확실)   : {summary['injector_self_failed']}건")
    print(f"  마커 인식 실패                 : {summary['unrecognized_marker']}건")
    if summary["no_category_mapping_n"]:
        print(
            f"  카테고리 매핑 자체가 없는 fault : {summary['no_category_mapping_n']}건 "
            f"({', '.join(summary['no_category_mapping_faults'])}) — ErrorCategory 정의 보완 필요"
        )

    if not summary["per_category"]:
        print("\n  [분석 대상 데이터 없음 — 실제 90일 운영 데이터가 쌓이는 VM에서 실행 필요]")
    else:
        print("\n" + "-" * 65)
        print("  카테고리별 재현율(recall) 및 오분류 분포")
        print("-" * 65)
        for cat, stat in summary["per_category"].items():
            print(f"  {cat:<22} recall={stat['recall']*100:>5.1f}%  (n={stat['n']})")
            for pred, cnt in stat["predicted_as"].items():
                if pred != cat:
                    print(f"      └─ {cnt}건이 '{pred}'(으)로 오분류됨")

        print("\n" + "-" * 65)
        print("  오분류 원인 태깅")
        print("-" * 65)
        for tag, cnt in summary["root_cause_tags"].items():
            print(f"  {cnt:>3}건  {tag}")
    print("=" * 65)


def main() -> dict:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    summary = analyze()
    print_report(summary)

    out_path = RESULTS_DIR / "fp_fn_analysis_summary.json"
    out_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n  JSON 저장: {out_path}")
    return summary


if __name__ == "__main__":
    main()
