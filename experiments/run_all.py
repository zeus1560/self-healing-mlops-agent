"""
7개 실험을 순차 실행하고 결과를 한 줄로 요약합니다.
사용법:
  python experiments/run_all.py            # 전체 실행
  python experiments/run_all.py --skip 13  # 특정 번호 건너뜀 (Ollama 의존 등)
  python experiments/run_all.py --timeout 120  # 실험별 타임아웃(초, 기본 300)
"""
import argparse
import concurrent.futures
import importlib
import sys
import time
import traceback
from pathlib import Path

from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent))

# 실험 목록: (표시번호, 모듈명, 설명, Ollama필요여부)
EXPERIMENTS = [
    ( 7, "run_threshold_sweep",  "Threshold Sweep (0.1~0.8)",     False),
    ( 8, "run_baseline_compare", "Baseline vs RAG 비교",           False),
    ( 9, "run_dataset_scale",    "Dataset Scale (Learning Curve)", False),
    (10, "run_top_k_sweep",      "Top-K Sweep (K=1,2,3,5)",        False),
    (11, "run_debouncer_sweep",  "Debouncer Window Sweep",          False),
    (12, "run_security_audit",   "Security Audit (악성커맨드 30개)", False),
    (13, "run_prompt_ab_test",   "Prompt A/B/C 비교",              True),
]

DEFAULT_TIMEOUT = 300  # 실험 하나당 최대 대기 시간(초)
WIDTH = 50


def _run_one(module_name: str, timeout: float) -> tuple[bool, float, str]:
    """모듈을 별도 스레드에서 실행하고 timeout 초 안에 끝나지 않으면 강제 종료."""
    t0 = time.perf_counter()

    def _target() -> None:
        mod = importlib.import_module(f"experiments.{module_name}")
        if hasattr(mod, "main"):
            mod.main()
        elif hasattr(mod, "audit"):
            mod.audit()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_target)
        try:
            future.result(timeout=timeout)
            return True, time.perf_counter() - t0, "OK"
        except concurrent.futures.TimeoutError:
            future.cancel()
            return False, time.perf_counter() - t0, f"TIMEOUT({timeout:.0f}s)"
        except SystemExit as e:
            elapsed = time.perf_counter() - t0
            if e.code == 0:
                return True, elapsed, "OK"
            return False, elapsed, f"EXIT({e.code})"
        except Exception:
            traceback.print_exc()
            return False, time.perf_counter() - t0, "FAIL"


def main(skip: list[int] | None = None, timeout: float = DEFAULT_TIMEOUT) -> None:
    skip = skip or []
    results: list[tuple[int, str, bool, float, str]] = []

    to_run = [(num, mod, desc) for num, mod, desc, _ in EXPERIMENTS if num not in skip]

    for num, _, desc, _ in EXPERIMENTS:
        if num in skip:
            tqdm.write(f"[SKIP] [{num:02d}] {desc}")

    bar = tqdm(to_run, unit="exp", dynamic_ncols=True)
    for num, mod, desc in bar:
        bar.set_description(f"[{num:02d}] {desc[:28]}")
        tqdm.write(f"\n{'='*WIDTH}\n  [{num:02d}] {desc}\n{'='*WIDTH}")
        ok, elapsed, status = _run_one(mod, timeout)
        results.append((num, desc, ok, elapsed, status))
        tqdm.write(f"  → [{status}] {elapsed:.1f}초")

    # ── 최종 요약 ──────────────────────────────────────────────────────
    print(f"\n{'='*WIDTH}")
    print("  실험 결과 요약")
    print(f"{'='*WIDTH}")
    passed = sum(1 for *_, ok, _, _ in results if ok)
    for num, desc, ok, elapsed, status in results:
        mark = "✅" if ok else "❌"
        print(f"  {mark} [{num:02d}] {desc:<35} {elapsed:6.1f}초  [{status}]")
    print(f"{'='*WIDTH}")
    print(f"  통과: {passed}/{len(results)}")
    print(f"  결과 위치: experiments/results/")
    print(f"{'='*WIDTH}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="모든 실험 일괄 실행")
    parser.add_argument(
        "--skip", nargs="*", type=int, default=[],
        help="건너뛸 실험 번호 (예: --skip 13)",
    )
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT,
        help=f"실험 하나당 최대 실행 시간(초, 기본 {DEFAULT_TIMEOUT})",
    )
    args = parser.parse_args()
    main(skip=args.skip, timeout=args.timeout)
