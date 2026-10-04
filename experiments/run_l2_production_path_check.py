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
  - ActionExecutor._validate_command() — 운영과 100% 동일한 4중 보안 화이트리스트
    (자유형식 명령어, action_type=EXECUTE_LLM_COMMAND 경로).
  - _validate_process_name() — 구조화 액션(RESTART_SERVICE/KILL_PROCESS/
    CLEAR_MEMORY, target_process)의 대상 검증. 2026-09-17 진단-라우팅 기능
    (_route_from_diagnosis, llm_engine.py) 추가 이후 L2가 이 세 ActionType도
    반환할 수 있게 됐는데, 이 스크립트는 그 기능이 생기기 전에 작성돼 "생성
    성공" 판정을 action_type==EXECUTE_LLM_COMMAND로만 했었다 — 구조화 액션을
    전부 "생성 실패"로 잘못 세는 계측 버그였다(2026-09-19 발견·수정,
    docs/RESEARCH_SUMMARY.md §3.1/§4 참고). 구조화 액션은 원본 코드에서도
    self-reflection을 의도적으로 건너뛰므로(대상 이름이 명확한 구조화 경로라
    자유형식보다 검증 필요성이 낮다는 설계, llm_engine.py `_route_from_diagnosis`
    docstring 참고) reflection_ok를 True로 취급한다 — whitelist(대상 검증)만
    통과하면 그대로 실행되는 게 실제 운영 동작과 일치한다.
  - _make_llm_response()가 내부에서 이미 호출하는 자가 반성(_reflect_on_command)
    결과는 별도로 재호출하지 않고 response.self_reflection_safe를 그대로
    읽는다(자유형식 경로만 해당 — 구조화 경로는 애초에 이 필드가 None).

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

실행 — 백엔드는 운영과 동일하게 LLM_PROVIDER로 정해진다(2026-10-04, §6 B2):
    LLM_PROVIDER=groq python -m experiments.run_l2_production_path_check          # 클라우드 모드
    GROQ_API_KEY= LLM_PROVIDER=ollama OLLAMA_MODEL=qwen2.5:0.5b \
        python -m experiments.run_l2_production_path_check --run 1                 # 로컬 모드

