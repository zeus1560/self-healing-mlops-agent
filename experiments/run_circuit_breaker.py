"""
run_circuit_breaker.py
Circuit Breaker 성능 평가 실험

측정 항목:
  1. OPEN 전환 정확도  — 3회 연속 실패 후 정확히 OPEN 전환되는지
  2. 차단율            — OPEN 상태에서 추가 요청 N회 전량 차단되는지
  3. HALF_OPEN 복구    — 타임아웃 후 시험 요청 → 성공 시 CLOSED 복구
  4. 중복 차단 원자성  — HALF_OPEN 상태에서 동시 요청 중 1개만 허용되는지

실험 편의를 위해 OPEN_TIMEOUT_SEC를 2초로 단축 (monkeypatch).
"""
import csv
import json
import os
import sys
import tempfile
import time
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# ── OPEN 타임아웃 단축 (실험용) ──────────────────────────────────────
import src.circuit_breaker as cb_module
cb_module.OPEN_TIMEOUT_SEC = 2   # 실제 30분 → 실험용 2초
from src.circuit_breaker import CircuitBreaker

RESULTS_DIR = Path("experiments/results")

# 실험용 에러 로그 픽스처
ERRORS = {
    "oom":     "FATAL: Out of memory. Killed process 1234 (python3)",
    "timeout": "TimeoutError: connection to db:5432 timed out after 30s",
    "disk":    "OSError: [Errno 28] No space left on device: /data/model.pt",
    "auth":    "AuthError: JWT token expired. Re-authenticate required.",
    "crash":   "SIGSEGV: Segmentation fault in training worker PID 5678",
}


def fresh_cb() -> tuple[CircuitBreaker, str]:
    """매 실험마다 독립적인 임시 DB로 CircuitBreaker 생성."""
    tmp = tempfile.mktemp(suffix=".db")
    return CircuitBreaker(db_path=tmp), tmp


def run_open_transition_test() -> dict:
    """실험 1: CLOSED → OPEN 전환 정확도."""
    print("\n[실험 1] CLOSED → OPEN 전환 정확도")
    print("-" * 50)

    results = {}
    for name, log in ERRORS.items():
        cb, db = fresh_cb()
        transitions = []

        # 1~2회 실패: CLOSED 유지 확인
        for i in range(1, cb_module.FAILURE_THRESHOLD):
            can = cb.can_proceed(log)
            cb.record_result(log, success=False)
            status = cb.get_status(log)
            transitions.append(status["state"])
            assert status["state"] == "CLOSED", f"조기 OPEN 전환: {i}회 실패 후"

        # 3회째 실패: OPEN 전환 확인
        can = cb.can_proceed(log)
        cb.record_result(log, success=False)
        status = cb.get_status(log)
        transitions.append(status["state"])
        correct = status["state"] == "OPEN"

        mark = "✓" if correct else "✗"
        print(f"  {mark} {name:<10} 전환: {' → '.join(transitions)}  "
              f"(failures={status['failures']})")
        results[name] = {"correct": correct, "transitions": transitions}
        os.unlink(db)

    accuracy = sum(1 for r in results.values() if r["correct"]) / len(results)
    print(f"\n  전환 정확도: {accuracy*100:.1f}% ({sum(r['correct'] for r in results.values())}/{len(results)})")
    return {"accuracy": round(accuracy, 4), "details": results}


def run_block_rate_test(n_requests: int = 20) -> dict:
    """실험 2: OPEN 상태에서 추가 요청 차단율."""
    print(f"\n[실험 2] OPEN 상태 차단율 (요청 {n_requests}회)")
    print("-" * 50)

    results = {}
    for name, log in ERRORS.items():
        cb, db = fresh_cb()

        # 3회 실패로 OPEN 상태 만들기
        for _ in range(cb_module.FAILURE_THRESHOLD):
            cb.can_proceed(log)
            cb.record_result(log, success=False)

        assert cb.get_status(log)["state"] == "OPEN"

        # OPEN 상태에서 N회 요청 → 전부 차단 확인
        blocked = 0
        for _ in range(n_requests):
            if not cb.can_proceed(log):
                blocked += 1

        block_rate = blocked / n_requests
        mark = "✓" if blocked == n_requests else "✗"
        print(f"  {mark} {name:<10} 차단: {blocked}/{n_requests} = {block_rate*100:.1f}%")
        results[name] = {"blocked": blocked, "total": n_requests, "block_rate": block_rate}
        os.unlink(db)

    avg_rate = sum(r["block_rate"] for r in results.values()) / len(results)
    print(f"\n  평균 차단율: {avg_rate*100:.1f}%")
    return {"avg_block_rate": round(avg_rate, 4), "details": results}


