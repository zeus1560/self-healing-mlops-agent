"""
LogDebouncer — 유사 에러 로그의 쿨타임 기반 중복 억제.

watchdog observer는 복수의 로그 파일 이벤트를 서로 다른 스레드에서
동시에 발생시킬 수 있으므로, 단일 인스턴스를 공유하는 경우
OrderedDict 수정이 경쟁 조건(Race Condition)을 유발한다.
threading.Lock으로 원자적 갱신을 보장한다.

정규화 전략:
  첫 줄만 취하고 숫자·IP·해시를 플레이스홀더로 치환한 뒤 MD5.
  예) "OOM killed pid 1234" == "OOM killed pid 5678" → 동일 버킷
      "Connection refused to 10.0.0.1" == "... to 192.168.1.1" → 동일 버킷
"""
import hashlib
import re
import threading
import time
from collections import OrderedDict

# 에러 로그에서 가변 값을 플레이스홀더로 치환하는 규칙.
# 순서 중요: IPv4를 먼저 처리해야 숫자 규칙에 IP가 분해되지 않는다.
_NORM_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r'\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b'), '<IP>'),   # IPv4[:port]
    (re.compile(r'\b[0-9a-f]{8,}\b', re.IGNORECASE), '<HEX>'),        # 메모리 주소·해시
    (re.compile(r'\b\d+\b'), '<N>'),                                    # 정수 (PID, 포트 등)
    (re.compile(r'\s+'), ' '),                                          # 공백 정규화
]


class LogDebouncer:
    """
    유사 에러 로그가 cooldown 시간 내에 반복되면 무시한다.

    thread-safety:
        should_process()·is_new_error()는 내부 Lock으로 보호돼 있어
        복수의 watchdog 스레드가 동시에 호출해도 안전하다.

    LRU 방식으로 캐시 크기를 max_cache_size로 제한한다.
    """

    def __init__(self, cooldown_seconds: int = 30, max_cache_size: int = 1000):
        self.cooldown_seconds = cooldown_seconds
        self.max_cache_size   = max_cache_size
        self._history: OrderedDict[str, float] = OrderedDict()
        self._lock            = threading.Lock()

    @staticmethod
    def _normalize(text: str) -> str:
        """
        첫 줄만 취하고 가변 값(숫자, IP, 해시)을 플레이스홀더로 치환한다.
        대소문자 무시로 같은 에러 패턴을 하나의 키로 수렴시킨다.
        """
        line = text.splitlines()[0].strip().lower() if text else ""
        for pattern, repl in _NORM_RULES:
            line = pattern.sub(repl, line)
        return line.strip()

    def _key(self, text: str) -> str:
        return hashlib.md5(self._normalize(text).encode("utf-8")).hexdigest()

    def should_process(self, log_text: str) -> bool:
        """
        True  → 처리 진행 (쿨타임 만료 또는 첫 등장).
        False → 쿨타임 내 유사 중복 — 무시.

        Lock으로 OrderedDict 갱신의 원자성을 보장한다.
        """
        now = time.time()
        key = self._key(log_text)

        with self._lock:
            last_seen = self._history.get(key)
            if last_seen is not None and now - last_seen < self.cooldown_seconds:
                return False

            self._history[key] = now
            self._history.move_to_end(key)
            if len(self._history) > self.max_cache_size:
                self._history.popitem(last=False)  # LRU 항목 제거

        return True

    def is_new_error(self, log_text: str) -> bool:
        """log_monitor.LogMonitor 호환 인터페이스."""
        return self.should_process(log_text)
