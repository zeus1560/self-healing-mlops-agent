"""
Debouncer 타임윈도우 튜닝
시나리오: 동일 에러를 0.5초 간격으로 100회 발생 → 중복 방어율
결과: experiments/results/debouncer_results.csv
"""
import csv
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.debouncer import LogDebouncer

RESULTS_DIR   = Path("experiments/results")
WINDOW_VALUES = [1, 3, 5, 10, 30]   # 초
BURST_INTERVAL = 0.05               # 0.05초 간격으로 발사 (빠른 시뮬레이션)
BURST_COUNT    = 20                 # 20회 발사 (실제 100회는 너무 느림)
DIFFERENT_ERRORS = [
    "ERROR: OOM killer invoked",
    "CRITICAL: nginx bind() failed",
    "ERROR: DB connection timeout",
]


def simulate(window_sec: int) -> dict:
    debouncer = LogDebouncer(cooldown_seconds=window_sec)
    same_error = "ERROR: CUDA out of memory"

    # 시나리오 1: 동일 에러 반복 → 중복 방어율
    processed = 0
    for _ in range(BURST_COUNT):
        if debouncer.should_process(same_error):
            processed += 1
        time.sleep(BURST_INTERVAL)

    defense_rate = (BURST_COUNT - processed) / BURST_COUNT * 100

    # 시나리오 2: 다른 에러가 섞였을 때 누락 없는지
    debouncer2 = LogDebouncer(cooldown_seconds=window_sec)
    passed_different = 0
    for err in DIFFERENT_ERRORS:
        if debouncer2.should_process(err):
            passed_different += 1

    return {
        "window_sec":       window_sec,
        "burst_count":      BURST_COUNT,
        "processed_same":   processed,
        "defense_rate_pct": round(defense_rate, 1),
        "different_errors_total":  len(DIFFERENT_ERRORS),
        "different_errors_passed": passed_different,
        "miss_rate_pct":    round((len(DIFFERENT_ERRORS) - passed_different) / len(DIFFERENT_ERRORS) * 100, 1),
    }


def main():
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    print(f"동일 에러 {BURST_COUNT}회 / {BURST_INTERVAL}초 간격 burst 시뮬레이션")
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