로컬 모드 측정은 GROQ_API_KEY를 빈 값으로 명시해 키를 환경에서 뺀다(load_dotenv는
이미 있는 환경변수를 덮어쓰지 않으므로 .env의 키도 안 읽힌다). 그 위에 모든 HTTP
요청을 목적지별로 세서(groq_http_requests/ollama_http_requests) 실제 쓰인 백엔드를
결과 JSON에 남기고, 로컬 모드인데 Groq 요청이 1건이라도 나가면 결과를 저장하지 않고
중단한다. 클라우드 모드가 아닌 결과는 공식 수치 파일(..._summary.json)을 덮어쓰지
않도록 provider·모델·회차를 붙인 별도 파일로 저장한다.
"""
import argparse
import csv
import json
import logging
import re
import subprocess
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from experiments.run_l2_accuracy import NOVEL_ERRORS
from src.executor import ActionExecutor, _validate_process_name
from src import llm_engine, llm_mode
from src.llm_engine import RAGEngine
from src.schemas import ActionType

RESULTS_DIR = Path("experiments/results")

# 2026-09-17 진단-라우팅(_route_from_diagnosis, llm_engine.py) 추가 이후 L2가
# 반환할 수 있는 구조화 액션 — L1과 동일하게 target_process를
# _validate_process_name()으로 검증하고, self-reflection은 설계상 건너뛴다.
_STRUCTURED_ACTION_TYPES = (
    ActionType.RESTART_SERVICE, ActionType.KILL_PROCESS, ActionType.CLEAR_MEMORY,
)

# Groq 무료 티어 레이트리밋(30 RPM) 회피 — run_l2_accuracy.py와 동일 규칙.
# 2026-09-15: 멀티에이전트 3단계(진단→제안→검토) 추가로 항목당 Groq 호출이
# 1~2회에서 2~3회(진단+생성+자가반성)로 늘어 기존 2.2초로는 ITPM 레이트리밋에
# 지속적으로 걸림(실측 확인 — 재시도 소진으로 생성 성공률까지 같이 떨어짐,
# 진짜 아키텍처 성능 저하와 구분이 안 됨) — 호출량 증가분만큼 여유 있게 늘림.
GROQ_CALL_INTERVAL_SEC = 6.0


class _EventCounter(logging.Handler):
    """llm_engine 로그에서 레이트리밋·폴백 이벤트를 센다(2026-10-04 — 측정 PC에 Ollama가 떠
    있어 Groq 실패분이 조용히 Ollama로 넘어간 걸 건별로 추적 못 했던 문제 보완)."""

    PATTERNS = {
        "groq_429":               "Rate limit(429)",
        "diagnosis_groq_failed":  "[진단 에이전트] Groq 호출 실패",
        "generation_fallback":    "[RAGEngine] Groq 실패",
        "review_ollama_fallback": "[자가 반성] Groq 검증 실패, Ollama로 폴백",
        "review_failed_all":      "[자가 반성] 검증 요청 실패",
    }

    def __init__(self):
        super().__init__(level=logging.INFO)
        self.counts = {k: 0 for k in self.PATTERNS}

    def emit(self, record):
        msg = record.getMessage()
        for key, pat in self.PATTERNS.items():
            if pat in msg:
                self.counts[key] += 1


def _self_reflection_passed(response) -> bool:
    """self-reflection이 실제로 계산한 (안전 여부)를 그대로 읽는다.

    2026-09-15 1차 발견·수정: "자가 반성" 문자열 포함 여부로 판정했는데, 이 부분
    문자열이 거부 사례("⚠️ 자가 반성이 위험 판정...")뿐 아니라 네트워크 오류로
    보수적 통과 처리된 사례("Groq 추론 성공 — 자가 반성 검증 요청 실패(네트워크
    오류 등) — 보수적으로 통과 처리: ...", src/llm_engine.py:694)에도 등장해
    후자를 "거부"로 잘못 집계했다(실측 50건 중 11건).

    2026-09-15 2차 발견·수정(code-review): "⚠️" 접두어 판정으로 1차 수정했지만,
    이것도 여전히 reasoning이라는 사람이 읽는 표시용 문자열을 다시 파싱해 판정을
    역추론하는 방식이라 문구가 또 바뀌면 또 조용히 깨질 수 있었다.
    AgentResponse.self_reflection_safe(2026-09-15 추가)가 _reflect_on_command()의
    원본 (안전 여부) 불리언을 그대로 보존하므로, 이제 그걸 직접 읽는다 — L1_CACHE/
    RULE/에스컬레이션 경로처럼 검토 자체가 없었던 응답은 None이라 False 취급.
    """
    return bool(response.self_reflection_safe)


class _HttpDestinationCounter:
    """urllib.request.urlopen을 감싸 요청 목적지(Groq/Ollama/기타)별 건수를 센다.

    llm_engine의 Groq·Ollama 호출은 전부 urllib.request.urlopen을 거치므로, 백엔드
    판정 로직이 아니라 실제로 나간 요청을 기준으로 어떤 백엔드가 쓰였는지 확인한다.
    """

    def __init__(self):
        self.groq_host   = urlparse(llm_engine.GROQ_API_URL).netloc
        self.ollama_host = urlparse(llm_engine.OLLAMA_BASE_URL).netloc
        self.counts      = {"groq": 0, "ollama": 0, "other": 0}
        self._orig       = urllib.request.urlopen

    def __enter__(self):
        def counting_urlopen(url, *args, **kwargs):
            full_url = url.full_url if isinstance(url, urllib.request.Request) else str(url)
            host = urlparse(full_url).netloc
            if host == self.groq_host:
                self.counts["groq"] += 1
            elif host == self.ollama_host:
                self.counts["ollama"] += 1
            else:
                self.counts["other"] += 1
            return self._orig(url, *args, **kwargs)
        urllib.request.urlopen = counting_urlopen
        return self

    def __exit__(self, *exc):
        urllib.request.urlopen = self._orig


def _git_commit() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception:
        return None


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=int, default=None,
                        help="반복 측정 회차 번호 — 결과 파일명에 _run<N>을 붙인다")
    parser.add_argument("--interval", type=float, default=GROQ_CALL_INTERVAL_SEC,
                        help="클라우드 모드 항목 간 대기(초). 2026-10-04 진단-라우팅 검토 추가로 "
                             "항목당 Groq 호출이 최대 4회라 6초면 429가 난다 — 재측정은 9초")
    args = parser.parse_args(argv)
    interval = args.interval

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    executor = ActionExecutor()

    use_groq = llm_engine._is_groq_available()
    provider = llm_mode.LLM_PROVIDER
    model    = llm_engine.GROQ_MODEL if use_groq else llm_engine.OLLAMA_MODEL

    # 클라우드 모드 기본 실행만 기존 공식 파일명을 쓰고, 그 외(로컬 모드·반복 회차)는
    # 공식 수치를 덮어쓰지 않도록 provider·모델·회차를 붙인 별도 파일로 저장한다.
    suffix = ""
    if not use_groq:
        suffix += "_" + provider + "-" + re.sub(r"[^A-Za-z0-9.]+", "-", model)
    if args.run is not None:
        suffix += f"_run{args.run}"

    # 로그 보존 + 이벤트 집계(레이트리밋·폴백·검토 실패)
    log_dir = RESULTS_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"l2_production_path_check{suffix}.log"
    file_handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    file_handler.setLevel(logging.INFO)
    events = _EventCounter()
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(events)

    n = len(NOVEL_ERRORS)
    print("=" * 70)
    print("  운영 L2 실제 경로 점검 — 신규 에러 50건")
    print("  (Category Accuracy 아님 — 실제 L2가 끝까지 안전하게 처리하는가)")
    print(f"  LLM_PROVIDER={provider}, Groq 사용={use_groq}, 모델={model}")
    print("=" * 70)
    if use_groq:
        print(f"  (레이트리밋 회피를 위해 요청 간 {interval}초 간격 — 총 약 {interval * n:.0f}초 소요 예상)\n")

    print(f"{'#':>3} {'Category':>20} {'생성':>5} {'화이트리스트':>7} {'자가반성':>7} {'ms':>7}")
    print("-" * 70)

    records = []
    counter = _HttpDestinationCounter()
    for i, item in enumerate(NOVEL_ERRORS):
        true_cat = item["category"]
        log = item["log"]

        if use_groq and i > 0:
            time.sleep(interval)

        t0 = time.perf_counter()
        # best_meta={}: 2026-09-15 l1_nearest_category 추가로 _l2_slow_track()에
        # best_meta 인자가 새로 생겼다 — 이 스크립트는 L1 미스를 인위적으로
        # 강제하는 것이라 실제 최근접 후보가 없으므로 빈 dict(카테고리 추측 없음)로
        # 넘긴다. best_meta.get()이 안전하게 None을 반환해 에러 나지 않는다.
        with counter:
            response = RAGEngine._l2_slow_track(None, log, {}, best_distance=999.0)
        latency_ms = (time.perf_counter() - t0) * 1000

        if provider != "groq" and counter.counts["groq"] > 0:
            raise SystemExit(
                f"중단: 로컬 모드(LLM_PROVIDER={provider})인데 Groq 요청이 "
                f"{counter.counts['groq']}건 나갔다 — 결과를 저장하지 않는다."
            )

        is_structured = response.action_type in _STRUCTURED_ACTION_TYPES
        generated = response.action_type == ActionType.EXECUTE_LLM_COMMAND or is_structured

        whitelist_ok = False
        reflection_ok = False
        if is_structured:
            # 구조화 액션 — 대상 이름은 _validate_process_name(보호 목록 포함)으로 검증.
            # 2026-10-04부터 진단-라우팅의 RESTART_SERVICE/KILL_PROCESS도 self-reflection을
            # 거치므로 실제 판정을 읽는다. CLEAR_MEMORY처럼 검토 대상이 아닌 액션은 None →
            # 통과로 취급(그 전엔 구조화 액션 전부를 True로 간주했다 — 이전 수치와 직접 비교 불가).
            whitelist_ok = _validate_process_name(response.target_process or "") is not None
            reflection_ok = (response.self_reflection_safe is not False
                             and not response.self_reflection_error)
        elif generated and response.command:
            _, err = executor._validate_command(response.command)
            whitelist_ok = err is None
            reflection_ok = _self_reflection_passed(response)
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
            "error_category":    response.error_category,
            "action_type":       response.action_type.value if response.action_type else None,
            "is_structured":     is_structured,
            "target_process":    response.target_process,
            "command":           response.command,
            "generated":         generated,
            "whitelist_ok":      whitelist_ok,
            "reflection_ok":     reflection_ok,
            "end_to_end_ok":     end_to_end_ok,
            "self_reflection_safe": response.self_reflection_safe,
            "self_reflection_error": response.self_reflection_error,
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
    print(f"  실제 HTTP 요청     : Groq {counter.counts['groq']}건 / Ollama {counter.counts['ollama']}건 / 기타 {counter.counts['other']}건")
    print("=" * 70)

    summary = {
        "metric_name": "l2_production_path_end_to_end_rate",
        "caveat": (
            "이 지표는 '실제 운영 L2 코드 경로가 신규 에러에 안전하게 끝까지 도달하는가'만 "
            "잰다 — 생성된 명령어가 그 에러의 객관적으로 올바른 해결책인지는 사람이 "
            "command/target_process 컬럼(CSV)을 직접 검토해야 한다. run_l2_accuracy.py의 "
            "92%(별도 분류 프롬프트, 운영 미사용)와는 완전히 다른 축이니 같이 인용하지 말 것. "
            "2026-09-19: 구조화 액션(RESTART_SERVICE/KILL_PROCESS/CLEAR_MEMORY, 진단-라우팅) "
            "을 '생성 실패'로 잘못 세던 계측 버그를 수정 — 이전 실측값(8%)은 이 버그가 "
            "반영된 것이라 폐기, 이 값이 수정 후 첫 공식 측정."
        ),
        "llm_provider":              provider,
        "groq_used":                 use_groq,
        "model":                     model,
        "groq_http_requests":        counter.counts["groq"],
        "ollama_http_requests":      counter.counts["ollama"],
        "other_http_requests":       counter.counts["other"],
        "run":                       args.run,
        "groq_call_interval_sec":    interval if use_groq else None,
        "events":                    events.counts,
        "review_failed_items":       sum(bool(r["self_reflection_error"]) for r in records),
        "log_file":                  str(log_path),
        "git_commit":                _git_commit(),
        "n_samples":                 n,
        "generation_rate":           round(gen_rate, 4),
        "whitelist_pass_rate":       round(wl_rate, 4),
        "self_reflection_pass_rate": round(refl_rate, 4),
        "end_to_end_pass_rate":      round(e2e_rate, 4),
        "avg_latency_ms":            round(avg_lat, 1),
    }
    summary_path = RESULTS_DIR / f"l2_production_path_check_summary{suffix}.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    csv_path = RESULTS_DIR / f"l2_production_path_check_results{suffix}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=records[0].keys())
        writer.writeheader()
        writer.writerows(records)

    print(f"\n  CSV  저장: {csv_path}")
    print(f"  JSON 저장: {summary_path}")
    return summary


if __name__ == "__main__":
    main()
