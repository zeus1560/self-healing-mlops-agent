"""
Debouncer 타임윈도우 튜닝
시나리오: 동일 에러를 시간 간격을 넓혀가며 발생시켜 Window 크기 효과를 확인
결과: experiments/results/debouncer_results.csv
"""
import csv
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.utils.debouncer as debouncer_module
from src.utils.debouncer import LogDebouncer

RESULTS_DIR   = Path("experiments/results")
WINDOW_VALUES = [1, 3, 5, 10, 30]   # 초
EVENT_TIMES   = [
    0.0, 0.5, 2.0, 8.0, 15.0, 25.0, 40.0, 40.5, 42.0, 45.0,
    50.0, 55.0, 60.0, 80.0, 90.0, 95.0, 98.0, 100.0, 130.0, 160.0,
]
DIFFERENT_ERRORS = [
    "ERROR: OOM killer invoked",
    "CRITICAL: nginx bind() failed",
    "ERROR: DB connection timeout",
]


def simulate(window_sec: int) -> dict:
    debouncer = LogDebouncer(cooldown_seconds=window_sec)
    same_error = "ERROR: CUDA out of memory"

    # 시나리오 1: 동일 에러를 시간 간격을 넓혀가며 발생시켜 Window 크기 효과를 확인.
    processed = 0
    original_time = debouncer_module.time.time
    try:
        for current_time in EVENT_TIMES:
            debouncer_module.time.time = lambda current_time=current_time: current_time
            if debouncer.should_process(same_error):
                processed += 1
    finally:
        debouncer_module.time.time = original_time

    defense_rate = (len(EVENT_TIMES) - processed) / len(EVENT_TIMES) * 100

    # 시나리오 2: 다른 에러가 섞였을 때 정상적으로 모두 통과하는지 확인.
    debouncer2 = LogDebouncer(cooldown_seconds=window_sec)
    passed_different = 0
    original_time = debouncer_module.time.time
    try:
        for idx, err in enumerate(DIFFERENT_ERRORS):
            debouncer_module.time.time = lambda current_time=idx: current_time
            if debouncer2.should_process(err):
                passed_different += 1
    finally:
        debouncer_module.time.time = original_time

    return {
        "window_sec":       window_sec,
        "event_count":      len(EVENT_TIMES),
        "processed_same":   processed,
        "defense_rate_pct": round(defense_rate, 1),
        "different_errors_total":  len(DIFFERENT_ERRORS),
        "different_errors_passed": passed_different,
        "miss_rate_pct":    round((len(DIFFERENT_ERRORS) - passed_different) / len(DIFFERENT_ERRORS) * 100, 1),
    }


def main():
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    print("동일 에러 20회 / 간격을 넓혀가며 발생시키는 시뮬레이션")
    print(f"{'Window(s)':>10} {'Defense%':>10} {'Processed':>10} {'Miss%(diff)':>12}")
    print("-" * 46)

    rows = []
    for w in WINDOW_VALUES:
        r = simulate(w)
        rows.append(r)
        print(f"{r['window_sec']:>10} {r['defense_rate_pct']:>10.1f} "
              f"{r['processed_same']:>10} {r['miss_rate_pct']:>12.1f}")

    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = RESULTS_DIR / f"debouncer_results_{ts}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nCSV 저장: {csv_path}")


if __name__ == "__main__":
    main()