def run_recovery_test() -> dict:
    """실험 3: OPEN → HALF_OPEN → CLOSED 복구 흐름."""
    print("\n[실험 3] HALF_OPEN 복구 흐름 (타임아웃 2초)")
    print("-" * 50)

    results = {}
    for name, log in ERRORS.items():
        cb, db = fresh_cb()

        # OPEN 상태 진입
        for _ in range(cb_module.FAILURE_THRESHOLD):
            cb.can_proceed(log)
            cb.record_result(log, success=False)

        assert cb.get_status(log)["state"] == "OPEN"

        # 타임아웃 전: 차단 확인
        blocked_before = not cb.can_proceed(log)

        # 타임아웃 대기 (2초 + 여유 0.5초)
        t0 = time.perf_counter()
        time.sleep(2.5)
        wait_ms = (time.perf_counter() - t0) * 1000

        # 타임아웃 후: HALF_OPEN 시험 요청 허용 확인
        allowed = cb.can_proceed(log)
        state_after = cb.get_status(log)["state"]

        # 성공 결과 기록 → CLOSED 복구
        cb.record_result(log, success=True)
        state_recovered = cb.get_status(log)["state"]

        correct = blocked_before and allowed and state_recovered == "CLOSED"
        mark = "✓" if correct else "✗"
        print(f"  {mark} {name:<10} "
              f"OPEN차단={blocked_before} → "
              f"타임아웃({wait_ms:.0f}ms) → "
              f"허용={allowed} → "
              f"복구={state_recovered}")
        results[name] = {
            "correct":          correct,
            "blocked_before":   blocked_before,
            "allowed_after":    allowed,
            "state_recovered":  state_recovered,
            "wait_ms":          round(wait_ms, 1),
        }
        os.unlink(db)

    accuracy = sum(1 for r in results.values() if r["correct"]) / len(results)
    print(f"\n  복구 정확도: {accuracy*100:.1f}% ({sum(r['correct'] for r in results.values())}/{len(results)})")
    return {"accuracy": round(accuracy, 4), "details": results}


def run_atomic_halfopen_test(n_threads: int = 10) -> dict:
    """실험 4: HALF_OPEN 동시 요청 중 단 1개만 허용되는지 (원자성)."""
    print(f"\n[실험 4] HALF_OPEN 원자성 — 동시 요청 {n_threads}개")
    print("-" * 50)

    results = {}
    for name, log in ERRORS.items():
        cb, db = fresh_cb()

        # OPEN 상태 진입 후 타임아웃 대기
        for _ in range(cb_module.FAILURE_THRESHOLD):
            cb.can_proceed(log)
            cb.record_result(log, success=False)
        time.sleep(2.5)  # OPEN → HALF_OPEN 전환 대기

        # n_threads개 스레드가 동시에 can_proceed 호출
        allowed_count = 0
        lock = threading.Lock()

        def try_proceed():
            nonlocal allowed_count
            result = cb.can_proceed(log)
            if result:
                with lock:
                    allowed_count += 1

        threads = [threading.Thread(target=try_proceed) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        correct = allowed_count == 1
        mark = "✓" if correct else "✗"
        print(f"  {mark} {name:<10} {n_threads}개 동시 요청 중 허용={allowed_count}개  "
              f"({'정상' if correct else '원자성 위반!'})")
        results[name] = {
            "correct":       correct,
            "allowed_count": allowed_count,
            "n_threads":     n_threads,
        }
        os.unlink(db)

    accuracy = sum(1 for r in results.values() if r["correct"]) / len(results)
    print(f"\n  원자성 정확도: {accuracy*100:.1f}% ({sum(r['correct'] for r in results.values())}/{len(results)})")
    return {"accuracy": round(accuracy, 4), "details": results}


def main():
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 65)
    print("  Circuit Breaker 성능 평가 실험")
    print("  (OPEN_TIMEOUT: 30분 → 실험용 2초로 단축)")
    print("=" * 65)
    print(f"  에러 종류: {len(ERRORS)}개 | FAILURE_THRESHOLD: {cb_module.FAILURE_THRESHOLD}회")

    t0 = time.perf_counter()
    r1 = run_open_transition_test()
    r2 = run_block_rate_test(n_requests=20)
    r3 = run_recovery_test()
    r4 = run_atomic_halfopen_test(n_threads=10)
    elapsed = time.perf_counter() - t0

    print("\n" + "=" * 65)
    print("  종합 결과")
    print("=" * 65)
    print(f"  {'실험 항목':<30} {'결과':>10}")
    print("  " + "-" * 43)
    print(f"  {'OPEN 전환 정확도':<30} {r1['accuracy']*100:>9.1f}%")
    print(f"  {'OPEN 상태 차단율 (20회)':<30} {r2['avg_block_rate']*100:>9.1f}%")
    print(f"  {'HALF_OPEN 복구 정확도':<30} {r3['accuracy']*100:>9.1f}%")
    print(f"  {'HALF_OPEN 원자성':<30} {r4['accuracy']*100:>9.1f}%")
    print("=" * 65)
    print(f"  총 소요 시간: {elapsed:.1f}초")

    # CSV 저장
    rows = [
        ["experiment", "metric", "value"],
        ["open_transition",  "accuracy(%)",    r1["accuracy"] * 100],
        ["block_rate",       "avg_rate(%)",     r2["avg_block_rate"] * 100],
        ["recovery",         "accuracy(%)",    r3["accuracy"] * 100],
        ["atomic_halfopen",  "accuracy(%)",    r4["accuracy"] * 100],
    ]
    out_csv = RESULTS_DIR / "circuit_breaker_results.csv"
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)

    # JSON 저장
    summary = {
        "open_transition_accuracy":  r1["accuracy"],
        "block_rate":                r2["avg_block_rate"],
        "recovery_accuracy":         r3["accuracy"],
        "atomic_halfopen_accuracy":  r4["accuracy"],
        "failure_threshold":         cb_module.FAILURE_THRESHOLD,
        "open_timeout_real_sec":     30 * 60,
        "n_error_types":             len(ERRORS),
    }
    (RESULTS_DIR / "circuit_breaker_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False)
    )
    print(f"\n  CSV  저장: {out_csv}")
    print(f"  JSON 저장: {RESULTS_DIR}/circuit_breaker_summary.json")
    return summary


if __name__ == "__main__":
    main()
