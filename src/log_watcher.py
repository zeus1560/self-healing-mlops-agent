"""
log_watcher — 로그 파일 실시간 감시 및 Self-Healing 파이프라인 오케스트레이터.

watchdog observer로 대상 로그 파일을 inotify 감시하고,
에러 라인 발견 시 RAGEngine → ActionExecutor → AgentObserver 파이프라인을 가동한다.

컴포넌트 공유:
  복수의 로그 파일을 동시에 감시할 때 RAGEngine·ActionExecutor·AgentObserver·
  CircuitBreaker를 모든 핸들러가 공유해 메모리 중복 생성을 방지한다.

종료 처리:
  SIGTERM/SIGINT 수신 시 _shutdown_event를 set하고, 메인 루프와 executor의
  승인 대기 루프가 모두 이를 감지해 최대 _OBSERVER_JOIN_TIMEOUT_SEC 내에
  클린하게 종료한다.
  signal.signal()은 반드시 메인 스레드에서만 등록한다.
"""
import logging
import os
import re
import signal
import sys
import threading
import time
import traceback
from collections import deque

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from src.circuit_breaker import CircuitBreaker
from src.error_clusterer import ErrorClusterer
from src.etl_scheduler import ETLScheduler
from src.executor import ActionExecutor, set_shutdown_event
from src.llm_engine import RAGEngine
from src.maintenance import MaintenanceRunner
from src.observability import AgentObserver
from src.proactive_monitor import ProactiveMonitor
from src.slack_bot import SlackChatOps
from src.telegram_bot import get_chatops_client
from src.utils.debouncer import LogDebouncer
from src.vector_db_purger import VectorDBPurger
from src.utils.logging_config import setup_json_logging

# ── 종료 이벤트 ───────────────────────────────────────────────────────────────
# SIGTERM/SIGINT 수신 시 set() → 메인 루프와 executor 승인 대기 루프가 동시에 감지.
_shutdown_event = threading.Event()

# watchdog Observer 종료 대기 상한(초).
# 이 시간 내에 종료되지 않으면 경고 후 강제 진행한다.
_OBSERVER_JOIN_TIMEOUT_SEC = int(os.getenv("OBSERVER_JOIN_TIMEOUT_SEC", "30"))


def _handle_shutdown(signum, frame) -> None:
    logging.info(
        f"[Shutdown] 종료 신호 수신 (signal {signum}). "
        "진행 중인 파이프라인 완료 후 종료합니다..."
    )
    _shutdown_event.set()


_CLUSTER_INTERVAL_SEC  = int(os.getenv("CLUSTER_INTERVAL_SEC",  "86400"))
_DEBOUNCE_COOLDOWN_SEC = int(os.getenv("DEBOUNCE_COOLDOWN_SEC", "30"))


