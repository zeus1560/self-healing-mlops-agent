"""
run_l2_production_path_check.py
운영 L2 파이프라인이 신규(미학습) 에러를 실제로 어떻게 처리하는지 측정한다.

배경 (2026-09-07): run_l2_accuracy.py의 "Category Accuracy 92%"는 운영 L2가 절대
쓰지 않는 별도 분류 프롬프트(CLASSIFY_PROMPT)에 대한 LLM 백본 단독 성능이었음이
같은 날 재검토로 확인됨(run_l2_accuracy.py 상단 경고 참고) — "운영 L2가 신규
에러를 얼마나 잘 처리하는가"에 대한 답이 될 수 없었다.

이 스크립트는 새 프롬프트나 새 채점 기준을 만들지 않고, 운영이 실제로 쓰는 코드를
그대로 호출해서 그 공백을 메운다:
  - RAGEngine._l2_slow_track() — Groq→Ollama→ipex_llm→Rule→에스컬레이션,
    운영과 100% 동일한 폴백 체인(내부에서 _build_prompt/_run_groq/
    _make_llm_response를 그대로 호출). 이 메서드 본문은 self를 전혀 참조하지
    않으므로 인스턴스 없이 첫 인자에 None을 넘겨도 안전하다(ChromaDB 등
    무거운 초기화를 유발하지 않음).
  - ActionExecutor._validate_command() — 운영과 100% 동일한 4중 보안 화이트리스트.
  - _make_llm_response()가 내부에서 이미 호출하는 자가 반성(_reflect_on_command)
    결과는 별도로 재호출하지 않고 response.reasoning에 "자가 반성" 문구가
    포함됐는지로 그대로 읽는다(tests/test_groq_smoke.py와 동일한 방식 —
    API 재호출 없이 비용 절약).

같은 신규 에러 50건(run_l2_accuracy.NOVEL_ERRORS 재사용 — 새로 만들지 않고
동일 테스트셋으로 두 실험을 비교 가능하게 유지)에 대해 아래를 측정한다:
  - LLM 생성 성공률   : action_type이 EXECUTE_LLM_COMMAND인 비율
                        (Rule 폴백/완전 에스컬레이션까지 안 갔다는 뜻)
  - 화이트리스트 통과율: 생성된 명령어가 실제 보안필터를 통과하는 비율
  - 자가반성 통과율   : 생성된 명령어가 실제 자가반성 게이트를 통과하는 비율
  - End-to-end 통과율 : 위 셋 다 통과 — 사람 승인만 받으면 그대로 실행됐을 비율

⚠️ 의도적으로 측정하지 않는 것: "이 명령어가 이 에러의 객관적으로 올바른
해결책인가" — 그건 사람 판단이나 별도 LLM-judge가 필요한 다른 축이라 여기
포함하지 않는다. CSV의 command 컬럼으로 사람이 직접 검토할 수 있게만 남겨둔다.

참고: gather_system_context()(_l2_slow_track 내부에서 호출됨)는 이 스크립트를
실행하는 머신의 실제 현재 상태(메모리/디스크 등)를 읽는다 — 에러 로그 내용과는
무관하지만, 운영에서도 시스템 컨텍스트는 항상 "그 순간의 실제 호스트 상태"이므로
이건 재현 오차가 아니라 운영과 동일한 동작이다.

실행 (GROQ_API_KEY 설정 시 Groq, 없으면 Ollama로 자동 폴백 — RAGEngine과 동일 규칙):
    python -m experiments.run_l2_production_path_check
"""
import csv
import json
import time
from pathlib import Path

from experiments.run_l2_accuracy import NOVEL_ERRORS
from src.executor import ActionExecutor
from src.llm_engine import GROQ_API_KEY, RAGEngine
from src.schemas import ActionType

RESULTS_DIR = Path("experiments/results")

# Groq 무료 티어 레이트리밋(30 RPM) 회피 — run_l2_accuracy.py와 동일 규칙.
GROQ_CALL_INTERVAL_SEC = 2.2


def _self_reflection_passed(reasoning: str) -> bool:
    """_make_llm_response()가 자가 반성 거부 시 reasoning에 남기는 표식으로 판정."""
    return "자가 반성" not in reasoning


