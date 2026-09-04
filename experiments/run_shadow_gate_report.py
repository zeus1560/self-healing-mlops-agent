"""
run_shadow_gate_report.py
Progressive Autonomy 승급(Shadow mode) 게이트 판정 리포트.

run_fp_fn_analysis.py와 같은 철학 — 새 산출물/시뮬레이션을 만들지 않고 이미 쌓이고
있는 데이터만 읽어서 판정한다. 승급 자체는 이 스크립트가 하지 않는다(2026-09-03
/grill-me 세션 결정: 최종 승급은 사람이 로그를 보고 scripts/set_autonomy_level.py로
직접 실행). 이 스크립트는 오직 "기준 충족 여부"만 보여준다.

전환별로 판정 근거가 다르다:
  - read_only → propose:
      분류 정확도(재현율) 문제라 experiments/run_fp_fn_analysis.py가 이미 계산한다.
      이 스크립트는 새로 계산하지 않고 그 결과 파일을 참고하라고 안내만 한다.
  - propose → approve_then_execute:
      PROPOSE 레벨은 실행을 안 하므로 성공/실패 라벨이 없다 — 제안 "건수"와
      "경과 기간"만 자동 판정하고, 제안 품질 자체는 사람이 로그를 직접 리뷰해야 한다.
  - approve_then_execute → auto:
      data/agent_metrics.db의 metrics 테이블만으로 완전히 자동 판정 가능하다.
      OBSERVED_ONLY/PROPOSED_ONLY를 제외한 "실제로 시도된" 행 중 실패 비율
      (HumanRejected·ApprovalTimeout·CalledProcessError 등 전부 포함)을 FP 근사치로 본다
      — 승인 거절/타임아웃은 "자동 실행됐다면 검증 없이 나갔을 조치"이므로 실패와 동일하게
      취급한다.

기준(2026-09-03 세션 확정): n>=50 그리고 기간>=2주. FN/FP는 전환 방향에 따라 별도 판정.
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src import autonomy_store
from src.schemas import AutonomyLevel

METRICS_DB  = Path("data/agent_metrics.db")
RESULTS_DIR = Path("experiments/results")

_MIN_N    = 50
_MIN_DAYS = 14
_MAX_FP   = 0.05

_NOT_ATTEMPTED = ("OBSERVED_ONLY", "PROPOSED_ONLY")


def _load_category_rows(category: str, since: datetime) -> list[dict]:
    if not METRICS_DB.exists():
        return []
    conn = sqlite3.connect(f"file:{METRICS_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT timestamp, success, result_category, error_type "
            "FROM metrics WHERE error_category = ? ORDER BY timestamp",
            (category,),
        ).fetchall()
    except sqlite3.OperationalError:
        # metrics 테이블이 아직 없음(에이전트를 한 번도 실행한 적 없는 샌드박스 등) — 데이터 없음으로 취급.
        return []
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
        if ts < since:
            continue
        out.append({
            "timestamp":       ts,
            "success":         bool(r["success"]),
            "result_category": r["result_category"],
            "error_type":      r["error_type"],
        })
    return out


def _judge_propose_to_approve(rows: list[dict], days_elapsed: float) -> dict:
    proposed = [r for r in rows if r["result_category"] == "PROPOSED_ONLY"]
    n = len(proposed)
    return {
        "transition":   "propose -> approve_then_execute",
        "n":            n,
        "days_elapsed": round(days_elapsed, 1),
        "n_ok":         n >= _MIN_N,
        "days_ok":      days_elapsed >= _MIN_DAYS,
        "note": (
            "PROPOSE 레벨은 실행 이력이 없어 FN을 자동 계산할 수 없음 — "
            "제안 로그(reasoning/command)를 사람이 직접 리뷰해서 품질을 확인할 것."
        ),
    }


def _judge_approve_to_auto(rows: list[dict], days_elapsed: float) -> dict:
    attempted = [r for r in rows if r["result_category"] not in _NOT_ATTEMPTED]
    n = len(attempted)
    n_success = sum(1 for r in attempted if r["success"])
    fp_rate = (1 - n_success / n) if n else None
    return {
        "transition":   "approve_then_execute -> auto",
        "n":            n,
        "days_elapsed": round(days_elapsed, 1),
        "n_ok":         n >= _MIN_N,
        "days_ok":      days_elapsed >= _MIN_DAYS,
        "fp_rate":      round(fp_rate, 4) if fp_rate is not None else None,
        "fp_ok":        (fp_rate is not None and fp_rate <= _MAX_FP),
        "note": (
            "실패로 집계: 실행 실패(CalledProcessError 등) + HumanRejected(승인 거절) "
            "+ ApprovalTimeout — 전부 '자동 실행이었다면 검증 없이 나갔을 조치'이므로."
        ),
    }


def analyze() -> dict:
    autonomy_store.init_table()
    candidates = [
        row for row in autonomy_store.list_all() if row["shadow_target_level"]
    ]

    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "categories": {}}

    for row in candidates:
        category      = row["category"]
        current_level = AutonomyLevel(row["level"])
        target_level  = AutonomyLevel(row["shadow_target_level"])
        started_at    = datetime.fromisoformat(row["shadow_started_at"])
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        days_elapsed = (datetime.now(timezone.utc) - started_at).total_seconds() / 86400

        if current_level == AutonomyLevel.READ_ONLY:
            report["categories"][category] = {
                "transition": "read_only -> propose",
                "note": (
                    "분류 재현율 문제 — experiments/run_fp_fn_analysis.py 결과의 "
                    f"'{category}' recall을 참고할 것. 이 스크립트는 재계산하지 않음."
                ),
            }
            continue

        rows = _load_category_rows(category, started_at)
        if current_level == AutonomyLevel.PROPOSE and target_level == AutonomyLevel.APPROVE_THEN_EXECUTE:
            report["categories"][category] = _judge_propose_to_approve(rows, days_elapsed)
        elif current_level == AutonomyLevel.APPROVE_THEN_EXECUTE and target_level == AutonomyLevel.AUTO:
            report["categories"][category] = _judge_approve_to_auto(rows, days_elapsed)
        else:
            report["categories"][category] = {
                "transition": f"{current_level.value} -> {target_level.value}",
                "note": "지원하지 않는 전환 조합 — 수동으로 판단할 것.",
            }

    return report


def print_report(report: dict) -> None:
    print("=" * 65)
    print("  Progressive Autonomy Shadow Gate 판정 리포트")
    print("=" * 65)
    if not report["categories"]:
        print("\n  [Shadow 검토 중인 카테고리 없음 — "
              "scripts/set_autonomy_level.py <카테고리> --shadow <목표레벨> 로 시작]")
        print("=" * 65)
        return

    for category, judgement in report["categories"].items():
        print(f"\n  [{category}] {judgement['transition']}")
        if "n" in judgement:
            n_flag    = "✅" if judgement["n_ok"] else "❌"
            days_flag = "✅" if judgement["days_ok"] else "❌"
            print(f"    n={judgement['n']} (기준 {_MIN_N}+) {n_flag}   "
                  f"경과 {judgement['days_elapsed']}일 (기준 {_MIN_DAYS}+일) {days_flag}")
        if judgement.get("fp_rate") is not None:
            fp_flag = "✅" if judgement["fp_ok"] else "❌"
            print(f"    FP 근사치={judgement['fp_rate']*100:.1f}% (기준 {_MAX_FP*100:.0f}% 이하) {fp_flag}")
        print(f"    {judgement['note']}")
    print("\n" + "=" * 65)


def main() -> dict:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    report = analyze()
    print_report(report)
    out_path = RESULTS_DIR / "shadow_gate_summary.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n  JSON 저장: {out_path}")
    return report


if __name__ == "__main__":
    main()