class LogTailHandler(FileSystemEventHandler):
    """
    watchdog FileSystemEventHandler 구현체.

    on_modified() 이벤트에서 신규 에러 라인을 읽어 파이프라인을 가동한다.
    _line_buf(deque)로 최근 10줄 컨텍스트를 유지해 LLM 판단 품질을 높인다.
    """

    def __init__(
        self,
        filepath: str,
        debouncer: LogDebouncer,
        executor: ActionExecutor = None,
        observer_agent: AgentObserver = None,
        engine: RAGEngine = None,
        circuit_breaker: CircuitBreaker = None,
    ):
        super().__init__()
        self.filepath        = os.path.abspath(filepath)
        self.debouncer       = debouncer
        self.file_ptr        = (
            os.path.getsize(self.filepath) if os.path.exists(self.filepath) else 0
        )
        self.error_pattern   = re.compile(
            r"(ERROR|CRITICAL|OOM|Timeout|Exception)", re.IGNORECASE
        )
        self.executor        = executor        or ActionExecutor()
        self.observer_agent  = observer_agent  or AgentObserver()
        self.engine          = engine          or RAGEngine()
        self.circuit_breaker = circuit_breaker or CircuitBreaker()
        self._line_buf: deque[str] = deque(maxlen=10)

    def on_modified(self, event) -> None:
        if os.path.abspath(event.src_path) != self.filepath:
            return
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                f.seek(self.file_ptr)
                new_lines    = f.readlines()
                self.file_ptr = f.tell()
        except OSError:
            logging.error(f"[LogWatcher] 로그 파일 읽기 실패:\n{traceback.format_exc()}")
            return

        # 배치 처리 전 컨텍스트 스냅샷 — 에러 줄 제외(이전 배치 오염 방지)
        pre_batch_context = [
            ln for ln in self._line_buf if not self.error_pattern.search(ln)
        ]

        for idx, raw in enumerate(new_lines):
            line = raw.strip()
            self._line_buf.append(line)

            if line and self.error_pattern.search(line):
                if _shutdown_event.is_set():
                    logging.info("[Shutdown] 종료 중 — 새 파이프라인 시작 생략.")
                    return
                if self.debouncer.should_process(line):
                    after_lines = [
                        ln.strip() for ln in new_lines[idx + 1: idx + 11]
                        if not self.error_pattern.search(ln)
                    ]
                    context = self._build_context_window(
                        line,
                        before=pre_batch_context,
                        after=after_lines,
                    )
                    self.trigger_agent_pipeline(context)

    @staticmethod
    def _build_context_window(
        error_line: str, before: list[str], after: list[str]
    ) -> str:
        """
        에러 줄을 맨 앞에 두고, 전후 컨텍스트를 [LOG CONTEXT] 블록으로 덧붙인다.
        에러 줄이 맨 앞이므로 circuit_breaker 서명(앞 100자)이
        단일 줄 전송과 동일하게 유지된다.
        """
        parts         = [error_line]
        context_lines = (
            [ln for ln in before if ln]
            + [f">>> {error_line}"]
            + [ln for ln in after if ln]
        )
        if context_lines:
            parts.append("[LOG CONTEXT]\n" + "\n".join(context_lines))
        return "\n".join(parts)

    def trigger_agent_pipeline(self, error_log: str) -> None:
        """[실전 파이프라인] CircuitBreaker → RAGEngine → ActionExecutor → Observer"""
        if not self.circuit_breaker.can_proceed(error_log):
            logging.warning(
                f"[Circuit OPEN] 파이프라인 차단됨. 30분 후 재시도: {error_log[:60]}"
            )
            return

        first_line = error_log.splitlines()[0]
        n_lines    = error_log.count("\n") + 1
        logging.info(f"[파이프라인 가동] ({n_lines}줄 컨텍스트) {first_line}")
        start_time = time.time()

        _demo = os.getenv("DEMO_MODE", "0") == "1"
        if _demo:
            _demo_print(f"[감지] {first_line[:90]}", "red")

        try:
            decision = self.engine.analyze_error(error_log)
        except Exception:
            logging.error(f"[파이프라인] RAGEngine 분석 실패:\n{traceback.format_exc()}")
            return

        source = decision.resolution_source

        if _demo:
            _src_label = "L1 cache hit" if source == "L1_CACHE" else "L2 LLM inference"
            _demo_print(
                f" -> {_src_label} / action: {decision.action_type.name.lower()}"
                + (f" (category: {decision.error_category})" if decision.error_category else ""),
                "yellow",
            )

        try:
            exec_result = self.executor.execute(decision, original_error_log=error_log)
        except Exception:
            logging.error(f"[파이프라인] ActionExecutor 실행 실패:\n{traceback.format_exc()}")
            return

        latency         = time.time() - start_time
        success         = exec_result["success"]
        result_category = exec_result["result_category"]
        error_type      = exec_result["error_type"]
        error_detail    = exec_result["error_detail"]

        if _demo:
            _status = "SUCCESS" if success else "FAILED"
            _demo_print(
                f" => {_status} ({latency*1000:.0f}ms)",
                "green" if success else "red",
            )

        # OBSERVED_ONLY/PROPOSED_ONLY는 success=True지만 실제로 실행해본 적이 없으므로
        # (Progressive Autonomy READ_ONLY/PROPOSE 레벨) 검증 안 된 커맨드를
        # "성공한 해결책"으로 학습시키면 안 된다.
        if (success and result_category not in ("OBSERVED_ONLY", "PROPOSED_ONLY")
                and source in ("L2_LLM", "RULE") and decision.command):
            try:
                self.engine.learn_from_feedback(error_log, decision.command)
            except Exception:
                logging.error(f"[파이프라인] Feedback 학습 실패:\n{traceback.format_exc()}")

        self.circuit_breaker.record_result(error_log, success)

        self.observer_agent.log_event(
            error_log=error_log,
            source=source,
            action_type=decision.action_type.name,
            latency_sec=latency,
            success=success,
            result_category=result_category,
            error_type=error_type,
            error_detail=error_detail,
            error_category=decision.error_category,
        )
        logging.info(
            f"[조치 완료] 소스:{source} | 결과:{result_category} ({latency:.2f}s)"
            + (f" | {error_type}" if error_type else "")
        )


