"""
구조화 JSON 로깅 설정.

python-json-logger를 이용해 모든 로그를 JSON 형식으로 출력한다.
Grafana Loki / ELK Stack에 바로 연동 가능.

사용법:
    from src.utils.logging_config import setup_json_logging
    setup_json_logging()          # 기본: INFO, stdout
    setup_json_logging(level=logging.DEBUG, log_file="agent.log")
"""
import logging
import sys

_JSON_AVAILABLE = False
try:
    from pythonjsonlogger import jsonlogger
    _JSON_AVAILABLE = True
except ImportError:
    pass


def setup_json_logging(
    level: int = logging.INFO,
    log_file: str | None = None,
) -> None:
    """
    루트 로거를 JSON 포맷으로 재구성한다.
    pythonjsonlogger가 없으면 기존 텍스트 포맷으로 폴백한다.
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
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    for h in handlers:
        h.setFormatter(fmt)
        root.addHandler(h)

    if not _JSON_AVAILABLE:
        logging.warning(
            "[logging_config] python-json-logger 미설치 — 텍스트 포맷 폴백. "
            "pip install python-json-logger 로 설치하세요."
        )
