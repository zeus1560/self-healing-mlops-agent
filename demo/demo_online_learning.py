"""
demo_online_learning.py
========================
온라인학습 파이프라인(RAGEngine.learn_from_feedback / record_learned_outcome,
src/llm_engine.py)이 실제로 동작함을 1회 실증하는 데모 스크립트.

배경: 2026-09-08 세션에서 실운영 VM 감사 결과 Learned_from_LLM 엔트리가 0건으로
확인됨 — 버그가 아니라 이 메커니즘의 트리거 조건(L2/Rule 경로가 끝까지 성공
실행됨) 자체가 보수적 보안 게이트 특성상 실운영에서 아직 드물게만 발생했기
때문(누적 필요). "메커니즘이 실제로 작동한다"는 증거를 발표/논문에 쓰기 위해
실제 프로덕션 코드 경로(src/log_watcher.py의 learn_from_feedback/
record_learned_outcome 호출부와 동일한 순서)를 그대로 재현한다.

이 스크립트는 로컬 개발 sandbox의 data/chroma_db에만 쓰고 지운다
(운영 VM ChromaDB는 절대 건드리지 않음) — 데모용 엔트리는 시작과 끝에
반드시 삭제해 잔여 데이터를 남기지 않는다. Groq/Ollama 등 외부 LLM 호출도
전혀 일어나지 않는다(학습된 엔트리와 조회 텍스트가 완전히 동일해 L1 캐시가
거리 0.0으로 즉시 히트하므로 L2 폴백 체인 자체가 트리거되지 않음).

실행:
    python demo/demo_online_learning.py
"""
import hashlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from rich.console import Console
from rich.panel import Panel

from src.llm_engine import RAGEngine

console = Console()

DEMO_ERROR_LOG = (
    "[ONLINE_LEARNING_DEMO] ConnectionPoolExhausted: could not obtain connection "
    "from pool within 30000ms (demo run, safe to ignore)"
)
DEMO_COMMAND = "systemctl restart demo-connection-pool"
DEMO_DOC_ID = f"learned_{hashlib.md5(DEMO_ERROR_LOG.encode('utf-8')).hexdigest()}"


def _step(title: str) -> None:
    console.print(Panel(title, style="bold cyan"))


def _cleanup(engine: RAGEngine) -> None:
    try:
        engine.collection.delete(ids=[DEMO_DOC_ID])
    except Exception:
        pass


def main() -> None:
    engine = RAGEngine()
    baseline_count = engine.collection.count()
    console.print(f"[dim]시작 시점 ChromaDB 총 엔트리: {baseline_count}개[/dim]\n")

    # 사전 정리: 이전 실행이 비정상 종료해 데모 엔트리가 남아있을 경우 대비
    _cleanup(engine)

    # ── 1단계: L2/Rule 경로가 방금 성공 실행됨을 시뮬레이션 ──────────────
    # 실제 파이프라인에서는 src/log_watcher.py:211이 ActionExecutor 성공 직후
    # 이 메서드를 호출한다. 여기서는 그 호출을 직접 재현한다.
    _step("1단계: L2/Rule 성공 실행 → learn_from_feedback() 호출 (신규 학습)")
    existing = engine.collection.get(ids=[DEMO_DOC_ID])
    assert not existing.get("ids"), "데모 엔트리가 이미 존재함 — 사전 정리 실패"
    engine.learn_from_feedback(DEMO_ERROR_LOG, DEMO_COMMAND)

    learned = engine.collection.get(ids=[DEMO_DOC_ID])
    assert learned.get("ids"), "learn_from_feedback 이후에도 엔트리가 생성되지 않음"
    meta = learned["metadatas"][0]
    console.print(
        f"  [green]OK[/green] 엔트리 생성 확인: source={meta['source']!r}, "
        f"success_count={meta['success_count']}, command={meta['command']!r}\n"
    )

    # ── 2단계: 같은 에러가 재발 → L1 캐시가 학습된 해결책을 즉시 재사용 ──
    _step("2단계: 동일 에러 재발 → analyze_error() 가 L1 캐시로 즉시 히트하는지 확인")
    response = engine.analyze_error(DEMO_ERROR_LOG)
    assert response.resolution_source == "L1_CACHE", (
        f"L1 캐시 히트 실패, resolution_source={response.resolution_source} "
        "(Groq/Ollama L2로 폴백됨 — 예상치 못한 경로)"
    )
    assert response.l1_source == "online_learning", f"l1_source={response.l1_source}"
    assert response.l1_doc_id == DEMO_DOC_ID, f"l1_doc_id={response.l1_doc_id}"
    assert response.command == DEMO_COMMAND
    console.print(
        f"  [green]OK[/green] L1_CACHE 히트, l1_source={response.l1_source!r}, "
        f"action={response.action_type.name}, command={response.command!r}\n"
        "  → Groq/Ollama 호출 없이 학습된 해결책을 그대로 재사용함(핵심 실증 포인트)\n"
    )

    # ── 3단계: 재사용된 해결책이 다시 성공 → success_count 누적 ──────────
    _step("3단계: 재사용된 해결책이 실행 성공 → record_learned_outcome(success=True)")
    engine.record_learned_outcome(response.l1_doc_id, success=True)
    meta = engine.collection.get(ids=[DEMO_DOC_ID])["metadatas"][0]
    console.print(
        f"  [green]OK[/green] success_count={meta['success_count']} "
        f"(1 -> {meta['success_count']})\n"
    )

    # ── 4단계: 이후 반복 실패 → 자동 제거(반복실패 방어 로직) ────────────
    _step("4단계: 이후 3회 연속 실패 시뮬레이션 → 반복실패 자동 제거 확인")
    for i in range(3):
        engine.record_learned_outcome(response.l1_doc_id, success=False)
        still_there = engine.collection.get(ids=[DEMO_DOC_ID])
        if still_there.get("ids"):
            m = still_there["metadatas"][0]
            console.print(
                f"  실패 {i + 1}/3 기록 → success={m['success_count']} "
                f"failure={m['failure_count']} (아직 유지)"
            )
        else:
            console.print(
                f"  [yellow]실패 {i + 1}/3 기록 → 실패율 임계값 초과, 엔트리 자동 삭제됨[/yellow]"
            )
            break

    removed = engine.collection.get(ids=[DEMO_DOC_ID])
    assert not removed.get("ids"), "반복 실패 후에도 엔트리가 제거되지 않음"
    console.print(
        "  [green]OK[/green] 신뢰도가 떨어진 학습 엔트리가 자동으로 제거됨 "
        "→ 다음 발생 시 L2가 새 해결책을 다시 탐색하게 됨\n"
    )

    final_count = engine.collection.count()
    console.print(
        Panel(
            f"실증 완료 — 데모 엔트리 정리 확인(시작 {baseline_count}개 == 종료 {final_count}개: "
            f"{baseline_count == final_count})",
            style="bold green" if baseline_count == final_count else "bold red",
        )
    )


if __name__ == "__main__":
    main()