def start_watching(target_log_files: str | list[str]) -> None:
    """
    하나 또는 여러 로그 파일을 동시 감시한다.

    컴포넌트(RAGEngine, ActionExecutor 등)를 모든 핸들러가 공유해
    메모리 중복 생성을 방지한다.

    주의:
      signal.signal() 등록은 메인 스레드에서만 허용된다.
      비메인 스레드에서 호출 시 ValueError를 방지하기 위해 스레드를 확인한다.
    """
    if isinstance(target_log_files, str):
        target_log_files = [target_log_files]

    debouncer       = LogDebouncer(cooldown_seconds=_DEBOUNCE_COOLDOWN_SEC)
    shared_engine   = RAGEngine()
    shared_executor = ActionExecutor()
    shared_observer = AgentObserver()
    shared_breaker  = CircuitBreaker()
    watch_observer  = Observer()
    first_handler   = None

    # executor의 승인 대기 루프에 종료 이벤트를 주입 → SIGTERM 시 즉시 취소 가능
    set_shutdown_event(_shutdown_event)

    for log_file in target_log_files:
        abs_path  = os.path.abspath(log_file)
        watch_dir = os.path.dirname(abs_path)
        os.makedirs(watch_dir, exist_ok=True)
        if not os.path.exists(abs_path):
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write("=== System Log Initialized ===\n")

        handler = LogTailHandler(
            abs_path, debouncer,
            executor=shared_executor,
            observer_agent=shared_observer,
            engine=shared_engine,
            circuit_breaker=shared_breaker,
        )
        watch_observer.schedule(handler, path=watch_dir or ".", recursive=False)
        logging.info(f"실시간 로그 감시 시작: {abs_path}")
        if first_handler is None:
            first_handler = handler

    watch_observer.start()

    # 신호 핸들러는 메인 스레드에서만 등록 가능
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _handle_shutdown)
        signal.signal(signal.SIGINT,  _handle_shutdown)
    else:
        logging.warning(
            "[Watcher] 신호 핸들러를 등록하지 못했습니다 — "
            "start_watching()은 메인 스레드에서 호출해야 합니다."
        )

    maintenance   = MaintenanceRunner()
    clusterer     = ErrorClusterer()
    purger        = VectorDBPurger()
    etl_scheduler = ETLScheduler()
    chatops       = get_chatops_client() or SlackChatOps()

    pipeline_cb = first_handler.trigger_agent_pipeline if first_handler else None
    if pipeline_cb is None:
        logging.warning("[Watcher] 감시할 로그 파일이 없습니다. ProactiveMonitor 콜백 비활성.")
    proactive = ProactiveMonitor(pipeline_callback=pipeline_cb)

    _last_cluster = time.time()

    # threading.Event.wait(timeout)은 이벤트가 set되면 True, 타임아웃이면 False를 반환한다.
    # "while not wait(1)" 패턴: 1초 폴링 + 즉시 종료 감지를 동시에 달성한다.
    while not _shutdown_event.wait(1):
        maintenance.run_if_due()
        proactive.check_and_trigger()
        etl_scheduler.run_if_due()
        if time.time() - _last_cluster >= _CLUSTER_INTERVAL_SEC:
            result = clusterer.run()
            _last_cluster = time.time()
            if result and result["new_patterns"]:
                patterns_str = "\n".join(f"• `{p}`" for p in result["new_patterns"])
                chatops.send_notification(
                    title="🆕 새로운 에러 패턴 감지",
                    message=(
                        f"*Vector DB에서 신규 에러 카테고리가 발견되었습니다.*\n"
                        f"{patterns_str}\n\n"
                        f"전체 클러스터: {result['n_clusters']}개 | "
                        f"벡터: {result['n_vectors']}개 | "
                        f"실루엣: {result['silhouette']:.3f}"
                    ),
                )
            purge_result = purger.run_if_due()
            if purge_result and purge_result["purged"]:
                purged_str = "\n".join(
                    f"• `{d[:32]}...`" for d in purge_result["purged"]
                )
                chatops.send_notification(
                    title="🗑️ Vector DB 불량 항목 자동 정제",
                    message=(
                        f"*반복 실패를 유발한 항목을 삭제했습니다.*\n"
                        f"{purged_str}\n\n"
                        f"검사: {purge_result['checked']}건 | "
                        f"삭제: {len(purge_result['purged'])}건"
                    ),
                )

    logging.info(
        f"[Shutdown] 감시 루프 종료. 실행 중인 작업 완료 대기 중 "
        f"(최대 {_OBSERVER_JOIN_TIMEOUT_SEC}s)..."
    )
    watch_observer.stop()
    watch_observer.join(timeout=_OBSERVER_JOIN_TIMEOUT_SEC)

    # join() 이후에도 Observer 스레드가 살아있으면 강제 진행 경고를 남긴다.
    if watch_observer.is_alive():
        logging.warning(
            f"[Shutdown] watchdog Observer가 {_OBSERVER_JOIN_TIMEOUT_SEC}s 내에 "
            "종료되지 않았습니다. 강제 진행합니다."
        )

    logging.info("[Shutdown] 종료 완료.")


_COLORS = {"red": "\033[91m", "yellow": "\033[93m", "green": "\033[92m", "reset": "\033[0m"}

def _demo_print(msg: str, color: str = "reset") -> None:
    c = _COLORS.get(color, "")
    print(f"{c}{msg}{_COLORS['reset']}", flush=True)


if __name__ == "__main__":
    if os.getenv("USE_JSON_LOG", "0") == "1":
        setup_json_logging()
    else:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(levelname)s - %(message)s",
        )
    # chromadb 버전 충돌로 발생하는 telemetry 에러 숨김
    logging.getLogger("chromadb.telemetry").setLevel(logging.CRITICAL)

    if os.getenv("DEMO_MODE", "0") == "1":
        logging.disable(logging.CRITICAL)
        print("self-healing agent started. watching logs...", flush=True)

    targets = sys.argv[1:] if len(sys.argv) > 1 else ["./data/realtime_system.log"]
    start_watching(targets)
