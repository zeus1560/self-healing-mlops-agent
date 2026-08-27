"""
LogMonitor — 로그 파일 폴링 기반 에러 감지 (log_watcher의 경량 대안).

watchdog inotify를 사용하지 않는 환경에서 last_position 기반 증분 읽기로
신규 에러 라인을 수집한다. LogDebouncer로 중복 억제.

파일 교체(로그 로테이션) 감지:
  seek(0, 2)로 파일 끝 위치를 먼저 확인하여 현재 파일 크기가
  last_position보다 작으면 로테이션이 발생한 것으로 판단해 처음부터 읽는다.
"""
import logging
import os

from src.utils.debouncer import LogDebouncer


class LogMonitor:
    """폴링 방식으로 로그 파일에서 신규 에러 라인을 수집한다."""

    def __init__(self, log_file_path: str = "data/system_dummy.log"):
        self.log_file_path = os.path.abspath(log_file_path)
        self.debouncer     = LogDebouncer()
        self.last_position = 0

        log_dir = os.path.dirname(self.log_file_path)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)

        if not os.path.exists(self.log_file_path):
            open(self.log_file_path, "a", encoding="utf-8").close()  # 빈 파일 생성

        logging.info(f"[LogMonitor] 감시 시작: {self.log_file_path}")

    def get_recent_errors(self) -> list[str]:
        """
        마지막 읽기 위치 이후의 신규 라인 중 debouncer를 통과한 것을 반환한다.
        파일이 없거나 읽기 실패 시 빈 리스트를 반환한다.
        """
        new_errors: list[str] = []
        try:
            if not os.path.exists(self.log_file_path):
                return []

            with open(self.log_file_path, "r", encoding="utf-8") as f:
                # 로그 로테이션 감지: 파일 크기가 last_position보다 작으면 처음부터 읽음
                f.seek(0, 2)
                if f.tell() < self.last_position:
                    self.last_position = 0

                f.seek(self.last_position)
                for raw in f.readlines():
                    line = raw.strip()
                    if line and self.debouncer.is_new_error(line):
                        new_errors.append(line)
                self.last_position = f.tell()

        except Exception as e:
            logging.error(f"[LogMonitor] 런타임 에러: {e}")

        return new_errors