def main():
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    executor = ActionExecutor()

    n = len(NOVEL_ERRORS)
    print("=" * 70)
    print("  운영 L2 실제 경로 점검 — 신규 에러 50건")
    print("  (Category Accuracy 아님 — 실제 L2가 끝까지 안전하게 처리하는가)")
    print("=" * 70)
    if GROQ_API_KEY:
        print(f"  (레이트리밋 회피를 위해 요청 간 {GROQ_CALL_INTERVAL_SEC}초 간격 — 총 약 {GROQ_CALL_INTERVAL_SEC * n:.0f}초 소요 예상)\n")

    print(f"{'#':>3} {'Category':>20} {'생성':>5} {'화이트리스트':>7} {'자가반성':>7} {'ms':>7}")
    print("-" * 70)

    records = []
    for i, item in enumerate(NOVEL_ERRORS):
        true_cat = item["category"]
        log = item["log"]

        if GROQ_API_KEY and i > 0:
            time.sleep(GROQ_CALL_INTERVAL_SEC)

        t0 = time.perf_counter()
        response = RAGEngine._l2_slow_track(None, log, best_distance=999.0)
        latency_ms = (time.perf_counter() - t0) * 1000

        generated = response.action_type == ActionType.EXECUTE_LLM_COMMAND
        whitelist_ok = False
        if generated and response.command:
            _, err = executor._validate_command(response.command)
            whitelist_ok = err is None
        reflection_ok = generated and _self_reflection_passed(response.reasoning)
        end_to_end_ok = generated and whitelist_ok and reflection_ok

        mark = "✓" if end_to_end_ok else "✗"
        print(
            f"{i+1:>3} {true_cat:>20} {'Y' if generated else 'N':>5} "
            f"{'Y' if whitelist_ok else 'N':>7} {'Y' if reflection_ok else 'N':>7} "
            f"{latency_ms:>6.0f}ms  {mark}"
        )

        records.append({
            "idx":               i + 1,
            "true_cat":          true_cat,
            "resolution_source": response.resolution_source,
            "command":           response.command,
            "generated":         generated,
            "whitelist_ok":      whitelist_ok,
            "reflection_ok":     reflection_ok,
            "end_to_end_ok":     end_to_end_ok,
            "reasoning":         response.reasoning,
            "latency_ms":        round(latency_ms, 1),
        })

    gen_rate  = sum(r["generated"]     for r in records) / n
    wl_rate   = sum(r["whitelist_ok"]  for r in records) / n
    refl_rate = sum(r["reflection_ok"] for r in records) / n
    e2e_rate  = sum(r["end_to_end_ok"] for r in records) / n
    avg_lat   = sum(r["latency_ms"]    for r in records) / n

    print("\n" + "=" * 70)
    print("  종합 결과 (n=50) — 운영 L2 실제 경로, '객관적 정답 여부'는 별도(CSV로 사람 검토)")
    print("=" * 70)
    print(f"  LLM 생성 성공률   : {gen_rate*100:.1f}%")
    print(f"  화이트리스트 통과 : {wl_rate*100:.1f}%")
    print(f"  자가반성 통과     : {refl_rate*100:.1f}%")
    print(f"  End-to-end 통과   : {e2e_rate*100:.1f}%  (승인만 받으면 그대로 실행됐을 비율)")
    print(f"  평균 응답 지연     : {avg_lat:.0f}ms")
    print("=" * 70)

    summary = {
        "metric_name": "l2_production_path_end_to_end_rate",
        "caveat": (
            "이 지표는 '실제 운영 L2 코드 경로가 신규 에러에 안전하게 끝까지 도달하는가'만 "
            "잰다 — 생성된 명령어가 그 에러의 객관적으로 올바른 해결책인지는 사람이 "
            "command 컬럼(CSV)을 직접 검토해야 한다. run_l2_accuracy.py의 92%(별도 분류 "
            "프롬프트, 운영 미사용)와는 완전히 다른 축이니 같이 인용하지 말 것."
        ),
        "n_samples":                 n,
        "generation_rate":           round(gen_rate, 4),
        "whitelist_pass_rate":       round(wl_rate, 4),
        "self_reflection_pass_rate": round(refl_rate, 4),
        "end_to_end_pass_rate":      round(e2e_rate, 4),
        "avg_latency_ms":            round(avg_lat, 1),
    }
    summary_path = RESULTS_DIR / "l2_production_path_check_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    csv_path = RESULTS_DIR / "l2_production_path_check_results.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=records[0].keys())
        writer.writeheader()
        writer.writerows(records)

    print(f"\n  CSV  저장: {csv_path}")
    print(f"  JSON 저장: {summary_path}")
    return summary


if __name__ == "__main__":
    main()
