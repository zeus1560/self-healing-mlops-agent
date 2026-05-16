"""
구조화 JSON 로깅 설정.

python-json-logger를 이용해 모든 로그를 JSON 형식으로 출력한다.
Grafana Loki / ELK Stack에 바로 연동 가능.

사용법:
    from src.utils.logging_config import setup_json_logging
    setup_json_logging()          # 기본: INFO, stdout
    setup_json_logging(level=logging.DEBUG, log_file="agent.log")
    setup_json_logging(log_file="agent.log", max_bytes=10*1024*1024, backup_count=5)

로그 로테이션:
    max_bytes > 0 이면 RotatingFileHandler (크기 기반 자동 회전).
    max_bytes == 0 이면 WatchedFileHandler (외부 logrotate 연동 — inode 변경 시 재오픈).
"""
import logging
import sys
from logging.handlers import RotatingFileHandler, WatchedFileHandler

_JSON_AVAILABLE = False
try:
    from pythonjsonlogger import jsonlogger
    _JSON_AVAILABLE = True
except ImportError:
    pass

# 기본 회전 설정: 파일 1개당 10 MB, 최대 5개 보관
_DEFAULT_MAX_BYTES   = 10 * 1024 * 1024  # 10 MB
_DEFAULT_BACKUP_COUNT = 5


def setup_json_logging(
    level: int = logging.INFO,
    log_file: str | None = None,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    backup_count: int = _DEFAULT_BACKUP_COUNT,
) -> None:
    """
    루트 로거를 JSON 포맷으로 재구성한다.
    pythonjsonlogger가 없으면 기존 텍스트 포맷으로 폴백한다.

    log_file 지정 시:
      max_bytes > 0  → RotatingFileHandler  (내장 크기 기반 회전)
      max_bytes == 0 → WatchedFileHandler   (외부 logrotate 연동)
    """
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    if _JSON_AVAILABLE:
        fmt = jsonlogger.JsonFormatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            rename_fields={"asctime": "timestamp", "levelname": "level"},
        )
    else:
        fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        if max_bytes > 0:
            fh = RotatingFileHandler(
                log_file,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            logging.getLogger(__name__).debug(
                f"[logging_config] RotatingFileHandler: {log_file} "
                f"(max {max_bytes//1024//1024}MB × {backup_count}개)"
            )
        else:
            # max_bytes=0 → 외부 logrotate 사용: inode 변경 감지 후 파일 재오픈
            fh = WatchedFileHandler(log_file, encoding="utf-8")
            logging.getLogger(__name__).debug(
                f"[logging_config] WatchedFileHandler: {log_file} (외부 logrotate 연동)"
            )
        handlers.append(fh)

    for h in handlers:
        h.setFormatter(fmt)
        root.addHandler(h)

    if not _JSON_AVAILABLE:
        logging.warning(
            "[logging_config] python-json-logger 미설치 — 텍스트 포맷 폴백. "
            "pip install python-json-logger 로 설치하세요."
        )
