import logging
import os
import re
import signal
import sys
import threading
import time
import traceback
from collections import deque
from typing import List

# 종료 신호를 받으면 set() → 메인 루프와 이벤트 핸들러가 함께 종료를 인지
_shutdown_event = threading.Event()


def _handle_shutdown(signum, frame):
    logging.info(
        f"[Shutdown] 종료 신호 수신 (signal {signum}). "
        "진행 중인 파이프라인 완료 후 종료합니다..."
    )
    _shutdown_event.set()

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from src.circuit_breaker import CircuitBreaker
from src.error_clusterer import ErrorClusterer
from src.etl_scheduler import ETLScheduler
from src.executor import ActionExecutor
from src.llm_engine import RAGEngine
from src.maintenance import MaintenanceRunner
from src.observability import AgentObserver
from src.proactive_monitor import ProactiveMonitor
from src.utils.debouncer import LogDebouncer
from src.utils.logging_config import setup_json_logging


class LogTailHandler(FileSystemEventHandler):
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
        self.filepath = os.path.abspath(filepath)
        self.debouncer = debouncer
        self.file_ptr = os.path.getsize(self.filepath) if os.path.exists(self.filepath) else 0
        self.error_pattern = re.compile(
            r"(ERROR|CRITICAL|OOM|Timeout|Exception)", re.IGNORECASE
        )
        self.executor        = executor        or ActionExecutor()
        self.observer_agent  = observer_agent  or AgentObserver()
        self.engine          = engine          or RAGEngine()
        self.circuit_breaker = circuit_breaker or CircuitBreaker()
        self._line_buf: deque[str] = deque(maxlen=10)

    def on_modified(self, event):
        if os.path.abspath(event.src_path) != self.filepath:
            return
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                f.seek(self.file_ptr)
                new_lines = f.readlines()
                self.file_ptr = f.tell()
        except OSError:
            logging.error(f"[LogWatcher] 로그 파일 읽기 실패:\n{traceback.format_exc()}")
            return

        for idx, raw in enumerate(new_lines):
            line = raw.strip()
            self._line_buf.append(line)

            if line and self.error_pattern.search(line):
                if _shutdown_event.is_set():
                    logging.info("[Shutdown] 종료 중 — 새 파이프라인 시작 생략.")
                    return
                if self.debouncer.should_process(line):
                    context = self._build_context_window(
                        line,
                        before=list(self._line_buf)[:-1],          # 에러 줄 제외 직전 최대 10줄
                        after=[l.strip() for l in new_lines[idx + 1: idx + 11]],  # 이후 최대 10줄
                    )
                    self.trigger_agent_pipeline(context)

    @staticmethod
    def _build_context_window(error_line: str, before: list[str], after: list[str]) -> str:
        """
        에러 줄을 맨 앞에 두고, 전후 컨텍스트를 [LOG CONTEXT] 블록으로 덧붙인다.
        에러 줄이 맨 앞이므로 circuit_breaker 서명(앞 100자)이 단일 줄 전송과 동일하게 유지된다.
        """
        parts = [error_line]
        context_lines = [l for l in before if l] + [f">>> {error_line}"] + [l for l in after if l]
        if context_lines:
            parts.append("[LOG CONTEXT]\n" + "\n".join(context_lines))
        return "\n".join(parts)

    def trigger_agent_pipeline(self, error_log: str) -> None:
        """[실전 파이프라인] CircuitBreaker → RAGEngine(L1/L2 판별) → ActionExecutor → Observer"""
        if not self.circuit_breaker.can_proceed(error_log):
            logging.warning(f"[Circuit OPEN] 파이프라인 차단됨. 30분 후 재시도: {error_log[:60]}")
            return

        first_line = error_log.splitlines()[0]
        n_lines    = error_log.count("\n") + 1
        logging.info(f"[파이프라인 가동] ({n_lines}줄 컨텍스트) {first_line}")
        start_time = time.time()

        try:
            decision = self.engine.analyze_error(error_log)
        except Exception:
            logging.error(f"[파이프라인] RAGEngine 분석 실패:\n{traceback.format_exc()}")
            return

        source = decision.resolution_source

        try:
            exec_result = self.executor.execute(decision, original_error_log=error_log)
        except Exception:
            logging.error(f"[파이프라인] ActionExecutor 실행 실패:\n{traceback.format_exc()}")
            return

        latency = time.time() - start_time

        success         = exec_result["success"]
        result_category = exec_result["result_category"]
        error_type      = exec_result["error_type"]
        error_detail    = exec_result["error_detail"]

        # L2 LLM과 Rule 기반 성공 모두 L1 캐시에 학습 (책임 일원화)
        if success and source in ("L2_LLM", "RULE") and decision.command:
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


def start_watching(target_log_files: str | List[str]) -> None:
    """
    하나 또는 여러 로그 파일을 동시 감시한다.
    target_log_files: 단일 경로(str) 또는 경로 목록(List[str])
    """
    if isinstance(target_log_files, str):
        target_log_files = [target_log_files]

    debouncer    = LogDebouncer(cooldown_seconds=30)
    watch_observer = Observer()
    first_handler = None

    for log_file in target_log_files:
        abs_path = os.path.abspath(log_file)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        if not os.path.exists(abs_path):
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write("=== System Log Initialized ===\n")

        handler = LogTailHandler(abs_path, debouncer)
        watch_observer.schedule(
            handler,
            path=os.path.dirname(abs_path),
            recursive=False,
        )
        logging.info(f"실시간 로그 감시 시작: {abs_path}")
        if first_handler is None:
            first_handler = handler

    watch_observer.start()

    # SIGTERM (systemd/docker stop) 과 SIGINT (Ctrl+C) 모두 graceful 처리
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT,  _handle_shutdown)

    maintenance   = MaintenanceRunner()
    clusterer     = ErrorClusterer()
    etl_scheduler = ETLScheduler()
    proactive     = ProactiveMonitor(
        pipeline_callback=first_handler.trigger_agent_pipeline
    )

    _cluster_interval = 86400  # 24시간마다 클러스터링
    _last_cluster     = time.time()  # 시작 시점부터 카운트 → 24h 후 첫 실행

    # _shutdown_event.wait(1): 종료 신호가 오면 sleep 없이 즉시 루프 탈출
    while not _shutdown_event.is_set():
        _shutdown_event.wait(1)
        if _shutdown_event.is_set():
            break
        maintenance.run_if_due()
        proactive.check_and_trigger()
        etl_scheduler.run_if_due()
        if time.time() - _last_cluster >= _cluster_interval:
            clusterer.run()
            _last_cluster = time.time()

    # 진행 중인 watchdog 이벤트 핸들러가 완료될 때까지 최대 30초 대기
    logging.info("[Shutdown] 감시 루프 종료. 실행 중인 작업 완료 대기 중 (최대 30s)...")
    watch_observer.stop()
    watch_observer.join(timeout=30)
    logging.info("[Shutdown] 종료 완료.")


if __name__ == "__main__":
    import json as _json
    import sys as _sys
    # JSON 로깅 활성화 (환경변수 USE_JSON_LOG=1)
    if os.getenv("USE_JSON_LOG", "0") == "1":
        setup_json_logging()
    else:
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s - %(levelname)s - %(message)s")

    # 복수 로그 파일 지원: 인자로 여러 경로 전달 가능
    # python -m src.log_watcher /var/log/app.log /var/log/nginx/error.log
    targets = _sys.argv[1:] if len(_sys.argv) > 1 else ["./data/realtime_system.log"]
    start_watching(targets)
