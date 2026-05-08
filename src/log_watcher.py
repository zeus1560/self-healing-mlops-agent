import logging
import os
import re
import sys
import time
import traceback
from collections import deque

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from src.circuit_breaker import CircuitBreaker
from src.executor import ActionExecutor
from src.llm_engine import RAGEngine
from src.maintenance import MaintenanceRunner
from src.observability import AgentObserver
from src.proactive_monitor import ProactiveMonitor
from src.utils.debouncer import LogDebouncer

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


class LogTailHandler(FileSystemEventHandler):
    def __init__(self, filepath: str, debouncer: LogDebouncer):
        super().__init__()
        self.filepath = os.path.abspath(filepath)
        self.debouncer = debouncer
        self.file_ptr = os.path.getsize(self.filepath) if os.path.exists(self.filepath) else 0
        self.error_pattern = re.compile(
            r"(ERROR|CRITICAL|OOM|Timeout|Exception)", re.IGNORECASE
        )
        self.executor        = ActionExecutor()
        self.observer_agent  = AgentObserver()
        self.engine          = RAGEngine()
        self.circuit_breaker = CircuitBreaker()
        self._line_buf: deque[str] = deque(maxlen=10)  # 에러 이전 최근 10줄 버퍼

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

        if "[LLM 추론 (L2)]" in decision.reasoning or "[Ollama 추론 (L2)]" in decision.reasoning:
            source = "L2_LLM"
        elif "[규칙 기반 추론]" in decision.reasoning:
            source = "RULE"
        else:
            source = "L1_CACHE"

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

        if success and source == "L2_LLM":
            clean_command = (
                decision.reasoning
                .replace("[LLM 추론 (L2)]", "")
                .replace("[Ollama 추론 (L2)]", "")
                .strip()
            )
            try:
                self.engine.learn_from_feedback(error_log, clean_command)
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
        )
        logging.info(
            f"[조치 완료] 소스:{source} | 결과:{result_category} ({latency:.2f}s)"
            + (f" | {error_type}" if error_type else "")
        )


def start_watching(target_log_file: str) -> None:
    abs_log_file = os.path.abspath(target_log_file)
    os.makedirs(os.path.dirname(abs_log_file), exist_ok=True)

    if not os.path.exists(abs_log_file):
        with open(abs_log_file, "w", encoding="utf-8") as f:
            f.write("=== System Log Initialized ===\n")

    debouncer = LogDebouncer(cooldown_seconds=30)
    event_handler = LogTailHandler(abs_log_file, debouncer)

    watch_observer = Observer()
    watch_observer.schedule(
        event_handler,
        path=os.path.dirname(abs_log_file),
        recursive=False,
    )
    watch_observer.start()
    logging.info(f"실시간 로그 감시 시작: {abs_log_file}")

    maintenance  = MaintenanceRunner()
    proactive    = ProactiveMonitor(
        pipeline_callback=event_handler.trigger_agent_pipeline
    )

    try:
        while True:
            time.sleep(1)
            maintenance.run_if_due()
            proactive.check_and_trigger()
    except KeyboardInterrupt:
        watch_observer.stop()
        logging.info("감시 종료.")
    watch_observer.join()


if __name__ == "__main__":
    TARGET_LOG = "./data/realtime_system.log"
    start_watching(TARGET_LOG)
